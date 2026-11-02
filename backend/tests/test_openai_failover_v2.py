"""Synthetic API-first handoffs retain their seals and actual fallback identity."""

from __future__ import annotations

import json

import app.services.openai_responses_v2 as api
import app.services.research_v2 as research
import httpx
import pytest
from app.db import Database
from app.models import AgentSignalV2Record
from app.responses_v2 import ResponsesHandoffRequestV3, ResponsesPredictionContent
from app.services.schema_readiness import upgrade_database
from sqlalchemy import select
from test_openai_responses_v2 import NOW, _Clock, _reply
from test_research_v2 import _draft_for_assignment, _settings, _snapshot, _write_wiki


@pytest.fixture
def routed(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "datetime", _Clock)
    monkeypatch.setattr(research, "datetime", _Clock)
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(_snapshot().model_dump_json())
    database = Database(settings.database_url)
    job = research.prepare_research_run(
        database,
        settings,
        snapshot_path=snapshot,
        mode="demo",
        provider="openai_responses",
        codex_fallback=True,
    )
    config = api.OpenAIResponsesSettings(_env_file=None, api_key="test-key")
    try:
        yield settings, database, job, config
    finally:
        database.dispose()


def _fallback(calls):
    def generate(request):
        assert isinstance(request, ResponsesHandoffRequestV3)
        calls.append(request.request_hash)
        return ResponsesPredictionContent.model_validate(
            {
                "drafts": [
                    {
                        "assignment_id": item.assignment_id,
                        "draft": _draft_for_assignment(item.model_dump(mode="json")),
                    }
                    for item in request.assignments
                    if item.producer != "deterministic"
                ]
            }
        )

    return generate


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (429, "insufficient_quota", "quota_exhausted"),
        (402, "credit_balance_exhausted", "quota_exhausted"),
        (503, "service_unavailable", "service_unavailable"),
        (408, None, "timeout"),
    ],
)
def test_confirmed_failure_finalizes_with_actual_provider(routed, status, code, reason):
    settings, database, job, config = routed
    sealed = (job / "input.json").read_bytes()
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": {"code": code}}),
        )
    ) as client:
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=client,
            fallback_generator=_fallback(calls),
        )
    bundle = json.loads((job / "drafts.json").read_bytes())
    assert bundle["generated_by"]["surface"] == "codex"
    assert bundle["generated_by"]["fallback_reason"] == reason
    assert "response_id" not in bundle["generated_by"]
    assert len(calls) == 1
    assert (job / "input.json").read_bytes() == sealed
    result = research.finalize_research_run(database, settings, job_dir=job, now=NOW)
    assert result.status == "completed"
    with database.session_factory() as session:
        signals = session.scalars(select(AgentSignalV2Record)).all()
        assert any(row.model_name == "gpt-5.6-sol" for row in signals)
        assert all(row.model_name != "gpt-6.1-sol" for row in signals)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_other_http_failures_do_not_fallback(routed, status):
    settings, database, job, config = routed
    calls = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, json={"error": {"code": "rate_limit_exceeded"}}),
            )
        ) as client,
        pytest.raises(api.OpenAIResponsesError),
    ):
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=client,
            fallback_generator=_fallback(calls),
        )
    assert not calls
    assert not (job / "drafts.json").exists()


def _stream_error(event):
    data = b"data: " + json.dumps(event).encode() + b"\n\n"
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data)


