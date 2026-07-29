from __future__ import annotations

import io
import tarfile
from pathlib import Path

from scripts.audit_release_artifacts import main


def _tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for name, body in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))


def test_release_artifact_audit_accepts_clean_archives(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _tar(
        artifacts / "forecast-loop-source.tar.gz",
        {"forecast-loop/README.md": b"public\n"},
    )
    (artifacts / "SHA256SUMS").write_text("synthetic checksum\n", encoding="utf-8")

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 0
    )


def test_release_artifact_audit_accepts_documented_empty_data_directory(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    archive_path = artifacts / "forecast-loop-source.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        directory = tarfile.TarInfo(name="forecast-loop/data/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        body = b"Runtime data is intentionally excluded.\n"
        readme = tarfile.TarInfo(name="forecast-loop/data/README.md")
        readme.size = len(body)
        archive.addfile(readme, io.BytesIO(body))

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 0
    )


def test_release_artifact_audit_blocks_embedded_secret_without_echo(
    tmp_path: Path,
    capsys,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    secret = "ghp_" + "A" * 36
    _tar(
        artifacts / "forecast-loop-source.tar.gz",
        {"forecast-loop/config.txt": secret.encode()},
    )

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 2
    )
    assert secret not in capsys.readouterr().out


def test_release_artifact_audit_rejects_source_maps(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _tar(
        artifacts / "forecast-loop-frontend.tar.gz",
        {"frontend/assets/app.js.map": b"{}"},
    )

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 2
    )


def test_release_artifact_audit_rejects_nested_private_data_tree(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _tar(
        artifacts / "forecast-loop-source.tar.gz",
        {"forecast-loop/data/snapshot.json": b"{}\n"},
    )

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 2
    )


def test_release_artifact_audit_rejects_non_regular_member(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    archive_path = artifacts / "forecast-loop-source.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        info = tarfile.TarInfo(name="forecast-loop/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "README.md"
        archive.addfile(info)

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 3
    )


def test_release_artifact_audit_rejects_duplicate_member_paths(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    archive_path = artifacts / "forecast-loop-source.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        for body in (b"first\n", b"second\n"):
            info = tarfile.TarInfo(name="forecast-loop/README.md")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 3
    )


def test_release_artifact_audit_does_not_silently_skip_macos_metadata(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / ".DS_Store").write_text("metadata\n", encoding="utf-8")

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
            ]
        )
        == 2
    )


def test_release_artifact_audit_redacts_external_private_boundary(
    tmp_path: Path,
    capsys,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    private_literal = "Project" + "Zephyr"
    private_member = f"forecast-loop/{private_literal}.txt"
    _tar(
        artifacts / "forecast-loop-source.tar.gz",
        {private_member: private_literal.encode()},
    )
    patterns = tmp_path / "private-boundary-patterns"
    patterns.write_text(private_literal + "\n", encoding="utf-8")
    patterns.chmod(0o600)

    assert (
        main(
            [
                "--repository",
                str(Path(__file__).resolve().parents[2]),
                "--artifact-dir",
                str(artifacts),
                "--private-patterns-file",
                str(patterns),
                "--require-private-patterns",
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert private_literal not in rendered
    assert private_member not in rendered
    assert str(patterns) not in rendered
