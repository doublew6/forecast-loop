"""Build a hash-sealed forecast-loop live evidence snapshot from a draft JSON file."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from app.schemas import EvidenceItem, FrozenEvidenceSnapshot
from app.services.snapshot import canonical_hash, evidence_item_hash, validate_live_snapshot


def build_snapshot(payload: dict[str, Any]) -> FrozenEvidenceSnapshot:
    payload = copy.deepcopy(payload)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("draft must contain at least one evidence item")
    for position, item in enumerate(items):
        if item.get("event_type") == "template" or "example.invalid" in item.get("source_url", ""):
            raise ValueError("replace template evidence with a real, timestamped source")
        item_without_hash = {key: value for key, value in item.items() if key != "content_hash"}
        normalized = EvidenceItem.model_validate({**item_without_hash, "content_hash": "0" * 64})
        items[position] = normalized.model_copy(
            update={"content_hash": evidence_item_hash(normalized)}
        ).model_dump(mode="json")

    payload["content_hash"] = "0" * 64
    provisional = FrozenEvidenceSnapshot.model_validate(payload)
    canonical = provisional.model_dump(mode="json", exclude={"content_hash"})
    payload["content_hash"] = canonical_hash(canonical)
    snapshot = FrozenEvidenceSnapshot.model_validate(payload)
    validate_live_snapshot(snapshot, as_of=snapshot.as_of)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and seal a forecast-loop frozen evidence snapshot."
    )
    parser.add_argument("draft", type=Path, help="Draft JSON without trusted hashes")
    parser.add_argument("output", type=Path, help="Destination for the sealed snapshot")
    args = parser.parse_args()

    payload = json.loads(args.draft.read_text(encoding="utf-8"))
    snapshot = build_snapshot(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"sealed {len(snapshot.items)} evidence items -> {args.output}")
    print(f"snapshot sha256: {snapshot.content_hash}")


if __name__ == "__main__":
    main()
