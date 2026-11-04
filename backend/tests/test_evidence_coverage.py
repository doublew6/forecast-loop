"""Synthetic coverage regressions, including direct-finalizer bypass attempts."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from app.evidence_coverage import EvidenceCoverageV1, validate_coverage
from app.premarket import (
    PremarketDraftBundleV3,
    build_premarket_handoff,
    canonical_json,
    finalize_premarket_forecast,
    parse_premarket_handoff,
    seal_premarket_snapshot,
)
from pydantic import ValidationError
from test_premarket import CUTOFF, _draft_bundle, _snapshot_body


def coverage(snapshot):
    start = snapshot.evidence_start
    return EvidenceCoverageV1(
        window_start=start,
        evidence_cutoff=snapshot.evidence_cutoff,
        snapshot_hash=snapshot.content_hash,
        required_source_groups=["official"],
        receipts=[
            dict(
                group_id="official",
                status="complete",
                window_start=start,
                observed_through=CUTOFF,
                captured_at=CUTOFF,
                source_hash="a" * 64,
            )
        ],
        events=[
            dict(
                event_id="policy-1",
                status="verified",
                evidence_item_ids=["news-1"],
                source_hashes={"news-1": snapshot.items[0].source_hash},
                required_agent_ids=["macro_policy_agent", "strategy_agent"],
            )
        ],
    )


def job():
    snapshot = seal_premarket_snapshot(_snapshot_body())
    handoff = build_premarket_handoff(
        run_id="synthetic",
        snapshot=snapshot,
        prepared_at=snapshot.created_at,
        evidence_coverage=coverage(snapshot),
        api_first=True,
    )
    payload = _draft_bundle(handoff).model_dump()
    payload.update(
        schema_version=handoff.schema_version,
        generated_by=dict(
            surface="openai_responses",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            response_id="resp_synthetic",
            response_model="gpt-6.1-sol",
        ),
    )
    return handoff, PremarketDraftBundleV3(**payload)


def test_required_event_survives_and_direct_finalize_accepts_supported_no_impact():
    handoff, bundle = job()
    for draft in bundle.drafts:
        if draft.agent_id in {"macro_policy_agent", "strategy_agent"}:
            draft.rationale = "The policy is verified; no incremental effect over this horizon."
            draft.evidence_item_ids = ["news-1"]
    forecast = finalize_premarket_forecast(
        handoff,
        bundle,
        accepted_at=bundle.generated_at,
    )
    assert forecast.evidence_item_ids
    assert (
        parse_premarket_handoff(canonical_json(handoff)).evidence_coverage
        == handoff.evidence_coverage
    )


def test_direct_finalize_rejects_omission_with_event_and_assignment_ids():
    handoff, bundle = job()
    draft = next(d for d in bundle.drafts if d.agent_id == "strategy_agent")
    draft.evidence_item_ids = ["fx-1"]
    with pytest.raises(ValueError, match="policy-1/strategy_agent"):
        finalize_premarket_forecast(handoff, bundle, accepted_at=bundle.generated_at)


@pytest.mark.parametrize("change", ["failed", "stale", "future", "short_window", "missing", "hash"])
def test_incomplete_collection_and_tampering_fail_closed(change):
    handoff, _ = job()
    value = handoff.evidence_coverage.model_copy(deep=True)
    if change == "failed":
        value.receipts[0].status = "failed"
    if change == "stale":
        value.receipts[0].observed_through -= timedelta(hours=1)
    if change == "future":
        value.receipts[0].captured_at += timedelta(seconds=1)
    if change == "short_window":
        value.receipts[0].window_start += timedelta(hours=1)
    if change == "missing":
        value.receipts.clear()
    if change == "hash":
        value.events[0].source_hashes["news-1"] = "f" * 64
    with pytest.raises(ValueError, match="input_incomplete"):
        validate_coverage(value, handoff.snapshot, handoff.assignments)


def test_unresolved_original_never_becomes_verified_absence():
    handoff, _ = job()
    payload = handoff.evidence_coverage.model_dump()
    payload["events"][0].update(
        status="unresolved",
        evidence_item_ids=[],
        source_hashes={},
        missing_reason="Original publication time is unknown",
    )
    with pytest.raises(ValueError, match="unresolved policy event policy-1"):
        validate_coverage(EvidenceCoverageV1(**payload), handoff.snapshot, handoff.assignments)


def test_new_schema_rejects_deleted_null_or_downgraded_coverage():
    handoff, _ = job()
    for mode in ("delete", "null", "downgrade"):
        payload = handoff.model_dump(mode="json")
        if mode == "delete":
            payload.pop("evidence_coverage")
        if mode == "null":
            payload["evidence_coverage"] = None
        if mode == "downgrade":
            payload["schema_version"] = "forecast-loop.premarket-handoff/v2"
        with pytest.raises(ValidationError):
            parse_premarket_handoff(__import__("json").dumps(payload))


def test_evening_receipts_must_cover_all_afternoon_events():
    handoff, _ = job()
    cutoff = CUTOFF.replace(hour=18)
    snapshot = SimpleNamespace(
        content_hash=handoff.snapshot.content_hash, data_cutoff=cutoff, items=handoff.snapshot.items
    )
    value = handoff.evidence_coverage.model_copy(deep=True)
    value.evidence_cutoff = cutoff
    value.window_start = cutoff.replace(hour=0, minute=0)
    value.receipts[0].captured_at = cutoff
    value.receipts[0].observed_through = cutoff
    value.receipts[0].window_start = cutoff - timedelta(minutes=1)
    with pytest.raises(ValueError, match="required source group"):
        validate_coverage(value, snapshot, handoff.assignments)


def test_responses_v5_prepare_and_direct_finalize_reject_missing_event(tmp_path, monkeypatch):
    import json

    import app.services.research_v2 as service
    from app.db import Database
    from app.models import ForecastV2
    from app.services.schema_readiness import upgrade_database
    from sqlalchemy import select
    from test_openai_responses_v2 import NOW, _Clock
    from test_research_v2 import _draft_for_assignment, _settings, _snapshot, _write_wiki

    monkeypatch.setattr(service, "datetime", _Clock)
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot = _snapshot()
    cutoff = snapshot.data_cutoff
    value = EvidenceCoverageV1(
        window_start=cutoff.replace(hour=0, minute=0),
        evidence_cutoff=cutoff,
        snapshot_hash=snapshot.content_hash,
        required_source_groups=["official"],
        receipts=[
            dict(
                group_id="official",
                status="complete",
                window_start=cutoff.replace(hour=0, minute=0),
                observed_through=cutoff,
                captured_at=cutoff,
                source_hash="a" * 64,
            )
        ],
        events=[
            dict(
                event_id="policy-evening",
                status="verified",
                evidence_item_ids=["event-1"],
                source_hashes={"event-1": snapshot.items[0].source_hash},
                required_agent_ids=["macro_policy_agent", "strategy_agent"],
            )
        ],
    )
    path = tmp_path / "snapshot.json"
    path.write_text(snapshot.model_dump_json())
    database = Database(settings.database_url)
    try:
        job = service.prepare_research_run(
            database,
            settings,
            snapshot_path=path,
            mode="demo",
            provider="openai_responses",
            evidence_coverage=value,
        )
        request = service.read_research_request(job / "input.json")
        assert request.schema_version.endswith("/v5")
        records = [
            {
                "assignment_id": a.assignment_id,
                "draft": _draft_for_assignment(a.model_dump(mode="json")),
            }
            for a in request.assignments
            if a.producer != "deterministic"
        ]
        # A non-policy item is available so omission remains structurally well-formed.
        strategy = next(r for r in records if r["assignment_id"].startswith("strategy_agent"))
        strategy["draft"]["evidence_item_ids"] = []
        bundle = dict(
            schema_version=request.schema_version,
            run_id=request.run_id,
            request_hash=request.request_hash,
            status="ready",
            issues=[],
            generated_at=NOW.isoformat(),
            generated_by=dict(
                surface="openai_responses",
                model="gpt-6.1-sol",
                reasoning_effort="high",
                response_id="resp_synthetic",
                response_model="gpt-6.1-sol",
            ),
            drafts=records,
        )
        (job / "drafts.json").write_text(json.dumps(bundle))
        with pytest.raises(service.ResearchV2Error, match="policy-evening/strategy_agent"):
            service.finalize_research_run(database, settings, job_dir=job, now=NOW)
        with database.session_factory() as session:
            assert not session.scalars(select(ForecastV2)).all()
        strategy["draft"]["evidence_item_ids"] = ["event-1"]
        raw = json.dumps(bundle).encode()
        service.validate_research_draft_bundle(settings, job_dir=job, raw_drafts=raw)
        (job / "drafts.json").write_bytes(raw)
        result = service.finalize_research_run(database, settings, job_dir=job, now=NOW)
        assert result.status == "completed"
        with database.session_factory() as session:
            assert session.scalars(select(ForecastV2)).all()
    finally:
        database.dispose()
