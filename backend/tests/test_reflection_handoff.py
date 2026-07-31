from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import app.services.reflection_handoff as reflection_handoff_module
import pytest
from app.domain import AgentDefinition, AgentSourceType, AgentWorkflowRole, Horizon
from app.models import (
    AgentOpinion,
    EvaluationBatch,
    Forecast,
    LessonProposal,
    ReflectionFinding,
    ReflectionRun,
    WorkflowRun,
)
from app.services.evaluation import evaluate_forecast
from app.services.reflection_handoff import (
    LEGACY_REFLECTION_PROTOCOL_VERSION,
    REFLECTION_PROTOCOL_VERSION,
    MarketSnapshotBundleInput,
    _canonical_hash,
    _expected_direction_result,
    finalize_reflection,
    freeze_reflection_sources,
    prepare_reflection,
)
from app.workflow import CommitteeWorkflow
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

ZONE = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 10, 15, 0, tzinfo=ZONE)


def _source_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


@pytest.mark.parametrize(
    ("predicted", "actual_return", "expected"),
    [
        ("up", 0.001, "right_but_noise"),
        ("down", 0.001, "wrong_noise"),
        ("down", -0.001, "right_but_noise"),
        ("up", -0.001, "wrong_noise"),
        ("up", 0.0, "zero_return"),
    ],
)
def test_noise_verdict_is_determined_by_realized_sign(
    predicted: str,
    actual_return: float,
    expected: str,
) -> None:
    assert (
        _expected_direction_result(
            predicted_direction=predicted,
            actual_label=reflection_handoff_module.Direction.NEUTRAL,
            actual_return=actual_return,
        )
        == expected
    )


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_evaluated_live_run(
    client,
    tmp_path: Path,
    *,
    as_of: datetime = AS_OF,
    horizon: Horizon = Horizon.D1,
) -> tuple[str, Path, datetime]:
    if horizon is Horizon.D2:
        legacy_settings = client.app.state.settings.model_copy(
            update={"checkpoint_path": tmp_path / "legacy-reflection-checkpoint.sqlite3"}
        )
        legacy_workflow = CommitteeWorkflow(
            settings=legacy_settings,
            database=client.app.state.database,
            provider=client.app.state.workflow.provider,
            wiki=client.app.state.workflow.wiki,
            runtime_mode="legacy_dual_horizon",
        )
        try:
            run_id = legacy_workflow.run(as_of=as_of).id
        finally:
            legacy_workflow.close()
    else:
        response = client.post("/api/runs", json={"as_of": as_of.isoformat()})
        assert response.status_code == 201, response.text
        run_id = response.json()["id"]
    database = client.app.state.database
    with database.session_factory() as session:
        run = session.scalar(
            select(WorkflowRun)
            .options(
                selectinload(WorkflowRun.forecasts).selectinload(Forecast.evaluation),
                selectinload(WorkflowRun.opinions),
            )
            .where(WorkflowRun.id == run_id)
        )
        assert run is not None
        run.mode = "live"
        forecasts = sorted(
            (item for item in run.forecasts if item.horizon == horizon.value),
            key=lambda item: item.index_code,
        )
        target_date = forecasts[0].target_date
        observed_at = datetime.combine(target_date, time(15, 10), tzinfo=ZONE)
        prepared_at = observed_at + timedelta(minutes=10)
        for forecast in forecasts:
            evaluate_forecast(
                session,
                forecast=forecast,
                price_source="trusted-reflection-test",
                observed_at=observed_at,
                start_trade_date=forecast.base_trade_date,
                start_close=100.0,
                start_source_url="https://www.csindex.com.cn/start",
                start_source_hash=_source_hash(f"{forecast.index_code}-start"),
                end_trade_date=forecast.target_date,
                end_close=97.0,
                end_source_url="https://www.csindex.com.cn/end",
                end_source_hash=_source_hash(f"{forecast.index_code}-end"),
                now=prepared_at,
                trusted_sources_only=True,
            )
        for opinion in run.opinions:
            opinion.raw_response = {
                **(opinion.raw_response or {}),
                "evidence_item_ids": ["evidence-used"],
            }
        source_input_hash = run.input_hash
        session.commit()

    prediction_handoff_root = tmp_path / "prediction-handoffs"
    prediction_handoff_root.mkdir()
    client.app.state.settings.handoff_root = prediction_handoff_root
    prediction_job = prediction_handoff_root / run_id
    prediction_job.mkdir()
    _json_write(
        prediction_job / "input.json",
        {
            "run_id": run_id,
            "input_hash": source_input_hash,
            "initial_state": {
                "evidence_snapshot": {
                    "items": [
                        {"id": "evidence-used"},
                        {"id": "evidence-missed"},
                    ]
                }
            },
        },
    )

    snapshot_payload: dict[str, Any] = {
        "protocol_version": "2.0.0",
        "target_date": target_date.isoformat(),
        "horizon": horizon.value,
        "captured_at": observed_at.isoformat(),
        "data_quality": {
            "status": "passed",
            "source_id": "synthetic-market-source",
            "policy_version": "synthetic-quality-v1",
            "checked_at": observed_at.isoformat(),
            "report_hash": _source_hash("quality-report"),
            "checks": {
                "quality_policy_passed": True,
                "target_session_published": True,
                "target_calendar_open": True,
                "required_instruments_complete": True,
                "outcome_metrics_complete": True,
                "publication_freshness_passed": True,
            },
            "warnings": [],
        },
        "trading_calendar": {
            "target_date": target_date.isoformat(),
            "calendar_id": "synthetic-exchange-calendar",
            "is_open": True,
            "source_url": "https://www.sse.com.cn/calendar",
            "source_hash": _source_hash("calendar"),
            "observed_at": datetime.combine(
                target_date,
                time(15, 5),
                tzinfo=ZONE,
            ).isoformat(),
        },
        "publication": {
            "source_id": "synthetic-market-source",
            "artifact_hashes": {
                "index-history": _source_hash("index-partition"),
                "publication-receipt": _source_hash("ingest-manifest"),
                "market-breadth": _source_hash("breadth-file"),
                "market-limits": _source_hash("limit-file"),
            },
        },
        "items": [],
        "content_hash": "0" * 64,
    }
    for index_code, index_name in (
        ("000300.SH", "沪深300"),
        ("000905.SH", "中证500"),
        ("000852.SH", "中证1000"),
        ("399006.SZ", "创业板指"),
        ("000688.SH", "科创50"),
    ):
        snapshot_payload["items"].append(
            {
                "index_code": index_code,
                "index_name": index_name,
                "target_date": target_date.isoformat(),
                "base_trade_date": forecasts[0].base_trade_date.isoformat(),
                "base_close": 100.0,
                "target_close": 97.0,
                "actual_return": -0.03,
                "source_url": "https://www.csindex.com.cn/end",
                "source_hash": _source_hash(f"{index_code}-end"),
                "base_source_url": "https://www.csindex.com.cn/start",
                "base_source_hash": _source_hash(f"{index_code}-start"),
                "target_source_url": "https://www.csindex.com.cn/end",
                "target_source_hash": _source_hash(f"{index_code}-end"),
                "captured_at": observed_at.isoformat(),
                "amount": 1_000_000_000.0,
                "advancers": 300,
                "decliners": 4700,
                "unchanged": 50,
                "limit_down_count": 180,
                "breadth_down_ratio": 0.94,
                "sector_contributions": [],
                "weight_contributions": [],
                "historical_abs_return_percentile": 0.995,
                "history_sample_size": 1250,
            }
        )
    unsigned = MarketSnapshotBundleInput.model_validate(snapshot_payload)
    snapshot_payload["content_hash"] = _canonical_hash(
        unsigned.model_dump(
            mode="json",
            exclude={"content_hash"},
            exclude_none=True,
        )
    )
    market_snapshot_root = tmp_path / "market-snapshots"
    market_snapshot_root.mkdir()
    client.app.state.settings.market_snapshot_root = market_snapshot_root
    snapshot_path = market_snapshot_root / "market-snapshot.json"
    _json_write(snapshot_path, snapshot_payload)
    return run_id, snapshot_path, prepared_at


