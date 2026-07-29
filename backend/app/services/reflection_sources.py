"""Read and verify the immutable source timeline for a reflection."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ..config import Settings
from .reflection_handoff import (
    FrozenSource,
    FrozenSourceSnapshot,
    _canonical_hash,
    _secure_read_json,
)


def load_frozen_source_timeline(
    settings: Settings,
    *,
    reflection_id: str,
    source_run_id: str,
    expected_hash: str | None,
) -> list[FrozenSource]:
    """Return source rows only after the on-disk snapshot seal is verified.

    Older or not-yet-frozen reflection rows have no source seal and therefore
    expose an empty timeline.  Once a seal exists, a missing or modified file is
    an integrity failure rather than an empty result.
    """

    if expected_hash is None:
        return []

    normalized_reflection_id = str(UUID(reflection_id))
    normalized_source_run_id = str(UUID(source_run_id))
    root = _existing_reflection_root(settings.reflection_root)
    directory = root / normalized_reflection_id
    if directory.is_symlink():
        raise ValueError("reflection job directory may not be a symlink")
    resolved_directory = directory.resolve(strict=True)
    if not resolved_directory.is_dir() or resolved_directory.parent != root:
        raise ValueError("reflection job directory escaped the configured root")

    sources_path = resolved_directory / "sources.json"
    if sources_path.is_symlink():
        raise ValueError("frozen source snapshot may not be a symlink")
    if sources_path.resolve(strict=True).parent != resolved_directory:
        raise ValueError("frozen source snapshot escaped its reflection job")
    _, payload = _secure_read_json(sources_path)
    snapshot = FrozenSourceSnapshot.model_validate(payload)

    if str(snapshot.reflection_id) != normalized_reflection_id:
        raise ValueError("frozen source snapshot belongs to another reflection")
    if str(snapshot.source_run_id) != normalized_source_run_id:
        raise ValueError("frozen source snapshot belongs to another source run")
    canonical_hash = _canonical_hash(
        snapshot.model_dump(mode="json", exclude={"content_hash"})
    )
    if snapshot.content_hash != canonical_hash:
        raise ValueError("frozen source snapshot content hash is invalid")
    if snapshot.content_hash != expected_hash:
        raise ValueError("frozen source snapshot differs from the database seal")
    for item in snapshot.items:
        item_hash = _canonical_hash(
            item.model_dump(
                mode="json",
                exclude={"content_hash", "time_class"},
            )
        )
        if item.content_hash != item_hash:
            raise ValueError(f"frozen source {item.id} content hash is invalid")
    return list(snapshot.items)


def _existing_reflection_root(path: Path) -> Path:
    configured = path.expanduser()
    if configured.is_symlink():
        raise ValueError("configured reflection root may not be a symlink")
    resolved = configured.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("configured reflection root is not a directory")
    return resolved
