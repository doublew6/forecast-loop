"""Redacted secret and PII audit for a release's complete reachable history."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

try:
    from scripts.private_boundary import (
        PrivateBoundaryError,
        load_private_literals,
        private_patterns_required,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from private_boundary import (  # type: ignore[no-redef]
        PrivateBoundaryError,
        load_private_literals,
        private_patterns_required,
    )

SCHEMA_VERSION: Final = "forecast-loop.release-history-audit/v3"
MAX_BLOB_BYTES: Final = 16 * 1024 * 1024
MAX_PRE_PUSH_INPUT_BYTES: Final = 1024 * 1024
_FINDING_SOURCES: Final = (
    "blob",
    "commit_metadata",
    "ref_name",
    "tag_metadata",
    "tree_path",
)


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: re.Pattern[bytes]


SECRET_RULES: Final = (
    Rule("private_key", re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    Rule("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule(
        "github_token",
        re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ),
    Rule("openai_key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    Rule("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    Rule("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b")),
    Rule(
        "credential_url",
        re.compile(
            rb"\b[a-z][a-z0-9+.-]{1,20}://"
            rb"[^/\s:@]+:[^/\s:@]{4,}@",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "sensitive_assignment",
        re.compile(
            rb"(?im)^\s*(?:"
            rb"api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
            rb"password|private[_-]?key|secret[_-]?key"
            rb")\s*[:=]\s*[\"']?"
            rb"(?!\s*(?:$|#|<|change[-_ ]?me|example|none|null|test|your[-_ ]))"
            rb"(?![A-Za-z_][A-Za-z0-9_]*\.)"
            rb"[^\s#\"']{8,}",
        ),
    ),
)

PII_RULES: Final = (
    Rule("macos_home_path", re.compile(rb"/" rb"Users/[^/\s\"']+/")),
    Rule("linux_home_path", re.compile(rb"/" rb"home/[^/\s\"']+/")),
    Rule(
        "email_address",
        re.compile(rb"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
)

ALLOWED_EMAIL_SUFFIXES: Final = (
    b"@users.noreply.github.com",
    b"@example.com",
    b"@example.org",
    b"@localhost",
)
ALLOWED_HOME_COMPONENTS: Final = frozenset(
    {
        b"<operator>",
        b"example",
        b"private",
        b"runner",
        b"test",
        b"user",
    }
)


class AuditError(RuntimeError):
    """The repository could not be audited deterministically."""


def _git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AuditError("a required Git operation failed")
    return result.stdout


_OBJECT_ID = re.compile(rb"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


def _pre_push_updates(payload: bytes) -> tuple[tuple[str, ...], tuple[bytes, ...]]:
    """Return pushed object IDs and ref names without decoding untrusted refs."""

    if not payload or b"\0" in payload:
        raise AuditError("pre-push input is missing or malformed")
    revisions: list[str] = []
    ref_names: list[bytes] = []
    update_count = 0
    for line in payload.splitlines():
        if not line:
            continue
        fields = line.split()
        if len(fields) != 4:
            raise AuditError("pre-push input is malformed")
        local_ref, local_object_id, remote_ref, remote_object_id = fields
        if (
            _OBJECT_ID.fullmatch(local_object_id) is None
            or _OBJECT_ID.fullmatch(remote_object_id) is None
        ):
            raise AuditError("pre-push input contains an invalid object ID")
        if any(
            not value
            or len(value) > 1024
            or any(byte < 0x20 or byte == 0x7F for byte in value)
            for value in (local_ref, remote_ref)
        ):
            raise AuditError("pre-push input contains an invalid ref name")
        update_count += 1
        deletion = not local_object_id.strip(b"0")
        if deletion != (local_ref == b"(delete)") or remote_ref == b"(delete)":
            raise AuditError("pre-push deletion metadata is inconsistent")
        if not deletion:
            ref_names.extend((local_ref, remote_ref))
            try:
                revisions.append(local_object_id.decode("ascii").lower())
            except UnicodeDecodeError as exc:  # pragma: no cover - regex is ASCII-only
                raise AuditError("pre-push object ID is malformed") from exc
    if update_count == 0:
        raise AuditError("pre-push input contains no updates")
    return tuple(dict.fromkeys(revisions)), tuple(dict.fromkeys(ref_names))


def _revisions(repository: Path, requested: Sequence[str]) -> tuple[str, ...]:
    if requested:
        for revision in requested:
            if revision.startswith("-"):
                raise AuditError("revision names must not start with an option prefix")
            _git(
                repository,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            )
        return tuple(requested)
    refs = (
        _git(
            repository,
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
            "refs/remotes",
            "refs/tags",
            "refs/pull",
        )
        .decode()
        .splitlines()
    )
    filtered = tuple(
        ref
        for ref in refs
        if ref and not ref.endswith("/HEAD")
    )
    head = subprocess.run(
        ("git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
        cwd=repository,
        check=False,
        capture_output=True,
    )
    selected = tuple(dict.fromkeys((*filtered, "HEAD" if head.returncode == 0 else "")))
    selected = tuple(item for item in selected if item)
    if not selected:
        raise AuditError("repository has no branch, remote or tag refs to audit")
    return selected


def _ref_names(repository: Path) -> tuple[bytes, ...]:
    return tuple(
        item
        for item in _git(
            repository,
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
            "refs/remotes",
            "refs/tags",
            "refs/pull",
        ).splitlines()
        if item
    )


def _reachable_commits(repository: Path, revisions: Sequence[str]) -> tuple[str, ...]:
    output = _git(repository, "rev-list", "--reverse", *revisions).decode().splitlines()
    commits = tuple(dict.fromkeys(item for item in output if item))
    if not commits:
        raise AuditError("selected revisions contain no commits")
    return commits


def _revision_object_ids(
    repository: Path,
    revisions: Sequence[str],
) -> tuple[str, ...]:
    object_ids = (
        _git(
            repository,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{object}}",
        )
        .decode("ascii")
        .strip()
        for revision in revisions
    )
    return tuple(dict.fromkeys(object_id for object_id in object_ids if object_id))


def _reachable_blobs(
    repository: Path,
    revisions: Sequence[str],
) -> tuple[str, ...]:
    lines = (
        _git(
            repository,
            "rev-list",
            "--objects",
            "--no-object-names",
            *revisions,
        )
        .decode("ascii")
        .splitlines()
    )
    blobs: list[str] = []
    for object_id in dict.fromkeys(line for line in lines if line):
        object_type = _git(repository, "cat-file", "-t", object_id).strip()
        if object_type == b"blob":
            blobs.append(object_id)
    return tuple(blobs)


def _historical_tree_paths(
    repository: Path,
    commits: Sequence[str],
) -> dict[str, set[bytes]]:
    paths_by_object: dict[str, set[bytes]] = defaultdict(set)
    for commit in commits:
        entries = _git(
            repository,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            commit,
        )
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            header, separator, raw_path = entry.partition(b"\t")
            fields = header.split(b" ")
            if not separator or len(fields) != 3 or not raw_path:
                raise AuditError(f"malformed tree entry in commit {commit}")
            try:
                object_id = fields[2].decode("ascii")
            except UnicodeDecodeError as exc:
                raise AuditError(f"non-ASCII object ID in commit {commit}") from exc
            paths_by_object[object_id].add(raw_path)
    return paths_by_object


def _object_type(repository: Path, object_id: str) -> bytes:
    return _git(repository, "cat-file", "-t", object_id).strip()


def _tag_target(tag_body: bytes, object_id: str) -> str:
    headers, separator, _message = tag_body.partition(b"\n\n")
    if not separator:
        raise AuditError(f"annotated tag object has no message boundary: {object_id}")
    for line in headers.splitlines():
        if line.startswith(b"object "):
            try:
                target = line.removeprefix(b"object ").decode("ascii")
            except UnicodeDecodeError as exc:
                raise AuditError(f"annotated tag has a non-ASCII target: {object_id}") from exc
            if re.fullmatch(r"[0-9a-f]+", target):
                return target
    raise AuditError(f"annotated tag has no valid target: {object_id}")


def _reachable_tag_objects(
    repository: Path,
    revisions: Sequence[str],
    commits: Sequence[str],
) -> tuple[str, ...]:
    tag_refs = (
        _git(repository, "for-each-ref", "--format=%(refname)", "refs/tags")
        .decode("utf-8", errors="surrogateescape")
        .splitlines()
    )
    candidates = tuple(dict.fromkeys((*revisions, *tag_refs)))
    reachable_commits = set(commits)
    tag_objects: set[str] = set()

    for candidate in candidates:
        object_id = (
            _git(repository, "rev-parse", "--verify", f"{candidate}^{{object}}")
            .decode("ascii")
            .strip()
        )
        chain: list[str] = []
        seen: set[str] = set()
        while _object_type(repository, object_id) == b"tag":
            if object_id in seen:
                raise AuditError(f"annotated tag cycle detected at {object_id}")
            seen.add(object_id)
            chain.append(object_id)
            body = _git(repository, "cat-file", "tag", object_id)
            object_id = _tag_target(body, object_id)
        if _object_type(repository, object_id) == b"commit" and object_id in reachable_commits:
            tag_objects.update(chain)
    return tuple(sorted(tag_objects))


def _path_fingerprint(paths: Iterable[bytes]) -> str:
    canonical = b"\0".join(sorted(paths))
    return hashlib.sha256(canonical).hexdigest()[:16]


def _matches(rule: Rule, body: bytes) -> bool:
    if rule.name in {"macos_home_path", "linux_home_path"}:
        return any(
            match.group(0).rstrip(b"/").rsplit(b"/", 1)[-1].lower() not in ALLOWED_HOME_COMPONENTS
            for match in rule.pattern.finditer(body)
        )
    if rule.name != "email_address":
        return bool(rule.pattern.search(body))
    return any(
        not match.group(0).lower().endswith(ALLOWED_EMAIL_SUFFIXES)
        for match in rule.pattern.finditer(body)
    )


def _commit_metadata(commit_body: bytes, object_id: str) -> bytes:
    headers, separator, message = commit_body.partition(b"\n\n")
    if not separator:
        raise AuditError(f"commit object has no message boundary: {object_id}")
    identities = [
        line for line in headers.splitlines() if line.startswith((b"author ", b"committer "))
    ]
    if not any(line.startswith(b"author ") for line in identities) or not any(
        line.startswith(b"committer ") for line in identities
    ):
        raise AuditError(f"commit identity metadata is incomplete: {object_id}")
    return b"\n".join((*identities, message))


def _tag_metadata(tag_body: bytes, object_id: str) -> bytes:
    headers, separator, message = tag_body.partition(b"\n\n")
    if not separator:
        raise AuditError(f"annotated tag object has no message boundary: {object_id}")
    public_headers = [
        line for line in headers.splitlines() if line.startswith((b"tag ", b"tagger "))
    ]
    return b"\n".join((*public_headers, message))


def _record_matches(
    rule_hits: dict[str, set[tuple[str, str, str]]],
    *,
    body: bytes,
    rules: Sequence[Rule],
    source: str,
    object_id: str = "",
    path_fingerprint: str = "",
) -> None:
    if source not in _FINDING_SOURCES:
        raise AuditError(f"unsupported finding source: {source}")
    for rule in rules:
        if _matches(rule, body):
            rule_hits[rule.name].add((source, object_id, path_fingerprint))


def _render_findings(
    rule_hits: dict[str, set[tuple[str, str, str]]],
    rule_names: set[str],
) -> dict[str, object]:
    rendered: dict[str, object] = {}
    for name in sorted(rule_names):
        matches = sorted(rule_hits[name])
        source_counts = {
            source: sum(match[0] == source for match in matches) for source in _FINDING_SOURCES
        }
        locations: list[dict[str, str]] = []
        for source, object_id, path_fingerprint in matches:
            location = {"source": source}
            if object_id:
                location["object"] = object_id
            if path_fingerprint:
                location["path_fingerprint"] = path_fingerprint
            locations.append(location)
        rendered[name] = {
            "hit_count": len(matches),
            "hit_blob_count": source_counts["blob"],
            "hit_commit_metadata_count": source_counts["commit_metadata"],
            "hit_ref_name_count": source_counts["ref_name"],
            "hit_tag_metadata_count": source_counts["tag_metadata"],
            "hit_tree_path_count": source_counts["tree_path"],
            "locations": locations,
        }
    return rendered


def _render_private_summary(
    rule_hits: dict[str, set[tuple[str, str, str]]],
    rule_names: set[str],
) -> dict[str, object]:
    matches = {
        match
        for name in rule_names
        for match in rule_hits[name]
    }
    return {
        "hit_count": len(matches),
        **{
            f"hit_{source}_count": sum(match[0] == source for match in matches)
            for source in _FINDING_SOURCES
        },
    }


def audit_repository(
    repository: Path,
    *,
    revisions: Sequence[str] = (),
    private_literals: Sequence[bytes] = (),
    additional_ref_names: Sequence[bytes] = (),
) -> dict[str, object]:
    """Return a redacted audit summary without returning matched values or paths."""

    root = repository.resolve(strict=True)
    if not (root / ".git").exists() and not (root / "HEAD").is_file():
        raise AuditError("the audit target is not a Git repository")
    selected = _revisions(root, revisions)
    commits = _reachable_commits(root, selected)
    blobs = _reachable_blobs(root, selected)
    paths_by_object = _historical_tree_paths(root, commits)
    tag_objects = _reachable_tag_objects(root, selected, commits)
    ref_names = tuple(dict.fromkeys((*_ref_names(root), *additional_ref_names)))
    private_rules = tuple(
        Rule(
            f"private_boundary_{index:03d}",
            re.compile(re.escape(value), re.IGNORECASE),
        )
        for index, value in enumerate(dict.fromkeys(private_literals), start=1)
    )
    rules = (*SECRET_RULES, *PII_RULES, *private_rules)
    rule_hits: dict[str, set[tuple[str, str, str]]] = {
        rule.name: set() for rule in rules
    }
    skipped_large_blobs = 0
    skipped_binary_blobs = 0

    for object_id in sorted(blobs):
        size = int(_git(root, "cat-file", "-s", object_id))
        if size > MAX_BLOB_BYTES:
            skipped_large_blobs += 1
            continue
        body = _git(root, "cat-file", "blob", object_id)
        if b"\0" in body[:8192]:
            skipped_binary_blobs += 1
            continue
        _record_matches(
            rule_hits,
            body=body,
            rules=rules,
            source="blob",
            object_id=object_id,
            path_fingerprint=_path_fingerprint(paths_by_object.get(object_id, set())),
        )

    for object_id in commits:
        body = _git(root, "cat-file", "commit", object_id)
        _record_matches(
            rule_hits,
            body=_commit_metadata(body, object_id),
            rules=rules,
            source="commit_metadata",
            object_id=object_id,
        )

    for object_id in tag_objects:
        body = _git(root, "cat-file", "tag", object_id)
        _record_matches(
            rule_hits,
            body=_tag_metadata(body, object_id),
            rules=rules,
            source="tag_metadata",
            object_id=object_id,
        )

    for ref_name in ref_names:
        _record_matches(
            rule_hits,
            body=ref_name,
            rules=rules,
            source="ref_name",
            path_fingerprint=_path_fingerprint((ref_name,)),
        )

    historical_paths = {path for paths in paths_by_object.values() for path in paths}
    for path in sorted(historical_paths):
        _record_matches(
            rule_hits,
            # Git tree paths are always relative. Prefix a separator while
            # scanning so a top-level `Users/name/...` or `home/name/...`
            # still matches the same local-path rules as an absolute path.
            body=b"/" + path,
            rules=rules,
            source="tree_path",
            path_fingerprint=_path_fingerprint((path,)),
        )

    secret_names = {rule.name for rule in SECRET_RULES}
    pii_names = {rule.name for rule in PII_RULES}
    private_names = {rule.name for rule in private_rules}
    return {
        "schema_version": SCHEMA_VERSION,
        "revisions": list(_revision_object_ids(root, selected)),
        "commit_count": len(commits),
        "tag_metadata_count": len(tag_objects),
        "ref_name_count": len(ref_names),
        "blob_count": len(blobs),
        "tree_path_count": len(historical_paths),
        "skipped_large_blob_count": skipped_large_blobs,
        "skipped_binary_blob_count": skipped_binary_blobs,
        "secret_findings": _render_findings(rule_hits, secret_names),
        "pii_findings": _render_findings(rule_hits, pii_names),
        "private_boundary_rule_count": len(private_rules),
        "private_boundary_findings": _render_private_summary(
            rule_hits,
            private_names,
        ),
    }


def _has_findings(report: dict[str, object], field: str) -> bool:
    findings = report[field]
    if not isinstance(findings, dict):
        raise AuditError(f"{field} report shape is invalid")
    return any(
        isinstance(item, dict) and int(item.get("hit_count", 0)) > 0 for item in findings.values()
    )


def _has_skipped_blobs(report: dict[str, object]) -> bool:
    return any(
        int(report.get(field, 0)) > 0
        for field in (
            "skipped_binary_blob_count",
            "skipped_large_blob_count",
        )
    )


def _has_private_findings(report: dict[str, object]) -> bool:
    findings = report.get("private_boundary_findings")
    return isinstance(findings, dict) and int(findings.get("hit_count", 0)) > 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan reachable blobs, commit/tag metadata, and historical tree paths "
            "for credential and PII patterns; output only redacted counts, object "
            "IDs, and fingerprints."
        )
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision", action="append", default=[])
    parser.add_argument(
        "--pre-push",
        action="store_true",
        help="read the Git pre-push update records from standard input",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--private-patterns-file", type=Path, default=None)
    parser.add_argument("--require-private-patterns", action="store_true")
    parser.add_argument("--public-gate", action="store_true")
    parser.add_argument("--fail-on-secrets", action="store_true")
    parser.add_argument("--fail-on-pii", action="store_true")
    parser.add_argument("--fail-on-private-boundary", action="store_true")
    parser.add_argument("--fail-on-skipped-blobs", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        repository = arguments.repository.resolve(strict=True)
        private_literals = load_private_literals(
            repository,
            arguments.private_patterns_file,
        )
        if (
            arguments.require_private_patterns
            or private_patterns_required(repository)
        ) and not private_literals:
            raise PrivateBoundaryError(
                "private-boundary patterns are required but not configured"
            )
        revisions = tuple(arguments.revision)
        additional_ref_names: tuple[bytes, ...] = ()
        if arguments.pre_push:
            if revisions or arguments.output is not None:
                raise AuditError(
                    "pre-push input cannot be combined with revisions or report output"
                )
            payload = sys.stdin.buffer.read(MAX_PRE_PUSH_INPUT_BYTES + 1)
            if len(payload) > MAX_PRE_PUSH_INPUT_BYTES:
                raise AuditError("pre-push input is too large")
            revisions, additional_ref_names = _pre_push_updates(payload)
            if not revisions:
                print("release-history audit passed: deletion-only push")
                return 0

        report = audit_repository(
            repository,
            revisions=revisions,
            private_literals=private_literals,
            additional_ref_names=additional_ref_names,
        )
        serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if arguments.output is None:
            print(serialized, end="")
        else:
            output = arguments.output
            if output.exists() or output.is_symlink():
                raise AuditError("refusing to overwrite an audit report")
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            output.write_text(serialized, encoding="utf-8")
            output.chmod(0o600)
    except (AuditError, PrivateBoundaryError, OSError, UnicodeError, ValueError):
        print("release-history audit could not complete safely")
        return 6

    fail_on_secrets = arguments.fail_on_secrets or arguments.public_gate
    fail_on_pii = arguments.fail_on_pii or arguments.public_gate
    fail_on_private = arguments.fail_on_private_boundary or arguments.public_gate
    fail_on_skipped = arguments.fail_on_skipped_blobs or arguments.public_gate
    if fail_on_skipped and _has_skipped_blobs(report):
        return 3
    if fail_on_secrets and _has_findings(report, "secret_findings"):
        return 2
    if fail_on_pii and _has_findings(report, "pii_findings"):
        return 4
    return 5 if fail_on_private and _has_private_findings(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
