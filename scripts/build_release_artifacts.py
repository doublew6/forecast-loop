"""Build byte-reproducible release artifacts and compare two independent builds."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Final

try:
    from scripts.audit_public_boundary import (
        BoundaryAuditError,
        assert_public_revision,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from audit_public_boundary import (  # type: ignore[no-redef]
        BoundaryAuditError,
        assert_public_revision,
    )

PROJECT_NAME: Final = "forecast-loop"
FRONTEND_PACKAGE_NAME: Final = "forecast-loop-frontend"
GIT_OBJECT_PATTERN: Final = re.compile(r"[0-9a-f]{40}")
BUILD_CONSTRAINTS_PATH: Final = Path("requirements/release-build-constraints.txt")
BUILD_REQUIREMENTS_PATH: Final = Path("requirements/release-build.in")
EXACT_REQUIREMENT_PATTERN: Final = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)"
)
SHA256_OPTION_PATTERN: Final = re.compile(r"--hash=sha256:[0-9a-f]{64}")
RELEASE_VITE_ENVIRONMENT: Final = {
    "VITE_API_BASE_URL": "",
    "VITE_API_PROXY_TARGET": "http://127.0.0.1:8000",
    "VITE_BASE_PATH": "/",
    "VITE_ROUTER_MODE": "browser",
    "VITE_STATIC_DEMO": "false",
}


class ReleaseBuildError(RuntimeError):
    """The release artifact set could not be built or verified."""


BuildOnce = Callable[[Path, Path, str, int, str], tuple[Path, ...]]


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            tuple(command),
            cwd=cwd,
            env=env,
            check=True,
            capture_output=capture_output,
        )
    except FileNotFoundError as exc:
        raise ReleaseBuildError(f"required command is unavailable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        detail = f": {stderr}" if stderr else ""
        raise ReleaseBuildError(f"command failed: {' '.join(command)}{detail}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _project_version(repository: Path) -> str:
    with (repository / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream).get("project", {})
    name = project.get("name")
    version = project.get("version")
    if name != PROJECT_NAME or not isinstance(version, str):
        raise ReleaseBuildError(
            f"pyproject.toml must declare project {PROJECT_NAME!r} with a version"
        )

    frontend_package = json.loads(
        (repository / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    if frontend_package.get("name") != FRONTEND_PACKAGE_NAME:
        raise ReleaseBuildError(
            f"frontend/package.json must declare {FRONTEND_PACKAGE_NAME!r}"
        )
    if frontend_package.get("version") != version:
        raise ReleaseBuildError(
            "Python and frontend package versions differ: "
            f"{version!r} != {frontend_package.get('version')!r}"
        )

    frontend_lock = json.loads(
        (repository / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    )
    lock_root = frontend_lock.get("packages", {}).get("")
    if not isinstance(lock_root, dict):
        raise ReleaseBuildError(
            "frontend/package-lock.json must contain a root package entry"
        )
    for label, package in (
        ("top-level package", frontend_lock),
        ("root package", lock_root),
    ):
        if package.get("name") != FRONTEND_PACKAGE_NAME:
            raise ReleaseBuildError(
                "frontend/package-lock.json "
                f"{label} must declare {FRONTEND_PACKAGE_NAME!r}"
            )
        if package.get("version") != version:
            raise ReleaseBuildError(
                "Python and frontend lockfile versions differ at "
                f"{label}: {version!r} != {package.get('version')!r}"
            )
    return version


def _normalized_requirement(requirement: str) -> str:
    match = EXACT_REQUIREMENT_PATTERN.fullmatch(requirement)
    if match is None:
        raise ReleaseBuildError(
            "release build requirements must use exact name==version pins: "
            f"{requirement!r}"
        )
    name = match.group("name").lower().replace("_", "-")
    return f"{name}=={match.group('version')}"


def _build_system_requirements(repository: Path) -> tuple[str, ...]:
    with (repository / "pyproject.toml").open("rb") as stream:
        build_system = tomllib.load(stream).get("build-system", {})
    requirements = build_system.get("requires")
    if (
        not isinstance(requirements, list)
        or not requirements
        or any(not isinstance(item, str) for item in requirements)
    ):
        raise ReleaseBuildError(
            "pyproject.toml build-system.requires must be a non-empty list of exact pins"
        )
    normalized = tuple(_normalized_requirement(item) for item in requirements)
    if len(set(normalized)) != len(normalized):
        raise ReleaseBuildError("pyproject.toml contains duplicate build requirements")
    return normalized


def _build_input_requirements(path: Path) -> tuple[str, ...]:
    try:
        requirements = tuple(
            _normalized_requirement(line)
            for raw_line in path.read_text(encoding="utf-8").splitlines()
            if (line := raw_line.strip()) and not line.startswith("#")
        )
    except (OSError, UnicodeError) as exc:
        raise ReleaseBuildError(f"cannot read release build requirements: {exc}") from exc
    if not requirements:
        raise ReleaseBuildError("release build requirements must not be empty")
    if len(set(requirements)) != len(requirements):
        raise ReleaseBuildError("release build requirements contain duplicate pins")
    return requirements


def _constraint_entries(path: Path) -> dict[str, tuple[str, ...]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseBuildError(f"cannot read release build constraints: {exc}") from exc

    logical_lines: list[str] = []
    pending: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending.append(line[:-1].strip() if continued else line)
        if not continued:
            logical_lines.append(" ".join(pending))
            pending = []
    if pending:
        raise ReleaseBuildError("release build constraints end with an incomplete continuation")

    entries: dict[str, tuple[str, ...]] = {}
    for logical_line in logical_lines:
        fields = logical_line.split()
        requirement = _normalized_requirement(fields[0])
        hashes = tuple(fields[1:])
        if not hashes or any(SHA256_OPTION_PATTERN.fullmatch(item) is None for item in hashes):
            raise ReleaseBuildError(
                f"release build constraint {requirement!r} must contain only SHA-256 hashes"
            )
        if requirement in entries:
            raise ReleaseBuildError(
                f"duplicate release build constraint: {requirement}"
            )
        entries[requirement] = hashes
    if not entries:
        raise ReleaseBuildError("release build constraints must not be empty")
    return entries


def validate_build_constraints(repository: Path) -> Path:
    """Require every Python build dependency to be exactly pinned and hash locked."""

    build_requirements = set(_build_system_requirements(repository))
    input_requirements = set(
        _build_input_requirements(repository / BUILD_REQUIREMENTS_PATH)
    )
    if input_requirements != build_requirements:
        raise ReleaseBuildError(
            "release build input does not match pyproject.toml build-system.requires"
        )
    path = repository / BUILD_CONSTRAINTS_PATH
    entries = _constraint_entries(path)
    missing = sorted(build_requirements - entries.keys())
    if missing:
        raise ReleaseBuildError(
            "release build constraints do not match pyproject.toml: "
            + ", ".join(missing)
        )
    return path


def release_build_environment(version: str, *, epoch: int) -> dict[str, str]:
    """Return a deterministic frontend environment without inherited VITE inputs."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VITE_")
    }
    environment.update(RELEASE_VITE_ENVIRONMENT)
    environment.update(
        {
            "LC_ALL": "C.UTF-8",
            "SOURCE_DATE_EPOCH": str(epoch),
            "TZ": "UTC",
            "VITE_RELEASE_VERSION": version,
        }
    )
    return environment


