"""Opt-in degraded-media coverage must retain warnings through real persistence."""

import json
from types import SimpleNamespace

import pytest
from app.evidence_coverage import EvidenceCoverageV1, validate_coverage
from app.evidence_coverage_v2 import MEDIA_WARNING, EvidenceCoverageV2
from app.services.snapshot import LiveEvidenceRequiredError, validate_trusted_source_url
from pydantic import ValidationError
from test_evidence_coverage import coverage, job


def test_reviewed_media_domain_keeps_https_and_hostname_boundaries():
    validate_trusted_source_url("https://api3.cls.cn/share/article/123")
    for url in ["http://api3.cls.cn/share/article/123", "https://cls.cn.example/article/123"]:
        with pytest.raises(LiveEvidenceRequiredError):
            validate_trusted_source_url(url)


def reported_coverage(snapshot):
    payload = coverage(snapshot).model_dump()
    payload.update(
        schema_version="forecast-loop.evidence-coverage/v2",
        media_policy="attributed-unverified-media/v1",
    )
    payload["events"][0].update(
        status="media_reported",
        missing_reason="Official original unavailable; attributed media capture only",
        verification_warning=MEDIA_WARNING,
    )
    return EvidenceCoverageV2(**payload)


def test_explicit_media_opt_in_and_warning_are_required():
    handoff, _ = job()
    value = reported_coverage(handoff.snapshot)
    payload = value.model_dump()
    payload.pop("media_policy")
    with pytest.raises(ValidationError):
        EvidenceCoverageV2(**payload)
    with pytest.raises(ValidationError):
        EvidenceCoverageV1(**value.model_dump())
    payload = value.model_dump()
    payload["events"][0].pop("verification_warning")
    with pytest.raises(ValidationError):
        EvidenceCoverageV2(**payload)


@pytest.mark.parametrize("fault", ["evidence_warning", "draft_warning", "hash", "unresolved"])
def test_degraded_gate_still_rejects_missing_labels_tampering_and_unresolved(fault):
    handoff, _ = job()
    value = reported_coverage(handoff.snapshot)
    items = [i.model_copy(deep=True) for i in handoff.snapshot.items]
    items[0].summary = MEDIA_WARNING + ": synthetic attributed report"
    snapshot = SimpleNamespace(
        content_hash=handoff.snapshot.content_hash,
        evidence_cutoff=handoff.snapshot.evidence_cutoff,
        evidence_start=handoff.snapshot.evidence_start,
        items=items,
    )
    drafts = [
        SimpleNamespace(
            assignment_id=a.assignment_id,
            evidence_item_ids=["news-1"],
            rationale=MEDIA_WARNING + ": conditional interpretation",
        )
        for a in handoff.assignments
    ]
    validate_coverage(value, snapshot, handoff.assignments, drafts)
    if fault == "evidence_warning":
        items[0].summary = "A report without its verification limitation"
    elif fault == "draft_warning":
        drafts[0].rationale = "An unlabeled prediction"
    elif fault == "hash":
        items[0].source_hash = "0" * 64
    else:
        value.events[0].status = "unresolved"
    with pytest.raises(ValueError, match="input_incomplete"):
        validate_coverage(value, snapshot, handoff.assignments, drafts)


def test_real_responses_prepare_finalize_preserves_degraded_forecast_label(tmp_path, monkeypatch):
    import app.services.research_v2 as service
    from app.db import Database
    from app.models import ForecastV2
    from app.research_v2 import EvidenceSnapshotBodyV2, seal_evidence_snapshot
    from app.services.schema_readiness import upgrade_database
    from sqlalchemy import select
    from test_openai_responses_v2 import NOW, _Clock
    from test_research_v2 import _draft_for_assignment, _settings, _snapshot, _write_wiki

    monkeypatch.setattr(service, "datetime", _Clock)
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    body = _snapshot().model_dump(exclude={"content_hash"})
    body["items"][0]["summary"] = MEDIA_WARNING + ": synthetic media report"
    snapshot = seal_evidence_snapshot(EvidenceSnapshotBodyV2(**body))
    cutoff = snapshot.data_cutoff
    value = EvidenceCoverageV2(
        window_start=cutoff.replace(hour=0, minute=0),
        evidence_cutoff=cutoff,
        snapshot_hash=snapshot.content_hash,
        media_policy="attributed-unverified-media/v1",
        required_source_groups=["synthetic-discovery"],
        receipts=[dict(group_id="synthetic-discovery", status="complete",
                       window_start=cutoff.replace(hour=0, minute=0),
                       observed_through=cutoff, captured_at=cutoff, source_hash="a" * 64)],
        events=[dict(event_id="synthetic-report", status="media_reported",
                     evidence_item_ids=[snapshot.items[0].item_id],
                     source_hashes={snapshot.items[0].item_id: snapshot.items[0].source_hash},
                     required_agent_ids=["macro_policy_agent", "strategy_agent"],
                     verification_warning=MEDIA_WARNING, missing_reason="No official original")],
    )
    path = tmp_path / "snapshot.json"
    path.write_text(snapshot.model_dump_json())
    database = Database(settings.database_url)
    try:
        package = service.prepare_research_run(
            database, settings, snapshot_path=path, mode="demo", provider="openai_responses",
            evidence_coverage=value,
        )
        request = service.read_research_request(package / "input.json")
        assert isinstance(request.evidence_coverage, EvidenceCoverageV2)
        assert "media_reported" in (package / "INSTRUCTIONS.md").read_text()
        records = []
        for assignment in request.assignments:
            if assignment.producer == "deterministic":
                continue
            draft = _draft_for_assignment(assignment.model_dump(mode="json"))
            draft["evidence_item_ids"] = [snapshot.items[0].item_id]
            draft["rationale"] = MEDIA_WARNING + ": " + draft["rationale"]
            records.append(dict(assignment_id=assignment.assignment_id, draft=draft))
        bundle = dict(schema_version=request.schema_version, run_id=request.run_id,
                      request_hash=request.request_hash, status="ready", issues=[],
                      generated_at=NOW.isoformat(), drafts=records,
                      generated_by=dict(surface="openai_responses", model="gpt-6.1-sol",
                                        reasoning_effort="high", response_id="resp_synthetic",
                                        response_model="gpt-6.1-sol"))
        bad = json.loads(json.dumps(bundle))
        bad["drafts"][0]["draft"]["rationale"] = "No media warning"
        (package / "drafts.json").write_text(json.dumps(bad))
        with pytest.raises(service.ResearchV2Error, match="missing media warning"):
            service.finalize_research_run(database, settings, job_dir=package, now=NOW)
        with database.session_factory() as session:
            assert not session.scalars(select(ForecastV2)).all()
        (package / "drafts.json").write_text(json.dumps(bundle))
        result = service.finalize_research_run(database, settings, job_dir=package, now=NOW)
        assert result.status == "completed"
        with database.session_factory() as session:
            forecasts = session.scalars(select(ForecastV2)).all()
            assert forecasts and all("降级预测" in f.rationale for f in forecasts)
    finally:
        database.dispose()
