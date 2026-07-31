from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from app.domain import AGENTS, legacy_v1_agent_hash_projection
from app.models import AgentOpinion, Forecast, WorkflowRun
from app.services.run_bundle import (
    ARTIFACT_NAMES,
    _canonical_json_bytes,
    _run_payloads,
    verify_run_bundle,
)
from app.workflow import CommitteeWorkflow

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "compat"
    / "v1"
    / "run-bundle"
)
ARTIFACT_HASHES = {
    "run.json": "353b7918a9d7ab392acd0b0e8e6f72076eb3a75fd72a9e4049e2ac5f69c10c88",
    "opinions.json": "8ff0da1adc14c4bf1ae42ac080c67e45c8cb8f6d61dd2856b06d57268e300cf1",
    "forecasts.json": "87d425c9600742d9f68162933f758426ca3703e185d82e99d4476478a2d7f0c6",
}


def _legacy_run() -> WorkflowRun:
    row = WorkflowRun(
        id="00000000-0000-0000-0000-000000000001",
        as_of=datetime(2026, 7, 13, 7, 0, tzinfo=UTC),
        data_cutoff=datetime(2026, 7, 13, 6, 55, tzinfo=UTC),
        status="completed",
        mode="live",
        started_at=datetime(2026, 7, 13, 7, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 13, 7, 1, tzinfo=UTC),
        duration_seconds=60.0,
        error=None,
        data_quality={"schema": "legacy-v1-fixture", "verified": True},
        workflow_steps=[
            {
                "id": "legacy",
                "label": "Legacy",
                "status": "completed",
                "started_at": "2026-07-13T07:00:00Z",
                "completed_at": "2026-07-13T07:01:00Z",
            }
        ],
        input_hash="a" * 64,
    )
    row.opinions = [
        AgentOpinion(
            id="00000000-0000-0000-0000-000000000002",
            run_id=row.id,
            agent_id="macro_policy_agent",
            agent_name="Macro",
            role="Research",
            agent_version="0.2.0",
            model_name="fixture-model",
            status="completed",
            index_code="000300.SH",
            horizon="D1",
            target_date=date(2026, 7, 14),
            direction="up",
            probability_up=0.6,
            probability_neutral=0.25,
            probability_down=0.15,
            summary="Fixture opinion.",
            evidence=["Fixture evidence."],
            counter_evidence=["Fixture counter."],
            invalidation_conditions=["Fixture invalidation."],
            citations=[],
            contribution="Fixture contribution.",
            weight=1.0,
            raw_response={},
        )
    ]
    row.forecasts = [
        Forecast(
            id="00000000-0000-0000-0000-000000000003",
            run_id=row.id,
            index_code="000300.SH",
            index_name="CSI 300",
            horizon="D1",
            base_trade_date=date(2026, 7, 13),
            target_date=date(2026, 7, 14),
            as_of=row.as_of,
            data_cutoff=row.data_cutoff,
            direction="up",
            probability_up=0.55,
            probability_neutral=0.3,
            probability_down=0.15,
            threshold=0.003,
            confidence=0.7857142857142857,
            rationale="Fixture rationale.",
            counter_evidence=["Fixture counter."],
            invalidation_conditions=["Fixture invalidation."],
            citations=[],
            abstain=False,
            model_name="fixture-policy",
            model_version="0.1.0",
            wiki_version="fixture",
            input_hash="b" * 64,
            created_at=datetime(2026, 7, 13, 7, 1, tzinfo=UTC),
        )
    ]
    return row


def test_historical_v1_bundle_bytes_and_hashes_are_unchanged() -> None:
    payloads = _run_payloads(_legacy_run())
    for name in ARTIFACT_NAMES:
        body = _canonical_json_bytes(payloads[name])
        assert body == (FIXTURE / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == ARTIFACT_HASHES[name]

    manifest = verify_run_bundle(FIXTURE)
    assert manifest.schema_version == "vericouncil.run-bundle/v1"
    assert manifest.bundle_hash == (
        "e720d336fcd338ad45a0dfe492b4662dbc826c4851041774c24dde1836d2b342"
    )


def test_legacy_v1_agent_hash_projection_is_byte_locked() -> None:
    model_names = {
        agent.id: f"fixture-model-{position}"
        for position, agent in enumerate(AGENTS)
    }
    projection = legacy_v1_agent_hash_projection(model_names)
    body = json.dumps(projection, sort_keys=True).encode()

    assert len(body) == 957
    assert hashlib.sha256(body).hexdigest() == (
        "ee5f15fefae0193a357a2ea006b8a88e7ff05ca43070dfee71ecb1f47e4d2b69"
    )
    assert [item["id"] for item in projection] == [agent.id for agent in AGENTS]
    assert all("content_hash" not in item for item in projection)


def test_current_and_legacy_prepare_run_input_hashes_are_byte_locked(
    client,
    monkeypatch,
    tmp_path,
) -> None:
    """Exercise the production hash path, not a reimplementation in the test."""

    fixed_run_id = UUID("00000000-0000-0000-0000-000000000099")
    monkeypatch.setattr("app.workflow.uuid4", lambda: fixed_run_id)

    prepared = client.app.state.workflow.prepare_run(
        as_of=datetime(
            2026,
            7,
            17,
            15,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        persist=False,
    )

    assert prepared.row.id == str(fixed_run_id)
    assert prepared.initial["input_hash"] == prepared.row.input_hash
    assert prepared.initial["forecast_horizons"] == ["D1"]
    assert prepared.execution_manifest["forecast_horizons"] == ["D1"]
    assert prepared.execution_manifest["forecast_target_count"] == 5
    assert prepared.execution_manifest["draft_assignment_count"] == 25
    assert prepared.row.input_hash == (
        "a5c9a36ee1489047e49aa60f13f9b46d4e88e206831f0d78fda8f7e9a366fef6"
    )

    legacy_settings = client.app.state.settings.model_copy(
        update={"checkpoint_path": tmp_path / "legacy-hash-checkpoint.sqlite3"}
    )
    legacy_workflow = CommitteeWorkflow(
        settings=legacy_settings,
        database=client.app.state.database,
        provider=client.app.state.workflow.provider,
        wiki=client.app.state.workflow.wiki,
        runtime_mode="legacy_dual_horizon",
    )
    try:
        legacy = legacy_workflow.prepare_run(
            as_of=datetime(
                2026,
                7,
                17,
                15,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            persist=False,
        )
    finally:
        legacy_workflow.close()

    assert "forecast_horizons" not in legacy.initial
    assert legacy.execution_manifest["forecast_horizons"] == ["D1", "D2"]
    assert legacy.execution_manifest["forecast_target_count"] == 10
    assert legacy.execution_manifest["draft_assignment_count"] == 50
    assert legacy.row.input_hash == (
        "5b5be6ae289379f90f8a29262f65bec64b1679bd0f53adf4ef68d4b70ffb68a9"
    )
