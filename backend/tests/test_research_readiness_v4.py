"""Synthetic coverage, rejection and compatibility checks; no external model calls."""

from __future__ import annotations

import json
from datetime import timedelta

import app.cli as cli
import app.services.openai_responses_v2 as api
import app.services.research_v2 as research
import httpx
import pytest
from app.db import Database
from app.models import AgentSignalV2Record, ForecastV2, ResearchRunV2
from app.research_readiness import EvidenceAdmissionV1, assess_evidence_readiness
from app.research_v2 import content_hash
from app.responses_v2 import ResponsesHandoffRequestV4, ResponsesPredictionContentV4
from app.services.schema_readiness import upgrade_database
from pydantic import ValidationError
from sqlalchemy import select
from test_openai_responses_v2 import NOW, _Clock, _reply
from test_research_v2 import _draft_for_assignment, _settings, _snapshot, _write_wiki

CATEGORIES = [
    "index_exposure",
    "market_context",
    "macro_context",
    "expectations_context",
    "industry_context",
    "company_index_mapping",
]


def admission_for(snapshot):
    # A semantic attestation is necessary, not a deterministic proof of content.
    body = {
        "schema_version": "forecast-loop.evidence-admission/v1",
        "snapshot_hash": snapshot.content_hash,
        "bindings": [
            {
                "category": c,
                "evidence_item_id": snapshot.items[0].item_id,
                "source_hash": snapshot.items[0].source_hash,
                "instruments": ["000852.SH", "000300.SH"],
                "effective_from": str(snapshot.base_session),
                "effective_through": str(snapshot.future_sessions[0]),
            }
            for c in CATEGORIES
        ],
    }
    return EvidenceAdmissionV1(**body, content_hash=content_hash(body))


@pytest.fixture
def setup_v4(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "datetime", _Clock)
    monkeypatch.setattr(api, "datetime", _Clock)
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot = _snapshot(source_url="https://www.szse.cn/market/")
    path = tmp_path / "snapshot.json"
    path.write_text(snapshot.model_dump_json())
    admission = tmp_path / "admission.json"
    admission.write_text(admission_for(snapshot).model_dump_json())
    database = Database(settings.database_url)
    config = api.OpenAIResponsesSettings(_env_file=None, api_key="test-key")

    def prepare(*, mode="live", admitted=True, fallback=False):
        return research.prepare_research_run(
            database,
            settings,
            snapshot_path=path,
            mode=mode,
            provider="openai_responses",
            evidence_admission_path=admission if admitted else None,
            codex_fallback=fallback,
        )

    yield settings, database, snapshot, path, admission, config, prepare
    database.dispose()


def _response(content):
    return httpx.Response(
        200,
        json={
            "id": "resp_synthetic_v4",
            "status": "completed",
            "model": "gpt-6.1-sol",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": json.dumps(content)}],
                }
            ],
            "usage": {"input_tokens": 1200, "output_tokens": 400},
        },
    )


def _content(request):
    return {
        "status": "ready",
        "issues": [],
        "drafts": [
            {"assignment_id": a.assignment_id, "draft": _draft_for_assignment(a.model_dump())}
            for a in request.assignments
            if a.producer != "deterministic"
        ],
    }


def _not_ready(request):
    return {
        "status": "not_ready",
        "drafts": [],
        "issues": [
            {
                "assignment_id": request.assignments[0].assignment_id,
                "code": "insufficient_evidence",
                "requirement": "macro_context",
                "explanation": "Admitted context contains no policy event or expectations.",
                "evidence_item_ids": ["event-1"],
            }
        ],
    }


def test_missing_inputs_do_not_create_a_formal_run_or_consume_anchor(setup_v4):
    _, database, _, _, _, _, prepare = setup_v4
    with pytest.raises(research.ResearchInputsNotReady) as caught:
        prepare(admitted=False)
    assert caught.value.report.status == "not_ready"
    assert {i.requirement for i in caught.value.report.issues} == set(CATEGORIES)
    with database.session_factory() as session:
        assert session.scalars(select(ResearchRunV2)).all() == []
    job = prepare()
    request = research.read_research_request(job / "input.json")
    assert isinstance(request, ResponsesHandoffRequestV4)
    assert request.evidence_readiness.status == "ready"
    assert prepare() == job