def _write_empty_discovery(job_dir: Path, generated_at: datetime) -> None:
    request = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    _json_write(
        job_dir / "source-discovery" / "drafts.json",
        {
            "protocol_version": request["protocol_version"],
            "reflection_id": request["reflection_id"],
            "request_hash": request["request_hash"],
            "generated_at": generated_at.isoformat(),
            "generated_by": {
                "surface": "codex",
                "task_id": "reflection-test",
                "model": "example-model",
                "reasoning_effort": "high",
            },
            "candidates": [],
        },
    )


def _write_discovery_and_captures(
    job_dir: Path,
    generated_at: datetime,
) -> Path:
    request = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat(request["source_run"]["data_cutoff"])
    target_close = datetime.combine(
        datetime.fromisoformat(request["target_date"]).date(),
        time(15, 0),
        tzinfo=ZONE,
    )
    source_specs = [
        (
            "source-pre-cutoff",
            "https://www.sse.com.cn/pre-cutoff",
            cutoff - timedelta(minutes=10),
        ),
        (
            "source-post-cutoff",
            "https://www.sse.com.cn/post-cutoff",
            cutoff + timedelta(minutes=10),
        ),
        (
            "source-after-close",
            "https://www.sse.com.cn/after-close",
            target_close + timedelta(minutes=5),
        ),
    ]
    candidates = [
        {
            "candidate_id": source_id,
            "source_url": source_url,
            "source_kind": "official_event",
            "related_index_codes": ["000300.SH"],
            "rationale": "用于验证反省来源时间边界。",
        }
        for source_id, source_url, _ in source_specs
    ]
    _json_write(
        job_dir / "source-discovery" / "drafts.json",
        {
            "protocol_version": request["protocol_version"],
            "reflection_id": request["reflection_id"],
            "request_hash": request["request_hash"],
            "generated_at": generated_at.isoformat(),
            "generated_by": {
                "surface": "codex",
                "task_id": "reflection-source-test",
                "model": "example-model",
                "reasoning_effort": "high",
            },
            "candidates": candidates,
        },
    )

    capture_items: list[dict[str, Any]] = []
    for source_id, source_url, published_at in source_specs:
        unsigned = {
            "id": source_id,
            "title": source_id,
            "summary": "可信采集器冻结的测试来源。",
            "quote": "测试事实摘要。",
            "source_url": source_url,
            "event_time": (published_at - timedelta(minutes=1)).isoformat(),
            "published_at": published_at.isoformat(),
            "ingested_at": (target_close + timedelta(minutes=10)).isoformat(),
            "source_kind": "official_event",
            "related_index_codes": ["000300.SH"],
        }
        capture_items.append(
            {**unsigned, "content_hash": _canonical_hash(unsigned)}
        )
    captures_path = job_dir.parent / f"{job_dir.name}-captures.json"
    _json_write(
        captures_path,
        {
            "protocol_version": request["protocol_version"],
            "reflection_id": request["reflection_id"],
            "captured_at": (target_close + timedelta(minutes=11)).isoformat(),
            "items": capture_items,
        },
    )
    return captures_path


