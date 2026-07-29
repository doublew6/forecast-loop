from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.audit_release_history import audit_repository, main


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _git_output(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "history"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.com")
    _write(repository / "README.md", "safe\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_history_audit_scans_deleted_blobs_without_returning_values(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    secret = "ghp_" + "A" * 36
    private_home = "/" + "Users/private-person/project"
    _write(
        repository / "local.env",
        f"ACCESS_TOKEN={secret}\npath={private_home}\n",
    )
    _git(repository, "add", "local.env")
    _git(repository, "commit", "-m", "historical fixture")
    (repository / "local.env").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "remove fixture")

    report = audit_repository(repository, revisions=("main",))
    rendered = json.dumps(report)

    assert report["commit_count"] == 3
    assert report["secret_findings"]["github_token"]["hit_blob_count"] == 1
    assert report["pii_findings"]["macos_home_path"]["hit_blob_count"] == 1
    assert secret not in rendered
    assert "private-person" not in rendered
    assert "local.env" not in rendered


def test_history_audit_cli_can_fail_on_secrets_and_writes_private_report(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _write(repository / "credential.txt", "sk-" + "z" * 32 + "\n")
    _git(repository, "add", "credential.txt")
    _git(repository, "commit", "-m", "credential fixture")
    output = tmp_path / "reports" / "audit.json"

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--revision",
                "main",
                "--output",
                str(output),
                "--fail-on-secrets",
            ]
        )
        == 2
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert "sk-" not in output.read_text(encoding="utf-8")


def test_noreply_and_example_emails_are_not_reported_as_pii(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _write(
        repository / "contacts.txt",
        (
            "123+contributor@users.noreply.github.com\n"
            "noreply@github.com\n"
            "support@github.com\n"
            "reader@example.org\n"
        ),
    )
    _git(repository, "add", "contacts.txt")
    _git(repository, "commit", "-m", "document public contacts")

    report = audit_repository(repository, revisions=("main",))

    assert report["pii_findings"]["email_address"]["hit_blob_count"] == 0


def test_history_audit_cli_fails_closed_when_a_blob_is_skipped(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "opaque.bin").write_bytes(b"safe\x00opaque\n")
    _git(repository, "add", "opaque.bin")
    _git(repository, "commit", "-m", "binary fixture")
    output = tmp_path / "reports" / "audit.json"

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--revision",
                "main",
                "--output",
                str(output),
                "--fail-on-secrets",
                "--fail-on-skipped-blobs",
            ]
        )
        == 3
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["skipped_binary_blob_count"] == 1


