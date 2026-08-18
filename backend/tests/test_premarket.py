from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.premarket import (
    ANALYST_AGENT_IDS,
    CODEX_AGENT_IDS,
    CSI1000_OPEN_TO_OPEN_D1_TARGET,
    DEFAULT_PREMARKET_PROGRAM_V1,
    EvidenceCategoryV1,
    HistoricalOpenV1,
    PremarketAgentDraftV1,
    PremarketDraftBundleV1,
    PremarketEvidenceItemV1,
    PremarketEvidenceSnapshotBodyV1,
    PremarketForecastV1,
    PremarketOutcomeBodyV1,
    PremarketProgramBodyV1,
    PremarketWikiReferenceV1,
    ProbabilitiesV1,
    SourceStampV1,
    SourceTierV1,
    TradingCalendarStampV1,
    build_premarket_handoff,
    content_hash,
    evaluate_premarket_forecast,
    finalize_premarket_forecast,
    seal_premarket_outcome,
    seal_premarket_snapshot,
)
from app.services.premarket import (
    build_premarket_brief,
    evaluate_premarket_run,
    finalize_premarket_run,
    load_premarket_history,
    prepare_premarket_run,
)
from pydantic import ValidationError

ZONE = ZoneInfo("Asia/Shanghai")
PREVIOUS = date(2026, 8, 17)
FORECAST = date(2026, 8, 18)
TARGET = date(2026, 8, 19)
CUTOFF = datetime(2026, 8, 18, 9, 10, tzinfo=ZONE)


def _hash(label: str) -> str:
    return content_hash({"label": label})


def _source(label: str, observed_at: datetime) -> SourceStampV1:
    return SourceStampV1(
        source_url=f"https://data.example/{label}",
        source_hash=_hash(label),
        observed_at=observed_at,
        ingested_at=observed_at + timedelta(seconds=5),
    )


def _item(
    item_id: str,
    category: EvidenceCategoryV1,
    agents: list[str],
    *,
    minute: int,
) -> PremarketEvidenceItemV1:
    published = datetime(2026, 8, 17, 16, minute, tzinfo=ZONE)
    return PremarketEvidenceItemV1(
        item_id=item_id,
        independence_key=f"event:{item_id}",
        category=category,
        source_tier=(
            SourceTierV1.TIER_3 if category is EvidenceCategoryV1.NEWS else SourceTierV1.TIER_2
        ),
        title=f"Synthetic {item_id}",
        summary=f"Synthetic evidence for {item_id}.",
        published_at=published,
        observed_at=published,
        ingested_at=published + timedelta(seconds=10),
        source_url=f"https://news.example/{item_id}",
        source_hash=_hash(item_id),
        assigned_agent_ids=agents,
    )