def _fill_unresolved_analysis(job_dir: Path, generated_at: datetime) -> dict[str, Any]:
    request = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    expected_by_finding = {
        item["finding_key"]: item["expected_direction_result"]
        for item in request["assignments"]
    }
    template = json.loads(
        (job_dir / "analysis" / "drafts.template.json").read_text(encoding="utf-8")
    )
    findings = [
        *template["agent_findings"],
        *template["committee_findings"],
        template["market_finding"],
    ]
    for finding in findings:
        finding["primary_error_type"] = "unresolved"
        finding["secondary_error_types"] = []
        finding["causal_status"] = "unresolved"
        finding["summary"] = "没有冻结事后解释来源，因此原因保持未解决。"
        finding["what_was_right"] = []
        finding["what_was_wrong"] = []
        finding["original_evidence_item_ids"] = []
        finding["missed_evidence_item_ids"] = []
        finding["source_ids"] = []
        finding["invalidation_conditions_triggered"] = []
        finding["remediation"] = ["补齐可信事后来源后创建新的审计版本。"]
        finding["counterfactual"] = {
            "direction": "not_applicable",
            "probabilities": None,
            "would_flip": None,
            "basis": "not_applicable",
            "explanation": "资料不足，不构造反事实。",
        }
        finding["confidence"] = 0.0
        expected_result = expected_by_finding[finding["finding_key"]]
        finding["outcome_verdict"] = {
            "wrong": "wrong",
            "right_but_noise": "right_but_noise",
            "wrong_noise": "wrong_noise",
            "zero_return": "not_applicable",
        }.get(expected_result, "unresolved")
    template["generated_at"] = generated_at.isoformat()
    template["generated_by"] = {
        "surface": "codex",
        "task_id": "reflection-analysis-test",
        "model": "example-model",
        "reasoning_effort": "high",
    }
    _json_write(job_dir / "analysis" / "drafts.json", template)
    return template


def _add_lesson_proposal(
    payload: dict[str, Any],
    *,
    recurrence_key: str = "risk.systemic-breadth",
) -> None:
    payload["lesson_proposals"] = [
        {
            "lesson_key": "lesson.systemic-breadth",
            "title": "系统性下跌时扩大风险覆盖",
            "lesson_type": "risk_check",
            "summary": "五个宽基同步下跌时，应单独审查市场广度与跌停扩散。",
            "proposed_action": "在后续回放中加入广度恶化的失效条件。",
            "supporting_finding_keys": [
                payload["committee_findings"][0]["finding_key"]
            ],
            "source_ids": [],
            "recurrence_key": recurrence_key,
            "promotion_status": "proposed",
        }
    ]


def _full_handoff(
    client,
    tmp_path: Path,
    *,
    as_of: datetime = AS_OF,
    horizon: Horizon = Horizon.D1,
):
    run_id, market_snapshot, prepared_at = _prepare_evaluated_live_run(
        client,
        tmp_path,
        as_of=as_of,
        horizon=horizon,
    )
    settings = client.app.state.settings
    reflection_root = tmp_path / "reflections"
    job_dir = prepare_reflection(
        settings,
        run_id,
        horizon=horizon,
        market_snapshot_path=market_snapshot,
        now=prepared_at,
        output_root=reflection_root,
    )
    _write_empty_discovery(job_dir, prepared_at + timedelta(minutes=1))
    freeze_reflection_sources(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=2),
        output_root=reflection_root,
    )
    return settings, reflection_root, job_dir, prepared_at


def _full_handoff_with_sources(client, tmp_path: Path):
    run_id, market_snapshot, prepared_at = _prepare_evaluated_live_run(
        client,
        tmp_path,
    )
    settings = client.app.state.settings
    reflection_root = tmp_path / "reflections"
    job_dir = prepare_reflection(
        settings,
        run_id,
        horizon=Horizon.D1,
        market_snapshot_path=market_snapshot,
        now=prepared_at,
        output_root=reflection_root,
    )
    captures_path = _write_discovery_and_captures(
        job_dir,
        prepared_at + timedelta(minutes=1),
    )
    snapshot = freeze_reflection_sources(
        settings,
        job_dir,
        sources_path=captures_path,
        now=prepared_at + timedelta(minutes=2),
        output_root=reflection_root,
    )
    return settings, reflection_root, job_dir, prepared_at, snapshot