@pytest.mark.parametrize(
    ("nested", "code", "reason"),
    [
        (False, "insufficient_quota", "quota_exhausted"),
        (False, "credit_balance_exhausted", "quota_exhausted"),
        (True, "credit_balance_exhausted", "quota_exhausted"),
        (True, "insufficient_quota", "quota_exhausted"),
        (True, "server_is_overloaded", "service_unavailable"),
    ],
)
def test_stream_error_uses_confirmed_code_for_one_fallback(routed, nested, code, reason):
    settings, database, job, config = routed
    sealed = (job / "input.json").read_bytes()
    error = {"code": code, "message": "private upstream diagnostic"}
    event = {"type": "error", "error": error} if nested else {"type": "error", **error}
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: _stream_error(event))) as client:
        api.draft_research_run(
            database, settings, config, job_dir=job, client=client,
            fallback_generator=_fallback(calls),
        )
    bundle = json.loads((job / "drafts.json").read_bytes())
    assert len(calls) == 1
    assert bundle["generated_by"]["surface"] == "codex"
    assert bundle["generated_by"]["fallback_reason"] == reason
    assert "response_id" not in bundle["generated_by"]
    assert (job / "input.json").read_bytes() == sealed
    assert "private upstream diagnostic" not in json.dumps(bundle)
    assert research.finalize_research_run(database, settings, job_dir=job, now=NOW).status == (
        "completed"
    )


@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "error": {"code": "rate_limit_exceeded"}},
        {"type": "error", "error": {"code": "invalid_api_key"}},
        {"type": "error", "error": {"code": "model_not_found"}},
        {"type": "error", "error": {"code": "invalid_json_schema"}},
        {"type": "error", "error": {"type": "insufficient_quota"}},
        {
            "type": "error", "code": "invalid_api_key",
            "error": {"code": "credit_balance_exhausted"},
        },
        {
            "type": "response.incomplete",
            "response": {"error": {"code": "credit_balance_exhausted"}},
        },
    ],
)
def test_noneligible_or_ambiguous_stream_errors_do_not_fallback(routed, event):
    settings, database, job, config = routed
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: _stream_error(event))) as client:
        with pytest.raises(api.OpenAIResponsesError):
            api.draft_research_run(
                database, settings, config, job_dir=job, client=client,
                fallback_generator=_fallback(calls),
            )
    assert not calls
    assert not (job / "drafts.json").exists()


def test_success_uses_api_and_keeps_versioned_policy(routed):
    settings, database, job, config = routed
    calls = []
    with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=client,
            fallback_generator=_fallback(calls),
        )
    assert not calls
    bundle = json.loads((job / "drafts.json").read_bytes())
    assert bundle["generated_by"]["surface"] == "openai_responses"
    assert bundle["schema_version"].endswith("/v3")
    research.finalize_research_run(database, settings, job_dir=job, now=NOW)


def test_schema_failure_does_not_fallback(routed):
    settings, database, job, config = routed
    calls = []

    def invalid(request):
        response = _reply(request)
        body = response.json()
        body["output"][1]["content"][0]["text"] = '{"drafts":[]}'
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(invalid)) as client:
        with pytest.raises(api.OpenAIResponsesError):
            api.draft_research_run(
                database,
                settings,
                config,
                job_dir=job,
                client=client,
                fallback_generator=_fallback(calls),
            )
    assert not calls
    assert not (job / "drafts.json").exists()


def test_old_handoff_cannot_gain_fallback_policy(routed):
    settings, database, job, _ = routed
    request = research.read_research_request(job / "input.json")
    snapshot = job.parent.parent.parent / "snapshot-again.json"
    snapshot.write_text(request.snapshot.model_dump_json())
    with pytest.raises(research.ResearchV2Error, match="fallback policy"):
        research.prepare_research_run(
            database, settings, snapshot_path=snapshot, mode="demo", provider="openai_responses"
        )


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_connection_failures_permit_one_fallback(routed, error):
    settings, database, job, config = routed
    calls = []

    def unavailable(request):
        raise error("synthetic transport failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=client,
            fallback_generator=_fallback(calls),
        )
    assert len(calls) == 1


def test_missing_credentials_do_not_fallback(routed):
    settings, database, job, config = routed
    config = api.OpenAIResponsesSettings(_env_file=None, api_key="")
    calls = []
    with pytest.raises(api.OpenAIResponsesError):
        api.draft_research_run(
            database, settings, config, job_dir=job, fallback_generator=_fallback(calls)
        )
    assert not calls


