from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import app.services.openai_responses_v2 as api_service
import app.services.research_v2 as research_service
import httpx
import pytest
from app.cli import main
from app.db import Database
from app.models import AgentSignalV2Record, ForecastV2, ResearchRunV2
from app.research_v2 import AgentSignalDraftV2, CodexDraftBundleV3, content_hash
from app.responses_v2 import (
    ResponsesDraftBundleV1,
    ResponsesHandoffRequestV1,
    ResponsesHandoffRequestV2,
)
from app.services.openai_responses_v2 import (
    OpenAIResponsesError,
    OpenAIResponsesSettings,
    draft_reasoning_review,
    draft_research_run,
)
from app.services.research_v2 import (
    ResearchV2Error,
    finalize_reasoning_review,
    finalize_research_run,
    prepare_research_run,
    validate_research_draft_bundle,
)
from app.services.schema_readiness import upgrade_database
from pydantic import ValidationError
from sqlalchemy import select
from test_research_v2 import _draft_for_assignment, _settings, _snapshot, _write_wiki

NOW = datetime(2026, 8, 12, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(research_service, "datetime", _Clock)
    monkeypatch.setattr(api_service, "datetime", _Clock)
    for key in (
        "OPENAI_API_KEY",
        "FORECAST_LOOP_OPENAI_MODEL",
        "FORECAST_LOOP_OPENAI_REASONING_EFFORT",
        "FORECAST_LOOP_OPENAI_TIMEOUT_SECONDS",
        "FORECAST_LOOP_OPENAI_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(_snapshot().model_dump_json())
    database = Database(settings.database_url)
    job = prepare_research_run(
        database,
        settings,
        snapshot_path=snapshot,
        mode="demo",
        provider="openai_responses",
    )
    config = OpenAIResponsesSettings(_env_file=None, api_key="test-key")
    try:
        yield settings, database, job, config
    finally:
        database.dispose()


def _reply(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    payload = json.loads(body["input"])
    if "assignments" in payload:
        content = {
            "drafts": [
                {
                    "assignment_id": item["assignment_id"],
                    "draft": AgentSignalDraftV2.model_validate(
                        _draft_for_assignment(item),
                    ).model_dump(mode="json"),
                }
                for item in payload["assignments"]
                if item["producer"] == "openai_responses"
            ]
        }
    else:
        content = {
            "reviews": [
                {
                    "signal_id": item["signal_id"],
                    "review_input_hash": item["review_input_hash"],
                    "rubric": {
                        "model": "gpt-6.1-sol",
                        "reasoning_effort": "high",
                        "evidence_relevance": 2,
                        "causal_chain": 2,
                        "target_horizon_mapping": 2,
                        "counter_evidence_and_invalidation": 2,
                        "probability_uncertainty_consistency": 2,
                        "advisory": "The frozen evidence supports the stated reasoning.",
                    },
                }
                for item in payload["reviews"]
            ]
        }
    return httpx.Response(
        200,
        json={
            "id": "resp_synthetic",
            "status": "completed",
            "model": "gpt-6.1-sol",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": json.dumps(content)},
                    ],
                },
            ],
            "usage": {"input_tokens": 1200, "output_tokens": 600},
        },
    )


