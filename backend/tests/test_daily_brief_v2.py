from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.config import Settings
from app.db import Database
from app.models import AgentSignalV2Record, ForecastV2, ResearchRunV2
from app.services.daily_brief_v2 import (
    DailyBriefV2Error,
    FeishuOwnerConfig,
    FeishuOwnerSender,
    build_latest_daily_brief,
    load_feishu_owner_config,
    publish_daily_brief,
)

ZONE = ZoneInfo("Asia/Shanghai")
RUN_ID = "11111111-1111-4111-8111-111111111111"
FORECAST_ID = "22222222-2222-4222-8222-222222222222"


def test_daily_brief_uses_trusted_strategy_and_risk_rows(tmp_path) -> None:
    database, settings = _database(tmp_path)
    _seed_forecast(database)

    brief = build_latest_daily_brief(database, settings)

    assert brief.forecast_id == FORECAST_ID
    assert brief.direction == "up"
    assert brief.sample_number == 1
    assert "【forecast-loop｜中证1000预测日报】" in brief.text
    assert "结论：偏涨" in brief.text
    assert "涨 41.0%｜小波动 34.0%｜跌 25.0%" in brief.text
    assert "唯一有效短周期信号是中证1000当日绝对及相对走强" in brief.text
    assert "强势可能在下一交易日有限延续" in brief.text
    assert "主要风险：中等；近十日相对超额可能提高拥挤风险。" in brief.text
    assert "V2 Shadow 第 1/20 个前瞻样本" in brief.text
    assert "数据截至 17:44" in brief.text
    assert "Forecast ID: 22222222" in brief.text
    assert brief.content_hash not in brief.text


def test_brief_rejects_nonexistent_run(tmp_path) -> None:
    database, settings = _database(tmp_path)
    _seed_forecast(database)

    with pytest.raises(DailyBriefV2Error, match="no completed Live"):
        build_latest_daily_brief(database, settings, run_id="missing")


def test_owner_config_reads_only_explicit_owner_fields(tmp_path) -> None:
    env_file = tmp_path / "signal.env"
    env_file.write_text(
        """FORECAST_LOOP_FEISHU_APP_ID='cli_test'
FORECAST_LOOP_FEISHU_APP_SECRET="secret"
FORECAST_LOOP_FEISHU_OWNER_ID_TYPE=open_id
FORECAST_LOOP_FEISHU_OWNER_ID=ou_owner
FORECAST_LOOP_FEISHU_GROUP_CHAT_IDS=oc_group
""",
        encoding="utf-8",
    )

    config = load_feishu_owner_config(env_file)

    assert config.receive_id_type == "open_id"
    assert "ou_owner" not in repr(config)
    assert "secret" not in repr(config)


def test_owner_delivery_is_idempotent_and_never_sends_to_group(tmp_path) -> None:
    database, settings = _database(tmp_path)
    _seed_forecast(database)
    brief = build_latest_daily_brief(database, settings)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    config = FeishuOwnerConfig("cli_test", "secret", "open_id", "ou_owner")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = FeishuOwnerSender(config, client=client)

    first = publish_daily_brief(brief, config, state_root=tmp_path / "state", sender=sender)
    duplicate = replace(
        brief,
        forecast_id="33333333-3333-4333-8333-333333333333",
        content_hash="3" * 64,
    )
    second = publish_daily_brief(
        duplicate,
        config,
        state_root=tmp_path / "state",
        sender=sender,
    )

    assert first.status == "sent"
    assert second.status == "already_sent"
    assert len(calls) == 2
    message = json.loads(calls[1].content)
    assert message["receive_id"] == "ou_owner"
    assert message["msg_type"] == "text"
    assert "oc_group" not in json.dumps(message)
    marker = json.loads(first.marker.read_text(encoding="utf-8"))
    assert first.marker.parent.name == "2026-08-18"
    assert first.marker.parents[1].name == "feishu-owner"
    assert marker["forecast_id"] == FORECAST_ID
    assert marker["brief_hash"] == brief.content_hash