def _snapshot_body() -> PremarketEvidenceSnapshotBodyV1:
    sessions = [PREVIOUS, FORECAST, TARGET]
    calendar_observed = datetime(2026, 8, 18, 8, 0, tzinfo=ZONE)
    calendar = TradingCalendarStampV1(
        source_url="https://calendar.example/sse",
        source_hash=content_hash(
            {
                "schema_version": "forecast-loop.premarket-calendar/v1",
                "sessions": sessions,
            }
        ),
        observed_at=calendar_observed,
        ingested_at=calendar_observed + timedelta(seconds=5),
        sessions=sessions,
    )
    history_dates = [PREVIOUS - timedelta(days=offset) for offset in range(20, -1, -1)]
    history = [
        HistoricalOpenV1(
            trade_date=trade_date,
            open_price=100.0 + index + (0.2 if index % 2 else -0.1),
            source=_source(
                f"open-{trade_date.isoformat()}",
                datetime(2026, 8, 17, 14, 0, tzinfo=ZONE),
            ),
        )
        for index, trade_date in enumerate(history_dates)
    ]
    returns = [
        current.open_price / previous.open_price - 1.0
        for previous, current in zip(history[:-1], history[1:], strict=True)
    ]
    wiki = [
        PremarketWikiReferenceV1(
            entry_id=f"VC-WIKI-{agent_id.upper().replace('_', '-')}",
            title=f"Synthetic Wiki for {agent_id}",
            version="1.0.0",
            section="scope",
            content_hash=_hash(f"wiki-{agent_id}"),
            published_at=datetime(2026, 8, 17, 6, 0, tzinfo=ZONE),
            assigned_agent_ids=[agent_id],
        )
        for agent_id in CODEX_AGENT_IDS
    ]
    return PremarketEvidenceSnapshotBodyV1(
        program_hash=DEFAULT_PREMARKET_PROGRAM_V1.content_hash,
        previous_session=PREVIOUS,
        forecast_session=FORECAST,
        target_session=TARGET,
        evidence_start=datetime(2026, 8, 17, 15, 0, tzinfo=ZONE),
        evidence_cutoff=CUTOFF,
        decision_time=datetime(2026, 8, 18, 9, 15, tzinfo=ZONE),
        finalization_deadline=datetime(2026, 8, 18, 9, 24, tzinfo=ZONE),
        created_at=datetime(2026, 8, 18, 9, 11, tzinfo=ZONE),
        calendar_source=calendar,
        open_history=history,
        volatility_20d=__import__("statistics").stdev(returns),
        items=[
            _item(
                "news-1",
                EvidenceCategoryV1.NEWS,
                ["market_news_agent", "macro_policy_agent"],
                minute=1,
            ),
            _item(
                "global-1",
                EvidenceCategoryV1.GLOBAL_EQUITY,
                ["global_market_agent", "ai_storage_industry_agent"],
                minute=2,
            ),
            _item(
                "fx-1",
                EvidenceCategoryV1.FX_RATES,
                ["macro_policy_agent", "global_market_agent"],
                minute=3,
            ),
            _item(
                "industry-1",
                EvidenceCategoryV1.INDUSTRY,
                ["ai_storage_industry_agent", "market_news_agent"],
                minute=4,
            ),
        ],
        wiki_references=wiki,
    )


def _draft_bundle(handoff) -> PremarketDraftBundleV1:
    drafts = []
    for assignment in handoff.assignments:
        base = {
            "assignment_id": assignment.assignment_id,
            "agent_id": assignment.agent_id,
            "role": assignment.role,
            "rationale": f"Synthetic rationale from {assignment.agent_id}.",
            "transmission_chain": ["Overnight evidence", "opening risk premium"],
            "counter_evidence": ["The move may already be priced."],
            "invalidation_conditions": ["The opening auction reverses the signal."],
            "evidence_item_ids": [assignment.allowed_evidence_item_ids[0]],
            "wiki_entry_id": assignment.wiki_reference.entry_id,
            "wiki_version": assignment.wiki_reference.version,
            "wiki_section": assignment.wiki_reference.section,
            "wiki_content_hash": assignment.wiki_reference.content_hash,
        }
        if assignment.role == "risk":
            drafts.append(PremarketAgentDraftV1(**base, risk_severity="medium"))
        else:
            probabilities = ProbabilitiesV1(up=0.55, neutral=0.25, down=0.20)
            drafts.append(
                PremarketAgentDraftV1(
                    **base,
                    direction="up",
                    probabilities=probabilities,
                )
            )
    return PremarketDraftBundleV1(
        run_id=handoff.run_id,
        request_hash=handoff.request_hash,
        generated_at=datetime(2026, 8, 18, 9, 18, tzinfo=ZONE),
        generated_by={
            "surface": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        },
        drafts=drafts,
    )


def test_program_has_distinct_open_to_open_identity() -> None:
    assert DEFAULT_PREMARKET_PROGRAM_V1.target_id == CSI1000_OPEN_TO_OPEN_D1_TARGET
    assert DEFAULT_PREMARKET_PROGRAM_V1.target_window == "open_to_open"
    with pytest.raises(ValidationError):
        PremarketProgramBodyV1(target_window="close_to_close")  # type: ignore[arg-type]