def test_api_prediction_and_blind_review_keep_finalize_boundary(prepared):
    settings, database, job, config = prepared
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return _reply(request)

    original = (job / "input.json").read_bytes()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        draft_research_run(database, settings, config, job_dir=job, client=client)
        with database.session_factory() as session:
            assert session.scalars(select(ForecastV2)).all() == []
            assert session.get(ResearchRunV2, job.name).status == "awaiting_draft"
        row = finalize_research_run(database, settings, job_dir=job)
        assert row.status == "completed"
        receipt = (job / "receipt.json").read_bytes()
        assert finalize_research_run(database, settings, job_dir=job).id == row.id
        assert (job / "receipt.json").read_bytes() == receipt
        draft_reasoning_review(database, settings, config, job_dir=job, client=client)
    reviews = finalize_reasoning_review(database, settings, job_dir=job)
    assert len(reviews) > 0
    assert all(item.rubric["model"] == "gpt-6.1-sol" for item in reviews)
    assert all(item.total_score == 10 for item in reviews)
    with database.session_factory() as session:
        signals = session.scalars(select(AgentSignalV2Record)).all()
        assert {item.model_name for item in signals if item.agent_id != "cio_agent"} == {
            "gpt-6.1-sol",
        }
        assert {item.model_name for item in signals if item.agent_id == "cio_agent"} == {
            "forecast-loop-deterministic-cio",
        }
    assert (job / "input.json").read_bytes() == original
    assert len(sent) == 2
    for body in sent:
        assert body["model"] == "gpt-6.1-sol"
        assert body["reasoning"] == {"effort": "high"}
        assert body["store"] is False
        assert "tools" not in body and "temperature" not in body
        assert body["text"]["format"]["strict"] is True
        assert "synthetic-test-credential" not in json.dumps(body)
    blind = json.loads(sent[1]["input"])
    prediction_input = json.loads(sent[0]["input"])
    assert prediction_input["mode"] == "demo"
    assert prediction_input["schema_version"] == "forecast-loop.responses-handoff/v2"
    assert blind["outcomes_included"] is False
    assert "outcome" not in blind["reviews"][0]
    draft = json.loads((job / "drafts.json").read_text())
    assert draft["generated_by"]["response_id"] == "resp_synthetic"
    assert draft["generated_by"]["input_tokens"] == "1200"
    with pytest.raises(ValidationError):
        CodexDraftBundleV3.model_validate(draft)


@pytest.mark.parametrize(
    "failure",
    [
        "incomplete",
        "refusal",
        "wrong_model",
        "bad_probability",
        "missing_probabilities",
        "foreign_evidence",
        "missing_assignment",
        "duplicate_assignment",
        "wrong_wiki",
        "http_error",
        "network",
    ],
)
def test_rejected_api_output_publishes_nothing(prepared, failure):
    settings, database, job, config = prepared

    def handler(request):
        if failure == "network":
            raise httpx.ConnectError("sensitive upstream detail", request=request)
        result = _reply(request).json()
        text = result["output"][1]["content"][0]
        content = json.loads(text["text"])
        if failure == "incomplete":
            result["status"] = "incomplete"
        elif failure == "refusal":
            text.update(type="refusal", text=None)
        elif failure == "wrong_model":
            result["model"] = "unexpected-model"
        elif failure == "http_error":
            return httpx.Response(429, text="sensitive upstream detail")
        elif failure == "bad_probability":
            content["drafts"][0]["draft"]["probabilities"] = {"up": 1, "neutral": 1, "down": 1}
        elif failure == "missing_probabilities":
            content["drafts"][0]["draft"].update(
                probabilities=None,
                direction=None,
                abstain=True,
            )
        elif failure == "foreign_evidence":
            content["drafts"][0]["draft"]["evidence_item_ids"] = ["not-frozen"]
        elif failure == "wrong_wiki":
            content["drafts"][0]["draft"]["wiki_version"] = "99.0.0"
        elif failure == "missing_assignment":
            content["drafts"].pop()
        elif failure == "duplicate_assignment":
            content["drafts"].append(content["drafts"][0])
        if text["type"] == "output_text":
            text["text"] = json.dumps(content)
        return httpx.Response(200, json=result)

    original = (job / "input.json").read_bytes()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((ResearchV2Error, ValidationError)) as error:
            draft_research_run(database, settings, config, job_dir=job, client=client)
    assert "sensitive upstream detail" not in str(error.value)
    assert not (job / "drafts.json").exists()
    assert (job / "input.json").read_bytes() == original
    assert not list(job.glob(".responses-draft-*"))


def test_existing_draft_and_missing_key_never_send(prepared):
    settings, database, job, config = prepared

    def forbidden(request):
        pytest.fail("unexpected cloud request")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        empty = OpenAIResponsesSettings(_env_file=None)
        with pytest.raises(OpenAIResponsesError, match="OPENAI_API_KEY"):
            draft_research_run(database, settings, empty, job_dir=job, client=client)
        (job / "drafts.json").write_text("keep existing draft")
        with pytest.raises(OpenAIResponsesError, match="already exists"):
            draft_research_run(database, settings, config, job_dir=job, client=client)
    assert (job / "drafts.json").read_text() == "keep existing draft"


