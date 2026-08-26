from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import app.services.research_v2 as research_v2_service
import pytest
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.models import (
    AgentSignalV2Record,
    AgentTrace,
    AgentTraceArtifactLink,
    AgentTraceSpan,
    ForecastV2,
    ResearchRunV2,
)
from app.research_v2 import (
    CSI300,
    CSI1000,
    CSI1000_D1_TARGET,
    CSI1000_D20_RESEARCH_TARGET,
    CSI1000_RELATIVE_W1_TARGET,
    DEFAULT_RESEARCH_PROGRAM_V2,
    AgentSignalDraftV2,
    DailyReturnV2,
    EvidenceItemV2,
    EvidenceSnapshotBodyV2,
    InstrumentEvidenceV2,
    OutcomeCalendarSourceStampV2,
    OutcomePriceSourceStampV2,
    ProbabilitiesV2,
    ReflectionDraftBodyV2,
    ReflectionDraftV2,
    SourceStampV2,
    TradingCalendarStampV2,
    content_hash,
    relative_volatility_20d,
    seal_evidence_snapshot,
    seal_outcome_observation,
    seal_reflection_draft_v2,
    threshold_for_target,
)
from app.services.research_v2 import (
    ResearchV2Error,
    _frozen_baselines,
    agent_scorecards_v2,
    create_reflection_v2,
    evaluate_research_target,
    finalize_reasoning_review,
    finalize_research_run,
    prepare_research_run,
    validate_research_draft_bundle,
)
from app.services.schema_readiness import upgrade_database
from app.services.snapshot import LiveEvidenceRequiredError
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.orm import selectinload

TZ = ZoneInfo("Asia/Shanghai")


