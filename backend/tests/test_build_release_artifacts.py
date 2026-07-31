from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from scripts.build_release_artifacts import (
    ReleaseBuildError,
    build_directory_archive,
    build_release_artifacts,
    build_source_archive,
    compare_artifact_sets,
    release_build_environment,
    validate_build_constraints,
)


def _git(repository: Path, *arguments: str, environment: dict[str, str] | None = None) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
    )


def _minimal_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "frontend").mkdir(parents=True)
    (repository / "requirements").mkdir()
    (repository / "pyproject.toml").write_text(
        (
            '[project]\nname = "forecast-loop"\nversion = "0.1.0"\n\n'
            '[build-system]\nrequires = ["hatchling==1.28.0"]\n'
            'build-backend = "hatchling.build"\n'
        ),
        encoding="utf-8",
    )
    (repository / "requirements" / "release-build-constraints.txt").write_text(
        "hatchling==1.28.0 \\\n"
        "    --hash=sha256:"
        + ("0" * 64)
        + "\n",
        encoding="utf-8",
    )
    (repository / "requirements" / "release-build.in").write_text(
        "hatchling==1.28.0\n",
        encoding="utf-8",
    )
    (repository / "frontend" / "package.json").write_text(
        json.dumps(
            {
                "name": "forecast-loop-frontend",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )
    (repository / "frontend" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "forecast-loop-frontend",
                "version": "0.1.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "forecast-loop-frontend",
                        "version": "0.1.0",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.com")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "release fixture")
    return repository


def _fake_artifacts(
    repository: Path,
    output: Path,
    version: str,
    epoch: int,
    revision: str,
) -> tuple[Path, ...]:
    del repository, epoch, revision
    output.mkdir(parents=True)
    artifacts = (
        output / f"forecast-loop-{version}-source.tar.gz",
        output / f"forecast_loop-{version}-py3-none-any.whl",
        output / f"forecast_loop-{version}.tar.gz",
        output / f"forecast-loop-{version}-frontend.tar.gz",
    )
    for index, artifact in enumerate(artifacts):
        artifact.write_bytes(f"artifact-{index}\n".encode())
    return artifacts


def test_directory_archive_normalizes_metadata_and_order(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    nested = source / "assets"
    nested.mkdir(parents=True)
    executable = source / "run"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (nested / "index.js").write_text("console.log('ok')\n", encoding="utf-8")

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    build_directory_archive(source, first, prefix="frontend", epoch=123456789)
    os.utime(executable, (987654321, 987654321))
    build_directory_archive(source, second, prefix="frontend", epoch=123456789)

    assert first.read_bytes() == second.read_bytes()
    with gzip.open(first) as stream, tarfile.open(fileobj=stream) as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == [
        "frontend",
        "frontend/assets",
        "frontend/assets/index.js",
        "frontend/run",
    ]
    assert {member.uid for member in members} == {0}
    assert {member.mtime for member in members} == {123456789}
    assert members[-1].mode == 0o755


def test_directory_archive_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    source.mkdir()
    target = source / "target.txt"
    target.write_text("safe\n", encoding="utf-8")
    (source / "link.txt").symlink_to(target)

    with pytest.raises(ReleaseBuildError, match="cannot contain symlinks"):
        build_directory_archive(
            source,
            tmp_path / "frontend.tar.gz",
            prefix="frontend",
            epoch=1,
        )


def test_source_archive_is_deterministic_for_a_fixed_revision(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.com")
    (repository / "README.md").write_text("release fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    _git(repository, "commit", "-m", "fixture", environment=environment)

    first = tmp_path / "source-a.tar.gz"
    second = tmp_path / "source-b.tar.gz"
    build_source_archive(
        repository,
        first,
        version="0.1.0",
        epoch=1767225600,
        revision="HEAD",
    )
    build_source_archive(
        repository,
        second,
        version="0.1.0",
        epoch=1767225600,
        revision="HEAD",
    )

    assert first.read_bytes() == second.read_bytes()


def test_source_archive_rejects_private_revision_without_writing_artifact(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.com")
    (repository / "README.md").write_text("release fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "public fixture")
    public_revision = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    private_document = repository / "docs" / "private" / "policy.md"
    private_document.parent.mkdir(parents=True)
    private_document.write_text("safe-looking private policy\n", encoding="utf-8")
    _git(repository, "add", "docs/private/policy.md")
    _git(repository, "commit", "-m", "private fixture")

    public_output = tmp_path / "public.tar.gz"
    build_source_archive(
        repository,
        public_output,
        version="0.1.0",
        epoch=1767225600,
        revision=public_revision,
    )
    assert public_output.is_file()

    blocked_output = tmp_path / "blocked.tar.gz"
    with pytest.raises(ReleaseBuildError, match="public-boundary audit"):
        build_source_archive(
            repository,
            blocked_output,
            version="0.1.0",
            epoch=1767225600,
            revision="HEAD",
        )
    assert not blocked_output.exists()


def test_release_builder_compares_two_builds_and_writes_checksums(
    tmp_path: Path,
) -> None:
    repository = _minimal_repository(tmp_path)
    output = tmp_path / "release"

    artifacts = build_release_artifacts(
        repository,
        output,
        version="0.1.0",
        epoch=123,
        build_once=_fake_artifacts,
    )

    checksums = output / "SHA256SUMS"
    assert checksums in artifacts
    lines = checksums.read_text(encoding="ascii").splitlines()
    assert lines == sorted(lines, key=lambda item: item.split("  ", 1)[1])
    for line in lines:
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()


def test_release_build_environment_rejects_inherited_vite_pollution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VITE_API_BASE_URL", "https://attacker.invalid")
    monkeypatch.setenv("VITE_STATIC_DEMO", "true")
    monkeypatch.setenv("VITE_UNREVIEWED_SECRET", "must-not-reach-vite")
    monkeypatch.setenv("FORECAST_LOOP_TEST_MARKER", "preserved")

    environment = release_build_environment("0.1.0", epoch=123)

    assert environment["FORECAST_LOOP_TEST_MARKER"] == "preserved"
    assert environment["SOURCE_DATE_EPOCH"] == "123"
    assert environment["VITE_API_BASE_URL"] == ""
    assert environment["VITE_STATIC_DEMO"] == "false"
    assert environment["VITE_BASE_PATH"] == "/"
    assert environment["VITE_ROUTER_MODE"] == "browser"
    assert environment["VITE_RELEASE_VERSION"] == "0.1.0"
    assert "VITE_UNREVIEWED_SECRET" not in environment


def test_release_build_constraints_are_exact_and_hash_locked() -> None:
    path = validate_build_constraints(Path(__file__).resolve().parents[2])

    assert path.name == "release-build-constraints.txt"


def test_hatch_sdist_excludes_git_worktree_metadata() -> None:
    repository = Path(__file__).resolve().parents[2]
    with (repository / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)

    assert "/.git" in pyproject["tool"]["hatch"]["build"]["exclude"]


def test_release_build_constraints_reject_pyproject_drift(tmp_path: Path) -> None:
    repository = _minimal_repository(tmp_path)
    pyproject = repository / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "hatchling==1.28.0",
            "hatchling==1.27.0",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseBuildError, match="does not match pyproject"):
        validate_build_constraints(repository)


def test_release_build_constraints_reject_missing_hash(tmp_path: Path) -> None:
    repository = _minimal_repository(tmp_path)
    constraints = repository / "requirements" / "release-build-constraints.txt"
    constraints.write_text("hatchling==1.28.0\n", encoding="utf-8")

    with pytest.raises(ReleaseBuildError, match="must contain only SHA-256 hashes"):
        validate_build_constraints(repository)


def test_release_builder_refuses_non_reproducible_output(tmp_path: Path) -> None:
    repository = _minimal_repository(tmp_path)
    calls = 0

    def mismatched(
        repository: Path,
        output: Path,
        version: str,
        epoch: int,
        revision: str,
    ) -> tuple[Path, ...]:
        nonlocal calls
        artifacts = _fake_artifacts(repository, output, version, epoch, revision)
        calls += 1
        if calls == 2:
            artifacts[0].write_bytes(b"different\n")
        return artifacts

    output = tmp_path / "release"
    with pytest.raises(ReleaseBuildError, match="reproducibility check failed"):
        build_release_artifacts(
            repository,
            output,
            epoch=123,
            build_once=mismatched,
        )
    assert not output.exists()


def test_compare_artifact_sets_rejects_filename_drift(tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "one.whl").write_bytes(b"same")
    (second / "two.whl").write_bytes(b"same")

    with pytest.raises(ReleaseBuildError, match="different artifact names"):
        compare_artifact_sets((first / "one.whl",), (second / "two.whl",))


def test_release_builder_rejects_lockfile_root_version_drift(tmp_path: Path) -> None:
    repository = _minimal_repository(tmp_path)
    lock_path = repository / "frontend" / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"][""]["version"] = "0.2.0"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    _git(repository, "add", "frontend/package-lock.json")
    _git(repository, "commit", "-m", "drift lock version")

    with pytest.raises(ReleaseBuildError, match="lockfile versions differ"):
        build_release_artifacts(
            repository,
            tmp_path / "release",
            version="0.1.0",
            epoch=123,
            build_once=_fake_artifacts,
        )


def test_release_builder_isolates_dirty_and_mismatched_head(tmp_path: Path) -> None:
    repository = _minimal_repository(tmp_path)
    selected = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    package_path = repository / "frontend" / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["version"] = "0.2.0"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    _git(repository, "add", "frontend/package.json")
    _git(repository, "commit", "-m", "new head")
    package_path.write_text('{"name":"dirty","version":"9.9.9"}', encoding="utf-8")
    (repository / "untracked-secret.txt").write_text("must not leak\n", encoding="utf-8")

    observed_sources: list[Path] = []

    def inspect_clean_source(
        clean: Path,
        output: Path,
        version: str,
        epoch: int,
        revision: str,
    ) -> tuple[Path, ...]:
        clean_package = json.loads(
            (clean / "frontend" / "package.json").read_text(encoding="utf-8")
        )
        clean_head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=clean,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert clean_package["version"] == "0.1.0"
        assert clean_head == selected
        assert revision == selected
        assert not (clean / "untracked-secret.txt").exists()
        observed_sources.append(clean)
        return _fake_artifacts(clean, output, version, epoch, revision)

    build_release_artifacts(
        repository,
        tmp_path / "release",
        version="0.1.0",
        revision=selected,
        epoch=123,
        build_once=inspect_clean_source,
    )
    assert len(observed_sources) == 2
    assert all(not source.exists() for source in observed_sources)


def test_release_builder_rejects_revision_option_injection(tmp_path: Path) -> None:
    repository = _minimal_repository(tmp_path)

    with pytest.raises(ReleaseBuildError, match="command failed"):
        build_release_artifacts(
            repository,
            tmp_path / "release",
            version="0.1.0",
            revision="--help",
            epoch=123,
            build_once=_fake_artifacts,
        )
