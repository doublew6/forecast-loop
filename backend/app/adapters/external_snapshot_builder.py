"""Executable adapters for repository-external snapshot producers."""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..domain import Horizon
from ..ports import NoApplicableSessionError, SnapshotBuilderError

MAX_ERROR_TEXT = 4000


@dataclass(frozen=True, slots=True)
class _ExternalBuilder:
    executable: Path
    timezone: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "executable", Path(self.executable))
        if self.timeout_seconds <= 0:
            raise ValueError("builder timeout must be greater than zero")

    def _run(self, arguments: list[str], *, output_path: Path) -> None:
        executable = _trusted_executable(self.executable)
        output = output_path.absolute()
        if output.exists() or output.is_symlink():
            raise SnapshotBuilderError(
                f"builder output path must not already exist: {output}"
            )
        try:
            completed = subprocess.run(
                [str(executable), *arguments, "--output", str(output)],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=_builder_environment(self.timezone),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SnapshotBuilderError(f"snapshot builder could not run: {exc}") from exc
        detail = (completed.stderr or completed.stdout).strip()[-MAX_ERROR_TEXT:]
        if completed.returncode == 3:
            raise NoApplicableSessionError(
                detail or "the requested source session is not applicable"
            )
        if completed.returncode != 0:
            raise SnapshotBuilderError(
                f"snapshot builder exited {completed.returncode}: {detail}"
            )
        if output.is_symlink() or not output.is_file():
            raise SnapshotBuilderError(
                "snapshot builder did not publish a regular output file"
            )


@dataclass(frozen=True, slots=True)
class ExternalEvidenceSnapshotBuilder(_ExternalBuilder):
    """Run a private/read-only adapter through the public evidence CLI contract."""

    def build_snapshot(
        self,
        *,
        base_session: date,
        captured_at: datetime,
        output_path: Path,
    ) -> None:
        self._run(
            [
                "--base-session",
                base_session.isoformat(),
                "--captured-at",
                captured_at.isoformat(),
            ],
            output_path=output_path,
        )


@dataclass(frozen=True, slots=True)
class ExternalMarketOutcomeSnapshotBuilder(_ExternalBuilder):
    """Run a private/read-only adapter through the public outcome CLI contract."""

    def build_snapshot(
        self,
        *,
        target_date: date,
        horizon: Horizon,
        captured_at: datetime,
        output_path: Path,
    ) -> None:
        self._run(
            [
                "--target-date",
                target_date.isoformat(),
                "--horizon",
                horizon.value,
                "--captured-at",
                captured_at.isoformat(),
            ],
            output_path=output_path,
        )


def _trusted_executable(configured: Path) -> Path:
    path = configured.expanduser()
    if path.is_symlink():
        raise SnapshotBuilderError(f"builder executable may not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise SnapshotBuilderError(f"builder executable is unavailable: {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise SnapshotBuilderError(
            f"builder executable must be an executable regular file: {resolved}"
        )
    return resolved


def _builder_environment(timezone: str) -> dict[str, str]:
    """Pass only non-secret runtime settings to the external adapter."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "TZ": timezone,
        "PYTHONNOUSERSITE": "1",
    }