@pytest.mark.parametrize("mutation", ["foreign_snapshot", "wrong_hash", "stale", "unknown_id"])
def test_admission_rejects_wrong_or_stale_source_binding(setup_v4, mutation):
    _, database, snapshot, _, path, _, prepare = setup_v4
    body = json.loads(path.read_text())
    if mutation == "foreign_snapshot":
        body["snapshot_hash"] = "a" * 64
    if mutation == "wrong_hash":
        body["bindings"][0]["source_hash"] = "a" * 64
    if mutation == "stale":
        body["bindings"][0]["effective_from"] = str(snapshot.base_session - timedelta(days=2))
        body["bindings"][0]["effective_through"] = str(snapshot.base_session - timedelta(days=1))
    if mutation == "unknown_id":
        body["bindings"][0]["evidence_item_id"] = "unknown"
    body["content_hash"] = content_hash(body)
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError):
        prepare()
    with database.session_factory() as session:
        assert session.scalars(select(ResearchRunV2)).all() == []


def test_readiness_uses_each_assignments_allowed_ids_and_rejects_forgery(setup_v4):
    _, _, snapshot, _, _, _, prepare = setup_v4
    job = prepare()
    request = research.read_research_request(job / "input.json")
    assignments = [
        a.model_copy(update={"allowed_evidence_item_ids": []}) for a in request.assignments
    ]
    report = assess_evidence_readiness(
        snapshot, assignments, request.evidence_admission, mode="live"
    )
    assert report.status == "not_ready"
    body = request.model_dump(mode="json")
    body["evidence_readiness"]["admission_hash"] = "b" * 64
    body["request_hash"] = content_hash(body, exclude=("request_hash",))
    with pytest.raises(ValidationError, match="readiness report"):
        ResponsesHandoffRequestV4.model_validate(body)


def test_index_validity_uses_the_first_frozen_forecast_session(setup_v4):
    _, _, snapshot, _, _, _, prepare = setup_v4
    job = prepare()
    request = research.read_research_request(job / "input.json")
    body = request.evidence_admission.model_dump(mode="json")
    binding = body["bindings"][0]
    binding["effective_from"] = str(snapshot.future_sessions[0])
    body["content_hash"] = content_hash(body)
    future_effective = EvidenceAdmissionV1.model_validate(body)
    assert assess_evidence_readiness(
        snapshot, request.assignments, future_effective, mode="live"
    ).status == "ready"
    binding["effective_from"] = binding["effective_through"] = str(snapshot.base_session)
    body["content_hash"] = content_hash(body)
    with pytest.raises(ValueError, match="stale"):
        assess_evidence_readiness(
            snapshot, request.assignments, EvidenceAdmissionV1.model_validate(body), mode="live"
        )


def test_noise_thresholds_are_derived_not_model_supplied(setup_v4):
    _, _, _, _, _, _, prepare = setup_v4
    request = research.read_research_request(prepare() / "input.json")
    body = request.model_dump(mode="json")
    first = next(iter(body["neutral_thresholds"]))
    body["neutral_thresholds"][first] *= 2
    body["request_hash"] = content_hash(body, exclude=("request_hash",))
    with pytest.raises(ValidationError, match="neutral thresholds"):
        ResponsesHandoffRequestV4.model_validate(body)


def test_semantic_rejection_is_terminal_audited_and_never_falls_back(setup_v4):
    settings, database, _, _, _, config, prepare = setup_v4
    job = prepare(fallback=True)
    request = research.read_research_request(job / "input.json")
    sealed = (job / "input.json").read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda _: _response(_not_ready(request)))) as c:
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=c,
            fallback_generator=lambda _: pytest.fail("unexpected fallback"),
        )
    with database.session_factory() as session:
        assert session.get(ResearchRunV2, job.name).status == "awaiting_draft"
    run = research.finalize_research_run(database, settings, job_dir=job, now=NOW)
    assert run.status == "failed" and run.error == "not_ready"
    assert run.receipt["failure_category"] == "not_ready"
    assert run.receipt["signal_ids"] == run.receipt["forecast_ids"] == []
    receipt = (job / "receipt.json").read_bytes()
    assert research.finalize_research_run(database, settings, job_dir=job, now=NOW).id == run.id
    assert (job / "receipt.json").read_bytes() == receipt
    (job / "receipt.json").unlink()
    research.finalize_research_run(database, settings, job_dir=job, now=NOW)
    assert (job / "receipt.json").read_bytes() == receipt
    assert not (job / "reasoning/input.json").exists()
    assert (job / "input.json").read_bytes() == sealed
    with database.session_factory() as session:
        assert session.scalars(select(AgentSignalV2Record)).all() == []
        assert session.scalars(select(ForecastV2)).all() == []
    changed = json.loads((job / "drafts.json").read_bytes())
    changed["issues"][0]["explanation"] = "Altered terminal result"
    (job / "drafts.json").write_text(json.dumps(changed))
    with pytest.raises(research.ResearchV2Error, match="no longer binds"):
        research.finalize_research_run(database, settings, job_dir=job, now=NOW)