def _resolve_revision(repository: Path, revision: str) -> tuple[str, str]:
    resolved: list[str] = []
    for object_type in ("commit", "tree"):
        result = _run(
            (
                "git",
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{{object_type}}}",
            ),
            cwd=repository,
            capture_output=True,
        )
        object_id = result.stdout.decode("ascii", errors="strict").strip()
        if not GIT_OBJECT_PATTERN.fullmatch(object_id):
            raise ReleaseBuildError(
                f"git returned an invalid {object_type} object ID: {object_id!r}"
            )
        resolved.append(object_id)
    return resolved[0], resolved[1]


@contextlib.contextmanager
def clean_revision_worktree(
    repository: Path,
    destination: Path,
    *,
    commit: str,
) -> Iterator[Path]:
    """Check out one resolved commit in an isolated temporary worktree."""

    if not GIT_OBJECT_PATTERN.fullmatch(commit):
        raise ReleaseBuildError(f"refusing unresolved Git revision: {commit!r}")
    if destination.exists() or destination.is_symlink():
        raise ReleaseBuildError(f"temporary worktree already exists: {destination}")
    _run(
        ("git", "worktree", "add", "--detach", str(destination), commit),
        cwd=repository,
    )
    try:
        yield destination
    finally:
        _run(
            ("git", "worktree", "remove", "--force", str(destination)),
            cwd=repository,
        )


def _git_source_date_epoch(repository: Path, revision: str) -> int:
    result = _run(
        ("git", "show", "-s", "--format=%ct", f"{revision}^{{commit}}"),
        cwd=repository,
        capture_output=True,
    )
    raw = result.stdout.decode("ascii", errors="strict").strip()
    try:
        epoch = int(raw)
    except ValueError as exc:
        raise ReleaseBuildError(f"git returned an invalid commit timestamp: {raw!r}") from exc
    if epoch < 0:
        raise ReleaseBuildError("SOURCE_DATE_EPOCH cannot be negative")
    return epoch


