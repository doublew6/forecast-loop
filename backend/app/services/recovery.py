"""Consistent, hash-sealed SQLite backup and isolated restore operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from .schema_readiness import CORE_TABLES, require_schema_current, upgrade_database

BACKUP_SCHEMA = "forecast-loop.recovery-backup/v1"
RESTORE_SCHEMA = "forecast-loop.restore-receipt/v1"
MANIFEST_NAME = "manifest.json"
RESTORE_RECEIPT_NAME = "restore-receipt.json"
_ROOT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class RecoveryError(RuntimeError):
    """Raised when a backup or restore fails a safety invariant."""


def create_backup(
    *,
    database_path: Path,
    checkpoint_path: Path,
    roots: Mapping[str, Path],
    output_root: Path,
) -> Path:
    """Create an individually consistent, immutable local recovery bundle."""

    database_source = _require_regular_file(database_path, "database")
    checkpoint_source = _require_regular_file(checkpoint_path, "checkpoint")
    root_sources = _validate_roots(roots)
    sources = (database_source, checkpoint_source, *root_sources.values())
    _reject_overlapping_backup_sources(sources)
    destination_root = _prepare_directory(output_root, "backup output")
    _reject_overlapping_backup_paths(destination_root, sources)

    staging = Path(
        tempfile.mkdtemp(prefix=".forecast-loop-backup-", dir=destination_root)
    )
    os.chmod(staging, 0o700)
    published = False
    try:
        artifacts: list[dict[str, Any]] = []
        files_dir = staging / "files"
        files_dir.mkdir(mode=0o700)

        database_copy = files_dir / "database.sqlite3"
        _online_sqlite_backup(database_source, database_copy)
        artifacts.append(
            _artifact_record(
                staging,
                database_copy,
                role="database",
            )
        )

        checkpoint_copy = files_dir / "checkpoint.sqlite3"
        _online_sqlite_backup(checkpoint_source, checkpoint_copy)
        artifacts.append(
            _artifact_record(
                staging,
                checkpoint_copy,
                role="checkpoint",
            )
        )

        roots_dir = staging / "roots"
        roots_dir.mkdir(mode=0o700)
        for name, source in sorted(root_sources.items()):
            destination = roots_dir / name
            destination.mkdir(mode=0o700)
            artifacts.extend(
                _copy_root_tree(
                    source=source,
                    destination=destination,
                    bundle_root=staging,
                    root_name=name,
                )
            )

        database_summary = _database_summary(database_copy)
        _verify_sqlite(checkpoint_copy, label="checkpoint snapshot")
        body: dict[str, Any] = {
            "schema_version": BACKUP_SCHEMA,
            "created_at": _utc_now(),
            "roots": sorted(root_sources),
            "database_summary": database_summary,
            "artifacts": sorted(artifacts, key=lambda item: item["path"]),
        }
        body["manifest_hash"] = _payload_hash(body)
        _write_new_private_json(staging / MANIFEST_NAME, body)

        final = destination_root / (
            f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid4().hex[:10]}"
        )
        if final.exists() or final.is_symlink():
            raise RecoveryError(f"backup destination already exists: {final}")
        os.rename(staging, final)
        published = True
        return final
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def verify_backup(bundle_path: Path) -> dict[str, Any]:
    """Verify manifest, paths, modes, hashes, SQLite integrity, and schema."""

    bundle = _require_directory(bundle_path, "backup bundle")
    _require_private_mode(bundle, "backup bundle")
    manifest_path = _require_regular_file(bundle / MANIFEST_NAME, "manifest")
    _require_private_mode(manifest_path, "manifest")
    manifest = _read_strict_json(manifest_path)
    if manifest.get("schema_version") != BACKUP_SCHEMA:
        raise RecoveryError("unsupported recovery backup schema")
    manifest_hash = manifest.get("manifest_hash")
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
        raise RecoveryError("manifest_hash must be a SHA-256 digest")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hash", None)
    if _payload_hash(unsigned) != manifest_hash:
        raise RecoveryError("backup manifest hash mismatch")

    roots = _validated_manifest_roots(manifest.get("roots"))
    artifacts = _validated_manifest_artifacts(manifest.get("artifacts"), roots)
    expected_files = {MANIFEST_NAME, *(item["path"] for item in artifacts)}
    expected_directories = {"files", "roots"}
    expected_directories.update(f"roots/{name}" for name in roots)
    for relative in expected_files:
        parent = PurePosixPath(relative).parent
        while str(parent) not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    actual_files, actual_directories = _walk_bundle(bundle)
    if actual_files != expected_files:
        raise RecoveryError(
            "backup artifact set mismatch: "
            f"expected {sorted(expected_files)}, got {sorted(actual_files)}"
        )
    if actual_directories != expected_directories:
        raise RecoveryError(
            "backup directory set mismatch: "
            f"expected {sorted(expected_directories)}, "
            f"got {sorted(actual_directories)}"
        )

    role_paths: dict[str, Path] = {}
    for artifact in artifacts:
        path = _require_regular_file(
            bundle / artifact["path"],
            f"artifact {artifact['path']}",
        )
        _require_private_mode(path, f"artifact {artifact['path']}")
        size, digest = _file_size_and_hash(path)
        if size != artifact["size"] or digest != artifact["sha256"]:
            raise RecoveryError(f"artifact hash mismatch: {artifact['path']}")
        role = artifact["role"]
        if role in {"database", "checkpoint"}:
            role_paths[role] = path

    database_summary = _database_summary(role_paths["database"])
    if database_summary != manifest.get("database_summary"):
        raise RecoveryError("database summary does not match the sealed snapshot")
    _verify_sqlite(role_paths["checkpoint"], label="checkpoint snapshot")
    return manifest


def restore_backup(
    bundle_path: Path,
    *,
    target_root: Path,
) -> Path:
    """Restore into an empty isolated target, migrate, and verify invariants."""

    manifest = verify_backup(bundle_path)
    bundle = _require_directory(bundle_path, "backup bundle")
    target = _prepare_empty_restore_target(target_root)
    if _paths_overlap(bundle, target):
        raise RecoveryError("restore target and backup bundle must be isolated")

    artifacts = _validated_manifest_artifacts(
        manifest["artifacts"],
        _validated_manifest_roots(manifest["roots"]),
    )
    for name in manifest["roots"]:
        (target / "roots" / name).mkdir(parents=True, mode=0o700)
    (target / "files").mkdir(mode=0o700, exist_ok=True)

    for artifact in artifacts:
        source = bundle / artifact["path"]
        destination = target / artifact["path"]
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _copy_verified_file(
            source,
            destination,
            expected_size=artifact["size"],
            expected_hash=artifact["sha256"],
        )

    restored_database = target / "files" / "database.sqlite3"
    database_url = f"sqlite:///{restored_database}"
    upgrade_database(database_url)
    restored_summary = _database_summary(restored_database)
    if restored_summary["core_row_counts"] != manifest["database_summary"][
        "core_row_counts"
    ]:
        raise RecoveryError("core row counts changed during isolated restore")
    _verify_sqlite(
        target / "files" / "checkpoint.sqlite3",
        label="restored checkpoint",
    )

    restored_files = []
    for relative in sorted(
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file()
    ):
        path = _require_regular_file(target / relative, f"restored {relative}")
        size, digest = _file_size_and_hash(path)
        restored_files.append(
            {"path": relative, "size": size, "sha256": digest}
        )
    receipt: dict[str, Any] = {
        "schema_version": RESTORE_SCHEMA,
        "restored_at": _utc_now(),
        "source_manifest_hash": manifest["manifest_hash"],
        "database_summary": restored_summary,
        "restored_files": restored_files,
    }
    receipt["receipt_hash"] = _payload_hash(receipt)
    receipt_path = target / RESTORE_RECEIPT_NAME
    _write_new_private_json(receipt_path, receipt)
    return receipt_path


def _database_summary(path: Path) -> dict[str, Any]:
    database_url = f"sqlite:///{path}"
    status = require_schema_current(database_url, deep=True)
    connection = _open_sqlite_read_only(path)
    try:
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[
                    0
                ]
            )
            for table in sorted(CORE_TABLES)
        }
    finally:
        connection.close()
    return {
        "migration_heads": list(status.current_heads),
        "core_row_counts": counts,
    }


def _verify_sqlite(path: Path, *, label: str) -> None:
    connection = _open_sqlite_read_only(path)
    try:
        integrity = tuple(
            str(row[0])
            for row in connection.execute("PRAGMA integrity_check").fetchall()
        )
        if integrity != ("ok",):
            raise RecoveryError(
                f"{label} integrity_check failed: {'; '.join(integrity)}"
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            rendered = "; ".join("|".join(map(str, row)) for row in violations)
            raise RecoveryError(
                f"{label} foreign_key_check failed: {rendered}"
            )
    except sqlite3.DatabaseError as exc:
        raise RecoveryError(f"{label} is not a valid SQLite database") from exc
    finally:
        connection.close()


def _online_sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_connection = _open_sqlite_read_only(source)
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA wal_checkpoint(FULL)")
            destination_connection.commit()
        finally:
            destination_connection.close()
    except sqlite3.DatabaseError as exc:
        raise RecoveryError(f"SQLite online backup failed: {source}") from exc
    finally:
        source_connection.close()
    os.chmod(destination, 0o600)


def _open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path), safe="/")
    try:
        connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except sqlite3.DatabaseError as exc:
        raise RecoveryError(f"cannot open SQLite file read-only: {path}") from exc


def _copy_root_tree(
    *,
    source: Path,
    destination: Path,
    bundle_root: Path,
    root_name: str,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        source_entry = Path(entry.path)
        destination_entry = destination / entry.name
        if entry.is_symlink():
            raise RecoveryError(f"backup roots may not contain symlinks: {source_entry}")
        if entry.is_dir(follow_symlinks=False):
            destination_entry.mkdir(mode=0o700)
            child_artifacts = _copy_root_tree(
                source=source_entry,
                destination=destination_entry,
                bundle_root=bundle_root,
                root_name=root_name,
            )
            if child_artifacts:
                artifacts.extend(child_artifacts)
            else:
                destination_entry.rmdir()
            continue
        if not entry.is_file(follow_symlinks=False):
            raise RecoveryError(
                f"backup roots may contain only files and directories: {source_entry}"
            )
        _copy_regular_file(source_entry, destination_entry)
        artifacts.append(
            _artifact_record(
                bundle_root,
                destination_entry,
                role="root",
                root=root_name,
            )
        )
    return artifacts


def _copy_regular_file(source: Path, destination: Path) -> None:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    try:
        source_descriptor = os.open(source, source_flags)
        with os.fdopen(source_descriptor, "rb", closefd=True) as input_stream:
            before = os.fstat(input_stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RecoveryError(f"source is not a regular file: {source}")
            output_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(output_descriptor, "wb", closefd=True) as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            after = os.fstat(input_stream.fileno())
    except OSError as exc:
        raise RecoveryError(f"could not safely copy file: {source}") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
    ):
        raise RecoveryError(f"source changed while it was copied: {source}")


def _copy_verified_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_hash: str,
) -> None:
    _copy_regular_file(source, destination)
    size, digest = _file_size_and_hash(destination)
    if size != expected_size or digest != expected_hash:
        raise RecoveryError(f"backup changed while restoring: {source}")


def _artifact_record(
    bundle_root: Path,
    path: Path,
    *,
    role: str,
    root: str | None = None,
) -> dict[str, Any]:
    size, digest = _file_size_and_hash(path)
    record: dict[str, Any] = {
        "path": path.relative_to(bundle_root).as_posix(),
        "role": role,
        "size": size,
        "sha256": digest,
    }
    if root is not None:
        record["root"] = root
    return record


def _validated_manifest_roots(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not _ROOT_NAME.fullmatch(item)
        for item in value
    ):
        raise RecoveryError("manifest roots must be safe logical names")
    if value != sorted(set(value)):
        raise RecoveryError("manifest roots must be sorted and unique")
    return tuple(value)


def _validated_manifest_artifacts(
    value: object,
    roots: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise RecoveryError("manifest artifacts must be a non-empty list")
    artifacts: list[dict[str, Any]] = []
    paths: set[str] = set()
    roles: dict[str, int] = {"database": 0, "checkpoint": 0}
    for raw in value:
        if not isinstance(raw, dict):
            raise RecoveryError("manifest artifact must be an object")
        role = raw.get("role")
        path = raw.get("path")
        size = raw.get("size")
        digest = raw.get("sha256")
        if role not in {"database", "checkpoint", "root"}:
            raise RecoveryError("manifest artifact role is invalid")
        if not isinstance(path, str):
            raise RecoveryError("manifest artifact path is invalid")
        _validate_relative_path(path)
        if path in paths:
            raise RecoveryError(f"duplicate manifest artifact path: {path}")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise RecoveryError(f"manifest metadata is invalid: {path}")
        if role in roles:
            roles[role] += 1
            expected = f"files/{role}.sqlite3"
            if path != expected or "root" in raw:
                raise RecoveryError(f"invalid {role} artifact path")
        else:
            root = raw.get("root")
            if root not in roots or not path.startswith(f"roots/{root}/"):
                raise RecoveryError(f"invalid root artifact path: {path}")
        artifacts.append(dict(raw))
        paths.add(path)
    if roles != {"database": 1, "checkpoint": 1}:
        raise RecoveryError("manifest requires exactly one database and checkpoint")
    if artifacts != sorted(artifacts, key=lambda item: item["path"]):
        raise RecoveryError("manifest artifacts must be sorted by path")
    return tuple(artifacts)


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RecoveryError(f"unsafe manifest path: {value!r}")


def _walk_bundle(bundle: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for root, directory_names, file_names in os.walk(
        bundle,
        topdown=True,
        followlinks=False,
    ):
        root_path = Path(root)
        for name in sorted(directory_names):
            path = root_path / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise RecoveryError(f"unsafe backup directory: {path}")
            _require_private_mode(path, f"directory {path}")
            directories.add(path.relative_to(bundle).as_posix())
        for name in sorted(file_names):
            path = root_path / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise RecoveryError(f"unsafe backup artifact: {path}")
            files.add(path.relative_to(bundle).as_posix())
    return files, directories


def _validate_roots(roots: Mapping[str, Path]) -> dict[str, Path]:
    validated: dict[str, Path] = {}
    for name, path in roots.items():
        if not _ROOT_NAME.fullmatch(name):
            raise RecoveryError(
                "backup root names must match [a-z][a-z0-9-]{0,63}"
            )
        if name in validated:
            raise RecoveryError(f"duplicate backup root: {name}")
        validated[name] = _require_directory(path, f"backup root {name}")
    return validated


def _validated_manifest_json(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryError("manifest must be a JSON object")
    return value


def _read_strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise RecoveryError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read recovery manifest: {path}") from exc
    return _validated_manifest_json(value)


def _write_new_private_json(path: Path, value: Mapping[str, Any]) -> None:
    body = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def _payload_hash(value: Mapping[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("manifest_hash", None)
    unsigned.pop("receipt_hash", None)
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_size_and_hash(path: Path) -> tuple[int, str]:
    descriptor = os.open(
        path,
        os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
    )
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryError(f"not a regular file: {path}")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
    ):
        raise RecoveryError(f"file changed while hashing: {path}")
    return after.st_size, digest.hexdigest()


def _require_regular_file(path: Path, label: str) -> Path:
    absolute = _absolute_path(path)
    _assert_no_symlink_components(absolute)
    try:
        mode = os.lstat(absolute).st_mode
    except OSError as exc:
        raise RecoveryError(f"{label} is missing: {absolute}") from exc
    if not stat.S_ISREG(mode):
        raise RecoveryError(f"{label} must be a regular file: {absolute}")
    return absolute


def _require_directory(path: Path, label: str) -> Path:
    absolute = _absolute_path(path)
    _assert_no_symlink_components(absolute)
    try:
        mode = os.lstat(absolute).st_mode
    except OSError as exc:
        raise RecoveryError(f"{label} is missing: {absolute}") from exc
    if not stat.S_ISDIR(mode):
        raise RecoveryError(f"{label} must be a directory: {absolute}")
    return absolute


def _prepare_directory(path: Path, label: str) -> Path:
    absolute = _absolute_path(path)
    _assert_no_symlink_components(absolute, allow_missing=True)
    try:
        absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise RecoveryError(f"cannot create {label}: {absolute}") from exc
    return _require_directory(absolute, label)


def _prepare_empty_restore_target(path: Path) -> Path:
    absolute = _absolute_path(path)
    _assert_no_symlink_components(absolute, allow_missing=True)
    if absolute.exists():
        target = _require_directory(absolute, "restore target")
        try:
            if any(target.iterdir()):
                raise RecoveryError(
                    f"restore target must be empty: {target}"
                )
        except OSError as exc:
            raise RecoveryError(f"cannot inspect restore target: {target}") from exc
        return target
    parent = _prepare_directory(absolute.parent, "restore target parent")
    try:
        os.mkdir(parent / absolute.name, 0o700)
    except OSError as exc:
        raise RecoveryError(f"cannot create restore target: {absolute}") from exc
    return _require_directory(absolute, "restore target")


def _assert_no_symlink_components(
    path: Path,
    *,
    allow_missing: bool = False,
) -> None:
    current = Path(path.anchor)
    missing = False
    for part in path.parts[1:]:
        current /= part
        if missing:
            continue
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if allow_missing:
                missing = True
                continue
            raise RecoveryError(f"path component is missing: {current}") from None
        except OSError as exc:
            raise RecoveryError(f"cannot inspect path component: {current}") from exc
        if stat.S_ISLNK(mode):
            raise RecoveryError(f"symlink paths are not allowed: {current}")


def _require_private_mode(path: Path, label: str) -> None:
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    if mode & 0o077:
        raise RecoveryError(
            f"{label} must not be accessible by group/other: {oct(mode)}"
        )


def _reject_overlapping_backup_paths(
    output_root: Path,
    sources: tuple[Path, ...],
) -> None:
    for source in sources:
        if _paths_overlap(output_root, source):
            raise RecoveryError(
                f"backup output must be isolated from every source: {source}"
            )


def _reject_overlapping_backup_sources(sources: tuple[Path, ...]) -> None:
    for index, source in enumerate(sources):
        for other in sources[index + 1 :]:
            if _paths_overlap(source, other):
                raise RecoveryError(
                    "backup sources must be mutually isolated: "
                    f"{source} and {other}"
                )


def _paths_overlap(left: Path, right: Path) -> bool:
    left = _absolute_path(left)
    right = _absolute_path(right)
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
