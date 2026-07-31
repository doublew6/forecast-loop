from __future__ import annotations

from typing import Any

import pytest
from app.services.handoff import HandoffReceipt
from pydantic import ValidationError


def _receipt_payload(
    *,
    protocol_version: str,
    provider: str,
    **attempt_metadata: Any,
) -> dict[str, Any]:
    return {
        "protocol_version": protocol_version,
        "run_id": "8ba90e29-9f8b-4e26-8306-98c22db872aa",
        "status": "completed",
        "finalized_at": "2026-07-31T10:30:00+00:00",
        "provider": provider,
        "input_hash": "1" * 64,
        "request_hash": "2" * 64,
        "request_raw_hash": "3" * 64,
        "drafts_hash": "4" * 64,
        "drafts_raw_hash": "5" * 64,
        "output_hash": "6" * 64,
        "opinion_count": 30,
        "forecast_count": 5,
        "generated_by": {
            "surface": "codex",
            "task_id": "receipt-schema-test",
            "model": "test-model",
        },
        "error": None,
        "receipt_hash": "7" * 64,
        **attempt_metadata,
    }


@pytest.mark.parametrize(
    ("protocol_version", "provider", "attempt_metadata", "expected_attempt"),
    [
        ("1.0.0", "codex-file-handoff-v1", {}, None),
        ("2.0.0", "codex-file-handoff-v2", {}, None),
        ("3.0.0", "codex-file-handoff-v3", {"attempt_number": 1}, 1),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {"attempt_number": 2, "previous_receipt_hash": "8" * 64},
            2,
        ),
    ],
)
def test_receipt_attempt_metadata_accepts_supported_protocol_shapes(
    protocol_version: str,
    provider: str,
    attempt_metadata: dict[str, Any],
    expected_attempt: int | None,
) -> None:
    receipt = HandoffReceipt.model_validate(
        _receipt_payload(
            protocol_version=protocol_version,
            provider=provider,
            **attempt_metadata,
        )
    )

    assert receipt.attempt_number == expected_attempt
    dumped = receipt.model_dump(mode="json")
    if expected_attempt is None:
        assert "attempt_number" not in dumped
        assert "previous_receipt_hash" not in dumped


@pytest.mark.parametrize(
    ("protocol_version", "provider", "attempt_metadata", "error"),
    [
        (
            "1.0.0",
            "codex-file-handoff-v1",
            {"attempt_number": 1},
            "must omit v3 attempt metadata",
        ),
        (
            "2.0.0",
            "codex-file-handoff-v2",
            {"previous_receipt_hash": "8" * 64},
            "must omit v3 attempt metadata",
        ),
        (
            "1.0.0",
            "codex-file-handoff-v1",
            {"attempt_number": None},
            "must omit v3 attempt metadata",
        ),
        (
            "2.0.0",
            "codex-file-handoff-v2",
            {"previous_receipt_hash": None},
            "must omit v3 attempt metadata",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v2",
            {"attempt_number": 1},
            "protocol_version and provider must use the same version",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {},
            "require a positive attempt_number",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {"attempt_number": None},
            "require a positive attempt_number",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {"attempt_number": 0},
            "greater than or equal to 1",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {"attempt_number": 1, "previous_receipt_hash": "8" * 64},
            "must omit previous_receipt_hash",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {"attempt_number": 1, "previous_receipt_hash": None},
            "must omit previous_receipt_hash",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {"attempt_number": 2},
            "require previous_receipt_hash",
        ),
        (
            "3.0.0",
            "codex-file-handoff-v3",
            {"attempt_number": 2, "previous_receipt_hash": None},
            "require previous_receipt_hash",
        ),
    ],
)
def test_receipt_attempt_metadata_rejects_protocol_inconsistencies(
    protocol_version: str,
    provider: str,
    attempt_metadata: dict[str, Any],
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        HandoffReceipt.model_validate(
            _receipt_payload(
                protocol_version=protocol_version,
                provider=provider,
                **attempt_metadata,
            )
        )