def test_old_api_only_protocol_cannot_accept_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "datetime", _Clock)
    monkeypatch.setattr(research, "datetime", _Clock)
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(_snapshot().model_dump_json())
    database = Database(settings.database_url)
    calls = []
    try:
        job = research.prepare_research_run(
            database, settings, snapshot_path=snapshot, mode="demo", provider="openai_responses"
        )
        config = api.OpenAIResponsesSettings(_env_file=None, api_key="test-key")
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            with pytest.raises(api.OpenAIAvailabilityError):
                api.draft_research_run(
                    database,
                    settings,
                    config,
                    job_dir=job,
                    client=client,
                    fallback_generator=_fallback(calls),
                )
        assert not calls
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (503, "service_unavailable", "codex"),
        (429, "insufficient_quota", "codex"),
        (429, "rate_limit_exceeded", None),
        (401, "invalid_api_key", None),
        (200, "credit_balance_exhausted", "codex"),
        (200, "rate_limit_exceeded", None),
        (200, None, "openai_responses"),
    ],
)
def test_blind_review_uses_same_sealed_fallback_policy(routed, status, code, expected):
    from app.responses_v2 import RoutedReviewContentV2

    settings, database, job, config = routed
    with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
        api.draft_research_run(database, settings, config, job_dir=job, client=client)
    research.finalize_research_run(database, settings, job_dir=job, now=NOW)
    sealed = (job / "reasoning/input.json").read_bytes()
    task = research.validate_responses_reasoning_task(database, settings, job_dir=job)
    calls = []

    def fallback(frozen):
        calls.append(frozen.task_hash)
        return RoutedReviewContentV2.model_validate(
            {
                "reviews": [
                    {
                        "signal_id": item.signal_id,
                        "review_input_hash": item.review_input_hash,
                        "rubric": {
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "high",
                            "evidence_relevance": 2,
                            "causal_chain": 2,
                            "target_horizon_mapping": 2,
                            "counter_evidence_and_invalidation": 2,
                            "probability_uncertainty_consistency": 2,
                            "advisory": "Synthetic review.",
                        },
                    }
                    for item in frozen.reviews
                ]
            }
        )

    def reply(request):
        if status == 200 and code is not None:
            return _stream_error({"type": "error", "error": {"code": code}})
        return (
            _reply(request)
            if status == 200
            else httpx.Response(status, json={"error": {"code": code}})
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        if expected is None:
            with pytest.raises(api.OpenAIResponsesError):
                api.draft_reasoning_review(
                    database,
                    settings,
                    config,
                    job_dir=job,
                    client=client,
                    fallback_generator=fallback,
                )
            assert not calls
            assert not (job / "reasoning/drafts.json").exists()
            return
        api.draft_reasoning_review(
            database, settings, config, job_dir=job, client=client, fallback_generator=fallback
        )
    assert calls == ([task.task_hash] if expected == "codex" else [])
    bundle = json.loads((job / "reasoning/drafts.json").read_bytes())
    assert bundle["generated_by"]["surface"] == expected
    assert (job / "reasoning/input.json").read_bytes() == sealed
    rows = research.finalize_reasoning_review(database, settings, job_dir=job)
    assert len(rows) == len(task.reviews)
    assert all(row.rubric["model"] == bundle["generated_by"]["model"] for row in rows)
    assert {
        row.id for row in research.finalize_reasoning_review(database, settings, job_dir=job)
    } == {row.id for row in rows}


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_failures_are_fallback_eligible(routed, failure):
    settings, database, job, config = routed
    calls = []

    def fail(request):
        raise failure("Synthetic transport failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=client,
            fallback_generator=_fallback(calls),
        )
    assert len(calls) == 1
    research.finalize_research_run(database, settings, job_dir=job, now=NOW)


def test_quota_error_type_alone_cannot_trigger_fallback(routed):
    settings, database, job, config = routed
    calls = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    429,
                    json={"error": {"type": "insufficient_quota", "code": "rate_limit_exceeded"}},
                )
            )
        ) as client,
        pytest.raises(api.OpenAIResponsesError),
    ):
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=client,
            fallback_generator=_fallback(calls),
        )
    assert not calls