@pytest.mark.parametrize("mutation", ["assignment", "evidence", "mixed", "no_issues"])
def test_invalid_rejection_cannot_publish_or_fallback(setup_v4, mutation):
    settings, database, _, _, _, config, prepare = setup_v4
    job = prepare(fallback=True)
    request = research.read_research_request(job / "input.json")
    content = _not_ready(request)
    if mutation == "assignment":
        content["issues"][0]["assignment_id"] = "another-task"
    if mutation == "evidence":
        content["issues"][0]["evidence_item_ids"] = ["unknown"]
    if mutation == "mixed":
        content["drafts"] = _content(request)["drafts"]
    if mutation == "no_issues":
        content["issues"] = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: _response(content))) as c:
        with pytest.raises(research.ResearchV2Error):
            api.draft_research_run(
                database,
                settings,
                config,
                job_dir=job,
                client=c,
                fallback_generator=lambda _: pytest.fail("unexpected fallback"),
            )
    assert not (job / "drafts.json").exists()


@pytest.mark.parametrize("fallback", [False, True])
def test_ready_pipeline_and_versioned_semantic_blind_prompt(setup_v4, fallback):
    settings, database, _, _, _, config, prepare = setup_v4
    job = prepare(mode="demo", admitted=False, fallback=fallback)
    request = research.read_research_request(job / "input.json")
    assert request.evidence_readiness.status == "demo_only"
    sent = []

    def handler(r):
        body = json.loads(r.content)
        sent.append(body)
        return _response(_content(request)) if len(sent) == 1 else _reply(r)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        api.draft_research_run(database, settings, config, job_dir=job, client=c)
        assert (
            research.finalize_research_run(database, settings, job_dir=job, now=NOW).status
            == "completed"
        )
        api.draft_reasoning_review(database, settings, config, job_dir=job, client=c)
    task = research.validate_responses_reasoning_task(database, settings, job_dir=job)
    assert task.schema_version == "forecast-loop.responses-reasoning-task/v3"
    assert task.prediction_context.request_hash == request.request_hash
    assert task.prediction_context.snapshot == request.snapshot
    assert task.fallback_policy == ("quota-or-availability/v1" if fallback else "none")
    assert "Do not award full causal" in sent[1]["instructions"]
    assert "not_ready" in sent[0]["instructions"]
    reviews = research.finalize_reasoning_review(database, settings, job_dir=job)
    assert reviews
    task_body = task.model_dump(mode="json")
    task_body["schema_version"] = "forecast-loop.responses-reasoning-task/v1"
    task_body["task_hash"] = content_hash(task_body, exclude=("task_hash",))
    (job / "reasoning/input.json").write_text(json.dumps(task_body))
    with pytest.raises(research.ResearchV2Error, match="cannot downgrade"):
        research.validate_responses_reasoning_task(database, settings, job_dir=job)


@pytest.mark.parametrize("enabled", [False, True])
def test_quota_fallback_uses_same_v4_result_contract_only_if_sealed(setup_v4, enabled):
    settings, database, _, _, _, config, prepare = setup_v4
    job = prepare(fallback=enabled)
    request = research.read_research_request(job / "input.json")
    calls = []

    def fallback(r):
        calls.append(r.request_hash)
        return ResponsesPredictionContentV4.model_validate(_not_ready(r))

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(402, json={"error": {"code": "credit_balance_exhausted"}})
        )
    ) as c:
        if enabled:
            api.draft_research_run(
                database, settings, config, job_dir=job, client=c, fallback_generator=fallback
            )
            bundle = json.loads((job / "drafts.json").read_bytes())
            assert bundle["status"] == "not_ready"
            assert bundle["generated_by"]["surface"] == "codex"
            assert calls == [request.request_hash]
        else:
            with pytest.raises(api.OpenAIAvailabilityError):
                api.draft_research_run(
                    database, settings, config, job_dir=job, client=c, fallback_generator=fallback
                )
            assert not calls


def test_cli_reports_not_ready_with_machine_readable_reasons(setup_v4, monkeypatch, capsys):
    settings, _, _, path, _, _, _ = setup_v4
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert (
        cli.main(
            [
                "research-v2",
                "prepare",
                "--snapshot",
                str(path),
                "--mode",
                "live",
                "--provider",
                "openai_responses",
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "not_ready" and report["issues"]
