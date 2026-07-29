"""Fail-closed public-tree audit without echoing matched values."""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

try:
    from scripts.audit_release_history import (
        MAX_BLOB_BYTES,
        PII_RULES,
        SECRET_RULES,
        Rule,
        _matches,
    )
    from scripts.private_boundary import (
        PrivateBoundaryError,
        load_private_literals,
        private_patterns_required,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from audit_release_history import (  # type: ignore[no-redef]
        MAX_BLOB_BYTES,
        PII_RULES,
        SECRET_RULES,
        Rule,
        _matches,
    )
    from private_boundary import (  # type: ignore[no-redef]
        PrivateBoundaryError,
        load_private_literals,
        private_patterns_required,
    )

ALLOWED_SENSITIVE_PATHS: Final = frozenset(
    {
        ".env.example",
        "data/README.md",
    }
)
BLOCKED_FILENAMES: Final = frozenset(
    {
        ".DS_Store",
        ".env",
        "AGENTS.local.md",
        "credentials.json",
        "secrets.json",
    }
)
BLOCKED_FILENAMES_LOWER: Final = frozenset(
    name.lower() for name in BLOCKED_FILENAMES
)
BLOCKED_SUFFIXES: Final = frozenset(
    {
        ".db",
        ".dmp",
        ".key",
        ".log",
        ".map",
        ".mobileprovision",
        ".p12",
        ".pem",
        ".pfx",
        ".sqlite",
        ".sqlite3",
    }
)
PRIVATE_NETWORK_RULES: Final = (
    Rule(
        "private_ipv4",
        re.compile(
            rb"(?<![0-9])(?:"
            rb"10(?:\.[0-9]{1,3}){3}|"
            rb"192\.168(?:\.[0-9]{1,3}){2}|"
            rb"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2}"
            rb")(?![0-9])"
        ),
    ),
    Rule(
        "windows_home_path",
        re.compile(
            rb"(?i)\b[A-Z]:\\Users\\(?!example\\|runner\\|test\\|user\\)[^\\\s\"']+\\"
        ),
    ),
)
GENERIC_RULES: Final = (*SECRET_RULES, *PII_RULES, *PRIVATE_NETWORK_RULES)


class BoundaryAuditError(RuntimeError):
    """The public-boundary audit could not complete safely."""


@dataclass(frozen=True, slots=True)
class Candidate:
    path: str
    body: bytes


def _run_git(repository: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise BoundaryAuditError("a required Git operation could not run") from exc
    if result.returncode != 0:
        raise BoundaryAuditError("a required Git operation failed")
    return result.stdout


def _safe_repository(path: Path) -> Path:
    try:
        repository = path.resolve(strict=True)
    except OSError as exc:
        raise BoundaryAuditError("repository is unavailable") from exc
    if not repository.is_dir():
        raise BoundaryAuditError("repository must be a directory")
    raw_root = _run_git(repository, "rev-parse", "--show-toplevel").strip()
    try:
        root = Path(raw_root.decode("utf-8", errors="strict")).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise BoundaryAuditError("Git returned an invalid repository root") from exc
    if not root.is_dir():
        raise BoundaryAuditError("Git repository root must be a directory")
    return root


def _load_private_rules(repository: Path, requested: Path | None) -> tuple[Rule, ...]:
    try:
        values = load_private_literals(repository, requested)
    except PrivateBoundaryError as exc:
        raise BoundaryAuditError(str(exc)) from exc

    return tuple(
        Rule(
            f"private_boundary_{index:03d}",
            re.compile(re.escape(value), re.IGNORECASE),
        )
        for index, value in enumerate(values, start=1)
    )


def _staged_candidates(repository: Path) -> tuple[Candidate, ...]:
    changed_paths = tuple(
        item
        for item in _run_git(
            repository,
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMRT",
            "-z",
            "--",
        ).split(b"\0")
        if item
    )
    index_entries: dict[bytes, list[tuple[bytes, bytes, bytes]]] = {}
    for entry in _run_git(
        repository,
        "ls-files",
        "--stage",
        "-z",
        "--",
    ).split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_path = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or not raw_path:
            raise BoundaryAuditError("Git returned a malformed index entry")
        if fields[2] != b"0":
            raise BoundaryAuditError("the staged index contains unresolved entries")
        index_entries.setdefault(raw_path, []).append((fields[0], fields[1], fields[2]))

    candidates: list[Candidate] = []
    for raw_path in changed_paths:
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BoundaryAuditError("staged path is not valid UTF-8") from exc
        matching = index_entries.get(raw_path, [])
        if len(matching) != 1 or matching[0][2] != b"0":
            raise BoundaryAuditError("the staged index contains unresolved entries")
        mode, object_id, _stage = matching[0]
        if mode not in {b"100644", b"100755"}:
            raise BoundaryAuditError("the staged index contains a non-regular file")
        try:
            object_name = object_id.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise BoundaryAuditError("Git returned a malformed staged object ID") from exc
        try:
            size = int(_run_git(repository, "cat-file", "-s", object_name))
        except ValueError as exc:
            raise BoundaryAuditError("Git returned an invalid staged object size") from exc
        body = (
            b"\0"
            if size > MAX_BLOB_BYTES
            else _run_git(repository, "cat-file", "blob", object_name)
        )
        candidates.append(Candidate(path=path, body=body))
    return tuple(candidates)


def _tracked_candidates(repository: Path) -> tuple[Candidate, ...]:
    entries = _run_git(repository, "ls-files", "-z", "--").split(b"\0")
    candidates: list[Candidate] = []
    for raw_path in entries:
        if not raw_path:
            continue
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BoundaryAuditError("tracked path is not valid UTF-8") from exc
        source = repository / path
        try:
            metadata = source.lstat()
        except OSError as exc:
            raise BoundaryAuditError("cannot read a tracked public-tree file") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise BoundaryAuditError(
                "tracked public-tree entries must be regular files"
            )
        try:
            body = (
                b"\0"
                if metadata.st_size > MAX_BLOB_BYTES
                else source.read_bytes()
            )
        except OSError as exc:
            raise BoundaryAuditError("cannot read a tracked public-tree file") from exc
        candidates.append(Candidate(path=path, body=body))
    return tuple(candidates)


def _filesystem_candidates(paths: Iterable[Path]) -> tuple[Candidate, ...]:
    candidates: list[Candidate] = []
    for requested in paths:
        if requested.is_symlink():
            raise BoundaryAuditError("audit targets must not be symlinks")
        try:
            root = requested.resolve(strict=True)
        except OSError as exc:
            raise BoundaryAuditError("audit target is unavailable") from exc
        if not root.is_file() and not root.is_dir():
            raise BoundaryAuditError("audit target must be a regular file or directory")
        entries = (root,) if root.is_file() else tuple(sorted(root.rglob("*")))
        for entry in entries:
            if entry.is_symlink():
                raise BoundaryAuditError("audit targets must not contain symlinks")
            if not entry.is_file():
                continue
            relative = entry.name if root.is_file() else entry.relative_to(root).as_posix()
            try:
                size = entry.stat().st_size
                body = b"\0" if size > MAX_BLOB_BYTES else entry.read_bytes()
            except OSError as exc:
                raise BoundaryAuditError("cannot read an audit target") from exc
            candidates.append(Candidate(path=relative, body=body))
    return tuple(candidates)


def _blocked_path(path: str) -> bool:
    normalized = PurePosixPath(path)
    rendered = normalized.as_posix()
    if rendered in ALLOWED_SENSITIVE_PATHS:
        return False
    name = normalized.name
    lowered_name = name.lower()
    if lowered_name in BLOCKED_FILENAMES_LOWER or (
        lowered_name.startswith(".env.") and lowered_name != ".env.example"
    ):
        return True
    if normalized.suffix.lower() in BLOCKED_SUFFIXES:
        return True
    parts = normalized.parts
    lowered_parts = tuple(part.lower() for part in parts)
    structural_paths = [lowered_parts]
    if lowered_parts and "!" in lowered_parts[0]:
        _artifact, member_root = lowered_parts[0].split("!", 1)
        member_parts = (
            (member_root, *lowered_parts[1:])
            if member_root
            else lowered_parts[1:]
        )
        structural_paths.extend(
            (member_parts, member_parts[1:])
            if len(member_parts) > 1
            else (member_parts,)
        )
    if any(
        candidate[:2] == ("docs", "private")
        for candidate in structural_paths
    ):
        return True
    return any(
        candidate[:1] == ("data",)
        and candidate not in {("data",), ("data", "readme.md")}
        for candidate in structural_paths
    )


def _location(path: str, rules: Sequence[Rule], *, private: bool = False) -> str:
    if private:
        return "<redacted-private-location>"
    raw = path.encode("utf-8", errors="surrogateescape")
    if any(_matches(rule, raw) for rule in rules):
        digest = hashlib.sha256(raw).hexdigest()[:16]
        return f"<redacted:{digest}>"
    return path


def audit_candidates(
    candidates: Sequence[Candidate],
    *,
    private_rules: Sequence[Rule],
) -> tuple[Counter[str], tuple[str, ...], int]:
    counts: Counter[str] = Counter()
    locations: set[str] = set()
    skipped = 0
    rules = (*GENERIC_RULES, *private_rules)
    for candidate in candidates:
        path_body = candidate.path.encode("utf-8", errors="surrogateescape")
        private_match = any(
            _matches(rule, path_body) or _matches(rule, candidate.body)
            for rule in private_rules
        )
        if _blocked_path(candidate.path):
            counts["blocked_path"] += 1
            locations.add(_location(candidate.path, rules, private=private_match))
        if len(candidate.body) > MAX_BLOB_BYTES or b"\0" in candidate.body:
            skipped += 1
            locations.add(_location(candidate.path, rules, private=private_match))
            continue
        for rule in rules:
            if _matches(rule, path_body) or _matches(rule, candidate.body):
                counts[rule.name] += 1
                locations.add(_location(candidate.path, rules, private=private_match))
    return counts, tuple(sorted(locations)), skipped


def aggregate_private_counts(counts: Counter[str]) -> Counter[str]:
    private = sum(
        count
        for name, count in counts.items()
        if name.startswith("private_boundary_")
    )
    public = Counter(
        {
            name: count
            for name, count in counts.items()
            if not name.startswith("private_boundary_")
        }
    )
    if private:
        public["private_boundary"] = private
    return public


def _tracked_ignored(repository: Path) -> bool:
    output = _run_git(
        repository,
        "ls-files",
        "-ci",
        "--exclude-standard",
        "-z",
        "--",
    )
    return bool(output.rstrip(b"\0"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan staged, tracked, or filesystem content for public-boundary "
            "violations without printing matched values."
        )
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--path", action="append", type=Path, default=[])
    parser.add_argument("--private-patterns-file", type=Path, default=None)
    parser.add_argument("--require-private-patterns", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        repository = _safe_repository(arguments.repository)
        private_rules = _load_private_rules(
            repository,
            arguments.private_patterns_file,
        )
        if (
            arguments.require_private_patterns
            or private_patterns_required(repository)
        ) and not private_rules:
            raise BoundaryAuditError(
                "private-boundary patterns are required but not configured"
            )
        if arguments.path:
            candidates = _filesystem_candidates(arguments.path)
        elif arguments.staged:
            candidates = _staged_candidates(repository)
        else:
            candidates = _tracked_candidates(repository)
        counts, locations, skipped = audit_candidates(
            candidates,
            private_rules=private_rules,
        )
        counts = aggregate_private_counts(counts)
        if not arguments.path and _tracked_ignored(repository):
            counts["tracked_ignored_file"] += 1
        if skipped:
            counts["skipped_file"] += skipped
    except (
        BoundaryAuditError,
        OSError,
        PrivateBoundaryError,
        UnicodeError,
        ValueError,
    ):
        print("public-boundary audit could not complete safely")
        return 3

    if counts:
        rendered = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        print(f"public-boundary audit failed: {rendered}")
        for location in locations:
            print(f"- {location}")
        return 2
    print(
        "public-boundary audit passed: "
        f"{len(candidates)} files, "
        f"private boundary {'enabled' if private_rules else 'not configured'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
