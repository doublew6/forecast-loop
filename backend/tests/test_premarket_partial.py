"""Synthetic partial-coverage forecasts retain all integrity boundaries."""

from datetime import timedelta

import pytest
from app.evidence_coverage import DEGRADED_WARNING, EvidenceCoverageV3
from app.premarket import (
    PremarketDraftBundleV4,
    build_premarket_handoff,
    canonical_json,
    finalize_premarket_forecast,
    parse_premarket_drafts,
    parse_premarket_handoff,
)
from test_evidence_coverage import job


def partial_job(change="unresolved"):
    old, bundle = job()
    payload = old.evidence_coverage.model_dump(exclude={"schema_version"})
    if change == "unresolved":
        payload["events"][0].update(
            status="unresolved",
            evidence_item_ids=[],
            source_hashes={},
            missing_reason="Original not captured",
        )
    elif change == "missing":
        payload["receipts"] = []
    elif change == "failed":
        payload["receipts"][0]["status"] = "failed"
    elif change == "stale":
        payload["receipts"][0]["observed_through"] -= timedelta(hours=1)
    h = build_premarket_handoff(
        run_id=old.run_id,
        snapshot=old.snapshot,
        prepared_at=old.prepared_at,
        api_first=True,
        evidence_coverage=EvidenceCoverageV3(**payload),
    )
    raw = bundle.model_dump(exclude={"schema_version", "request_hash"})
    return h, PremarketDraftBundleV4(**raw, request_hash=h.request_hash)


@pytest.mark.parametrize("change", ["unresolved", "missing", "failed", "stale"])
def test_partial_coverage_persists_probabilities_and_discloses_gaps(change):
    h, b = partial_job(change)
    f = finalize_premarket_forecast(h, b, accepted_at=b.generated_at)
    assert DEGRADED_WARNING in f.rationale
    assert sum(f.probabilities.model_dump().values()) == pytest.approx(1)
    assert f.probabilities.up == pytest.approx(0.65 * b.drafts[4].probabilities.up + 0.35 / 3)
    assert parse_premarket_handoff(canonical_json(h)) == h
    assert parse_premarket_drafts(canonical_json(b)) == b


@pytest.mark.parametrize("change", ["future", "hash", "snapshot", "citation", "deadline"])
def test_partial_coverage_never_relaxes_integrity(change):
    h, b = partial_job("complete")
    if change == "future":
        h.evidence_coverage.receipts[0].captured_at += timedelta(minutes=1)
    elif change == "hash":
        h.evidence_coverage.events[0].source_hashes["news-1"] = "f" * 64
    elif change == "snapshot":
        h.evidence_coverage.snapshot_hash = "f" * 64
    elif change == "citation":
        b.drafts[4].evidence_item_ids = ["fx-1"]
    with pytest.raises(ValueError):
        finalize_premarket_forecast(
            h,
            b,
            accepted_at=(
                h.snapshot.finalization_deadline if change == "deadline" else b.generated_at
            ),
        )


def test_partial_file_prepare_generate_finalize_and_brief(tmp_path):
    import httpx
    from app.config import Settings
    from app.services.openai_responses_v2 import OpenAIResponsesSettings
    from app.services.premarket import (
        build_premarket_brief,
        finalize_premarket_run,
        load_premarket_forecast,
        prepare_premarket_run,
    )
    from app.services.premarket_generation import generate_premarket_drafts

    h, b = partial_job()
    settings = Settings(handoff_root=tmp_path / "handoffs")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(canonical_json(h.snapshot))
    job_dir = prepare_premarket_run(
        settings,
        snapshot_path=snapshot,
        api_first=True,
        evidence_coverage=h.evidence_coverage,
        now=h.prepared_at,
    )
    actual = parse_premarket_handoff((job_dir / "input.json").read_bytes())
    sealed = (job_dir / "input.json").read_bytes()
    assert "Partial evidence policy" in (job_dir / "INSTRUCTIONS.md").read_text()
    bundle = generate_premarket_drafts(
        actual,
        OpenAIResponsesSettings(api_key="test-key", _env_file=None),
        fallback=lambda budget: b.drafts,
        timeout_seconds=20,
        invocation_time=h.prepared_at,
        clock=lambda: b.generated_at,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(503, json={"error": {"code": "server_error"}})
            )
        ),
    )
    (job_dir / "drafts.json").write_bytes(canonical_json(bundle))
    f = finalize_premarket_run(settings, job_dir=job_dir, now=b.generated_at)
    assert load_premarket_forecast(settings, job_dir=job_dir) == f
    assert DEGRADED_WARNING in build_premarket_brief(settings, job_dir=job_dir).text
    assert (job_dir / "input.json").read_bytes() == sealed
