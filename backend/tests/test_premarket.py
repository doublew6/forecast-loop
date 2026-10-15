from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import app.services.premarket as premarket_service
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
    PremarketEvaluationV1,
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
    canonical_json,
    content_hash,
    evaluate_premarket_forecast,
    finalize_premarket_forecast,
    seal_premarket_outcome,
    seal_premarket_snapshot,
)
from app.services.premarket import (
    PremarketServiceError,
    build_premarket_brief,
    evaluate_premarket_run,
    finalize_premarket_run,
    load_premarket_forecast,
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


def _shifted_snapshot_body(days: int) -> PremarketEvidenceSnapshotBodyV1:
    payload = _snapshot_body().model_dump(mode="python")
    delta = timedelta(days=days)
    for field in ("previous_session", "forecast_session", "target_session"):
        payload[field] += delta
    for field in (
        "evidence_start",
        "evidence_cutoff",
        "decision_time",
        "finalization_deadline",
        "created_at",
    ):
        payload[field] += delta
    payload["calendar_source"]["sessions"] = [
        session + delta for session in payload["calendar_source"]["sessions"]
    ]
    payload["calendar_source"]["source_hash"] = content_hash(
        {
            "schema_version": "forecast-loop.premarket-calendar/v1",
            "sessions": payload["calendar_source"]["sessions"],
        }
    )
    for field in ("observed_at", "ingested_at"):
        payload["calendar_source"][field] += delta
    for item in payload["open_history"]:
        item["trade_date"] += delta
        item["source"]["observed_at"] += delta
        item["source"]["ingested_at"] += delta
    for item in payload["items"]:
        for field in ("published_at", "observed_at", "ingested_at"):
            item[field] += delta
    for item in payload["wiki_references"]:
        item["published_at"] += delta
    return PremarketEvidenceSnapshotBodyV1.model_validate(payload)


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


def _prepared_service_job(
    tmp_path: Path,
    *,
    handoff_root: Path | None = None,
) -> tuple[Settings, Path]:
    snapshot = seal_premarket_snapshot(_snapshot_body())
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_bytes(canonical_json(snapshot))
    settings = Settings(handoff_root=handoff_root or tmp_path / "handoffs")
    prepared_at = datetime(2026, 8, 18, 9, 12, tzinfo=ZONE)
    job_dir = prepare_premarket_run(
        settings,
        snapshot_path=snapshot_path,
        now=prepared_at,
    )
    handoff = build_premarket_handoff(
        run_id=job_dir.name,
        snapshot=snapshot,
        prepared_at=prepared_at,
    )
    (job_dir / "drafts.json").write_bytes(canonical_json(_draft_bundle(handoff)))
    return settings, job_dir


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


def test_evaluation_rejects_outcome_before_target_open() -> None:
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
    observed_at = datetime(2026, 8, 18, 9, 21, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=observed_at,
            source=_source("early-outcome", observed_at - timedelta(seconds=10)),
        )
    )

    with pytest.raises(ValueError, match="target-session open"):
        evaluate_premarket_forecast(
            forecast,
            outcome,
            evaluated_at=observed_at,
        )


def test_evaluation_rejects_outcome_observed_after_acceptance() -> None:
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
    observed_at = datetime(2026, 8, 19, 9, 31, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=observed_at,
            source=_source("future-outcome", observed_at - timedelta(seconds=10)),
        )
    )

    with pytest.raises(ValueError, match="follows evaluation acceptance"):
        evaluate_premarket_forecast(
            forecast,
            outcome,
            evaluated_at=observed_at - timedelta(seconds=1),
        )


def test_finalize_recovers_missing_receipt_from_verified_forecast(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    forecast = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    forecast_bytes = (job_dir / "forecast.json").read_bytes()
    receipt_bytes = (job_dir / "receipt.json").read_bytes()
    (job_dir / "receipt.json").unlink()

    recovered = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 21, tzinfo=ZONE),
    )

    assert recovered.content_hash == forecast.content_hash
    assert (job_dir / "forecast.json").read_bytes() == forecast_bytes
    assert (job_dir / "receipt.json").read_bytes() == receipt_bytes

    validated = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 22, tzinfo=ZONE),
    )
    assert validated.content_hash == forecast.content_hash
    assert (job_dir / "forecast.json").read_bytes() == forecast_bytes
    assert (job_dir / "receipt.json").read_bytes() == receipt_bytes


def test_finalize_rejects_receipt_without_forecast(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    (job_dir / "forecast.json").unlink()

    with pytest.raises(PremarketServiceError, match="receipt exists without forecast"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 21, tzinfo=ZONE),
        )