def test_prepare_rejects_provider_switch_and_preserves_seal(prepared):
    settings, database, job, config = prepared
    snapshot = job.parents[2] / "snapshot.json"
    with pytest.raises(ResearchV2Error, match="another draft provider"):
        prepare_research_run(database, settings, snapshot_path=snapshot, mode="demo")
    request = ResponsesHandoffRequestV2.model_validate_json((job / "input.json").read_bytes())
    assert request.reasoning_effort == "high"
    assert all(item.producer != "codex" for item in request.assignments)


def test_tampered_input_is_rejected_before_network(prepared):
    settings, database, job, config = prepared
    value = json.loads((job / "input.json").read_text())
    value["input_hash"] = "f" * 64
    value["request_hash"] = content_hash(value, exclude=("request_hash",))
    (job / "input.json").write_text(json.dumps(value))
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("network"))
    ) as client:
        with pytest.raises(OpenAIResponsesError, match="sealed awaiting-draft"):
            draft_research_run(database, settings, config, job_dir=job, client=client)


def test_symlink_output_does_not_write_outside_job(prepared, tmp_path):
    settings, database, job, config = prepared
    outside = tmp_path / "outside.json"
    (job / "drafts.json").symlink_to(outside)
    with pytest.raises(OpenAIResponsesError, match="already exists"):
        draft_research_run(database, settings, config, job_dir=job)
    assert not outside.exists()


def test_codex_surface_cannot_impersonate_api_bundle(prepared):
    settings, database, job, config = prepared
    with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
        draft_research_run(database, settings, config, job_dir=job, client=client)
    value = json.loads((job / "drafts.json").read_text())
    value["generated_by"]["surface"] = "codex"
    with pytest.raises(ValidationError, match="frozen API provider"):
        validate_research_draft_bundle(settings, job_dir=job, raw_drafts=json.dumps(value).encode())


def test_configuration_masks_key_and_requires_requested_profile(tmp_path):
    env = tmp_path / "private.env"
    env.write_text("OPENAI_API_KEY=synthetic-test-credential\n")
    config = OpenAIResponsesSettings(_env_file=env)
    assert config.model == "gpt-6.1-sol" and config.reasoning_effort == "high"
    assert "synthetic-test-credential" not in repr(config)
    with pytest.raises(ValidationError):
        OpenAIResponsesSettings(_env_file=None, model="unexpected-model")


def test_responses_contract_and_draft_cli_are_exposed(capsys):
    assert main(["contract", "schema", "responses-handoff-v1"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "ResponsesHandoffRequestV1"
    assert main(["contract", "schema", "responses-handoff-v2"]) == 0
    current = json.loads(capsys.readouterr().out)
    assert "mode" in current["required"]
    assert current["properties"]["mode"]["enum"] == ["demo", "live"]
    with pytest.raises(SystemExit) as exit_info:
        main(["research-v2", "draft", "--help"])
    assert exit_info.value.code == 0
    assert "--env-file" in capsys.readouterr().out


def test_expired_prediction_does_not_send(prepared, monkeypatch):
    settings, database, job, config = prepared

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 14, 20, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(api_service, "datetime", ExpiredClock)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("network"))
    ) as client:
        with pytest.raises(OpenAIResponsesError, match="cutoff has passed"):
            draft_research_run(database, settings, config, job_dir=job, client=client)


@pytest.mark.parametrize("tampering", ["unsealed_outcome", "rehash_signal"])
def test_blind_input_tampering_is_rejected_before_cloud(prepared, tampering):
    settings, database, job, config = prepared
    with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
        draft_research_run(database, settings, config, job_dir=job, client=client)
    finalize_research_run(database, settings, job_dir=job)
    task_path = job / "reasoning" / "input.json"
    value = json.loads(task_path.read_text())
    if tampering == "unsealed_outcome":
        value["realized_outcome"] = "up"
    else:
        value["reviews"][0]["rationale"] = "Changed after the source signal was sealed."
        value["reviews"][0]["review_input_hash"] = content_hash(
            value["reviews"][0],
            exclude=("review_input_hash",),
        )
    value["task_hash"] = content_hash(value, exclude=("task_hash",))
    task_path.write_text(json.dumps(value))
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("network"))
    ) as client:
        with pytest.raises((ResearchV2Error, ValidationError)):
            draft_reasoning_review(database, settings, config, job_dir=job, client=client)
    assert not (job / "reasoning" / "drafts.json").exists()


