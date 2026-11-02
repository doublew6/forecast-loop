"""Synthetic premarket routing preserves seals and actual generation identity."""

import json
from datetime import timedelta

import httpx
import pytest
from app.config import Settings
from app.premarket import (
    PremarketHandoffV1,
    PremarketHandoffV2,
    build_premarket_handoff,
    canonical_json,
    finalize_premarket_forecast,
    seal_premarket_snapshot,
)
from app.services.openai_responses_v2 import OpenAIResponsesError, OpenAIResponsesSettings
from app.services.premarket import (
    finalize_premarket_run,
    load_premarket_forecast,
    prepare_premarket_run,
)
from app.services.premarket_generation import generate_premarket_drafts
from test_premarket import CUTOFF, _draft_bundle, _snapshot_body


def handoff():
    return build_premarket_handoff(
        run_id="synthetic",
        snapshot=seal_premarket_snapshot(_snapshot_body()),
        prepared_at=CUTOFF + timedelta(minutes=1),
        api_first=True,
    )


@pytest.mark.parametrize(
    "status,code,eligible",
    [
        (429, "credit_balance_exhausted", True),
        (429, "insufficient_quota", True),
        (503, "server_error", True),
        (401, "invalid_api_key", False),
        (429, "rate_limit_exceeded", False),
        (400, "unsupported_model", False),
    ],
)
def test_actual_writer_routes_only_confirmed_failures(status, code, eligible):
    h = handoff()
    frozen = canonical_json(h)
    calls = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json={"error": {"code": code}})
        )
    )

    def fallback(budget):
        calls.append(budget)
        return _draft_bundle(h).drafts

    kwargs = dict(
        fallback=fallback,
        timeout_seconds=20,
        invocation_time=CUTOFF + timedelta(minutes=2),
        clock=lambda: CUTOFF + timedelta(minutes=3),
        client=client,
    )
    config = OpenAIResponsesSettings(api_key="test-key", _env_file=None)
    if not eligible:
        with pytest.raises(OpenAIResponsesError):
            generate_premarket_drafts(h, config, **kwargs)
        assert calls == []
    else:
        bundle = generate_premarket_drafts(h, config, **kwargs)
        assert len(calls) == 1
        assert bundle.generated_by["surface"] == "codex"
        assert bundle.generated_by["fallback_reason"] in {"quota_exhausted", "service_unavailable"}
        finalize_premarket_forecast(h, bundle, accepted_at=h.snapshot.decision_time)
        old = build_premarket_handoff(
            run_id=h.run_id, snapshot=h.snapshot, prepared_at=CUTOFF + timedelta(minutes=1)
        )
        with pytest.raises(ValueError):
            finalize_premarket_forecast(old, bundle, accepted_at=h.snapshot.decision_time)
    assert canonical_json(h) == frozen


def test_api_response_keeps_actual_identity_and_checks_evidence():
    h = handoff()
    drafts = _draft_bundle(h).model_dump(mode="json")["drafts"]

    def response(r):
        assert json.loads(r.content)["store"] is False
        return httpx.Response(
            200,
            json={
                "id": "resp_synthetic",
                "status": "completed",
                "model": "gpt-6.1-sol",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps({"drafts": drafts})}
                        ],
                    }
                ],
            },
        )

    kwargs = dict(
        fallback=lambda _: pytest.fail("invalid output must not fallback"),
        timeout_seconds=20,
        invocation_time=CUTOFF + timedelta(minutes=2),
        clock=lambda: CUTOFF + timedelta(minutes=3),
        client=httpx.Client(transport=httpx.MockTransport(response)),
    )
    config = OpenAIResponsesSettings(api_key="test-key", _env_file=None)
    bundle = generate_premarket_drafts(h, config, **kwargs)
    assert bundle.generated_by["response_id"] == "resp_synthetic"
    drafts[0]["evidence_item_ids"] = ["unauthorized"]
    with pytest.raises(ValueError, match="outside its assignment"):
        generate_premarket_drafts(h, config, **kwargs)


def test_v2_file_prepare_finalize_and_legacy_seals(tmp_path):
    h = handoff()
    settings = Settings(handoff_root=tmp_path / "handoffs")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(canonical_json(h.snapshot))
    job = prepare_premarket_run(
        settings, snapshot_path=snapshot, now=CUTOFF + timedelta(minutes=1), api_first=True
    )
    frozen = (job / "input.json").read_bytes()
    actual = PremarketHandoffV2.model_validate_json(frozen)
    assert (
        prepare_premarket_run(
            settings, snapshot_path=snapshot, now=CUTOFF + timedelta(minutes=1), api_first=True
        )
        == job
    )
    with pytest.raises(ValueError):
        PremarketHandoffV1.model_validate_json(frozen)
    with pytest.raises(Exception, match="different content"):
        prepare_premarket_run(settings, snapshot_path=snapshot, now=CUTOFF + timedelta(minutes=1))
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, json={"error": {"code": "insufficient_quota"}})
        )
    )
    bundle = generate_premarket_drafts(
        actual,
        OpenAIResponsesSettings(api_key="test-key", _env_file=None),
        fallback=lambda _: _draft_bundle(actual).drafts,
        timeout_seconds=20,
        invocation_time=CUTOFF + timedelta(minutes=1),
        clock=lambda: CUTOFF + timedelta(minutes=3),
        client=client,
    )
    (job / "drafts.json").write_bytes(canonical_json(bundle))
    forecast = finalize_premarket_run(settings, job_dir=job, now=h.snapshot.decision_time)
    assert load_premarket_forecast(settings, job_dir=job) == forecast
    assert (job / "input.json").read_bytes() == frozen