def test_finalize_rejects_self_consistent_receipt_for_other_forecast(
    tmp_path: Path,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    receipt_path = job_dir / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["forecast_hash"] = "0" * 64
    receipt.pop("receipt_hash")
    receipt["receipt_hash"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    receipt_path.unlink()
    receipt_path.write_bytes(canonical_json(receipt))

    with pytest.raises(PremarketServiceError, match="does not match forecast"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 21, tzinfo=ZONE),
        )


def test_finalize_rejects_resealed_forecast_that_conflicts_with_drafts(
    tmp_path: Path,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    forecast_path = job_dir / "forecast.json"
    forecast = json.loads(forecast_path.read_bytes())
    forecast["rationale"] = "Tampered but internally resealed rationale."
    forecast["content_hash"] = content_hash(forecast)
    forecast_path.unlink()
    forecast_path.write_bytes(canonical_json(forecast))

    with pytest.raises(PremarketServiceError, match="forecast has different content"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 21, tzinfo=ZONE),
        )


def test_finalize_does_not_recover_missing_receipt_at_deadline(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 23, 59, tzinfo=ZONE),
    )
    (job_dir / "receipt.json").unlink()

    with pytest.raises(PremarketServiceError, match="deadline"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 24, tzinfo=ZONE),
        )
    assert not (job_dir / "receipt.json").exists()


def test_finalize_normalizes_utc_time_for_fresh_and_recovered_receipt(
    tmp_path: Path,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    forecast = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 1, 20, tzinfo=ZoneInfo("UTC")),
    )

    assert forecast.created_at == datetime(2026, 8, 18, 9, 20, tzinfo=ZONE)
    receipt_bytes = (job_dir / "receipt.json").read_bytes()
    (job_dir / "receipt.json").unlink()
    recovered = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 1, 21, tzinfo=ZoneInfo("UTC")),
    )

    assert recovered.content_hash == forecast.content_hash
    assert (job_dir / "receipt.json").read_bytes() == receipt_bytes