def test_reflection_handoff_persists_exact_findings_and_receipt(
    client,
    tmp_path: Path,
) -> None:
    settings, reflection_root, job_dir, prepared_at = _full_handoff(client, tmp_path)
    _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))

    receipt = finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=reflection_root,
    )

    assert receipt.status == "completed"
    assert receipt.protocol_version == REFLECTION_PROTOCOL_VERSION
    assert receipt.finding_count == 31
    request = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    assert request["protocol_version"] == REFLECTION_PROTOCOL_VERSION
    assert request["provider"] == "codex-reflection-file-v2"
    assert (
        job_dir / "source-discovery" / "INSTRUCTIONS.md"
    ).read_text(encoding="utf-8").startswith("# forecast-loop 反省：来源发现")
    assert (job_dir / "receipt.json").is_file()
    settings.reflection_root = reflection_root
    detail = client.get(f"/api/reflections/{job_dir.name}")
    assert detail.status_code == 200, detail.text
    first_finding = detail.json()["findings"][0]
    assert first_finding["what_was_right"] == []
    assert first_finding["what_was_wrong"] == []
    assert first_finding["original_evidence_item_ids"] == []
    assert first_finding["missed_evidence_item_ids"] == []
    assert first_finding["source_ids"] == []
    with client.app.state.database.session_factory() as session:
        row = session.get(ReflectionRun, str(receipt.reflection_id))
        assert row is not None
        assert row.status == "completed"
        assert row.source_snapshot_hash
        assert row.output_hash == receipt.output_hash
        assert row.receipt_hash == receipt.receipt_hash
        count = session.scalar(
            select(func.count())
            .select_from(ReflectionFinding)
            .where(ReflectionFinding.reflection_run_id == row.id)
        )
        assert count == 31
        persisted = session.scalars(
            select(ReflectionFinding).where(
                ReflectionFinding.reflection_run_id == row.id
            )
        ).all()
        assert {item.scope_type for item in persisted} == {
            "agent",
            "committee",
            "market_event",
        }
        assert {
            item.subject_id for item in persisted if item.scope_type == "agent"
        } == {
            "macro_policy_agent",
            "market_news_agent",
            "ai_storage_industry_agent",
            "strategy_agent",
            "risk_critic_agent",
        }
        assert {
            item.subject_id
            for item in persisted
            if item.scope_type == "committee"
        } == {"committee"}
        assert {
            item.subject_id
            for item in persisted
            if item.scope_type == "market_event"
        } == {"market_event"}
        assert all(
            item.counterfactual["reflection_metadata"]
            == {
                "what_was_right": [],
                "what_was_wrong": [],
                "invalidation_conditions_triggered": [],
                "original_evidence_item_ids": [],
                "missed_evidence_item_ids": [],
                "source_ids": [],
            }
            for item in persisted
        )


def test_legacy_reflection_v1_package_keeps_original_prompts_and_finalizes(
    client,
    tmp_path: Path,
) -> None:
    run_id, market_snapshot, prepared_at = _prepare_evaluated_live_run(
        client,
        tmp_path,
    )
    settings = client.app.state.settings
    reflection_root = tmp_path / "legacy-reflections"
    job_dir = prepare_reflection(
        settings,
        run_id,
        horizon=Horizon.D1,
        market_snapshot_path=market_snapshot,
        protocol_version=LEGACY_REFLECTION_PROTOCOL_VERSION,
        now=prepared_at,
        output_root=reflection_root,
    )
    request = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    assert request["protocol_version"] == "1.0.0"
    assert request["provider"] == "codex-reflection-file-v1"
    frozen_input = (job_dir / "input.json").read_bytes()
    resumed_job = prepare_reflection(
        settings,
        run_id,
        horizon=Horizon.D1,
        market_snapshot_path=market_snapshot,
        now=prepared_at,
        output_root=reflection_root,
    )
    assert resumed_job == job_dir
    assert (job_dir / "input.json").read_bytes() == frozen_input
    discovery_instructions = (
        job_dir / "source-discovery" / "INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")
    assert discovery_instructions.startswith("# VeriCouncil 反省：来源发现")
    assert "forecast-loop" not in discovery_instructions

    _write_empty_discovery(job_dir, prepared_at + timedelta(minutes=1))
    freeze_reflection_sources(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=2),
        output_root=reflection_root,
    )
    analysis_instructions = (
        job_dir / "analysis" / "INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")
    assert analysis_instructions.startswith("# VeriCouncil 反省：结构化分析")
    assert "forecast-loop" not in analysis_instructions
    _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))

    receipt = finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=reflection_root,
    )
    assert receipt.protocol_version == "1.0.0"
    assert receipt.status == "completed"