def test_snapshot_requires_premarket_categories_and_cutoff_safe_evidence() -> None:
    body = _snapshot_body()
    snapshot = seal_premarket_snapshot(body)
    assert snapshot.open_history[-1].trade_date == PREVIOUS
    assert snapshot.forecast_session == FORECAST
    assert snapshot.target_session == TARGET

    payload = body.model_dump()
    payload["items"] = [item for item in payload["items"] if item["category"] != "global_equity"]
    with pytest.raises(ValidationError, match="global-equity"):
        PremarketEvidenceSnapshotBodyV1.model_validate(payload)

    payload = body.model_dump()
    payload["items"][0]["ingested_at"] = CUTOFF + timedelta(seconds=1)
    with pytest.raises(ValidationError, match="outside the premarket window"):
        PremarketEvidenceSnapshotBodyV1.model_validate(payload)


def test_handoff_routes_dynamic_evidence_and_wiki_by_agent() -> None:
    snapshot = seal_premarket_snapshot(_snapshot_body())
    handoff = build_premarket_handoff(
        run_id="run-2026-08-18",
        snapshot=snapshot,
        prepared_at=datetime(2026, 8, 18, 9, 12, tzinfo=ZONE),
    )
    assert {item.agent_id for item in handoff.assignments} == set(CODEX_AGENT_IDS)
    analysts = [item for item in handoff.assignments if item.agent_id in ANALYST_AGENT_IDS]
    assert all(item.allowed_evidence_item_ids for item in analysts)
    strategy = next(item for item in handoff.assignments if item.agent_id == "strategy_agent")
    assert len(strategy.depends_on_assignment_ids) == 4


def test_finalize_rejects_cross_assignment_evidence_and_late_acceptance() -> None:
    snapshot = seal_premarket_snapshot(_snapshot_body())
    handoff = build_premarket_handoff(
        run_id="run-2026-08-18",
        snapshot=snapshot,
        prepared_at=datetime(2026, 8, 18, 9, 12, tzinfo=ZONE),
    )
    bundle = _draft_bundle(handoff)
    payload = bundle.model_dump()
    macro = next(item for item in payload["drafts"] if item["agent_id"] == "macro_policy_agent")
    macro["evidence_item_ids"] = ["global-1"]
    tampered = PremarketDraftBundleV1.model_validate(payload)
    with pytest.raises(ValueError, match="outside its assignment"):
        finalize_premarket_forecast(
            handoff,
            tampered,
            accepted_at=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )

    with pytest.raises(ValueError, match="deadline"):
        finalize_premarket_forecast(
            handoff,
            bundle,
            accepted_at=datetime(2026, 8, 18, 9, 24, tzinfo=ZONE),
        )

    with pytest.raises(ValueError, match="before decision time"):
        early_payload = bundle.model_dump()
        early_payload["generated_at"] = datetime(2026, 8, 18, 9, 13, tzinfo=ZONE)
        finalize_premarket_forecast(
            handoff,
            PremarketDraftBundleV1.model_validate(early_payload),
            accepted_at=datetime(2026, 8, 18, 9, 14, tzinfo=ZONE),
        )


def test_open_to_open_forecast_and_evaluation_form_a_sealed_episode() -> None:
    snapshot = seal_premarket_snapshot(_snapshot_body())
    handoff = build_premarket_handoff(
        run_id="run-2026-08-18",
        snapshot=snapshot,
        prepared_at=datetime(2026, 8, 18, 9, 12, tzinfo=ZONE),
    )
    forecast = finalize_premarket_forecast(
        handoff,
        _draft_bundle(handoff),
        accepted_at=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    assert forecast.direction == "up"
    assert forecast.target_window == "open_to_open"
    assert forecast.forecast_session == FORECAST
    assert forecast.target_session == TARGET
    assert math.isclose(forecast.threshold, snapshot.volatility_20d * 0.25)

    outcome_observed = datetime(2026, 8, 19, 9, 31, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=outcome_observed,
            source=_source("outcome", outcome_observed - timedelta(seconds=10)),
        )
    )
    evaluation = evaluate_premarket_forecast(
        forecast,
        outcome,
        evaluated_at=outcome_observed + timedelta(minutes=1),
    )
    assert evaluation.actual_label == "up"
    assert evaluation.direction_correct is True
    assert math.isclose(evaluation.realized_return, 0.02)