def test_installed_client_fallback_has_no_tools_or_primary_key(routed, monkeypatch, tmp_path):
    from types import SimpleNamespace

    import app.services.codex_responses_v2 as codex

    settings, database, job, config = routed
    request = research.read_research_request(job / "input.json")
    binary = tmp_path / "client"
    binary.write_text("synthetic executable")
    binary.chmod(0o700)
    config_root = tmp_path / "client-config"
    config_root.mkdir()
    (config_root / "config.toml").write_text('[mcp_servers.synthetic]\ncommand="example"\n')
    monkeypatch.setenv("CODEX_HOME", str(config_root))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-never-pass-to-fallback")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:12345")

    def run(command, **kwargs):
        assert "--ignore-user-config" not in command
        assert "--ignore-rules" not in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["HTTPS_PROXY"] == "http://127.0.0.1:12345"
        assert "shell_tool" in command and "unified_exec" in command
        assert 'web_search="disabled"' in command
        assert "mcp_servers.synthetic.enabled=false" in command
        from pathlib import Path

        Path(command[command.index("--output-last-message") + 1]).write_text(
            _fallback([])(request).model_dump_json()
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex.subprocess, "run", run)
    result = codex.CodexResponsesFallback(binary).prediction(request)
    assert result.drafts


def test_existing_review_task_is_never_silently_upgraded(routed):
    from app.research_v2 import content_hash
    from app.responses_v2 import ResponsesReasoningTaskV1

    settings, database, job, config = routed
    with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
        api.draft_research_run(database, settings, config, job_dir=job, client=client)
    research.finalize_research_run(database, settings, job_dir=job, now=NOW)
    path = job / "reasoning/input.json"
    task = research.validate_responses_reasoning_task(database, settings, job_dir=job)
    value = task.model_dump(
        exclude={"fallback_policy", "fallback_model", "fallback_reasoning_effort"}
    )
    value["schema_version"] = "forecast-loop.responses-reasoning-task/v1"
    value["task_hash"] = content_hash(value, exclude=("task_hash",))
    legacy = ResponsesReasoningTaskV1.model_validate(value)
    path.write_text(legacy.model_dump_json())
    sealed = path.read_bytes()
    research.prepare_reasoning_review(database, settings, run_id=task.run_id, job_dir=job)
    assert path.read_bytes() == sealed
    with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
        api.draft_reasoning_review(database, settings, config, job_dir=job, client=client)
    assert research.finalize_reasoning_review(database, settings, job_dir=job)


@pytest.mark.parametrize(
    ("code", "allowed"),
    [
        ("server_error", True),
        ("insufficient_quota", True),
        ("rate_limit_exceeded", False),
    ],
)
def test_stream_failure_requires_an_explicit_quota_or_availability_code(routed, code, allowed):
    settings, database, job, config = routed
    calls = []
    event = {"type": "response.failed", "response": {"error": {"code": code}}}

    def reply(_):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(event) + "\n\n").encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        if not allowed:
            with pytest.raises(api.OpenAIResponsesError):
                api.draft_research_run(
                    database,
                    settings,
                    config,
                    job_dir=job,
                    client=client,
                    fallback_generator=_fallback(calls),
                )
            assert not calls
            return
        api.draft_research_run(
            database,
            settings,
            config,
            job_dir=job,
            client=client,
            fallback_generator=_fallback(calls),
        )
    assert len(calls) == 1
    research.finalize_research_run(database, settings, job_dir=job, now=NOW)