def test_finalize_binds_handoff_run_id_to_job_directory(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    renamed = job_dir.with_name("different-job-directory")
    job_dir.rename(renamed)

    with pytest.raises(PremarketServiceError, match="directory does not match"):
        finalize_premarket_run(
            settings,
            job_dir=renamed,
            now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )


def test_finalize_serializes_concurrent_identical_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    first_receipt_started = threading.Event()
    release_first_receipt = threading.Event()
    unexpected_second_receipt = threading.Event()
    second_clock_called = threading.Event()
    receipt_calls = 0
    receipt_calls_lock = threading.Lock()
    original_builder = premarket_service._build_premarket_receipt

    def controlled_builder(forecast: PremarketForecastV1):
        nonlocal receipt_calls
        with receipt_calls_lock:
            receipt_calls += 1
            call_number = receipt_calls
        if call_number == 1:
            first_receipt_started.set()
            assert release_first_receipt.wait(timeout=2)
        elif not release_first_receipt.is_set():
            unexpected_second_receipt.set()
        return original_builder(forecast)

    monkeypatch.setattr(
        premarket_service,
        "_build_premarket_receipt",
        controlled_builder,
    )
    def second_clock() -> datetime:
        second_clock_called.set()
        return datetime(2026, 8, 18, 9, 20, 1, tzinfo=ZONE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            finalize_premarket_run,
            settings,
            job_dir=job_dir,
            clock=lambda: datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )
        assert first_receipt_started.wait(timeout=2)
        second = pool.submit(
            finalize_premarket_run,
            settings,
            job_dir=job_dir,
            clock=second_clock,
        )
        assert not second_clock_called.wait(timeout=0.2)
        assert not unexpected_second_receipt.wait(timeout=0.2)
        release_first_receipt.set()
        first_forecast = first.result(timeout=2)
        second_forecast = second.result(timeout=2)

    assert first_forecast.content_hash == second_forecast.content_hash
    assert (job_dir / "forecast.json").is_file()
    assert (job_dir / "receipt.json").is_file()


def test_finalize_resamples_clock_after_waiting_for_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    first_receipt_started = threading.Event()
    release_first_receipt = threading.Event()
    second_clock_called = threading.Event()
    original_builder = premarket_service._build_premarket_receipt
    builder_calls = 0
    builder_lock = threading.Lock()

    def blocking_builder(forecast: PremarketForecastV1):
        nonlocal builder_calls
        with builder_lock:
            builder_calls += 1
            call_number = builder_calls
        if call_number == 1:
            first_receipt_started.set()
            assert release_first_receipt.wait(timeout=2)
        return original_builder(forecast)

    def deadline_clock() -> datetime:
        second_clock_called.set()
        return datetime(2026, 8, 18, 9, 24, tzinfo=ZONE)

    monkeypatch.setattr(
        premarket_service,
        "_build_premarket_receipt",
        blocking_builder,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            finalize_premarket_run,
            settings,
            job_dir=job_dir,
            clock=lambda: datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )
        assert first_receipt_started.wait(timeout=2)
        second = pool.submit(
            finalize_premarket_run,
            settings,
            job_dir=job_dir,
            clock=deadline_clock,
        )
        assert not second_clock_called.wait(timeout=0.2)
        release_first_receipt.set()
        first.result(timeout=2)
        with pytest.raises(PremarketServiceError, match="deadline"):
            second.result(timeout=2)


def test_finalize_rechecks_deadline_before_receipt_write(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    sampled = iter(
        (
            datetime(2026, 8, 18, 9, 23, 59, tzinfo=ZONE),
            datetime(2026, 8, 18, 9, 23, 59, tzinfo=ZONE),
            datetime(2026, 8, 18, 9, 24, tzinfo=ZONE),
        )
    )

    with pytest.raises(PremarketServiceError, match="deadline"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            clock=lambda: next(sampled),
        )

    assert (job_dir / "forecast.json").is_file()
    assert not (job_dir / "receipt.json").exists()

    with pytest.raises(PremarketServiceError, match="receipt.json"):
        load_premarket_forecast(settings, job_dir=job_dir)
    with pytest.raises(PremarketServiceError, match="receipt.json"):
        build_premarket_brief(settings, job_dir=job_dir)

    forecast = PremarketForecastV1.model_validate_json(
        (job_dir / "forecast.json").read_bytes()
    )
    observed_at = datetime(2026, 8, 19, 9, 31, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=observed_at,
            source=_source("unsealed-outcome", observed_at - timedelta(seconds=10)),
        )
    )
    outcome_path = tmp_path / "unsealed-outcome.json"
    outcome_path.write_bytes(canonical_json(outcome))
    with pytest.raises(PremarketServiceError, match="receipt.json"):
        evaluate_premarket_run(
            settings,
            job_dir=job_dir,
            outcome_path=outcome_path,
            now=observed_at,
        )
    assert not (job_dir / "outcome.json").exists()
    assert not (job_dir / "evaluation.json").exists()

    evaluation = evaluate_premarket_forecast(
        forecast,
        outcome,
        evaluated_at=observed_at,
    )
    (job_dir / "outcome.json").write_bytes(canonical_json(outcome))
    (job_dir / "evaluation.json").write_bytes(canonical_json(evaluation))
    with pytest.raises(PremarketServiceError, match="receipt.json"):
        load_premarket_history(settings)


def test_prepare_reuses_same_snapshot_without_changing_first_handoff(
    tmp_path: Path,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    input_bytes = (job_dir / "input.json").read_bytes()
    draft_bytes = (job_dir / "drafts.json").read_bytes()

    repeated = prepare_premarket_run(
        settings,
        snapshot_path=tmp_path / "snapshot.json",
        now=datetime(2026, 8, 18, 9, 13, tzinfo=ZONE),
    )

    assert repeated == job_dir
    assert (job_dir / "input.json").read_bytes() == input_bytes
    assert (job_dir / "drafts.json").read_bytes() == draft_bytes


def test_evaluate_rejects_early_outcome_without_publishing_artifacts(
    tmp_path: Path,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    forecast = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    observed_at = datetime(2026, 8, 18, 9, 21, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=observed_at,
            source=_source("early-service-outcome", observed_at - timedelta(seconds=10)),
        )
    )
    outcome_path = tmp_path / "early-outcome.json"
    outcome_path.write_bytes(canonical_json(outcome))

    with pytest.raises(PremarketServiceError, match="target-session open"):
        evaluate_premarket_run(
            settings,
            job_dir=job_dir,
            outcome_path=outcome_path,
            now=observed_at,
        )

    assert not (job_dir / "outcome.json").exists()
    assert not (job_dir / "evaluation.json").exists()


def test_completed_forecast_reader_reproduces_frozen_drafts(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    drafts_path = job_dir / "drafts.json"
    drafts = json.loads(drafts_path.read_bytes())
    strategy = next(
        item for item in drafts["drafts"] if item["agent_id"] == "strategy_agent"
    )
    strategy["rationale"] = "Resealed but different strategy rationale."
    drafts_path.unlink()
    drafts_path.write_bytes(canonical_json(drafts))

    with pytest.raises(PremarketServiceError, match="different content"):
        load_premarket_forecast(settings, job_dir=job_dir)


def test_finalize_rejects_symlinked_lock_file(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    outside = tmp_path / "outside-lock"
    outside.write_text("do not touch", encoding="utf-8")
    (job_dir / ".finalize.lock").symlink_to(outside)

    with pytest.raises(PremarketServiceError, match="locked safely"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )

    assert outside.read_text(encoding="utf-8") == "do not touch"
    assert not (job_dir / "forecast.json").exists()


def test_finalize_rejects_ancestor_symlink_rebinding(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    configured_root = settings.handoff_root
    relocated_root = tmp_path / "relocated-handoffs"
    configured_root.rename(relocated_root)
    configured_root.symlink_to(relocated_root, target_is_directory=True)

    with pytest.raises(PremarketServiceError, match="unavailable"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )

    relocated_job = relocated_root / "premarket" / job_dir.name
    assert not (relocated_job / "forecast.json").exists()
    assert not (relocated_job / "receipt.json").exists()


def test_finalize_accepts_stable_symlink_alias_above_configured_root(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "stable-alias"
    alias.symlink_to(physical, target_is_directory=True)
    settings, job_dir = _prepared_service_job(
        tmp_path,
        handoff_root=alias / "handoffs",
    )

    forecast = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )

    assert forecast.run_id == job_dir.name
    assert (job_dir / "forecast.json").is_file()
    assert (job_dir / "receipt.json").is_file()


def test_finalize_rejects_self_referential_job_symlink(tmp_path: Path) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    relocated_job = job_dir.with_name(f"{job_dir.name}-relocated")
    job_dir.rename(relocated_job)
    job_dir.symlink_to(job_dir.name, target_is_directory=True)

    with pytest.raises(PremarketServiceError, match="unavailable"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )

    assert not (relocated_job / "forecast.json").exists()


def test_finalize_rejects_fifo_artifact_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    drafts_path = job_dir / "drafts.json"
    drafts_path.unlink()
    os.mkfifo(drafts_path, mode=0o600)
    real_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        if path == "drafts.json":
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(premarket_service.os, "open", guarded_open)

    with pytest.raises(PremarketServiceError, match="invalid file size"):
        finalize_premarket_run(
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
        )


def test_fd_atomic_write_cleans_temporary_file_after_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_fsync = os.fsync

    def failing_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("synthetic file fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(premarket_service.os, "fsync", failing_file_fsync)
    try:
        with pytest.raises(PremarketServiceError, match="unable to write"):
            premarket_service._atomic_write_at(
                directory_fd,
                "artifact.json",
                b"{}\n",
                mode=0o400,
            )
    finally:
        os.close(directory_fd)

    assert not (tmp_path / "artifact.json").exists()
    assert list(tmp_path.glob(".artifact.json.*")) == []


def test_fd_atomic_write_cleans_temporary_file_after_link_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def failing_link(*_args, **_kwargs) -> None:
        raise OSError("synthetic link failure")

    monkeypatch.setattr(premarket_service.os, "link", failing_link)
    try:
        with pytest.raises(PremarketServiceError, match="unable to publish"):
            premarket_service._atomic_write_at(
                directory_fd,
                "artifact.json",
                b"{}\n",
                mode=0o400,
            )
    finally:
        os.close(directory_fd)

    assert not (tmp_path / "artifact.json").exists()
    assert list(tmp_path.glob(".artifact.json.*")) == []


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
    evaluation_bytes = (job_dir / "evaluation.json").read_bytes()
    repeated_evaluation = evaluate_premarket_run(
        settings,
        job_dir=job_dir,
        outcome_path=outcome_path,
        now=observed_at + timedelta(seconds=1),
    )
    assert evaluation.actual_label == "up"
    assert repeated_evaluation.content_hash == evaluation.content_hash
    assert repeated_evaluation.evaluated_at == evaluation.evaluated_at
    assert (job_dir / "evaluation.json").read_bytes() == evaluation_bytes
    assert (job_dir / "outcome.json").exists()
    assert (job_dir / "evaluation.json").exists()

    first_history = load_premarket_history(settings)
    assert len(first_history) == 1
    assert first_history[0]["direction_correct"] is True
    assert first_history[0]["long_only_cumulative_return"] == pytest.approx(0.02)
    assert first_history[0]["long_short_cumulative_return"] == pytest.approx(0.02)
    assert load_premarket_history(settings, settled_before=TARGET) == []

    down_snapshot = seal_premarket_snapshot(_shifted_snapshot_body(1))
    down_snapshot_path = tmp_path / "down-snapshot.json"
    down_snapshot_path.write_bytes(canonical_json(down_snapshot))
    down_job = prepare_premarket_run(
        settings,
        snapshot_path=down_snapshot_path,
        now=datetime(2026, 8, 19, 9, 12, tzinfo=ZONE),
    )
    down_handoff = build_premarket_handoff(
        run_id=down_job.name,
        snapshot=down_snapshot,
        prepared_at=datetime(2026, 8, 19, 9, 12, tzinfo=ZONE),
    )
    down_drafts_payload = _draft_bundle(down_handoff).model_dump(mode="json")
    strategy_draft = next(
        item
        for item in down_drafts_payload["drafts"]
        if item["agent_id"] == "strategy_agent"
    )
    strategy_draft["direction"] = "down"
    strategy_draft["probabilities"] = {
        "up": 0.20,
        "neutral": 0.25,
        "down": 0.55,
    }
    down_drafts_payload["generated_at"] = datetime(
        2026,
        8,
        19,
        9,
        18,
        tzinfo=ZONE,
    ).isoformat()
    down_drafts = PremarketDraftBundleV1.model_validate(down_drafts_payload)
    (down_job / "drafts.json").write_bytes(canonical_json(down_drafts))
    down_forecast = finalize_premarket_run(
        settings,
        job_dir=down_job,
        now=datetime(2026, 8, 19, 9, 20, tzinfo=ZONE),
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


def test_evaluate_recovers_existing_outcome_and_serializes_identical_calls(
    tmp_path: Path,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    forecast = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    observed_at = datetime(2026, 8, 19, 9, 31, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=observed_at,
            source=_source("recoverable-outcome", observed_at - timedelta(seconds=10)),
        )
    )
    outcome_path = tmp_path / "recoverable-outcome.json"
    outcome_path.write_bytes(canonical_json(outcome))
    (job_dir / "outcome.json").write_bytes(canonical_json(outcome))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            evaluate_premarket_run,
            settings,
            job_dir=job_dir,
            outcome_path=outcome_path,
            now=observed_at,
        )
        second = pool.submit(
            evaluate_premarket_run,
            settings,
            job_dir=job_dir,
            outcome_path=outcome_path,
            now=observed_at + timedelta(seconds=1),
        )
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert results[0].content_hash == results[1].content_hash
    assert results[0].evaluated_at == results[1].evaluated_at
    assert PremarketEvaluationV1.model_validate_json(
        (job_dir / "evaluation.json").read_bytes()
    ).content_hash == results[0].content_hash


def test_evaluate_rejects_conflicting_outcome_and_resealed_evaluation(
    tmp_path: Path,
) -> None:
    settings, job_dir = _prepared_service_job(tmp_path)
    forecast = finalize_premarket_run(
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 18, 9, 20, tzinfo=ZONE),
    )
    observed_at = datetime(2026, 8, 19, 9, 31, tzinfo=ZONE)
    outcome = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=102.0,
            observed_at=observed_at,
            source=_source("original-outcome", observed_at - timedelta(seconds=10)),
        )
    )
    outcome_path = tmp_path / "original-outcome.json"
    outcome_path.write_bytes(canonical_json(outcome))
    evaluate_premarket_run(
        settings,
        job_dir=job_dir,
        outcome_path=outcome_path,
        now=observed_at,
    )

    conflicting = seal_premarket_outcome(
        PremarketOutcomeBodyV1(
            forecast_hash=forecast.content_hash,
            forecast_session=FORECAST,
            target_session=TARGET,
            start_open=100.0,
            end_open=101.0,
            observed_at=observed_at,
            source=_source("conflicting-outcome", observed_at - timedelta(seconds=10)),
        )
    )
    conflicting_path = tmp_path / "conflicting-outcome.json"
    conflicting_path.write_bytes(canonical_json(conflicting))
    with pytest.raises(PremarketServiceError, match="outcome.json has different content"):
        evaluate_premarket_run(
            settings,
            job_dir=job_dir,
            outcome_path=conflicting_path,
            now=observed_at + timedelta(seconds=1),
        )

    evaluation_path = job_dir / "evaluation.json"
    tampered = json.loads(evaluation_path.read_bytes())
    tampered["brier_score"] = 0.0
    tampered["content_hash"] = content_hash(tampered)
    evaluation_path.unlink()
    evaluation_path.write_bytes(canonical_json(tampered))
    with pytest.raises(PremarketServiceError, match="does not reproduce"):
        evaluate_premarket_run(
            settings,
            job_dir=job_dir,
            outcome_path=outcome_path,
            now=observed_at + timedelta(seconds=1),
        )