def test_reflection_assignments_follow_the_persisted_mature_agent_roster(
    client,
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, market_snapshot, prepared_at = _prepare_evaluated_live_run(
        client,
        tmp_path,
    )
    added_agent = AgentDefinition(
        id="liquidity_agent",
        name="流动性研究员",
        role="研究资金面与市场流动性。",
        kind="research",
        workflow_role=AgentWorkflowRole.RESEARCH,
        source_type=AgentSourceType.AI,
        version="0.1.0",
    )
    monkeypatch.setitem(
        reflection_handoff_module.AGENT_BY_ID,
        added_agent.id,
        added_agent,
    )
    with client.app.state.database.session_factory() as session:
        strategies = session.scalars(
            select(AgentOpinion).where(
                AgentOpinion.run_id == run_id,
                AgentOpinion.horizon == "D1",
                AgentOpinion.agent_id == "strategy_agent",
            )
        ).all()
        assert len(strategies) == 5
        for source in strategies:
            session.add(
                AgentOpinion(
                    id=str(uuid4()),
                    run_id=source.run_id,
                    agent_id=added_agent.id,
                    agent_name=added_agent.name,
                    role=added_agent.role,
                    agent_version=added_agent.version,
                    model_name=source.model_name,
                    status="active",
                    index_code=source.index_code,
                    horizon=source.horizon,
                    target_date=source.target_date,
                    direction=source.direction,
                    probability_up=source.probability_up,
                    probability_neutral=source.probability_neutral,
                    probability_down=source.probability_down,
                    summary="持久化的新增成熟 Agent 意见。",
                    evidence=list(source.evidence),
                    counter_evidence=list(source.counter_evidence),
                    invalidation_conditions=list(source.invalidation_conditions),
                    citations=list(source.citations),
                    contribution="流动性输入",
                    weight=0.1,
                    raw_response={"evidence_item_ids": ["evidence-used"]},
                )
            )
        session.commit()

    settings = client.app.state.settings
    reflection_root = tmp_path / "reflections"
    job_dir = prepare_reflection(
        settings,
        run_id,
        horizon="D1",
        market_snapshot_path=market_snapshot,
        now=prepared_at,
        output_root=reflection_root,
    )
    request = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    agent_assignments = [
        item for item in request["assignments"] if item["scope_type"] == "agent"
    ]
    assert len(agent_assignments) == 30
    assert {
        item["index_code"]
        for item in agent_assignments
        if item["agent_id"] == added_agent.id
    } == {
        "000300.SH",
        "000905.SH",
        "000852.SH",
        "399006.SZ",
        "000688.SH",
    }

    _write_empty_discovery(job_dir, prepared_at + timedelta(minutes=1))
    freeze_reflection_sources(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=2),
        output_root=reflection_root,
    )
    payload = _fill_unresolved_analysis(
        job_dir,
        prepared_at + timedelta(minutes=3),
    )
    assert len(payload["agent_findings"]) == 30
    receipt = finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=reflection_root,
    )
    assert receipt.finding_count == 36
    with client.app.state.database.session_factory() as session:
        row = session.get(ReflectionRun, str(receipt.reflection_id))
        assert row is not None
        count = session.scalar(
            select(func.count())
            .select_from(ReflectionFinding)
            .where(ReflectionFinding.reflection_run_id == row.id)
        )
        assert count == 36
        liquidity_findings = session.scalars(
            select(ReflectionFinding).where(
                ReflectionFinding.reflection_run_id == row.id,
                ReflectionFinding.subject_id == added_agent.id,
            )
        ).all()
        assert len(liquidity_findings) == 5


def test_prepare_without_trusted_market_snapshot_records_blocked_upstream(
    client,
    tmp_path: Path,
) -> None:
    run_id, _, prepared_at = _prepare_evaluated_live_run(client, tmp_path)

    with pytest.raises(ValueError, match="blocked|market-snapshot"):
        prepare_reflection(
            client.app.state.settings,
            run_id,
            horizon="D1",
            market_snapshot_path=None,
            now=prepared_at,
            output_root=tmp_path / "reflections",
        )

    with client.app.state.database.session_factory() as session:
        blocked = session.scalars(
            select(EvaluationBatch).where(EvaluationBatch.status == "blocked_upstream")
        ).all()
        assert len(blocked) == 1
        assert (
            session.scalar(select(func.count()).select_from(ReflectionRun)) == 0
        )


def test_prepare_reflection_rejects_custom_market_universe_source_run(
    client,
    tmp_path: Path,
) -> None:
    run_id, snapshot_path, prepared_at = _prepare_evaluated_live_run(
        client,
        tmp_path,
    )
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, run_id)
        assert run is not None
        run.market_universe_hash = "f" * 64
        session.commit()

    with pytest.raises(ValueError, match="default five-index A-share"):
        prepare_reflection(
            client.app.state.settings,
            run_id,
            horizon="D1",
            market_snapshot_path=snapshot_path,
            now=prepared_at,
            output_root=tmp_path / "reflections",
        )

    with client.app.state.database.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(EvaluationBatch)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(ReflectionRun)) == 0
        )


def test_market_snapshot_rejects_inconsistent_market_wide_statistics(
    client,
    tmp_path: Path,
) -> None:
    _, snapshot_path, _ = _prepare_evaluated_live_run(client, tmp_path)
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["items"][0]["decliners"] += 1

    with pytest.raises(ValueError, match="decliners.*match"):
        MarketSnapshotBundleInput.model_validate(payload)


def test_extreme_reflection_cannot_be_dismissed_as_noise(
    client,
    tmp_path: Path,
) -> None:
    settings, reflection_root, job_dir, prepared_at = _full_handoff(client, tmp_path)
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    extreme = next(
        item
        for item in payload["committee_findings"]
        if item["severity"] in {"large", "extreme", "systemic_extreme_down"}
    )
    extreme["primary_error_type"] = "market_noise"
    extreme["outcome_verdict"] = "wrong_noise"
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="noise|non-noise|extreme"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=reflection_root,
        )

    with client.app.state.database.session_factory() as session:
        row = session.get(ReflectionRun, job_dir.name)
        assert row is not None
        assert row.status == "awaiting_analysis"
        assert (
            session.scalar(select(func.count()).select_from(ReflectionFinding)) == 0
        )