def test_history_audit_scans_secret_commit_messages_and_can_block(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    secret = "ghp_" + "C" * 36
    _write(repository / "change.txt", "safe body\n")
    _git(repository, "add", "change.txt")
    _git(repository, "commit", "-m", f"remove exposed token {secret}")

    report = audit_repository(repository, revisions=("main",))
    rendered = json.dumps(report)
    finding = report["secret_findings"]["github_token"]

    assert finding["hit_count"] == 1
    assert finding["hit_blob_count"] == 0
    assert finding["hit_commit_metadata_count"] == 1
    assert finding["locations"][0]["source"] == "commit_metadata"
    assert secret not in rendered

    output = tmp_path / "reports" / "commit-message-audit.json"
    assert (
        main(
            [
                "--repository",
                str(repository),
                "--revision",
                "main",
                "--output",
                str(output),
                "--fail-on-secrets",
            ]
        )
        == 2
    )


def test_history_audit_scans_author_and_committer_email_without_returning_it(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    private_domain = "private-" + "domain.invalid"
    author_email = "author@" + private_domain
    committer_email = "committer@" + private_domain
    _write(repository / "author-change.txt", "safe body\n")
    _git(repository, "add", "author-change.txt")
    _git(
        repository,
        "commit",
        "--author",
        f"Private Author <{author_email}>",
        "-m",
        "author identity fixture",
    )
    _git(repository, "config", "user.email", committer_email)
    _write(repository / "committer-change.txt", "safe body\n")
    _git(repository, "add", "committer-change.txt")
    _git(
        repository,
        "commit",
        "--author",
        "Release Test <release@example.com>",
        "-m",
        "committer identity fixture",
    )

    report = audit_repository(repository, revisions=("main",))
    rendered = json.dumps(report)
    finding = report["pii_findings"]["email_address"]

    assert finding["hit_count"] == 2
    assert finding["hit_blob_count"] == 0
    assert finding["hit_commit_metadata_count"] == 2
    assert finding["locations"][0]["source"] == "commit_metadata"
    assert author_email not in rendered
    assert committer_email not in rendered
    assert "private-domain" not in rendered


def test_history_audit_scans_historical_tree_path_bytes_and_can_block_pii(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    personal_component = "private-person"
    fixture = repository / "Users" / personal_component / "forecast.txt"
    fixture.parent.mkdir(parents=True)
    _write(fixture, "safe body\n")
    _git(repository, "add", "Users")
    _git(repository, "commit", "-m", "path fixture")
    fixture.unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "remove path fixture")

    report = audit_repository(repository, revisions=("main",))
    rendered = json.dumps(report)
    finding = report["pii_findings"]["macos_home_path"]

    assert finding["hit_count"] == 1
    assert finding["hit_blob_count"] == 0
    assert finding["hit_tree_path_count"] == 1
    assert finding["locations"][0]["source"] == "tree_path"
    assert "path_fingerprint" in finding["locations"][0]
    assert personal_component not in rendered
    assert "forecast.txt" not in rendered

    output = tmp_path / "reports" / "tree-path-audit.json"
    assert (
        main(
            [
                "--repository",
                str(repository),
                "--revision",
                "main",
                "--output",
                str(output),
                "--fail-on-pii",
            ]
        )
        == 4
    )


def test_history_audit_scans_annotated_tag_message_attached_to_revision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    secret = "sk-" + "t" * 32
    _git(repository, "tag", "--annotate", "v0.1.0", "-m", f"release token {secret}")

    report = audit_repository(repository, revisions=("main",))
    rendered = json.dumps(report)
    finding = report["secret_findings"]["openai_key"]

    assert report["tag_metadata_count"] == 1
    assert finding["hit_count"] == 1
    assert finding["hit_blob_count"] == 0
    assert finding["hit_tag_metadata_count"] == 1
    assert finding["locations"][0]["source"] == "tag_metadata"
    assert secret not in rendered

    output = tmp_path / "reports" / "tag-message-audit.json"
    assert (
        main(
            [
                "--repository",
                str(repository),
                "--revision",
                "main",
                "--output",
                str(output),
                "--fail-on-secrets",
            ]
        )
        == 2
    )


def test_default_history_audit_includes_unmerged_local_branches(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "switch", "-c", "unmerged")
    secret = "ghp_" + "D" * 36
    _write(repository / "branch-only.txt", secret)
    _git(repository, "add", "branch-only.txt")
    _git(repository, "commit", "-m", "branch-only fixture")
    _git(repository, "switch", "main")

    report = audit_repository(repository)

    assert report["commit_count"] == 2
    assert report["secret_findings"]["github_token"]["hit_blob_count"] == 1


def test_history_audit_aggregates_private_boundary_without_echoing_rules(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    private_literal = "Project" + "Zephyr"
    _write(repository / "private.txt", private_literal)
    _git(repository, "add", "private.txt")
    _git(repository, "commit", "-m", "private boundary fixture")
    (repository / "private.txt").unlink()
    _git(repository, "add", "-u")
    _git(repository, "commit", "-m", "remove fixture")

    report = audit_repository(
        repository,
        revisions=("main",),
        private_literals=(private_literal.encode(),),
    )
    rendered = json.dumps(report)

    assert report["private_boundary_findings"]["hit_count"] == 1
    assert private_literal not in rendered
    assert "private_boundary_001" not in rendered


def test_history_audit_scans_ref_names_without_returning_them(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    personal_component = "private-person"
    ref_name = "codex/" + f"Users/{personal_component}/topic"
    _git(repository, "branch", ref_name)

    report = audit_repository(repository)
    rendered = json.dumps(report)
    finding = report["pii_findings"]["macos_home_path"]

    assert finding["hit_ref_name_count"] == 1
    assert personal_component not in rendered
    assert ref_name not in rendered


def test_pre_push_audits_an_unreferenced_raw_commit_object(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repository = _repository(tmp_path)
    secret = "ghp_" + "R" * 36
    _write(repository / "raw-object.txt", secret)
    _git(repository, "add", "raw-object.txt")
    _git(repository, "commit", "-m", "raw object fixture")
    object_id = _git_output(repository, "rev-parse", "HEAD")
    _git(repository, "reset", "--hard", "HEAD^")
    payload = (
        f"refs/heads/release {object_id} refs/heads/release {'0' * 40}\n"
    ).encode()
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(payload)),
    )

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--pre-push",
                "--public-gate",
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert secret not in rendered
    assert "raw-object.txt" not in rendered


def test_pre_push_rejects_malformed_input_without_echo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repository = _repository(tmp_path)
    private_ref = "refs/heads/Project" + "Zephyr"
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(f"{private_ref} invalid\n".encode())),
    )

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--pre-push",
                "--public-gate",
            ]
        )
        == 6
    )
    rendered = capsys.readouterr().out
    assert rendered.strip() == "release-history audit could not complete safely"
    assert private_ref not in rendered


def test_history_cli_redacts_external_private_boundary_configuration(
    tmp_path: Path,
    capsys,
) -> None:
    repository = _repository(tmp_path)
    private_literal = "Project" + "Zephyr"
    private_path = tmp_path / "private-boundary-patterns"
    private_path.write_text(private_literal + "\n", encoding="utf-8")
    private_path.chmod(0o600)
    _write(repository / "private.txt", private_literal)
    _git(repository, "add", "private.txt")
    _git(repository, "commit", "-m", "private boundary fixture")

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--revision",
                "main",
                "--private-patterns-file",
                str(private_path),
                "--fail-on-private-boundary",
            ]
        )
        == 5
    )
    rendered = capsys.readouterr().out
    assert private_literal not in rendered
    assert str(private_path) not in rendered
    assert "private_boundary_001" not in rendered
