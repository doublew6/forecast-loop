from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from scripts.audit_public_boundary import main


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Boundary Test")
    _git(repository, "config", "user.email", "boundary@example.com")
    (repository / ".gitignore").write_text(".env\n*.db\n", encoding="utf-8")
    (repository / "README.md").write_text("safe\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_staged_audit_blocks_and_redacts_personal_path(
    tmp_path: Path,
    capsys,
) -> None:
    repository = _repository(tmp_path)
    private_component = "private-person"
    private_path = "/" + f"Users/{private_component}/project"
    (repository / "notes.txt").write_text(private_path, encoding="utf-8")
    _git(repository, "add", "notes.txt")

    assert main(["--repository", str(repository), "--staged"]) == 2
    rendered = capsys.readouterr().out
    assert private_component not in rendered
    assert private_path not in rendered


def test_staged_audit_blocks_external_private_literal_without_echoing_it(
    tmp_path: Path,
    capsys,
) -> None:
    repository = _repository(tmp_path)
    private_literal = "Project" + "Zephyr"
    patterns = tmp_path / "private-patterns"
    patterns.write_text(private_literal + "\n", encoding="utf-8")
    patterns.chmod(0o600)
    (repository / "notes.txt").write_text(private_literal, encoding="utf-8")
    _git(repository, "add", "notes.txt")

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--staged",
                "--private-patterns-file",
                str(patterns),
                "--require-private-patterns",
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert private_literal not in rendered
    assert "notes.txt" not in rendered
    assert "private_boundary" in rendered


def test_private_patterns_must_remain_outside_repository(
    tmp_path: Path,
    capsys,
) -> None:
    repository = _repository(tmp_path)
    patterns = repository / "private-patterns"
    patterns.write_text("internal-project\n", encoding="utf-8")
    patterns.chmod(0o600)

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--private-patterns-file",
                str(patterns),
            ]
        )
        == 3
    )
    rendered = capsys.readouterr().out
    assert str(patterns) not in rendered


def test_tracked_ignored_file_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / ".env").write_text("EXAMPLE=true\n", encoding="utf-8")
    _git(repository, "add", "--force", ".env")

    assert main(["--repository", str(repository), "--staged"]) == 2


def test_binary_file_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "opaque.bin").write_bytes(b"safe\x00opaque")
    _git(repository, "add", "opaque.bin")

    assert main(["--repository", str(repository), "--staged"]) == 2


def test_staged_audit_rejects_symlink_without_echoing_target(
    tmp_path: Path,
    capsys,
) -> None:
    repository = _repository(tmp_path)
    private_target = "/" + "Users/private-person/private-project"
    (repository / "link").symlink_to(private_target)
    _git(repository, "add", "link")

    assert main(["--repository", str(repository), "--staged"]) == 3
    rendered = capsys.readouterr().out
    assert private_target not in rendered
    assert "private-person" not in rendered


def test_staged_audit_blocks_private_document_tree(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    private_doc = repository / "docs" / "private" / "research.md"
    private_doc.parent.mkdir(parents=True)
    private_doc.write_text("safe-looking text\n", encoding="utf-8")
    _git(repository, "add", "docs/private/research.md")

    assert main(["--repository", str(repository), "--staged"]) == 2


def test_staged_audit_fails_closed_on_unresolved_index(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "switch", "-c", "other")
    (repository / "README.md").write_text("other\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "other change")
    _git(repository, "switch", "main")
    (repository / "README.md").write_text("main\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "main change")
    result = subprocess.run(
        ("git", "merge", "other"),
        cwd=repository,
        check=False,
        capture_output=True,
    )
    assert result.returncode != 0

    assert main(["--repository", str(repository), "--staged"]) == 3


def test_repository_hooks_are_executable() -> None:
    hooks = Path(__file__).resolve().parents[2] / ".githooks"

    for hook in hooks.iterdir():
        assert hook.stat().st_mode & stat.S_IXUSR