def test_frozen_sources_classify_all_publication_time_windows(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, _, snapshot = _full_handoff_with_sources(
        client,
        tmp_path,
    )

    assert {item.id: item.time_class for item in snapshot.items} == {
        "source-pre-cutoff": "published_before_cutoff_not_frozen",
        "source-post-cutoff": "post_cutoff_preclose",
        "source-after-close": "post_close_explanation",
    }
    settings.reflection_root = root
    response = client.get(f"/api/reflections/{job_dir.name}")
    assert response.status_code == 200, response.text
    assert {
        item["id"]: item["time_class"]
        for item in response.json()["source_timeline"]
    } == {
        "source-pre-cutoff": "published_before_cutoff_not_frozen",
        "source-post-cutoff": "post_cutoff_preclose",
        "source-after-close": "post_close_explanation",
    }


def _correct_direction_finding(
    job_dir: Path,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = json.loads((job_dir / "input.json").read_text(encoding="utf-8"))
    assignment = next(
        item
        for item in request["assignments"]
        if item["expected_direction_result"] == "correct"
        and item["scope_type"] in {"agent", "committee"}
        and item.get("agent_id") != "risk_critic_agent"
    )
    findings = [*payload["agent_findings"], *payload["committee_findings"]]
    finding = next(
        item for item in findings if item["finding_key"] == assignment["finding_key"]
    )
    return finding, assignment


def test_empty_source_snapshot_rejects_right_reason(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, prepared_at = _full_handoff(client, tmp_path)
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    finding, assignment = _correct_direction_finding(job_dir, payload)
    finding["outcome_verdict"] = "right_reason"
    finding["causal_status"] = "supported"
    finding["original_evidence_item_ids"] = assignment["original_evidence_item_ids"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="without frozen outcome sources|unresolved"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )


def test_right_reason_requires_supported_original_and_outcome_evidence(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, prepared_at, _ = _full_handoff_with_sources(
        client,
        tmp_path,
    )
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    finding, assignment = _correct_direction_finding(job_dir, payload)
    finding["outcome_verdict"] = "right_reason"
    finding["causal_status"] = "hypothesis"
    finding["original_evidence_item_ids"] = assignment["original_evidence_item_ids"]
    finding["source_ids"] = ["source-after-close"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="supported or verified"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )

    finding["causal_status"] = "supported"
    finding["original_evidence_item_ids"] = []
    _json_write(job_dir / "analysis" / "drafts.json", payload)
    with pytest.raises(ValueError, match="original frozen evidence"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )

    finding["original_evidence_item_ids"] = assignment["original_evidence_item_ids"]
    finding["source_ids"] = []
    _json_write(job_dir / "analysis" / "drafts.json", payload)
    with pytest.raises(ValueError, match="frozen outcome source"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )

    finding["source_ids"] = ["source-after-close"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)
    receipt = finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=root,
    )
    assert receipt.status == "completed"


def test_lucky_correct_requires_error_and_frozen_outcome_source(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, prepared_at, _ = _full_handoff_with_sources(
        client,
        tmp_path,
    )
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    finding, _ = _correct_direction_finding(job_dir, payload)
    finding["outcome_verdict"] = "lucky_correct"
    finding["causal_status"] = "hypothesis"
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="identified original error"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )

    finding["what_was_wrong"] = ["原理由未受到实际行情事实支持。"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)
    with pytest.raises(ValueError, match="frozen outcome source"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )

    finding["source_ids"] = ["source-after-close"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)
    receipt = finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=root,
    )
    assert receipt.status == "completed"


def test_post_cutoff_source_is_not_eligible_missed_evidence(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, prepared_at, _ = _full_handoff_with_sources(
        client,
        tmp_path,
    )
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    risk = next(
        item
        for item in payload["agent_findings"]
        if item["agent_id"] == "risk_critic_agent"
    )
    risk["missed_evidence_item_ids"] = ["source-post-cutoff"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="invented or reused missed evidence"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )


def test_post_cutoff_source_cannot_share_a_finding_with_valid_missed_evidence(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, prepared_at, _ = _full_handoff_with_sources(
        client,
        tmp_path,
    )
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    risk = next(
        item
        for item in payload["agent_findings"]
        if item["agent_id"] == "risk_critic_agent"
    )
    risk["missed_evidence_item_ids"] = ["evidence-missed"]
    risk["source_ids"] = ["source-post-cutoff"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="may not be mixed"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )


def test_reflection_draft_requires_declared_model_and_reasoning_effort(
    client,
    tmp_path: Path,
) -> None:
    run_id, market_snapshot, prepared_at = _prepare_evaluated_live_run(
        client,
        tmp_path,
    )
    settings = client.app.state.settings
    root = tmp_path / "reflections"
    job_dir = prepare_reflection(
        settings,
        run_id,
        horizon=Horizon.D1,
        market_snapshot_path=market_snapshot,
        now=prepared_at,
        output_root=root,
    )
    _write_empty_discovery(job_dir, prepared_at + timedelta(minutes=1))
    draft_path = job_dir / "source-discovery" / "drafts.json"
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    payload["generated_by"]["model"] = ""
    payload["generated_by"]["reasoning_effort"] = ""
    _json_write(draft_path, payload)

    with pytest.raises(ValueError, match="string_too_short"):
        freeze_reflection_sources(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=2),
            output_root=root,
        )


def test_data_coverage_failure_cannot_cite_post_cutoff_source(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, prepared_at, _ = _full_handoff_with_sources(
        client,
        tmp_path,
    )
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    risk = next(
        item
        for item in payload["agent_findings"]
        if item["agent_id"] == "risk_critic_agent"
    )
    risk["primary_error_type"] = "data_coverage_failure"
    risk["causal_status"] = "supported"
    risk["source_ids"] = ["source-post-cutoff"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="pre-cutoff source"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )


def test_unresolved_causality_keeps_post_cutoff_availability_class(
    client,
    tmp_path: Path,
) -> None:
    settings, root, job_dir, prepared_at, _ = _full_handoff_with_sources(
        client,
        tmp_path,
    )
    payload = _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    risk = next(
        item
        for item in payload["agent_findings"]
        if item["agent_id"] == "risk_critic_agent"
    )
    risk["primary_error_type"] = "risk_plan_failure"
    risk["causal_status"] = "unresolved"
    risk["source_ids"] = ["source-post-cutoff"]
    _json_write(job_dir / "analysis" / "drafts.json", payload)
    finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=root,
    )

    with client.app.state.database.session_factory() as session:
        persisted = session.scalar(
            select(ReflectionFinding).where(
                ReflectionFinding.reflection_run_id == job_dir.name,
                ReflectionFinding.scope_type == "agent",
                ReflectionFinding.subject_id == "risk_critic_agent",
                ReflectionFinding.index_code == risk["index_code"],
            )
        )
        assert persisted is not None
        assert persisted.causal_status == "unresolved"
        assert persisted.availability_class == "post_cutoff_event"


def test_reflection_rejects_input_tamper_and_symlinked_draft(
    client,
    tmp_path: Path,
) -> None:
    run_id, market_snapshot, prepared_at = _prepare_evaluated_live_run(client, tmp_path)
    settings = client.app.state.settings
    root = tmp_path / "reflections"
    job_dir = prepare_reflection(
        settings,
        run_id,
        horizon="D1",
        market_snapshot_path=market_snapshot,
        now=prepared_at,
        output_root=root,
    )
    input_path = job_dir / "input.json"
    original_input = input_path.read_bytes()
    input_path.chmod(0o600)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    payload["overall_severity"] = "noise"
    _json_write(input_path, payload)
    _write_empty_discovery(job_dir, prepared_at + timedelta(minutes=1))
    with pytest.raises(ValueError, match="hash|canonical"):
        freeze_reflection_sources(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=2),
            output_root=root,
        )

    input_path.write_bytes(original_input)
    input_path.chmod(0o400)
    external = tmp_path / "external-discovery.json"
    _write_empty_discovery(job_dir, prepared_at + timedelta(minutes=1))
    discovery = job_dir / "source-discovery" / "drafts.json"
    external.write_bytes(discovery.read_bytes())
    discovery.unlink()
    discovery.symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        freeze_reflection_sources(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=2),
            output_root=root,
        )


def test_duplicate_finalize_does_not_duplicate_findings(client, tmp_path: Path) -> None:
    settings, reflection_root, job_dir, prepared_at = _full_handoff(client, tmp_path)
    _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=reflection_root,
    )

    with pytest.raises(RuntimeError, match="status|continue|finalized"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=5),
            output_root=reflection_root,
        )

    with client.app.state.database.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ReflectionFinding)) == 31
        )