def test_snapshot_requires_two_aligned_instruments_and_exchange_sessions() -> None:
    snapshot = _snapshot()

    assert snapshot.future_sessions[0] == date(2026, 8, 13)
    assert snapshot.future_sessions[4] == date(2026, 8, 19)

    body = snapshot.model_dump(mode="json", exclude={"content_hash"})
    body["future_sessions"][-1] = "2026-09-11"
    with pytest.raises(ValidationError, match="frozen calendar payload"):
        EvidenceSnapshotBodyV2.model_validate(body)

    forged = snapshot.model_dump(mode="json", exclude={"content_hash"})
    forged["calendar_source"]["source_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="does not bind the frozen sessions"):
        EvidenceSnapshotBodyV2.model_validate(forged)
    primary_volatility = statistics.stdev(
        item.daily_return for item in snapshot.instruments[CSI1000].returns[-20:]
    )
    assert threshold_for_target(snapshot, CSI1000_D1_TARGET) == pytest.approx(
        0.25 * primary_volatility
    )
    assert threshold_for_target(snapshot, CSI1000_D20_RESEARCH_TARGET) == pytest.approx(
        0.25 * primary_volatility * math.sqrt(20)
    )
    assert threshold_for_target(snapshot, CSI1000_RELATIVE_W1_TARGET) == pytest.approx(
        0.25 * relative_volatility_20d(snapshot) * math.sqrt(5)
    )

    invalid = snapshot.model_dump(mode="json", exclude={"content_hash"})
    invalid["instruments"].pop(CSI300)
    with pytest.raises(ValidationError, match="exactly CSI1000 and CSI300"):
        EvidenceSnapshotBodyV2.model_validate(invalid)

    forged_volatility = snapshot.model_dump(mode="json", exclude={"content_hash"})
    forged_volatility["instruments"][CSI1000]["volatility_20d"] *= 2
    with pytest.raises(ValidationError, match="recomputed from the frozen trailing 20"):
        EvidenceSnapshotBodyV2.model_validate(forged_volatility)


def test_probabilistic_signal_allows_neutral_as_the_predicted_class() -> None:
    draft = AgentSignalDraftV2(
        signal_kind="natural_view",
        target_id=CSI1000_D1_TARGET,
        natural_horizon="D1",
        direction="neutral",
        probabilities=ProbabilitiesV2(up=0.2, neutral=0.6, down=0.2),
        rationale="The frozen evidence supports a noise-band outcome.",
        counter_evidence=["A late liquidity impulse could break the neutral band."],
        invalidation_conditions=["Volatility doubles before the target close."],
        evidence_item_ids=["event-1"],
        wiki_entry_id="VC-WIKI-V2",
        wiki_version="1.0.0",
        wiki_section="method",
        wiki_content_hash="a" * 64,
    )

    assert draft.direction == "neutral"
    invalid = draft.model_dump()
    invalid["probabilities"] = {"up": 0.4, "neutral": 0.2, "down": 0.4}
    with pytest.raises(ValidationError, match="unique maximum"):
        AgentSignalDraftV2.model_validate(invalid)


def test_live_prepare_rejects_untrusted_snapshot_sources(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot_path = tmp_path / "untrusted.json"
    snapshot_path.write_bytes(
        json.dumps(_snapshot().model_dump(mode="json"), sort_keys=True).encode()
    )

    database = Database(settings.database_url)
    try:
        with pytest.raises(LiveEvidenceRequiredError, match="trusted allowlist"):
            prepare_research_run(
                database,
                settings,
                snapshot_path=snapshot_path,
                mode="live",
            )
    finally:
        database.dispose()


def test_prepare_finalize_creates_d1_and_nonoverlapping_w1_with_deterministic_cio(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_bytes(
        json.dumps(_snapshot().model_dump(mode="json"), sort_keys=True).encode()
    )
    database = Database(settings.database_url)
    try:
        job_dir = prepare_research_run(
            database,
            settings,
            snapshot_path=snapshot_path,
            mode="demo",
        )
        request = json.loads((job_dir / "input.json").read_text())
        assignments = request["assignments"]
        assert {
            (item["target_id"], item["signal_kind"]): item["generation_reason"]
            for item in assignments
            if item["signal_kind"] == "natural_view"
        } == {
            (CSI1000_D1_TARGET, "natural_view"): "daily",
            (CSI1000_RELATIVE_W1_TARGET, "natural_view"): "bootstrap",
            (CSI1000_D20_RESEARCH_TARGET, "natural_view"): "bootstrap",
        }
        assert sum(item["producer"] == "deterministic" for item in assignments) == 2
        assert any(item["signal_kind"] == "d1_impact" for item in assignments)
        drafts = []
        for assignment in assignments:
            if assignment["producer"] != "codex":
                continue
            drafts.append(
                {
                    "assignment_id": assignment["assignment_id"],
                    "draft": _draft_for_assignment(assignment),
                }
            )
        bundle = {
            "schema_version": "forecast-loop.codex-handoff/v3",
            "run_id": request["run_id"],
            "request_hash": request["request_hash"],
            "generated_at": datetime(2026, 8, 12, 18, 30, tzinfo=TZ).isoformat(),
            "generated_by": {
                "surface": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
            },
            "drafts": drafts,
        }
        (job_dir / "drafts.json").write_text(json.dumps(bundle), encoding="utf-8")
        finalized = finalize_research_run(
            database,
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
        )
        assert finalized.status == "completed"
        with database.session_factory() as session:
            forecasts = session.scalars(
                select(ForecastV2).order_by(ForecastV2.target_id)
            ).all()
            assert {item.target_id for item in forecasts} == {
                CSI1000_D1_TARGET,
                CSI1000_RELATIVE_W1_TARGET,
            }
            assert {item.effective_lane for item in forecasts} == {"shadow"}
            cio_signals = session.scalars(
                select(AgentSignalV2Record).where(
                    AgentSignalV2Record.signal_kind == "decision_forecast"
                )
            ).all()
            assert len(cio_signals) == 2
            assert {item.model_name for item in cio_signals} == {
                "forecast-loop-deterministic-cio"
            }

        next_snapshot = _snapshot(base_session=date(2026, 8, 13))
        next_path = tmp_path / "next.json"
        next_path.write_text(next_snapshot.model_dump_json(), encoding="utf-8")
        next_job = prepare_research_run(
            database,
            settings,
            snapshot_path=next_path,
            mode="demo",
        )
        next_request = json.loads((next_job / "input.json").read_text())
        assert {
            (item["target_id"], item["generation_reason"])
            for item in next_request["assignments"]
            if item["signal_kind"] == "natural_view"
        } == {(CSI1000_D1_TARGET, "daily")}
        assert not any(
            item["target_id"] == CSI1000_RELATIVE_W1_TARGET
            and item["signal_kind"] in {"strategy_forecast", "decision_forecast"}
            for item in next_request["assignments"]
        )
    finally:
        database.dispose()


def test_prepare_reuses_first_frozen_run_for_same_anchor_date(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    first_snapshot = _snapshot()
    first_path = tmp_path / "first.json"
    first_path.write_text(first_snapshot.model_dump_json(), encoding="utf-8")

    revised_body = first_snapshot.model_dump(mode="python", exclude={"content_hash"})
    revised_body["items"][0]["summary"] = "A later same-day source revision."
    revised_snapshot = seal_evidence_snapshot(
        EvidenceSnapshotBodyV2.model_validate(revised_body)
    )
    assert revised_snapshot.content_hash != first_snapshot.content_hash
    revised_path = tmp_path / "revised.json"
    revised_path.write_text(revised_snapshot.model_dump_json(), encoding="utf-8")

    database = Database(settings.database_url)
    try:
        first_job = prepare_research_run(
            database,
            settings,
            snapshot_path=first_path,
            mode="demo",
        )
        repeated_job = prepare_research_run(
            database,
            settings,
            snapshot_path=revised_path,
            mode="demo",
        )

        assert repeated_job == first_job
        with database.session_factory() as session:
            runs = session.scalars(select(ResearchRunV2)).all()
            assert len(runs) == 1
            assert runs[0].snapshot_hash == first_snapshot.content_hash
    finally:
        database.dispose()


def test_external_dispatcher_can_validate_before_publishing_drafts(
    tmp_path: Path,
) -> None:
    settings, database, job_dir, _request = _prepared_demo_job(tmp_path)
    drafts_path = job_dir / "drafts.json"
    raw_drafts = drafts_path.read_bytes()
    drafts_path.unlink()
    try:
        request, bundle = validate_research_draft_bundle(
            settings,
            job_dir=job_dir,
            raw_drafts=raw_drafts,
        )
        assert request.run_id == bundle.run_id
        assert not drafts_path.exists()

        tampered = json.loads(raw_drafts)
        tampered["drafts"][0]["draft"]["evidence_item_ids"] = ["outside-snapshot"]
        with pytest.raises(ResearchV2Error, match="outside the frozen snapshot"):
            validate_research_draft_bundle(
                settings,
                job_dir=job_dir,
                raw_drafts=json.dumps(tampered).encode(),
            )
        assert not drafts_path.exists()
    finally:
        database.dispose()


def test_external_dispatcher_rejects_duplicate_assignment_before_publishing(
    tmp_path: Path,
) -> None:
    settings, database, job_dir, _request = _prepared_demo_job(tmp_path)
    drafts_path = job_dir / "drafts.json"
    payload = json.loads(drafts_path.read_bytes())
    duplicate = json.loads(json.dumps(payload["drafts"][0]))
    duplicate["draft"]["rationale"] = "A conflicting duplicate must not be accepted."
    payload["drafts"].append(duplicate)
    raw_drafts = json.dumps(payload).encode()
    drafts_path.unlink()
    try:
        with pytest.raises(ValidationError, match="draft assignment IDs must be unique"):
            validate_research_draft_bundle(
                settings,
                job_dir=job_dir,
                raw_drafts=raw_drafts,
            )
        assert not drafts_path.exists()
    finally:
        database.dispose()


def test_finalize_rejects_duplicate_assignment_without_persisting(
    tmp_path: Path,
) -> None:
    settings, database, job_dir, request = _prepared_demo_job(tmp_path)
    drafts_path = job_dir / "drafts.json"
    payload = json.loads(drafts_path.read_bytes())
    duplicate = json.loads(json.dumps(payload["drafts"][0]))
    duplicate["draft"]["rationale"] = "A conflicting duplicate must not be accepted."
    payload["drafts"].append(duplicate)
    raw_drafts = json.dumps(payload).encode()
    drafts_path.write_bytes(raw_drafts)
    try:
        with pytest.raises(ValidationError, match="draft assignment IDs must be unique"):
            finalize_research_run(
                database,
                settings,
                job_dir=job_dir,
                now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
            )

        with database.session_factory() as session:
            run = session.get(ResearchRunV2, request["run_id"])
            assert run is not None and run.status == "awaiting_draft"
            assert session.scalars(select(AgentSignalV2Record)).all() == []
            assert session.scalars(select(ForecastV2)).all() == []
            attempts = session.scalars(
                select(AgentTrace).order_by(AgentTrace.attempt_number)
            ).all()
            assert [(item.attempt_number, item.status) for item in attempts] == [
                (1, "failed")
            ]
        assert not (job_dir / "receipt.json").exists()
        assert drafts_path.read_bytes() == raw_drafts
    finally:
        database.dispose()


def test_prepare_instructions_match_non_abstaining_d1_impact_contract(
    tmp_path: Path,
) -> None:
    _settings_value, database, job_dir, request = _prepared_demo_job(tmp_path)
    try:
        instructions = " ".join((job_dir / "INSTRUCTIONS.md").read_text().split())
        assert (
            "Every non-abstaining D1 impact must provide a non-empty "
            "`transmission_chain`; only an explicit no-impact abstention may leave "
            "that list empty."
        ) in instructions

        assignment = next(
            item
            for item in request["assignments"]
            if item["signal_kind"] == "d1_impact" and item["state_available"]
        )
        invalid_draft = _draft_for_assignment(assignment)
        invalid_draft["transmission_chain"] = []
        with pytest.raises(
            ValidationError,
            match="non-abstaining D1 impact requires a transmission chain",
        ):
            AgentSignalDraftV2.model_validate(invalid_draft)
    finally:
        database.dispose()


def test_finalize_rejects_target_day_and_recovers_missing_receipt(tmp_path: Path) -> None:
    settings, database, job_dir, _request = _prepared_demo_job(tmp_path)
    try:
        with pytest.raises(ResearchV2Error, match="cutoff has passed"):
            finalize_research_run(
                database,
                settings,
                job_dir=job_dir,
                now=datetime(2026, 8, 13, 0, 0, tzinfo=TZ),
            )
        with database.session_factory() as session:
            first_attempt = session.scalars(
                select(AgentTrace).order_by(AgentTrace.attempt_number)
            ).all()
            assert [(item.attempt_number, item.status) for item in first_attempt] == [
                (1, "failed")
            ]

        finalized = finalize_research_run(
            database,
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
        )
        assert finalized.status == "completed"
        with database.session_factory() as session:
            attempts = session.scalars(
                select(AgentTrace).order_by(AgentTrace.attempt_number)
            ).all()
            assert [(item.attempt_number, item.status) for item in attempts] == [
                (1, "failed"),
                (2, "completed"),
            ]

        receipt_path = job_dir / "receipt.json"
        original = receipt_path.read_bytes()
        receipt_path.unlink()
        recovered = finalize_research_run(database, settings, job_dir=job_dir)
        assert recovered.id == finalized.id
        assert receipt_path.read_bytes() == original
    finally:
        database.dispose()


def test_reasoning_task_failure_does_not_undo_completed_forecast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, job_dir, _request = _prepared_demo_job(tmp_path)
    original = research_v2_service.prepare_reasoning_review

    def fail_reasoning(*_args, **_kwargs):
        raise OSError("simulated advisory file failure")

    try:
        monkeypatch.setattr(
            research_v2_service,
            "prepare_reasoning_review",
            fail_reasoning,
        )
        finalized = finalize_research_run(
            database,
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
        )
        assert finalized.status == "completed"
        assert (job_dir / "receipt.json").is_file()
        assert not (job_dir / "reasoning" / "input.json").exists()
        with database.session_factory() as session:
            trace = session.scalar(select(AgentTrace))
            assert trace is not None and trace.status == "degraded"

        monkeypatch.setattr(
            research_v2_service,
            "prepare_reasoning_review",
            original,
        )
        recovered = finalize_research_run(database, settings, job_dir=job_dir)
        assert recovered.id == finalized.id
        assert (job_dir / "reasoning" / "input.json").is_file()
    finally:
        database.dispose()


def test_live_outcome_requires_trusted_stamps_and_is_mode_bound(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    try:
        observed_at = datetime(2026, 8, 13, 15, 5, tzinfo=TZ)
        untrusted = _outcome(
            mode="live",
            source_url="https://example.com/market",
            observed_at=observed_at,
        )
        path = tmp_path / "untrusted-outcome.json"
        path.write_text(untrusted.model_dump_json(), encoding="utf-8")
        with pytest.raises(LiveEvidenceRequiredError, match="trusted allowlist"):
            evaluate_research_target(database, settings, observation_path=path)

        malformed = untrusted.model_dump(mode="json", exclude={"content_hash"})
        malformed["calendar_source"]["sessions"][-1] = "2026-08-14"
        with pytest.raises(ValidationError, match="bound to the target horizon"):
            seal_outcome_observation(malformed)
    finally:
        database.dispose()


def test_live_baseline_excludes_demo_outcome_for_same_episode(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot_path = tmp_path / "trusted-snapshot.json"
    snapshot_path.write_text(
        _snapshot(source_url="https://www.csindex.com.cn/market").model_dump_json(),
        encoding="utf-8",
    )
    database = Database(settings.database_url)
    try:
        for mode in ("live", "demo"):
            job_dir = prepare_research_run(
                database,
                settings,
                snapshot_path=snapshot_path,
                mode=mode,
            )
            _write_drafts(job_dir)
            finalize_research_run(
                database,
                settings,
                job_dir=job_dir,
                now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
            )

        observed_at = datetime(2026, 8, 13, 15, 5, tzinfo=TZ)
        live_path = tmp_path / "live-outcome.json"
        live_path.write_text(
            _outcome(
                mode="live",
                source_url="https://www.csindex.com.cn/market",
                observed_at=observed_at,
                end_close=101,
            ).model_dump_json(),
            encoding="utf-8",
        )
        demo_path = tmp_path / "demo-outcome.json"
        demo_path.write_text(
            _outcome(
                mode="demo",
                source_url="https://example.com/market",
                observed_at=observed_at,
                end_close=99,
            ).model_dump_json(),
            encoding="utf-8",
        )
        evaluate_research_target(database, settings, observation_path=live_path)
        evaluate_research_target(database, settings, observation_path=demo_path)

        live = _frozen_baselines(
            database,
            observed_at + timedelta(hours=1),
            mode="live",
        )[CSI1000_D1_TARGET]
        demo = _frozen_baselines(
            database,
            observed_at + timedelta(hours=1),
            mode="demo",
        )[CSI1000_D1_TARGET]
        assert live.as_dict() == pytest.approx({"up": 0.5, "neutral": 0.25, "down": 0.25})
        assert demo.as_dict() == pytest.approx({"up": 0.25, "neutral": 0.25, "down": 0.5})
    finally:
        database.dispose()


def test_risk_critic_scorecard_uses_coverage_and_missed_risk_not_direction(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot_path = tmp_path / "trusted-risk-snapshot.json"
    snapshot_path.write_text(
        _snapshot(source_url="https://www.csindex.com.cn/market").model_dump_json(),
        encoding="utf-8",
    )
    database = Database(settings.database_url)
    try:
        job_dir = prepare_research_run(
            database,
            settings,
            snapshot_path=snapshot_path,
            mode="live",
        )
        _write_drafts(job_dir)
        bundle = json.loads((job_dir / "drafts.json").read_text())
        for item in bundle["drafts"]:
            if item["draft"]["signal_kind"] == "risk_critique":
                item["draft"]["risk_severity"] = "low"
        (job_dir / "drafts.json").write_text(json.dumps(bundle), encoding="utf-8")
        finalize_research_run(
            database,
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
        )
        outcome_path = tmp_path / "risk-outcome.json"
        outcome_path.write_text(
            _outcome(
                mode="live",
                source_url="https://www.csindex.com.cn/market",
                observed_at=datetime(2026, 8, 13, 15, 5, tzinfo=TZ),
                end_close=99,
            ).model_dump_json(),
            encoding="utf-8",
        )
        evaluate_research_target(database, settings, observation_path=outcome_path)

        with database.session_factory() as session:
            scorecards = agent_scorecards_v2(
                session,
                generated_at=datetime(2026, 8, 13, 16, 0, tzinfo=TZ),
            )
        reasoning = next(
            section
            for section in scorecards["sections"]
            if section["axis"] == "reasoning"
        )
        critic = next(
            item
            for item in reasoning["items"]
            if item["signal_kind"] == "risk_critique"
            and item["target_id"] == CSI1000_D1_TARGET
        )
        assert critic["average_brier"] is None
        assert critic["direction_accuracy"] is None
        assert critic["risk_diagnostics"] == {
            "critique_count": 1,
            "counter_evidence_coverage_rate": 1.0,
            "invalidation_coverage_rate": 1.0,
            "risk_flag_rate": 0.0,
            "evaluated_system_errors": 1,
            "missed_risk_count": 1,
            "missed_risk_rate": 1.0,
        }
    finally:
        database.dispose()


def test_scorecard_brier_skill_is_relative_to_the_frozen_baseline() -> None:
    assert research_v2_service._relative_brier_skill(0.19, 0.22) == pytest.approx(
        0.1363636364
    )
    assert research_v2_service._relative_brier_skill(0.0, 0.0) is None


def test_reasoning_evaluation_and_reflection_have_sealed_artifact_traces(
    tmp_path: Path,
) -> None:
    settings, database, job_dir, request = _prepared_demo_job(tmp_path)
    try:
        finalize_research_run(
            database,
            settings,
            job_dir=job_dir,
            now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
        )
        reasoning_task = json.loads((job_dir / "reasoning" / "input.json").read_text())
        reasoning_bundle = {
            "schema_version": "forecast-loop.reasoning-review-drafts/v2",
            "run_id": request["run_id"],
            "generated_at": datetime(2026, 8, 12, 21, 0, tzinfo=TZ).isoformat(),
            "generated_by": {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
            },
            "reviews": [
                {
                    "signal_id": item["signal_id"],
                    "review_input_hash": item["review_input_hash"],
                    "rubric": {
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "high",
                        "evidence_relevance": 2,
                        "causal_chain": 2,
                        "target_horizon_mapping": 2,
                        "counter_evidence_and_invalidation": 2,
                        "probability_uncertainty_consistency": 2,
                        "advisory": "The frozen input is internally consistent.",
                    },
                }
                for item in reasoning_task["reviews"]
            ],
        }
        (job_dir / "reasoning" / "drafts.json").write_text(
            json.dumps(reasoning_bundle),
            encoding="utf-8",
        )
        reviews = finalize_reasoning_review(database, settings, job_dir=job_dir)
        reasoning_trace = _assert_artifact_trace(
            database,
            subject_prefix="reasoning:",
            expected_kind="reasoning_review",
            expected_ids={item.id for item in reviews},
        )
        assert {item.node_id for item in reasoning_trace.spans} == {
            "reasoning.validator",
            "reasoning.persistence",
        }

        outcome_path = tmp_path / "trace-outcome.json"
        outcome_path.write_text(
            _outcome(
                mode="demo",
                source_url="https://example.com/market",
                observed_at=datetime(2026, 8, 13, 15, 5, tzinfo=TZ),
            ).model_dump_json(),
            encoding="utf-8",
        )
        signal_evaluations = evaluate_research_target(
            database,
            settings,
            observation_path=outcome_path,
        )
        evaluation_trace = _assert_artifact_trace(
            database,
            subject_prefix="evaluation:",
            expected_kind="evaluation",
            expected_ids={item.id for item in signal_evaluations},
            allow_more=True,
        )
        assert {item.node_id for item in evaluation_trace.spans} == {
            "evaluation.validator",
            "evaluation.persistence",
        }

        with database.session_factory() as session:
            forecast = session.scalar(
                select(ForecastV2)
                .options(selectinload(ForecastV2.evaluation))
                .where(ForecastV2.target_id == CSI1000_D1_TARGET)
            )
            assert forecast is not None and forecast.evaluation is not None
            reflection_draft = _reflection_draft(forecast)
        tampered = reflection_draft.model_dump(mode="json")
        tampered["actual_label"] = "down"
        with pytest.raises(ValidationError, match="content_hash mismatch"):
            ReflectionDraftV2.model_validate(tampered)
        wrong_binding = seal_reflection_draft_v2(
            ReflectionDraftBodyV2(
                **reflection_draft.model_dump(exclude={"content_hash", "forecast_hash"}),
                forecast_hash="f" * 64,
            )
        )
        with pytest.raises(ResearchV2Error, match="identity does not match"):
            create_reflection_v2(database, settings, draft=wrong_binding)
        reflection = create_reflection_v2(
            database,
            settings,
            draft=reflection_draft,
        )
        assert reflection.envelope == reflection_draft.model_dump(mode="json")
        assert reflection.content_hash == reflection_draft.content_hash
        assert reflection.forecast_hash == forecast.content_hash
        assert reflection.evaluation_id == forecast.evaluation.id
        assert reflection.evaluation_hash == forecast.evaluation.content_hash
        reflection_trace = _assert_artifact_trace(
            database,
            subject_prefix="reflection:",
            expected_kind="reflection",
            expected_ids={reflection.id},
        )
        assert {item.node_id for item in reflection_trace.spans} == {
            "reflection.validator",
            "reflection.persistence",
        }

        with TestClient(create_app(settings, allow_schema_bootstrap=True)) as client:
            for trace, kind, artifact_id in (
                (reasoning_trace, "reasoning_review", reviews[0].id),
                (evaluation_trace, "evaluation", signal_evaluations[0].id),
                (reflection_trace, "reflection", reflection.id),
            ):
                detail = client.get(f"/api/agent-traces/{trace.id}")
                assert detail.status_code == 200, detail.text
                assert any(
                    item["artifact_kind"] == kind
                    and item["artifact_id"] == artifact_id
                    and item["span_id"] is not None
                    for item in detail.json()["artifact_links"]
                )

        before = _trace_count(database)
        assert finalize_reasoning_review(database, settings, job_dir=job_dir)
        assert evaluate_research_target(database, settings, observation_path=outcome_path)
        assert (
            create_reflection_v2(
                database,
                settings,
                draft=reflection_draft,
            ).id
            == reflection.id
        )
        conflicting = seal_reflection_draft_v2(
            ReflectionDraftBodyV2(
                **reflection_draft.model_dump(exclude={"content_hash", "verdict"}),
                verdict="wrong",
            )
        )
        with pytest.raises(ResearchV2Error, match="conflicting content"):
            create_reflection_v2(database, settings, draft=conflicting)
        assert _trace_count(database) == before
    finally:
        database.dispose()


def test_v2_api_empty_state(client) -> None:
    assert client.get("/api/v2/research-program").status_code == 200
    forecasts = client.get("/api/v2/forecasts/latest").json()
    assert forecasts["formal"] is None
    assert forecasts["shadow"] is None
    scorecards = client.get("/api/v2/agent-scorecards").json()
    assert [item["axis"] for item in scorecards["sections"]] == [
        "final_system",
        "natural_horizon",
        "d1_impact",
        "reasoning",
        "incremental_value",
    ]
    assert client.get("/api/v2/reasoning-reviews").json()["items"] == []
    assert client.get("/api/agent-evals/jobs-v2").json()["items"] == []


def test_outcome_migration_separates_demo_and_live_episode_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    try:
        inspector = inspect(database.engine)
        columns = {
            item["name"] for item in inspector.get_columns("outcome_observations_v2")
        }
        constraints = {
            item["name"]: tuple(item["column_names"])
            for item in inspector.get_unique_constraints("outcome_observations_v2")
        }
        assert "mode" in columns
        assert constraints["uq_outcome_observation_v2_episode"] == (
            "program_hash",
            "target_id",
            "anchor_date",
            "target_date",
            "mode",
        )
        reflection_columns = {
            item["name"] for item in inspector.get_columns("reflections_v2")
        }
        assert {
            "forecast_hash",
            "evaluation_id",
            "evaluation_hash",
            "anchor_date",
            "actual_label",
            "envelope",
        }.issubset(reflection_columns)
        reflection_constraints = {
            item["name"]: tuple(item["column_names"])
            for item in inspector.get_unique_constraints("reflections_v2")
        }
        assert reflection_constraints["uq_reflection_v2_evaluation"] == (
            "evaluation_id",
        )
    finally:
        database.dispose()


def _snapshot(
    base_session: date = date(2026, 8, 12),
    *,
    source_url: str = "https://example.com/market",
):
    observed = datetime.combine(base_session, datetime.min.time(), TZ) + timedelta(hours=15)
    stamp = SourceStampV2(
        source_url=source_url,
        source_hash="1" * 64,
        observed_at=observed,
        ingested_at=observed,
    )
    history_dates = _business_days_ending(base_session, 20)
    primary_returns = [0.001 * (-1 if index % 2 else 1) for index in range(20)]
    benchmark_returns = [0.0004 * (-1 if index % 3 else 1) for index in range(20)]
    future_sessions = _future_business_days(base_session, 20)
    calendar_payload = {
        "schema_version": "forecast-loop.trading-calendar-payload/v2",
        "base_session": base_session,
        "sessions": future_sessions,
    }
    calendar_stamp = TradingCalendarStampV2(
        source_url=source_url,
        source_hash=content_hash(calendar_payload),
        observed_at=observed,
        ingested_at=observed,
        base_session=base_session,
        sessions=future_sessions,
    )
    body = EvidenceSnapshotBodyV2(
        program_hash=DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
        as_of=observed + timedelta(hours=2),
        data_cutoff=observed + timedelta(hours=1),
        created_at=observed + timedelta(hours=2),
        base_session=base_session,
        future_sessions=future_sessions,
        calendar_source=calendar_stamp,
        instruments={
            CSI1000: InstrumentEvidenceV2(
                code=CSI1000,
                volatility_20d=statistics.stdev(primary_returns),
                returns=[
                    DailyReturnV2(trade_date=trade_date, daily_return=value, source=stamp)
                    for trade_date, value in zip(history_dates, primary_returns, strict=True)
                ],
            ),
            CSI300: InstrumentEvidenceV2(
                code=CSI300,
                volatility_20d=statistics.stdev(benchmark_returns),
                returns=[
                    DailyReturnV2(trade_date=trade_date, daily_return=value, source=stamp)
                    for trade_date, value in zip(history_dates, benchmark_returns, strict=True)
                ],
            ),
        },
        items=[
            EvidenceItemV2(
                item_id="event-1",
                title="Frozen event",
                summary="A bounded pre-cutoff event.",
                published_at=observed,
                ingested_at=observed,
                source_url=source_url,
                source_hash="2" * 64,
                entities=[CSI1000],
            )
        ],
    )
    return seal_evidence_snapshot(body)


def _outcome(
    *,
    mode: str,
    source_url: str,
    observed_at: datetime,
    end_close: float = 101,
):
    source = OutcomePriceSourceStampV2(
        instrument=CSI1000,
        start_trade_date=date(2026, 8, 12),
        end_trade_date=date(2026, 8, 13),
        source_url=source_url,
        source_hash="3" * 64,
        observed_at=observed_at,
        ingested_at=observed_at,
    )
    calendar = OutcomeCalendarSourceStampV2(
        sessions=[date(2026, 8, 12), date(2026, 8, 13)],
        source_url=source_url,
        source_hash="4" * 64,
        observed_at=observed_at,
        ingested_at=observed_at,
    )
    return seal_outcome_observation(
        {
            "program_hash": DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
            "mode": mode,
            "target_id": CSI1000_D1_TARGET,
            "anchor_date": date(2026, 8, 12),
            "target_date": date(2026, 8, 13),
            "primary_start_close": 100,
            "primary_end_close": end_close,
            "observed_at": observed_at,
            "primary_source": source,
            "calendar_source": calendar,
            "source_hashes": {CSI1000: "3" * 64, "calendar": "4" * 64},
        }
    )


def _prepared_demo_job(tmp_path: Path):
    settings = _settings(tmp_path)
    upgrade_database(settings.database_url)
    _write_wiki(settings.wiki_path)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(_snapshot().model_dump_json(), encoding="utf-8")
    database = Database(settings.database_url)
    job_dir = prepare_research_run(
        database,
        settings,
        snapshot_path=snapshot_path,
        mode="demo",
    )
    request = _write_drafts(job_dir)
    return settings, database, job_dir, request


def _write_drafts(job_dir: Path) -> dict:
    request = json.loads((job_dir / "input.json").read_text())
    bundle = {
        "schema_version": "forecast-loop.codex-handoff/v3",
        "run_id": request["run_id"],
        "request_hash": request["request_hash"],
        "generated_at": datetime(2026, 8, 12, 18, 30, tzinfo=TZ).isoformat(),
        "generated_by": {
            "surface": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        },
        "drafts": [
            {
                "assignment_id": assignment["assignment_id"],
                "draft": _draft_for_assignment(assignment),
            }
            for assignment in request["assignments"]
            if assignment["producer"] == "codex"
        ],
    }
    (job_dir / "drafts.json").write_text(json.dumps(bundle), encoding="utf-8")
    return request


def _assert_artifact_trace(
    database: Database,
    *,
    subject_prefix: str,
    expected_kind: str,
    expected_ids: set[str],
    allow_more: bool = False,
) -> AgentTrace:
    with database.session_factory() as session:
        traces = session.scalars(
            select(AgentTrace).where(AgentTrace.subject_id.startswith(subject_prefix))
        ).all()
        assert len(traces) == 1
        trace = traces[0]
        spans = session.scalars(
            select(AgentTraceSpan).where(AgentTraceSpan.trace_id == trace.id)
        ).all()
        links = session.scalars(
            select(AgentTraceArtifactLink).where(
                AgentTraceArtifactLink.trace_id == trace.id,
                AgentTraceArtifactLink.artifact_kind == expected_kind,
            )
        ).all()
        linked_ids = {item.artifact_id for item in links}
        if allow_more:
            assert linked_ids.issuperset(expected_ids)
        else:
            assert linked_ids == expected_ids
        assert trace.status == "completed"
        assert trace.telemetry_complete is True
        assert len(spans) == 2
        persistence = next(item for item in spans if item.span_kind == "persistence")
        validator = next(item for item in spans if item.span_kind == "validator")
        assert persistence.parent_span_id == validator.span_id
        trace.spans = spans
        return trace


def _trace_count(database: Database) -> int:
    with database.session_factory() as session:
        return len(session.scalars(select(AgentTrace)).all())


def _reflection_draft(forecast: ForecastV2) -> ReflectionDraftV2:
    assert forecast.evaluation is not None
    return seal_reflection_draft_v2(
        ReflectionDraftBodyV2(
            forecast_id=forecast.id,
            forecast_hash=forecast.content_hash,
            evaluation_id=forecast.evaluation.id,
            evaluation_hash=forecast.evaluation.content_hash,
            target_id=forecast.target_id,
            anchor_date=forecast.anchor_date,
            target_date=forecast.target_date,
            actual_label=forecast.evaluation.actual_label,
            verdict="right_reason",
            findings=[],
        )
    )


def _draft_for_assignment(assignment: dict) -> dict:
    common = {
        "signal_kind": assignment["signal_kind"],
        "target_id": assignment["target_id"],
        "natural_horizon": assignment["natural_horizon"],
        "decision_horizon": assignment["decision_horizon"],
        "state_available": assignment["state_available"],
        "rationale": "Frozen evidence and the stated transmission mechanism support this view.",
        "transmission_chain": ["evidence", "earnings", "index"] ,
        "counter_evidence": ["The transmission could be offset before the target close."],
        "invalidation_conditions": ["The stated catalyst reverses before target close."],
        "evidence_item_ids": ["event-1"],
        "wiki_entry_id": assignment["wiki_entry_id"],
        "wiki_version": assignment["wiki_version"],
        "wiki_section": assignment["wiki_section"],
        "wiki_content_hash": assignment["wiki_content_hash"],
    }
    if assignment["signal_kind"] in {"natural_view", "strategy_forecast"}:
        return {
            **common,
            "direction": "up",
            "probabilities": {"up": 0.5, "neutral": 0.3, "down": 0.2},
        }
    if assignment["signal_kind"] == "d1_impact":
        if not assignment["state_available"]:
            return {
                **common,
                "impact": "none",
                "importance": "none",
                "abstain": True,
                "transmission_chain": [],
            }
        return {**common, "impact": "positive", "importance": "medium"}
    return {
        **common,
        "transmission_chain": [],
        "risk_severity": "medium",
    }


def _business_days_ending(end: date, count: int) -> list[date]:
    result = []
    current = end
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current -= timedelta(days=1)
    return sorted(result)


def _future_business_days(start: date, count: int) -> list[date]:
    result = []
    current = start + timedelta(days=1)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'v2.sqlite3'}",
        checkpoint_path=tmp_path / "checkpoint.sqlite3",
        wiki_path=tmp_path / "wiki",
        wiki_source_root=tmp_path / "wiki-sources",
        wiki_handoff_root=tmp_path / "wiki-jobs",
        wiki_feedback_root=tmp_path / "wiki-feedback",
        prediction_status_root=tmp_path / "prediction-status",
        user_judgment_wiki_root=tmp_path / "user-wiki",
        handoff_root=tmp_path / "handoffs",
        agent_eval_private_root=tmp_path / "evals",
        demo_mode=True,
        auto_seed=False,
    )


def _write_wiki(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "v2.md").write_text(
        """---
id: VC-WIKI-V2
title: Focused V2 Method
version: 1.0.0
updated_at: 2026-08-01
published_at: 2026-08-01T00:00:00+08:00
status: active
owners: [forecast-loop]
tags: [macro, market, ai, strategy, risk, evidence]
source_urls: [https://example.com/method]
---
<!-- section:method -->
# Method
Freeze evidence, distinguish horizons, and state invalidation conditions.
""",
        encoding="utf-8",
    )
    (root / "strategy.md").write_text(
        """---
id: VC-WIKI-MARKET-STRATEGY
title: Focused Strategy
version: 1.0.0
updated_at: 2026-08-01
published_at: 2026-08-01T00:00:00+08:00
status: active
owners: [strategy_agent]
tags: [strategy, allocation]
source_urls: [https://example.com/strategy]
---
<!-- section:method -->
# Method
Aggregate frozen evidence without crossing target or horizon boundaries.
""",
        encoding="utf-8",
    )
