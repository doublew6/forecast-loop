"""Scan release archives and metadata before they are uploaded."""

from __future__ import annotations

import argparse
import os
import stat
import tarfile
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

try:
    from scripts.audit_public_boundary import (
        BoundaryAuditError,
        Candidate,
        _load_private_rules,
        aggregate_private_counts,
        audit_candidates,
    )
    from scripts.audit_release_history import Rule
    from scripts.private_boundary import PrivateBoundaryError, private_patterns_required
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from audit_public_boundary import (  # type: ignore[no-redef]
        BoundaryAuditError,
        Candidate,
        _load_private_rules,
        aggregate_private_counts,
        audit_candidates,
    )
    from audit_release_history import Rule  # type: ignore[no-redef]
    from private_boundary import (  # type: ignore[no-redef]
        PrivateBoundaryError,
        private_patterns_required,
    )

MAX_ARCHIVE_MEMBERS: Final = 20_000
MAX_MEMBER_BYTES: Final = 16 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 512 * 1024 * 1024
MAX_PATH_DEPTH: Final = 32
MAX_MEMBER_PATH_BYTES: Final = 4096
MAX_METADATA_BYTES: Final = 64 * 1024
ALLOWED_ARCHIVE_OWNERS: Final = frozenset({"", "root", "wheel"})


class ArtifactAuditError(RuntimeError):
    """A release artifact cannot be inspected safely."""


def _metadata_body(values: Iterable[str | bytes]) -> bytes:
    encoded = tuple(
        value
        if isinstance(value, bytes)
        else value.encode("utf-8", errors="surrogateescape")
        for value in values
        if value
    )
    body = b"\n".join(encoded)
    if len(body) > MAX_METADATA_BYTES:
        raise ArtifactAuditError("archive metadata is too large to inspect safely")
    return body


def _with_metadata(metadata: bytes, body: bytes) -> bytes:
    return metadata + b"\n" + body if metadata else body


def _gzip_metadata(stream: BinaryIO) -> bytes:
    def read_exact(length: int) -> bytes:
        body = stream.read(length)
        if len(body) != length:
            raise ArtifactAuditError("gzip metadata is truncated")
        return body

    def read_terminated() -> bytes:
        value = bytearray()
        while len(value) <= MAX_METADATA_BYTES:
            byte = read_exact(1)
            if byte == b"\0":
                return bytes(value)
            value.extend(byte)
        raise ArtifactAuditError("gzip metadata is too large to inspect safely")

    try:
        header = read_exact(10)
        if header[:3] != b"\x1f\x8b\x08" or header[3] & 0xE0:
            raise ArtifactAuditError("gzip header is invalid")
        flags = header[3]
        values: list[bytes] = []
        if flags & 0x04:
            extra_length = int.from_bytes(read_exact(2), "little")
            values.append(read_exact(extra_length))
        if flags & 0x08:
            values.append(read_terminated())
        if flags & 0x10:
            values.append(read_terminated())
        if flags & 0x02:
            read_exact(2)
        return _metadata_body(values)
    finally:
        stream.seek(0)


def _safe_member_path(name: str, *, directory: bool) -> str:
    if (
        "\0" in name
        or "\\" in name
        or len(name.encode("utf-8", errors="surrogateescape"))
        > MAX_MEMBER_PATH_BYTES
    ):
        raise ArtifactAuditError("archive member paths must use POSIX separators")
    if not directory and name.endswith("/"):
        raise ArtifactAuditError("archive contains a non-canonical path")
    canonical_source = name[:-1] if directory and name.endswith("/") else name
    raw_parts = canonical_source.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ArtifactAuditError("archive contains a non-canonical path")
    if (
        len(raw_parts[0]) == 2
        and raw_parts[0][0].isalpha()
        and raw_parts[0][1] == ":"
    ):
        raise ArtifactAuditError("archive contains an absolute path")
    path = PurePosixPath(canonical_source)
    if path.is_absolute() or not path.parts:
        raise ArtifactAuditError("archive contains an absolute or empty path")
    if path.as_posix() != canonical_source:
        raise ArtifactAuditError("archive contains a non-canonical path")
    if len(path.parts) > MAX_PATH_DEPTH:
        raise ArtifactAuditError("archive member path is too deep")
    return path.as_posix()