def test_file_first_service_prepares_finalizes_and_renders_brief(tmp_path) -> None:
    snapshot = seal_premarket_snapshot(_snapshot_body())
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(), encoding="utf-8")
    settings = Settings(handoff_root=tmp_path / "handoffs")
    prepared_at = datetime(2026, 8, 18, 9, 12, tzinfo=ZONE)

    job_dir = prepare_premarket_run(
        settings,
        snapshot_path=snapshot_path,
        now=prepared_at,
    )
    repeated = prepare_premarket_run(
        settings,
        snapshot_path=snapshot_path,
        now=prepared_at,
    )
    assert repeated == job_dir
    handoff = build_premarket_handoff(
        run_id=job_dir.name,
        snapshot=snapshot,
        prepared_at=prepared_at,
    )
    (job_dir / "drafts.json").write_text(
        _draft_bundle(handoff).model_dump_json(),
        encoding="utf-8",
    )
    forecast = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    brief = build_premarket_brief(settings, job_dir=job_dir)

    assert forecast.target_window == "open_to_open"
    assert "盘前预测" in brief.text
    assert "2026-08-18 开盘 → 2026-08-19 开盘" in brief.text
    assert "仅为研究信号" in brief.text

    observed_at = datetime(2026, 8, 19, 9, 31, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=observed_at,
            source=_source("outcome-service", observed_at - timedelta(seconds=10)),
        )
    )
    outcome_path = tmp_path / "outcome.json"
    outcome_path.write_text(outcome.model_dump_json(), encoding="utf-8")
    evaluation = evaluate_premarket_run(
        settings,
        job_dir=job_dir,
        outcome_path=outcome_path,
        now=observed_at,
    )
    assert evaluation.actual_label == "up"
    assert (job_dir / "outcome.json").exists()
    assert (job_dir / "evaluation.json").exists()

    first_history = load_premarket_history(settings)
    assert len(first_history) == 1
    assert first_history[0]["direction_correct"] is True
    assert first_history[0]["long_only_cumulative_return"] == pytest.approx(0.02)
    assert first_history[0]["long_short_cumulative_return"] == pytest.approx(0.02)
    assert load_premarket_history(settings, settled_before=TARGET) == []

    down_payload = forecast.model_dump(mode="json", exclude={"content_hash"})
    down_payload.update(
        {
            "run_id": "down-run",
            "forecast_session": TARGET.isoformat(),
            "target_session": (TARGET + timedelta(days=1)).isoformat(),
            "direction": "down",
            "probabilities": {"up": 0.20, "neutral": 0.25, "down": 0.55},
        }
    )
    down_forecast = PremarketForecastV1(
        **down_payload,
        content_hash=content_hash(down_payload),
    )
    down_observed_at = datetime(2026, 8, 20, 9, 31, tzinfo=ZONE)
    down_outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=down_forecast.content_hash,
            forecast_session=TARGET,
            target_session=TARGET + timedelta(days=1),
            start_open=100.0,
            end_open=95.0,
            observed_at=down_observed_at,
            source=_source("outcome-down", down_observed_at - timedelta(seconds=10)),
        )
    )
    down_evaluation = evaluate_premarket_forecast(
        down_forecast,
        down_outcome,
        evaluated_at=down_observed_at,
    )
    down_job = settings.handoff_root / "premarket" / "down-run"
    down_job.mkdir()
    (down_job / "forecast.json").write_text(
        down_forecast.model_dump_json(),
        encoding="utf-8",
    )
    (down_job / "outcome.json").write_text(
        down_outcome.model_dump_json(),
        encoding="utf-8",
    )
    (down_job / "evaluation.json").write_text(
        down_evaluation.model_dump_json(),
        encoding="utf-8",
    )

    history = load_premarket_history(settings)
    assert len(history) == 2
    assert history[-1]["long_only_period_return"] == 0.0
    assert history[-1]["long_short_period_return"] == pytest.approx(0.05)
    assert history[-1]["long_only_cumulative_return"] == pytest.approx(0.02)
    assert history[-1]["long_short_cumulative_return"] == pytest.approx(0.071)
    available_for_august_20_brief = load_premarket_history(
        settings,
        settled_before=TARGET + timedelta(days=1),
    )
    assert [item["target_session"] for item in available_for_august_20_brief] == [TARGET]