def _gzip_bytes(body: bytes, output: Path, *, epoch: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw,
            mtime=epoch,
        ) as compressed:
            compressed.write(body)


def _assert_public_release_revision(repository: Path, revision: str) -> None:
    try:
        assert_public_revision(repository, revision)
    except BoundaryAuditError as exc:
        raise ReleaseBuildError(
            "selected revision failed the public-boundary audit"
        ) from exc


def build_source_archive(
    repository: Path,
    output: Path,
    *,
    version: str,
    epoch: int,
    revision: str,
) -> None:
    """Archive exactly the selected Git tree with deterministic gzip metadata."""

    commit, _tree = _resolve_revision(repository, revision)
    _assert_public_release_revision(repository, commit)
    result = _run(
        (
            "git",
            "archive",
            "--format=tar",
            f"--prefix={PROJECT_NAME}-{version}/",
            commit,
        ),
        cwd=repository,
        capture_output=True,
    )
    _gzip_bytes(result.stdout, output, epoch=epoch)


def _normalized_tar_info(
    path: Path,
    *,
    archive_name: str,
    epoch: int,
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(archive_name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = epoch
    if path.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
        info.size = path.stat().st_size
    return info


def build_directory_archive(
    source: Path,
    output: Path,
    *,
    prefix: str,
    epoch: int,
) -> None:
    """Create a sorted tar.gz with normalized ownership, modes, and timestamps."""

    root = source.resolve(strict=True)
    if not root.is_dir():
        raise ReleaseBuildError(f"archive source is not a directory: {root}")

    tar_buffer = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(
            _normalized_tar_info(root, archive_name=prefix, epoch=epoch)
        )
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            if path.is_symlink():
                raise ReleaseBuildError(
                    f"release directory archives cannot contain symlinks: {path}"
                )
            relative = path.relative_to(root).as_posix()
            archive_name = f"{prefix}/{relative}"
            info = _normalized_tar_info(
                path,
                archive_name=archive_name,
                epoch=epoch,
            )
            if path.is_dir():
                archive.addfile(info)
            elif path.is_file():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                raise ReleaseBuildError(
                    f"unsupported release archive entry: {path}"
                )
    tar_buffer.seek(0)
    _gzip_bytes(tar_buffer.read(), output, epoch=epoch)
    tar_buffer.close()


def _build_python_distributions(
    repository: Path,
    output: Path,
    *,
    environment: dict[str, str],
    version: str,
) -> tuple[Path, Path]:
    python_output = output / "python"
    python_output.mkdir(parents=True)
    constraints = validate_build_constraints(repository)
    _run(
        (
            "uv",
            "build",
            "--build-constraints",
            str(constraints),
            "--require-hashes",
            "--sdist",
            "--wheel",
            "--clear",
            "--no-build-logs",
            "--no-create-gitignore",
            "--out-dir",
            str(python_output),
            ".",
        ),
        cwd=repository,
        env=environment,
    )
    wheels = tuple(python_output.glob("*.whl"))
    sdists = tuple(python_output.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseBuildError(
            "Python build must emit exactly one wheel and one sdist; "
            f"found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    expected_prefix = f"forecast_loop-{version}"
    if not wheels[0].name.startswith(expected_prefix):
        raise ReleaseBuildError(f"unexpected wheel filename: {wheels[0].name}")
    if sdists[0].name != f"{expected_prefix}.tar.gz":
        raise ReleaseBuildError(f"unexpected sdist filename: {sdists[0].name}")

    wheel = output / wheels[0].name
    sdist = output / sdists[0].name
    wheels[0].replace(wheel)
    sdists[0].replace(sdist)
    python_output.rmdir()
    return wheel, sdist


def _build_frontend_archive(
    repository: Path,
    output: Path,
    *,
    environment: dict[str, str],
    version: str,
    epoch: int,
) -> Path:
    frontend = repository / "frontend"
    _run(("npm", "run", "build"), cwd=frontend, env=environment)
    archive = output / f"{PROJECT_NAME}-{version}-frontend.tar.gz"
    build_directory_archive(
        frontend / "dist",
        archive,
        prefix=f"{PROJECT_NAME}-{version}-frontend",
        epoch=epoch,
    )
    return archive


def _build_once(
    repository: Path,
    output: Path,
    version: str,
    epoch: int,
    revision: str,
) -> tuple[Path, ...]:
    output.mkdir(parents=True, exist_ok=False)
    environment = release_build_environment(version, epoch=epoch)
    _run(
        ("npm", "ci", "--no-audit", "--no-fund"),
        cwd=repository / "frontend",
        env=environment,
    )
    source_archive = output / f"{PROJECT_NAME}-{version}-source.tar.gz"
    build_source_archive(
        repository,
        source_archive,
        version=version,
        epoch=epoch,
        revision=revision,
    )
    wheel, sdist = _build_python_distributions(
        repository,
        output,
        environment=environment,
        version=version,
    )
    frontend_archive = _build_frontend_archive(
        repository,
        output,
        environment=environment,
        version=version,
        epoch=epoch,
    )
    return tuple(sorted((source_archive, wheel, sdist, frontend_archive)))


def artifact_hashes(paths: Sequence[Path]) -> dict[str, str]:
    """Return a filename-to-SHA256 map after rejecting duplicate filenames."""

    hashes: dict[str, str] = {}
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ReleaseBuildError(f"release artifact must be a regular file: {path}")
        if path.name in hashes:
            raise ReleaseBuildError(f"duplicate release artifact filename: {path.name}")
        hashes[path.name] = _sha256(path)
    return dict(sorted(hashes.items()))


def compare_artifact_sets(first: Sequence[Path], second: Sequence[Path]) -> dict[str, str]:
    """Fail unless two build outputs have identical names and bytes."""

    first_hashes = artifact_hashes(first)
    second_hashes = artifact_hashes(second)
    if first_hashes.keys() != second_hashes.keys():
        raise ReleaseBuildError(
            "reproducibility check produced different artifact names: "
            f"{sorted(first_hashes)} != {sorted(second_hashes)}"
        )
    mismatches = [
        name
        for name in first_hashes
        if first_hashes[name] != second_hashes[name]
    ]
    if mismatches:
        raise ReleaseBuildError(
            "reproducibility check failed for: " + ", ".join(mismatches)
        )
    return first_hashes


def _write_checksums(output: Path, hashes: dict[str, str]) -> Path:
    checksums = output / "SHA256SUMS"
    body = "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items()))
    checksums.write_text(body, encoding="ascii", newline="\n")
    return checksums


def build_release_artifacts(
    repository: Path,
    output: Path,
    *,
    version: str | None = None,
    epoch: int | None = None,
    revision: str = "HEAD",
    build_once: BuildOnce = _build_once,
) -> tuple[Path, ...]:
    """Build twice, compare every byte, then publish one verified artifact set."""

    root = repository.resolve(strict=True)
    commit, _tree = _resolve_revision(root, revision)
    _assert_public_release_revision(root, commit)
    source_date_epoch = (
        _git_source_date_epoch(root, commit)
        if epoch is None
        else epoch
    )
    if source_date_epoch < 0:
        raise ReleaseBuildError("SOURCE_DATE_EPOCH cannot be negative")

    destination = output.absolute()
    if destination.exists() or destination.is_symlink():
        raise ReleaseBuildError(f"refusing to overwrite release output: {destination}")

    with tempfile.TemporaryDirectory(prefix=f"{PROJECT_NAME}-release-") as raw:
        temporary = Path(raw)
        with clean_revision_worktree(
            root,
            temporary / "source-a",
            commit=commit,
        ) as first_source:
            declared_version = _project_version(first_source)
            release_version = version or declared_version
            if release_version != declared_version:
                raise ReleaseBuildError(
                    f"requested version {release_version!r} does not match "
                    f"selected revision {declared_version!r}"
                )
            first = build_once(
                first_source,
                temporary / "build-a",
                release_version,
                source_date_epoch,
                commit,
            )
        with clean_revision_worktree(
            root,
            temporary / "source-b",
            commit=commit,
        ) as second_source:
            if _project_version(second_source) != release_version:
                raise ReleaseBuildError(
                    "selected revision version changed between isolated builds"
                )
            second = build_once(
                second_source,
                temporary / "build-b",
                release_version,
                source_date_epoch,
                commit,
            )
        hashes = compare_artifact_sets(first, second)

        destination.mkdir(parents=True, exist_ok=False)
        copied: list[Path] = []
        for artifact in sorted(first, key=lambda item: item.name):
            target = destination / artifact.name
            shutil.copyfile(artifact, target)
            copied.append(target)
        copied.append(_write_checksums(destination, hashes))
    return tuple(copied)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build source, wheel, sdist, and frontend archives twice; publish "
            "only when every artifact is byte-identical."
        )
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--source-date-epoch", type=int)
    arguments = parser.parse_args(argv)
    artifacts = build_release_artifacts(
        arguments.repository,
        arguments.output_dir,
        version=arguments.version,
        epoch=arguments.source_date_epoch,
        revision=arguments.revision,
    )
    for artifact in artifacts:
        print(f"{_sha256(artifact)}  {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