def _tar_candidates(path: Path, stream: BinaryIO) -> tuple[Candidate, ...]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    total = 0
    with tarfile.open(fileobj=stream, mode="r:*") as archive:
        archive_metadata = _metadata_body(
            value
            for item in archive.pax_headers.items()
            for value in item
        )
        if archive_metadata:
            total += len(archive_metadata)
            candidates.append(
                Candidate(
                    path=f"{path.name}!<archive-metadata>",
                    body=archive_metadata,
                )
            )
        for member_count, member in enumerate(archive, start=1):
            if member_count > MAX_ARCHIVE_MEMBERS:
                raise ArtifactAuditError("archive contains too many members")
            name = _safe_member_path(member.name, directory=member.isdir())
            if name in seen:
                raise ArtifactAuditError("archive contains duplicate member paths")
            seen.add(name)
            if (
                member.uid != 0
                or member.gid != 0
                or member.uname not in ALLOWED_ARCHIVE_OWNERS
                or member.gname not in ALLOWED_ARCHIVE_OWNERS
            ):
                raise ArtifactAuditError(
                    "archive ownership metadata is not release-safe"
                )
            member_metadata = _metadata_body(
                (
                    member.uname,
                    member.gname,
                    *(
                        value
                        for item in member.pax_headers.items()
                        for value in item
                    ),
                )
            )
            total += len(member_metadata)
            if total > MAX_TOTAL_BYTES:
                raise ArtifactAuditError("archive expands beyond the audit limit")
            if member.isdir():
                candidates.append(
                    Candidate(path=f"{path.name}!{name}", body=member_metadata)
                )
                continue
            if (
                member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or not member.isfile()
            ):
                raise ArtifactAuditError("archive contains a non-regular member")
            if member.size > MAX_MEMBER_BYTES:
                raise ArtifactAuditError("archive contains an oversized member")
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise ArtifactAuditError("archive expands beyond the audit limit")
            stream = archive.extractfile(member)
            if stream is None:
                raise ArtifactAuditError("archive member cannot be read")
            body = stream.read(MAX_MEMBER_BYTES + 1)
            if len(body) != member.size:
                raise ArtifactAuditError("archive member size is inconsistent")
            candidates.append(
                Candidate(
                    path=f"{path.name}!{name}",
                    body=_with_metadata(member_metadata, body),
                )
            )
    if not seen:
        raise ArtifactAuditError("archive contains no inspectable members")
    return tuple(candidates)


def _zip_candidates(path: Path, stream: BinaryIO) -> tuple[Candidate, ...]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(stream) as archive:
        if archive.comment:
            archive_metadata = _metadata_body((archive.comment,))
            total += len(archive_metadata)
            candidates.append(
                Candidate(
                    path=f"{path.name}!<archive-metadata>",
                    body=archive_metadata,
                )
            )
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ArtifactAuditError("archive contains too many members")
        for member in members:
            name = _safe_member_path(member.filename, directory=member.is_dir())
            if name in seen:
                raise ArtifactAuditError("archive contains duplicate member paths")
            seen.add(name)
            member_metadata = _metadata_body((member.comment, member.extra))
            total += len(member_metadata)
            if total > MAX_TOTAL_BYTES:
                raise ArtifactAuditError("archive expands beyond the audit limit")
            if member.is_dir():
                candidates.append(
                    Candidate(path=f"{path.name}!{name}", body=member_metadata)
                )
                continue
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG}:
                raise ArtifactAuditError("archive contains a non-regular member")
            if member.file_size > MAX_MEMBER_BYTES:
                raise ArtifactAuditError("archive contains an oversized member")
            total += member.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArtifactAuditError("archive expands beyond the audit limit")
            with archive.open(member) as stream:
                body = stream.read(MAX_MEMBER_BYTES + 1)
            if len(body) != member.file_size:
                raise ArtifactAuditError("archive member size is inconsistent")
            candidates.append(
                Candidate(
                    path=f"{path.name}!{name}",
                    body=_with_metadata(member_metadata, body),
                )
            )
    if not seen:
        raise ArtifactAuditError("archive contains no inspectable members")
    return tuple(candidates)


