from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import app.services.user_judgment as user_judgment_service
import pytest
from app.domain import USER_JUDGMENT_AGENT
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import (
    EvaluationBatch,
    EvaluationResult,
    Forecast,
    UserJudgment,
    WorkflowRun,
)
from app.services.user_judgment import (
    USER_JUDGMENT_POLICY_VERSION,
    materialize_user_judgment_evaluation,
    verify_user_judgment,
)
from app.services.user_judgment_markdown import (
    USER_JUDGMENT_POLICY_V1,
    USER_JUDGMENT_POLICY_V2,
    USER_JUDGMENT_POLICY_V3,
    USER_JUDGMENT_SCHEMA_V1,
    USER_JUDGMENT_SCHEMA_V2,
    UserJudgmentWikiError,
    publish_user_judgment_markdown,
    render_user_judgment_markdown,
    render_user_judgment_markdown_v1,
)
from fastapi.testclient import TestClient
from pydantic import SecretStr

ZONE = ZoneInfo("Asia/Shanghai")
TEST_OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"
OPERATOR_HEADERS = {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def _enable_live_http(client: TestClient) -> None:
    client.app.state.settings.execution_provider = "codex_file"
    client.app.state.settings.demo_mode = False
    client.app.state.settings.operator_token = SecretStr(TEST_OPERATOR_TOKEN)


def _create_demo_run(client: TestClient) -> dict:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-25T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _payload(forecast_id: str, *, blind: bool = False) -> dict:
    return {
        "forecast_id": forecast_id,
        "direction": "up",
        "confidence": 0.67,
        "rationale": "流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。",
        "counter_evidence": "海外利率重新上行可能压制成长估值，并削弱本地流动性改善的效果。",
        "invalidation_condition": "若目标日前成交额显著收缩且指数跌破基准日低点，则本判断失效。",
        "blind_attestation": blind,
    }


def _legacy_v1_payload() -> dict:
    return {
        "schema": USER_JUDGMENT_SCHEMA_V1,
        "id": "11111111-1111-1111-1111-111111111111",
        "actor_id": "legacy-operator",
        "agent_id": "user_judgment_agent",
        "agent_version": "0.1.0",
        "forecast_id": "FORECAST-LEGACY-V1",
        "run_id": "RUN-LEGACY-V1",
        "mode": "demo",
        "index_code": "000300.SH",
        "horizon": "D2",
        "target_date": "2026-07-29",
        "direction": "up",
        "confidence_hex": (0.67).hex(),
        "rationale": "流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。",
        "counter_evidence": "海外利率重新上行可能压制成长估值，并削弱本地流动性改善的效果。",
        "invalidation_condition": "若目标日前成交额显著收缩且指数跌破基准日低点，则本判断失效。",
        "blind_attestation": False,
        "submitted_at": "2026-07-25T15:05:00+08:00",
        "submission_deadline": None,
        "formal_score_eligible": False,
        "run_input_hash": "a" * 64,
        "forecast_input_hash": "b" * 64,
        "policy_version": USER_JUDGMENT_POLICY_V1,
        "wiki_path": "decisions/2026-07-29/legacy-v1.md",
    }


def _canonical_hash(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_v1_markdown_renderer_remains_byte_compatible() -> None:
    payload = _legacy_v1_payload()
    content_hash = _canonical_hash(payload)
    markdown = render_user_judgment_markdown_v1(payload, content_hash)

    assert content_hash == (
        "afd587e246539e6e59df902a74174a8c12adad8873b377f6b08b3b47a5d8d77b"
    )
    assert hashlib.sha256(markdown).hexdigest() == (
        "e30679f3cf5709d3b800c8db85e6e52689201a4c47f0b0639b9a5fb5ebb4305d"
    )
    assert b"schema: vericouncil.user-judgment/v1" in markdown
    assert "不是 VeriCouncil 正式".encode() in markdown


def test_v2_markdown_renderer_remains_supported() -> None:
    payload = _legacy_v1_payload()
    payload["schema"] = USER_JUDGMENT_SCHEMA_V2
    payload["policy_version"] = USER_JUDGMENT_POLICY_V2

    markdown = render_user_judgment_markdown(payload, _canonical_hash(payload))

    assert f"schema: {USER_JUDGMENT_SCHEMA_V2}".encode() in markdown
    assert f'policy_version: "{USER_JUDGMENT_POLICY_V2}"'.encode() in markdown
    assert "不是 forecast-loop 正式".encode() in markdown


@pytest.mark.parametrize(
    ("policy_version", "schema"),
    [
        (USER_JUDGMENT_POLICY_V1, USER_JUDGMENT_SCHEMA_V2),
        (USER_JUDGMENT_POLICY_V2, USER_JUDGMENT_SCHEMA_V1),
        (USER_JUDGMENT_POLICY_V3, USER_JUDGMENT_SCHEMA_V1),
        ("user-judgment/unknown", USER_JUDGMENT_SCHEMA_V2),
    ],
)
def test_markdown_renderer_rejects_policy_schema_mismatch(
    policy_version: str,
    schema: str,
) -> None:
    payload = _legacy_v1_payload()
    payload["policy_version"] = policy_version
    payload["schema"] = schema

    with pytest.raises(UserJudgmentWikiError):
        render_user_judgment_markdown(payload, _canonical_hash(payload))


def test_user_judgment_blind_target_seal_and_private_wiki(
    client: TestClient,
) -> None:
    _create_demo_run(client)
    target_response = client.get("/api/user-judgments/targets")
    assert target_response.status_code == 200
    targets = target_response.json()["items"]
    assert len(targets) == 10
    assert all("direction" not in item for item in targets)
    target = targets[0]
    assert target["submission_open"] is True
    assert target["score_eligible_if_blind"] is False

    too_short = _payload(target["forecast_id"])
    too_short["rationale"] = "太短"
    assert client.post("/api/user-judgments", json=too_short).status_code == 422

    payload = _payload(target["forecast_id"])
    created = client.post("/api/user-judgments", json=payload)
    assert created.status_code == 201, created.text
    judgment = created.json()
    assert judgment["agent_id"] == "user_judgment_agent"
    assert judgment["policy_version"] == USER_JUDGMENT_POLICY_V3
    assert judgment["policy_version"] == USER_JUDGMENT_POLICY_VERSION
    assert judgment["formal_score_eligible"] is False
    assert judgment["committee_direction"] in {"up", "down"}
    assert len(judgment["content_hash"]) == 64
    assert len(judgment["wiki_artifact_hash"]) == 64

    wiki_file = (
        Path(client.app.state.settings.user_judgment_wiki_root)
        / judgment["wiki_path"]
    )
    assert wiki_file.is_file()
    assert wiki_file.stat().st_mode & 0o777 == 0o400
    wiki = client.get(judgment["wiki_url"])
    assert wiki.status_code == 200
    assert payload["rationale"] in wiki.text
    assert judgment["content_hash"] in wiki.text
    assert f"schema: {USER_JUDGMENT_SCHEMA_V2}" in wiki.text
    assert "不是 forecast-loop 正式" in wiki.text
    assert "不是 VeriCouncil 正式" not in wiki.text

    retried = client.post("/api/user-judgments", json=payload)
    assert retried.status_code == 200
    assert retried.json()["id"] == judgment["id"]
    changed = {**payload, "direction": "down"}
    conflict = client.post("/api/user-judgments", json=changed)
    assert conflict.status_code == 409

    sealed_target = next(
        item
        for item in client.get("/api/user-judgments/targets").json()["items"]
        if item["forecast_id"] == target["forecast_id"]
    )
    assert sealed_target["submission_open"] is False
    assert sealed_target["existing_judgment_id"] == judgment["id"]
    history = client.get("/api/user-judgments").json()["items"]
    assert [item["id"] for item in history] == [judgment["id"]]
    assert all(
        entry["id"] != judgment["id"]
        for entry in client.get("/api/wiki").json()["items"]
    )

    os.chmod(wiki_file, 0o600)
    wiki_file.write_text(wiki_file.read_text(encoding="utf-8") + "\n篡改\n")
    assert client.get(judgment["wiki_url"]).status_code == 409


def test_historical_v1_record_and_markdown_still_verify(
    client: TestClient,
) -> None:
    run = _create_demo_run(client)
    wiki_root = Path(client.app.state.settings.user_judgment_wiki_root)
    submitted_at = datetime(2026, 7, 25, 15, 5, tzinfo=ZONE)

    with client.app.state.database.session_factory() as session:
        run_row = session.get(WorkflowRun, run["id"])
        assert run_row is not None
        forecast = sorted(
            run_row.forecasts,
            key=lambda item: (item.index_code, item.horizon),
        )[0]
        judgment_id = "22222222-2222-2222-2222-222222222222"
        wiki_path = (
            f"decisions/{forecast.target_date.isoformat()}/"
            f"legacy-v1-{judgment_id}.md"
        )
        payload = {
            "schema": USER_JUDGMENT_SCHEMA_V1,
            "id": judgment_id,
            "actor_id": client.app.state.settings.user_judgment_actor_id,
            "agent_id": USER_JUDGMENT_AGENT.id,
            "agent_version": USER_JUDGMENT_AGENT.version,
            "forecast_id": forecast.id,
            "run_id": run_row.id,
            "mode": run_row.mode,
            "index_code": forecast.index_code,
            "horizon": forecast.horizon,
            "target_date": forecast.target_date.isoformat(),
            "direction": "up",
            "confidence_hex": (0.67).hex(),
            "rationale": "流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。",
            "counter_evidence": "海外利率重新上行可能压制成长估值，并削弱本地流动性改善的效果。",
            "invalidation_condition": (
                "若目标日前成交额显著收缩且指数跌破基准日低点，则本判断失效。"
            ),
            "blind_attestation": False,
            "submitted_at": submitted_at.isoformat(),
            "submission_deadline": None,
            "formal_score_eligible": False,
            "run_input_hash": run_row.input_hash,
            "forecast_input_hash": forecast.input_hash,
            "policy_version": USER_JUDGMENT_POLICY_V1,
            "wiki_path": wiki_path,
        }
        content_hash = _canonical_hash(payload)
        markdown = render_user_judgment_markdown_v1(payload, content_hash)
        artifact_hash = publish_user_judgment_markdown(
            wiki_root,
            wiki_path,
            markdown,
        )
        row = UserJudgment(
            id=judgment_id,
            actor_id=payload["actor_id"],
            agent_id=payload["agent_id"],
            agent_version=payload["agent_version"],
            forecast_id=forecast.id,
            run_id=run_row.id,
            mode=run_row.mode,
            index_code=forecast.index_code,
            horizon=forecast.horizon,
            target_date=forecast.target_date,
            direction=payload["direction"],
            confidence=0.67,
            rationale=payload["rationale"],
            counter_evidence=payload["counter_evidence"],
            invalidation_condition=payload["invalidation_condition"],
            blind_attestation=False,
            submitted_at=submitted_at,
            submission_deadline=None,
            formal_score_eligible=False,
            run_input_hash=run_row.input_hash,
            forecast_input_hash=forecast.input_hash,
            policy_version=USER_JUDGMENT_POLICY_V1,
            content_hash=content_hash,
            wiki_path=wiki_path,
            wiki_artifact_hash=artifact_hash,
            forecast=forecast,
        )
        session.add(row)
        session.commit()

        verified = verify_user_judgment(
            row,
            wiki_root=wiki_root,
            timezone=client.app.state.settings.timezone,
        )

    assert verified.encode("utf-8") == markdown
    assert (wiki_root / wiki_path).read_bytes() == markdown
    assert "不是 VeriCouncil 正式" in verified

    retried = client.post(
        "/api/user-judgments",
        json={
            "forecast_id": payload["forecast_id"],
            "direction": payload["direction"],
            "confidence": 0.67,
            "rationale": payload["rationale"],
            "counter_evidence": payload["counter_evidence"],
            "invalidation_condition": payload["invalidation_condition"],
            "blind_attestation": payload["blind_attestation"],
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["id"] == judgment_id
    assert retried.json()["policy_version"] == USER_JUDGMENT_POLICY_V1


def test_live_blind_judgment_gets_derived_shadow_score(
    client: TestClient,
    monkeypatch,
) -> None:
    run = _create_demo_run(client)
    now = datetime.now(ZONE).replace(microsecond=0)
    with client.app.state.database.session_factory() as session:
        run_row = session.get(WorkflowRun, run["id"])
        assert run_row is not None
        run_row.mode = "live"
        run_row.completed_at = now - timedelta(minutes=5)
        forecasts = sorted(
            run_row.forecasts,
            key=lambda item: (item.index_code, item.horizon),
        )
        forecast = forecasts[0]
        forecast.target_date = (now + timedelta(days=2)).date()
        session.commit()
        forecast_id = forecast.id
        horizon = forecast.horizon

    _enable_live_http(client)
    response = client.post(
        "/api/user-judgments",
        json=_payload(forecast_id, blind=True),
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 201, response.text
    judgment = response.json()
    assert judgment["formal_score_eligible"] is True
    original_actor = client.app.state.settings.user_judgment_actor_id
    client.app.state.settings.user_judgment_actor_id = "second-local-operator"
    second = client.post(
        "/api/user-judgments",
        json={**_payload(forecast_id, blind=True), "direction": "down"},
        headers=OPERATOR_HEADERS,
    )
    assert second.status_code == 201, second.text
    client.app.state.settings.user_judgment_actor_id = original_actor

    with client.app.state.database.session_factory() as session:
        forecast = session.get(Forecast, forecast_id)
        assert forecast is not None
        evaluation = EvaluationResult(
            id="user-judgment-evaluation-result",
            forecast_id=forecast.id,
            actual_return=0.02,
            actual_label="up",
            correct=forecast.direction == "up",
            brier_score=0.2,
            evaluated_at=now,
            price_source="test",
            observed_at=now,
            start_trade_date=forecast.base_trade_date,
            start_close=100.0,
            start_source_url="https://example.com/start",
            start_source_hash="a" * 64,
            end_trade_date=forecast.target_date,
            end_close=102.0,
            end_source_url="https://example.com/end",
            end_source_hash="b" * 64,
            observation_hash="c" * 64,
        )
        batch = EvaluationBatch(
            id="user-judgment-evaluation-batch",
            target_date=forecast.target_date,
            horizon=forecast.horizon,
            status="completed",
            evaluation_set_hash="d" * 64,
            source_hash="e" * 64,
            data_quality={},
            started_at=now,
            completed_at=now,
            error=None,
        )
        session.add_all([evaluation, batch])
        session.flush()
        with monkeypatch.context() as patch:
            patch.setattr(
                user_judgment_service,
                "USER_JUDGMENT_AGENT",
                replace(
                    user_judgment_service.USER_JUDGMENT_AGENT,
                    version="0.2.0",
                ),
            )
            rows = materialize_user_judgment_evaluation(
                session,
                batch=batch,
                forecast=forecast,
                evaluation=evaluation,
                material_outcome=True,
                now=now,
            )
        assert len(rows) == 2
        session.commit()

    scorecard = client.get(
        f"/api/agents/user_judgment_agent/scorecard?horizon={horizon}"
    )
    assert scorecard.status_code == 200
    body = scorecard.json()
    assert body["sample_size"] == 1
    assert body["sign_accuracy"] == 1
    assert body["material_direction_accuracy"] == 1
    assert body["average_brier"] is None
    assert body["sample_sufficient"] is False

    with client.app.state.database.session_factory() as session:
        scored = session.get(UserJudgment, judgment["id"])
        assert scored is not None
        scored.forecast.run.market_universe_hash = "f" * 64
        session.commit()
    foreign_universe_scorecard = client.get(
        f"/api/agents/user_judgment_agent/scorecard?horizon={horizon}"
    )
    assert foreign_universe_scorecard.status_code == 200
    assert foreign_universe_scorecard.json()["sample_size"] == 0

    with client.app.state.database.session_factory() as session:
        sealed = session.get(UserJudgment, judgment["id"])
        assert sealed is not None
        assert sealed.evaluation is not None
        sealed.forecast.run.market_universe_hash = (
            DEFAULT_MARKET_UNIVERSE.content_hash
        )
        sealed.evaluation.actual_label = "down"
        session.commit()
    integrity_failure = client.get(
        f"/api/agents/user_judgment_agent/scorecard?horizon={horizon}"
    )
    assert integrity_failure.status_code == 409


def test_cross_universe_forecast_cannot_receive_user_judgment(
    client: TestClient,
) -> None:
    run = _create_demo_run(client)
    with client.app.state.database.session_factory() as session:
        row = session.get(WorkflowRun, run["id"])
        assert row is not None
        forecast_id = row.forecasts[0].id
        row.market_universe_hash = "f" * 64
        session.commit()

    response = client.post(
        "/api/user-judgments",
        json=_payload(forecast_id),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Forecast not found"}


def test_live_judgment_remains_open_until_target_market_open(
    client: TestClient,
) -> None:
    run = _create_demo_run(client)
    now = datetime.now(ZONE).replace(microsecond=0)
    with client.app.state.database.session_factory() as session:
        run_row = session.get(WorkflowRun, run["id"])
        assert run_row is not None
        run_row.mode = "live"
        run_row.completed_at = now - timedelta(hours=3)
        forecast = run_row.forecasts[0]
        forecast.target_date = (now + timedelta(days=1)).date()
        forecast_id = forecast.id
        session.commit()

    _enable_live_http(client)
    response = client.post(
        "/api/user-judgments",
        json=_payload(forecast_id, blind=True),
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 201, response.text
    assert response.json()["submission_deadline"].endswith("09:30:00+08:00")


def test_live_judgment_closes_at_target_market_open(
    client: TestClient,
) -> None:
    run = _create_demo_run(client)
    now = datetime.now(ZONE).replace(microsecond=0)
    with client.app.state.database.session_factory() as session:
        run_row = session.get(WorkflowRun, run["id"])
        assert run_row is not None
        run_row.mode = "live"
        run_row.completed_at = now - timedelta(days=2)
        forecast = run_row.forecasts[0]
        forecast.target_date = (now - timedelta(days=1)).date()
        forecast_id = forecast.id
        session.commit()

    _enable_live_http(client)
    response = client.post(
        "/api/user-judgments",
        json=_payload(forecast_id, blind=True),
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 409
    assert "window has closed" in response.text