def test_freeze_publication_failure_keeps_reflection_retryable(
    client,
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, market_snapshot, prepared_at = _prepare_evaluated_live_run(client, tmp_path)
    settings = client.app.state.settings
    root = tmp_path / "reflections"
    job_dir = prepare_reflection(
        settings,
        run_id,
        horizon="D1",
        market_snapshot_path=market_snapshot,
        now=prepared_at,
        output_root=root,
    )
    _write_empty_discovery(job_dir, prepared_at + timedelta(minutes=1))
    publish = reflection_handoff_module._publish_staged_file

    def fail_sources(temporary: Path, destination: Path, payload: bytes) -> bool:
        if destination.name == "sources.json":
            raise OSError("simulated source publication failure")
        return publish(temporary, destination, payload)

    monkeypatch.setattr(
        reflection_handoff_module,
        "_publish_staged_file",
        fail_sources,
    )
    with pytest.raises(OSError, match="publication failure"):
        freeze_reflection_sources(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=2),
            output_root=root,
        )

    with client.app.state.database.session_factory() as session:
        row = session.get(ReflectionRun, job_dir.name)
        assert row is not None
        assert row.status == "awaiting_sources"
        assert row.source_snapshot_hash is None
    assert not (job_dir / "sources.json").exists()

    monkeypatch.setattr(
        reflection_handoff_module,
        "_publish_staged_file",
        publish,
    )
    snapshot = freeze_reflection_sources(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=2),
        output_root=root,
    )
    assert snapshot.content_hash
    assert (job_dir / "analysis" / "drafts.template.json").is_file()