def test_blind_output_requires_every_frozen_signal(prepared):
    settings, database, job, config = prepared
    with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
        draft_research_run(database, settings, config, job_dir=job, client=client)
    finalize_research_run(database, settings, job_dir=job)

    def handler(request):
        value = _reply(request).json()
        part = value["output"][1]["content"][0]
        content = json.loads(part["text"])
        content["reviews"].pop()
        part["text"] = json.dumps(content)
        return httpx.Response(200, json=value)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenAIResponsesError, match="signal/hash coverage"):
            draft_reasoning_review(database, settings, config, job_dir=job, client=client)
    assert not (job / "reasoning" / "drafts.json").exists()


def test_racing_writer_output_is_preserved(prepared):
    settings, database, job, config = prepared

    def handler(request):
        (job / "drafts.json").write_text("another writer's output")
        return _reply(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenAIResponsesError, match="appeared during generation"):
            draft_research_run(database, settings, config, job_dir=job, client=client)
    assert (job / "drafts.json").read_text() == "another writer's output"


def test_cli_draft_loads_private_config_and_does_not_finalize(
    prepared, tmp_path, monkeypatch, capsys
):
    import app.cli as cli

    settings, database, job, config = prepared
    env = tmp_path / "private.env"
    env.write_text("OPENAI_API_KEY=synthetic-test-credential\n")
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    def writer(database, settings, loaded_config, *, job_dir):
        assert loaded_config.api_key.get_secret_value() == "synthetic-test-credential"
        with httpx.Client(transport=httpx.MockTransport(_reply)) as client:
            return draft_research_run(
                database, settings, loaded_config, job_dir=job_dir, client=client
            )

    monkeypatch.setattr(cli, "draft_research_run", writer)
    assert main(["research-v2", "draft", str(job), "--env-file", str(env)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "draft_written"
    assert "synthetic-test-credential" not in json.dumps(result)
    assert not (job / "receipt.json").exists()


def test_actual_api_model_identity_is_preserved_in_signals(prepared):
    settings, database, job, config = prepared

    def handler(request):
        value = _reply(request).json()
        value["model"] = "gpt-6.1-sol-2026-10-26"
        return httpx.Response(200, json=value)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        draft_research_run(database, settings, config, job_dir=job, client=client)
    finalize_research_run(database, settings, job_dir=job)
    with database.session_factory() as session:
        signals = session.scalars(select(AgentSignalV2Record)).all()
        assert {item.model_name for item in signals if item.agent_id != "cio_agent"} == {
            "gpt-6.1-sol-2026-10-26",
        }


def test_proxy_initialization_errors_do_not_echo_private_configuration(prepared, monkeypatch):
    settings, database, job, config = prepared

    def invalid_proxy(**kwargs):
        raise httpx.InvalidURL("private proxy configuration detail")

    monkeypatch.setattr(api_service.httpx, "Client", invalid_proxy)
    with pytest.raises(OpenAIResponsesError, match="connection failed") as error:
        draft_research_run(database, settings, config, job_dir=job)
    assert "private proxy configuration detail" not in str(error.value)
    assert not (job / "drafts.json").exists()


def _stream_reply(request, *, ending="completed"):
    response = _reply(request).json()
    events = [
        {
            "type": "response.created",
            "response": {
                "id": response["id"],
                "status": "in_progress",
            },
        },
        {"type": "response.output_text.delta", "delta": "untrusted partial output"},
    ]
    if ending in {"completed", "early_done"}:
        events.append({"type": "response.completed", "response": response})
    elif ending in {"failed", "incomplete"}:
        events.append(
            {
                "type": "response." + ending,
                "response": {
                    "id": response["id"],
                    "status": ending,
                    "error": {"message": "private upstream diagnostic"},
                },
            }
        )
    elif ending == "wrong_identity":
        response["id"] = "resp_other"
        events.append({"type": "response.completed", "response": response})
    data = b": synthetic keepalive\r\n\r\n" + b"".join(
        b"data: " + json.dumps(event).encode() + b"\r\n\r\n" for event in events
    )
    if ending == "early_done":
        data = b"data: [DONE]\r\n\r\n" + data

    class FragmentedStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(0, len(data), 17):
                yield data[index : index + 17]

    return httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=FragmentedStream()
    )


def test_real_sse_transport_waits_for_completed_response_before_publishing(prepared):
    settings, database, job, config = prepared

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        assert not (job / "drafts.json").exists()
        return _stream_reply(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        draft_research_run(database, settings, config, job_dir=job, client=client)
    bundle = json.loads((job / "drafts.json").read_text())
    assert bundle["generated_by"]["response_id"] == "resp_synthetic"
    assert len(bundle["drafts"]) > 1


@pytest.mark.parametrize(
    "ending", ["missing", "failed", "incomplete", "wrong_identity", "early_done"]
)
def test_partial_failed_or_mismatched_stream_never_publishes_draft(prepared, ending):
    settings, database, job, config = prepared
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: _stream_reply(request, ending=ending),
        )
    ) as client:
        with pytest.raises(OpenAIResponsesError) as error:
            draft_research_run(database, settings, config, job_dir=job, client=client)
    assert "private upstream diagnostic" not in str(error.value)
    assert not (job / "drafts.json").exists()


