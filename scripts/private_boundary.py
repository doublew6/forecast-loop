"""Load private public-boundary literals from outside a public repository."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Final

MAX_PRIVATE_PATTERN_BYTES: Final = 64 * 1024
MAX_PRIVATE_PATTERN_COUNT: Final = 256
PRIVATE_PATTERN_CONFIG: Final = "forecastloop.privateBoundaryFile"
PRIVATE_PATTERN_ENV: Final = "FORECAST_LOOP_PRIVATE_BOUNDARY_FILE"


class PrivateBoundaryError(RuntimeError):
    """Private-boundary rules are missing or unsafe to read."""


def _is_git_repository(repository: Path) -> bool:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--git-dir"),
            cwd=repository,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise PrivateBoundaryError(
            "cannot determine the private-boundary Git configuration"
        ) from exc
    if result.returncode == 0:
        return True
    has_git_metadata = (repository / ".git").exists() or (
        (repository / "HEAD").is_file()
        and (repository / "objects").is_dir()
        and (repository / "refs").is_dir()
    )
    if result.returncode != 128 or has_git_metadata:
        raise PrivateBoundaryError(
            "cannot determine the private-boundary Git configuration"
        )
    return False


def _git_config_path(repository: Path) -> Path | None:
    if not _is_git_repository(repository):
        return None
    try:
        result = subprocess.run(
            ("git", "config", "--local", "--get", PRIVATE_PATTERN_CONFIG),
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PrivateBoundaryError(
            "cannot read private-boundary Git configuration"
        ) from exc
    if result.returncode not in {0, 1}:
        raise PrivateBoundaryError("cannot read private-boundary Git configuration")
    configured = result.stdout.strip()
    return Path(configured) if configured else None


def private_patterns_required(repository: Path) -> bool:
    if not _is_git_repository(repository):
        return False
    try:
        result = subprocess.run(
            (
                "git",
                "config",
                "--local",
                "--bool",
                "--get",
                "forecastloop.privateBoundaryRequired",
            ),
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PrivateBoundaryError(
            "cannot read private-boundary requirement"
        ) from exc
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        raise PrivateBoundaryError("cannot read private-boundary requirement")
    return result.stdout.strip() == "true"


def _selected_path(repository: Path, requested: Path | None) -> Path | None:
    if requested is not None:
        return requested
    environment = os.environ.get(PRIVATE_PATTERN_ENV, "").strip()
    if environment:
        return Path(environment)
    return _git_config_path(repository)


def load_private_literals(
    repository: Path,
    requested: Path | None,
) -> tuple[bytes, ...]:
    selected = _selected_path(repository, requested)
    if selected is None:
        return ()
    if not selected.is_absolute():
        raise PrivateBoundaryError("private-boundary pattern path must be absolute")
    if selected.is_symlink():
        raise PrivateBoundaryError("private-boundary pattern file must not be a symlink")
    try:
        path = selected.resolve(strict=True)
    except OSError as exc:
        raise PrivateBoundaryError("private-boundary pattern file is unavailable") from exc
    if path == repository or repository in path.parents:
        raise PrivateBoundaryError(
            "private-boundary pattern file must remain outside the public repository"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise PrivateBoundaryError(
                    "private-boundary pattern source must be a regular file"
                )
            if metadata.st_uid != os.geteuid():
                raise PrivateBoundaryError(
                    "private-boundary pattern file must be owned by the current user"
                )
            if metadata.st_nlink != 1:
                raise PrivateBoundaryError(
                    "private-boundary pattern file must not be hard-linked"
                )
            if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise PrivateBoundaryError(
                    "private-boundary pattern file must not grant group or other permissions"
                )
            chunks: list[bytes] = []
            remaining = MAX_PRIVATE_PATTERN_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(body) > MAX_PRIVATE_PATTERN_BYTES:
            raise PrivateBoundaryError("private-boundary pattern file is too large")
        lines = body.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PrivateBoundaryError(
            "cannot read private-boundary patterns as UTF-8"
        ) from exc

    values: list[bytes] = []
    for raw in lines:
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        encoded = value.encode("utf-8")
        if len(encoded) < 4:
            raise PrivateBoundaryError(
                "private-boundary patterns must contain at least four UTF-8 bytes"
            )
        values.append(encoded)
    values = list(dict.fromkeys(values))
    if not values:
        raise PrivateBoundaryError("private-boundary pattern file contains no patterns")
    if len(values) > MAX_PRIVATE_PATTERN_COUNT:
        raise PrivateBoundaryError(
            "private-boundary pattern file contains too many patterns"
        )
    return tuple(values)