def test_legacy_forecast_marker_prevents_resend(tmp_path) -> None:
    database, settings = _database(tmp_path)
    _seed_forecast(database)
    brief = build_latest_daily_brief(database, settings)
    config = FeishuOwnerConfig("cli_test", "secret", "open_id", "ou_owner")
    destination_hash = hashlib.sha256(b"open_id:ou_owner").hexdigest()[:16]
    legacy_marker = (
        tmp_path
        / "state"
        / "feishu-owner"
        / brief.forecast_id
        / f"owner-{destination_hash}.json"
    )
    legacy_marker.parent.mkdir(parents=True)
    legacy_marker.write_text(
        json.dumps({"target_date": brief.target_date.isoformat()}),
        encoding="utf-8",
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    sender = FeishuOwnerSender(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = publish_daily_brief(
        brief,
        config,
        state_root=tmp_path / "state",
        sender=sender,
    )

    assert result.status == "already_sent"
    assert result.marker == legacy_marker
    assert calls == []


def test_failed_delivery_writes_no_marker_or_secret(tmp_path) -> None:
    database, settings = _database(tmp_path)
    _seed_forecast(database)
    brief = build_latest_daily_brief(database, settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        return httpx.Response(200, json={"code": 230001, "msg": "invalid receive id"})

    config = FeishuOwnerConfig("cli_test", "sensitive-secret", "open_id", "ou_owner")
    sender = FeishuOwnerSender(config, client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(DailyBriefV2Error, match="code=230001") as error:
        publish_daily_brief(brief, config, state_root=tmp_path / "state", sender=sender)

    assert "sensitive-secret" not in str(error.value)
    assert not list((tmp_path / "state").rglob("*.json"))


def _database(tmp_path) -> tuple[Database, Settings]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        auto_seed=False,
        reflection_shadow_target_dates=20,
    )
    database = Database(settings.database_url)
    database.create_all()
    return database, settings


def _seed_forecast(database: Database) -> None:
    created_at = datetime(2026, 8, 17, 18, 38, tzinfo=ZONE)
    with database.session_factory.begin() as session:
        session.add(
            ResearchRunV2(
                id=RUN_ID,
                schema_version="forecast-loop.research-run/v2",
                program_hash="a" * 64,
                snapshot_hash="b" * 64,
                input_hash="c" * 64,
                request_hash="d" * 64,
                mode="live",
                status="completed",
                anchor_date=date(2026, 8, 17),
                as_of=datetime(2026, 8, 17, 17, 55, tzinfo=ZONE),
                data_cutoff=datetime(2026, 8, 17, 17, 44, tzinfo=ZONE),
                prepared_at=datetime(2026, 8, 17, 17, 55, tzinfo=ZONE),
                completed_at=created_at,
                error=None,
                program={},
                snapshot={},
                receipt={},
            )
        )
        strategy = _signal(
            signal_id="strategy-signal",
            agent_id="strategy_agent",
            signal_kind="strategy_forecast",
            content_hash="e" * 64,
            created_at=created_at,
            draft={
                "rationale": "策略依据冻结行情形成轻度偏涨判断。",
                "transmission_chain": [
                    "宏观与产业输入对D1均为no-impact",
                    "唯一有效短周期信号是中证1000当日绝对及相对走强",
                    "缺少反向新增事件时，强势可能在下一交易日有限延续",
                ],
            },
        )
        critic = _signal(
            signal_id="critic-signal",
            agent_id="risk_critic_agent",
            signal_kind="risk_critique",
            content_hash="f" * 64,
            created_at=created_at,
            draft={
                "risk_severity": "medium",
                "counter_evidence": [
                    "两项行情证据共享同一数据入口。",
                    "近十日相对超额可能提高拥挤风险。",
                ],
            },
        )
        cio = _signal(
            signal_id="cio-signal",
            agent_id="cio_agent",
            signal_kind="decision_forecast",
            content_hash="1" * 64,
            created_at=created_at,
            draft={},
        )
        session.add_all((strategy, critic, cio))
        session.add(
            ForecastV2(
                id=FORECAST_ID,
                run_id=RUN_ID,
                source_signal_id=cio.id,
                schema_version="forecast-loop.forecast/v2",
                program_hash="a" * 64,
                target_id="csi1000-absolute-d1",
                horizon="D1",
                configured_lane="formal",
                effective_lane="shadow",
                anchor_date=date(2026, 8, 17),
                target_date=date(2026, 8, 18),
                probability_up=0.41,
                probability_neutral=0.34,
                probability_down=0.25,
                threshold=0.005,
                baseline_probabilities={"up": 1 / 3, "neutral": 1 / 3, "down": 1 / 3},
                rationale="deterministic CIO rationale",
                counter_evidence=[],
                invalidation_conditions=[],
                input_hash="c" * 64,
                content_hash="2" * 64,
                created_at=created_at,
            )
        )


def _signal(
    *,
    signal_id: str,
    agent_id: str,
    signal_kind: str,
    content_hash: str,
    created_at: datetime,
    draft: dict[str, object],
) -> AgentSignalV2Record:
    return AgentSignalV2Record(
        id=signal_id,
        run_id=RUN_ID,
        schema_version="forecast-loop.agent-signal/v2",
        agent_id=agent_id,
        agent_version="0.2.0",
        model_name="gpt-5.6-sol",
        prompt_version="forecast-loop.codex-handoff/v3",
        target_id="csi1000-absolute-d1",
        signal_kind=signal_kind,
        natural_horizon="D1",
        decision_horizon="D1",
        anchor_date=date(2026, 8, 17),
        target_date=date(2026, 8, 18),
        evidence_cutoff=datetime(2026, 8, 17, 17, 44, tzinfo=ZONE),
        program_hash="a" * 64,
        input_hash="c" * 64,
        threshold=0.005,
        baseline_probabilities={"up": 1 / 3, "neutral": 1 / 3, "down": 1 / 3},
        state_available=True,
        abstain=False,
        content_hash=content_hash,
        envelope={"draft": draft},
        created_at=created_at,
    )