def test_stream_size_limit_is_enforced_before_publication(prepared, monkeypatch):
    settings, database, job, config = prepared
    monkeypatch.setattr(api_service, "MAX_JSON_BYTES", 100)
    with httpx.Client(transport=httpx.MockTransport(_stream_reply)) as client:
        with pytest.raises(OpenAIResponsesError, match="size limit"):
            draft_research_run(database, settings, config, job_dir=job, client=client)
    assert not (job / "drafts.json").exists()


def test_legacy_api_request_and_drafts_keep_their_original_contract(prepared):
    from uuid import uuid4

    settings, database, job, config = prepared
    body = json.loads((job / "input.json").read_text())
    body.pop("mode")
    body["schema_version"] = "forecast-loop.responses-handoff/v1"
    body["run_id"] = str(uuid4())
    body["input_hash"] = content_hash(
        {
            "schema_version": "forecast-loop.research-run/v2",
            "program_hash": body["program"]["content_hash"],
            "snapshot_hash": body["snapshot"]["content_hash"],
            "mode": "demo",
            "draft_provider": "openai_responses",
            "model": "gpt-6.1-sol",
            "effort": "high",
        }
    )
    for assignment in body["assignments"]:
        if assignment["producer"] == "openai_responses":
            assignment["prompt_version"] = body["schema_version"]
    body["request_hash"] = content_hash(body, exclude=("request_hash",))
    legacy = ResponsesHandoffRequestV1.model_validate(body)
    legacy_job = job.parent / body["run_id"]
    legacy_job.mkdir()
    (legacy_job / "input.json").write_text(legacy.model_dump_json())
    bundle = ResponsesDraftBundleV1(
        run_id=legacy.run_id,
        request_hash=legacy.request_hash,
        generated_at=NOW,
        generated_by={
            "surface": "openai_responses",
            "model": "gpt-6.1-sol",
            "reasoning_effort": "high",
            "response_id": "resp_legacy",
            "response_model": "gpt-6.1-sol",
        },
        drafts=[
            {"assignment_id": item["assignment_id"], "draft": _draft_for_assignment(item)}
            for item in body["assignments"]
            if item["producer"] == "openai_responses"
        ],
    )
    parsed, accepted = validate_research_draft_bundle(
        settings,
        job_dir=legacy_job,
        raw_drafts=bundle.model_dump_json().encode(),
    )
    assert parsed.request_hash == legacy.request_hash
    assert accepted.schema_version == "forecast-loop.responses-handoff/v1"
    assert "mode" not in parsed.model_dump()


def test_explicit_demo_live_mode_is_hash_bound_before_cloud(prepared):
    settings, database, job, config = prepared
    body = json.loads((job / "input.json").read_text())
    body["mode"] = "live"
    body["request_hash"] = content_hash(body, exclude=("request_hash",))
    (job / "input.json").write_text(json.dumps(body))
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("network"))
    ) as client:
        with pytest.raises(OpenAIResponsesError, match="sealed awaiting-draft"):
            draft_research_run(database, settings, config, job_dir=job, client=client)