def _artifact_candidates(paths: Iterable[Path]) -> tuple[Candidate, ...]:
    candidates: list[Candidate] = []
    for path in paths:
        lowered = path.name.lower()
        archive_kind = (
            "tar"
            if lowered.endswith((".tar.gz", ".tgz", ".tar"))
            else "zip"
            if lowered.endswith((".whl", ".zip"))
            else ""
        )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ArtifactAuditError(
                    "release artifacts must be regular files"
                )
            size_limit = MAX_TOTAL_BYTES if archive_kind else MAX_MEMBER_BYTES
            if metadata.st_size > size_limit:
                raise ArtifactAuditError(
                    "release artifact is too large to inspect safely"
                )
            if archive_kind == "tar":
                if lowered.endswith((".tar.gz", ".tgz")):
                    gzip_metadata = _gzip_metadata(stream)
                    if gzip_metadata:
                        candidates.append(
                            Candidate(
                                path=f"{path.name}!<compression-metadata>",
                                body=gzip_metadata,
                            )
                        )
                candidates.extend(_tar_candidates(path, stream))
            elif archive_kind == "zip":
                candidates.extend(_zip_candidates(path, stream))
            else:
                body = stream.read(MAX_MEMBER_BYTES + 1)
                if len(body) > MAX_MEMBER_BYTES:
                    raise ArtifactAuditError("release metadata file is too large")
                candidates.append(Candidate(path=path.name, body=body))
    return tuple(candidates)


def audit_artifacts(
    artifact_dir: Path,
    *,
    private_rules: Sequence[Rule],
) -> tuple[int, str]:
    if artifact_dir.is_symlink():
        raise ArtifactAuditError("artifact directory must not be a symlink")
    root = artifact_dir.resolve(strict=True)
    if not root.is_dir():
        raise ArtifactAuditError("artifact directory must be a directory")
    paths = tuple(sorted(root.iterdir()))
    if not paths:
        raise ArtifactAuditError("artifact directory is empty")
    candidates = _artifact_candidates(paths)
    counts, _locations, skipped = audit_candidates(
        candidates,
        private_rules=private_rules,
    )
    counts = aggregate_private_counts(counts)
    if skipped:
        counts["skipped_file"] += skipped
    if counts:
        summary = ", ".join(
            f"{name}={count}" for name, count in sorted(counts.items())
        )
        return 2, f"release-artifact audit failed: {summary}"
    return 0, (
        "release-artifact audit passed: "
        f"{len(paths)} artifacts, {len(candidates)} inspected entries"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect release archives without extracting them to disk."
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--private-patterns-file", type=Path, default=None)
    parser.add_argument("--require-private-patterns", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        repository = arguments.repository.resolve(strict=True)
        private_rules = _load_private_rules(
            repository,
            arguments.private_patterns_file,
        )
        if (
            arguments.require_private_patterns
            or private_patterns_required(repository)
        ) and not private_rules:
            raise ArtifactAuditError(
                "private-boundary patterns are required but not configured"
            )
        status, message = audit_artifacts(
            arguments.artifact_dir,
            private_rules=private_rules,
        )
    except (
        ArtifactAuditError,
        BoundaryAuditError,
        EOFError,
        NotImplementedError,
        OSError,
        PrivateBoundaryError,
        RuntimeError,
        ValueError,
        tarfile.TarError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        print("release-artifact audit could not complete safely")
        return 3
    print(message)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