def test_receipt_publication_failure_rolls_back_findings_and_status(
    client,
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, root, job_dir, prepared_at = _full_handoff(client, tmp_path)
    _fill_unresolved_analysis(job_dir, prepared_at + timedelta(minutes=3))
    publish = reflection_handoff_module._publish_staged_file

    def fail_receipt(temporary: Path, destination: Path, payload: bytes) -> bool:
        if destination.name == "receipt.json":
            raise OSError("simulated receipt publication failure")
        return publish(temporary, destination, payload)

    monkeypatch.setattr(
        reflection_handoff_module,
        "_publish_staged_file",
        fail_receipt,
    )
    with pytest.raises(OSError, match="receipt publication failure"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )

    with client.app.state.database.session_factory() as session:
        row = session.get(ReflectionRun, job_dir.name)
        assert row is not None
        assert row.status == "awaiting_analysis"
        assert (
            session.scalar(select(func.count()).select_from(ReflectionFinding)) == 0
        )
    assert not (job_dir / "receipt.json").exists()

    monkeypatch.setattr(
        reflection_handoff_module,
        "_publish_staged_file",
        publish,
    )
    receipt = finalize_reflection(
        settings,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=root,
    )
    assert receipt.status == "completed"


def test_lesson_shadow_gate_and_market_date_episode_count(
    client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_one = tmp_path / "case-one"
    case_one.mkdir()
    settings, root, job_dir, prepared_at = _full_handoff(client, case_one)
    payload = _fill_unresolved_analysis(
        job_dir,
        prepared_at + timedelta(minutes=3),
    )
    _add_lesson_proposal(payload)
    _json_write(job_dir / "analysis" / "drafts.json", payload)

    with pytest.raises(ValueError, match="lesson proposals are disabled"):
        finalize_reflection(
            settings,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LessonProposal)) == 0

    enabled = settings.model_copy(
        update={"reflection_lesson_proposals_enabled": True}
    )
    with pytest.raises(ValueError, match="10 completed Live reflections"):
        finalize_reflection(
            enabled,
            job_dir,
            now=prepared_at + timedelta(minutes=4),
            output_root=root,
        )
    monkeypatch.setattr(
        reflection_handoff_module,
        "approved_reflection_review_count",
        lambda _session, **_: 10,
    )
    first_receipt = finalize_reflection(
        enabled,
        job_dir,
        now=prepared_at + timedelta(minutes=4),
        output_root=root,
    )
    assert first_receipt.lesson_proposal_count == 1

    case_two = tmp_path / "case-two"
    case_two.mkdir()
    settings_two, root_two, job_two, prepared_two = _full_handoff(
        client,
        case_two,
        as_of=AS_OF - timedelta(days=1),
        horizon=Horizon.D2,
    )
    payload_two = _fill_unresolved_analysis(
        job_two,
        prepared_two + timedelta(minutes=3),
    )
    _add_lesson_proposal(payload_two)
    _json_write(job_two / "analysis" / "drafts.json", payload_two)
    finalize_reflection(
        settings_two.model_copy(
            update={"reflection_lesson_proposals_enabled": True}
        ),
        job_two,
        now=prepared_two + timedelta(minutes=4),
        output_root=root_two,
    )

    with client.app.state.database.session_factory() as session:
        lessons = session.scalars(
            select(LessonProposal).order_by(LessonProposal.created_at)
        ).all()
        assert len(lessons) == 2
        assert {item.episode_key for item in lessons} == {
            payload["market_finding"]["target_date"]
        }
        assert [item.independent_episode_count for item in lessons] == [1, 1]
        assert all(
            item.replay_metrics["automatic_promotion_allowed"] is False
            for item in lessons
        )
        assert all(
            "shadow_target_dates_below_minimum"
            in item.replay_metrics["blockers"]
            for item in lessons
        )


def test_corrected_reflection_requires_new_version_and_supersedes_link(
    client,
    tmp_path: Path,
) -> None:
    settings, root, first_job, prepared_at = _full_handoff(client, tmp_path)
    _fill_unresolved_analysis(first_job, prepared_at + timedelta(minutes=3))
    finalize_reflection(
        settings,
        first_job,
        now=prepared_at + timedelta(minutes=4),
        output_root=root,
    )
    first_input = json.loads(
        (first_job / "input.json").read_text(encoding="utf-8")
    )
    market_snapshot = settings.market_snapshot_root / "market-snapshot.json"

    with pytest.raises(ValueError, match="newer schema version"):
        prepare_reflection(
            settings,
            first_input["source_run_id"],
            horizon=first_input["horizon"],
            market_snapshot_path=market_snapshot,
            schema_version="1.0.0",
            supersedes_id=first_job.name,
            now=prepared_at + timedelta(minutes=5),
            output_root=root,
        )

    revised_job = prepare_reflection(
        settings,
        first_input["source_run_id"],
        horizon=first_input["horizon"],
        market_snapshot_path=market_snapshot,
        schema_version="1.1.0",
        supersedes_id=first_job.name,
        now=prepared_at + timedelta(minutes=5),
        output_root=root,
    )
    assert revised_job != first_job
    revised_input = json.loads(
        (revised_job / "input.json").read_text(encoding="utf-8")
    )
    assert revised_input["schema_version"] == "1.1.0"
    assert revised_input["supersedes_id"] == first_job.name
    with client.app.state.database.session_factory() as session:
        revised = session.get(ReflectionRun, revised_job.name)
        assert revised is not None
        assert revised.schema_version == "1.1.0"
        assert revised.supersedes_id == first_job.name
