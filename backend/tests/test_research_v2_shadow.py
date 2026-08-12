from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.db import Database
from app.models import (
    AgentSignalV2Record,
    AgentTrace,
    AgentTraceArtifactLink,
    AgentTraceSpan,
    ForecastV2,
    ReasoningReviewV2,
    ResearchRunV2,
)
from app.research_v2 import (
    CSI300,
    CSI1000,
    CSI1000_D1_TARGET,
    DEFAULT_RESEARCH_PROGRAM_V2,
    DailyReturnV2,
    EvidenceSnapshotBodyV2,
    InstrumentEvidenceV2,
    OutcomeCalendarSourceStampV2,
    OutcomePriceSourceStampV2,
    SourceStampV2,
    TradingCalendarStampV2,
    content_hash,
    seal_evidence_snapshot,
    seal_outcome_observation,
    threshold_for_target,
)
from app.services.research_v2 import agent_scorecards_v2, evaluate_research_target
from app.services.research_v2_shadow import (
    MANUAL_SHADOW_AGENT_VERSION_V2,
    ManualShadowInputV2,
    admit_manual_shadow_signal_v2,
    admit_quant_shadow_signal_v2,
    finalize_shadow_reasoning_review_v2,
    seal_manual_shadow_input_v2,
)
from app.services.schema_readiness import upgrade_database
from pydantic import ValidationError
from sqlalchemy import func, select

TZ = ZoneInfo("Asia/Shanghai")


def test_manual_v2_shadow_is_d1_only_idempotent_and_never_creates_forecast(
    tmp_path: Path,
) -> None:
    settings, database, run, snapshot = _completed_run(tmp_path, mode="live")
    try:
        submission = _manual_submission(run, snapshot)
        accepted_at = datetime(2026, 8, 12, 20, 35, tzinfo=TZ)

        first = admit_manual_shadow_signal_v2(
            database,
            settings,
            submission=submission,
            accepted_at=accepted_at,
        )
        replay = admit_manual_shadow_signal_v2(
            database,
            settings,
            submission=submission,
            accepted_at=accepted_at + timedelta(minutes=1),
        )

        assert replay.id == first.id
        assert first.agent_id == "user_judgment_agent"
        assert first.agent_version == MANUAL_SHADOW_AGENT_VERSION_V2
        assert first.target_id == CSI1000_D1_TARGET
        assert first.signal_kind == "natural_view"
        assert first.natural_horizon == "D1"
        assert first.decision_horizon is None
        assert first.program_hash == run.program_hash
        assert first.input_hash == run.input_hash
        assert first.evidence_cutoff == snapshot.data_cutoff
        assert first.envelope["draft"]["probabilities"] == {
            "up": 0.6,
            "neutral": 0.25,
            "down": 0.15,
        }
        with database.session_factory() as session:
            traces = session.scalars(
                select(AgentTrace)
                .where(AgentTrace.subject_id == f"shadow:{first.id}"[:64])
                .order_by(AgentTrace.attempt_number)
            ).all()
            assert len(traces) == 2
            spans = session.scalars(
                select(AgentTraceSpan)
                .where(AgentTraceSpan.trace_id == traces[0].id)
                .order_by(AgentTraceSpan.started_at, AgentTraceSpan.node_id)
            ).all()
            assert {span.node_id for span in spans} == {
                "shadow.external_receipt",
                "shadow.validator",
                "shadow.persistence",
            }
            by_node = {span.node_id: span for span in spans}
            assert (
                by_node["shadow.validator"].parent_span_id
                == by_node["shadow.external_receipt"].span_id
            )
            assert (
                by_node["shadow.persistence"].parent_span_id == by_node["shadow.validator"].span_id
            )
            link = session.scalar(
                select(AgentTraceArtifactLink).where(
                    AgentTraceArtifactLink.trace_id == traces[0].id
                )
            )
            assert link is not None
            assert link.artifact_kind == "signal"
            assert link.artifact_id == first.id
            forecasts = session.scalars(select(ForecastV2)).all()
            assert len(forecasts) == 1
            assert session.scalar(select(func.count()).select_from(AgentSignalV2Record)) == 1
            scorecards = agent_scorecards_v2(
                session,
                generated_at=datetime(2026, 8, 13, tzinfo=TZ),
            )
        natural = next(
            section for section in scorecards["sections"] if section["axis"] == "natural_horizon"
        )
        item = natural["items"][0]
        assert item["agent_version"] == MANUAL_SHADOW_AGENT_VERSION_V2
        assert item["model_name"] == "human-explicit-multiclass"
        assert item["prompt_version"] == "manual-shadow-d1/v2"
        assert item["note"].startswith("Shadow-only D1 benchmark")

        review_dir = settings.handoff_root / "v2" / run.id / "shadow-reasoning" / first.id
        task = json.loads((review_dir / "input.json").read_text())
        assert task["outcomes_included"] is False
        assert task["signal_id"] == first.id
        assert not any("actual" in key or "outcome" in key for key in task["review"])
        template = json.loads((review_dir / "drafts.template.json").read_text())
        template["reviews"][0]["rubric"] = {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "evidence_relevance": 2,
            "causal_chain": 2,
            "target_horizon_mapping": 2,
            "counter_evidence_and_invalidation": 2,
            "probability_uncertainty_consistency": 2,
            "advisory": "The blind shadow reasoning is internally consistent.",
        }
        (review_dir / "drafts.json").write_text(
            json.dumps(template),
            encoding="utf-8",
        )
        review = finalize_shadow_reasoning_review_v2(
            database,
            settings,
            job_dir=review_dir,
        )
        replayed_review = finalize_shadow_reasoning_review_v2(
            database,
            settings,
            job_dir=review_dir,
        )
        assert replayed_review.id == review.id
        assert review.signal_id == first.id
        assert review.total_score == 10
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ReasoningReviewV2)) == 1
    finally:
        database.dispose()


def test_manual_v2_rejects_inferred_probability_shape_wrong_binding_and_late_input(
    tmp_path: Path,
) -> None:
    settings, database, run, snapshot = _completed_run(tmp_path, mode="demo")
    try:
        raw = _manual_submission(run, snapshot).model_dump(mode="json", exclude={"content_hash"})
        raw.pop("probabilities")
        raw["confidence"] = 0.7
        with pytest.raises(ValidationError):
            seal_manual_shadow_input_v2(raw)

        wrong_mode = _manual_submission(run, snapshot).model_dump(
            mode="json", exclude={"content_hash"}
        )
        wrong_mode["mode"] = "live"
        forged = seal_manual_shadow_input_v2(wrong_mode)
        with pytest.raises(Exception, match="does not exactly bind"):
            admit_manual_shadow_signal_v2(
                database,
                settings,
                submission=forged,
                accepted_at=datetime(2026, 8, 12, 20, 35, tzinfo=TZ),
            )

        with pytest.raises(Exception, match="deadline has passed"):
            admit_manual_shadow_signal_v2(
                database,
                settings,
                submission=_manual_submission(run, snapshot),
                accepted_at=datetime(2026, 8, 13, 0, 0, tzinfo=TZ),
            )
    finally:
        database.dispose()


def test_manual_v2_shadow_uses_the_standard_expiry_evaluator(tmp_path: Path) -> None:
    settings, database, run, snapshot = _completed_run(tmp_path, mode="demo")
    try:
        signal = admit_manual_shadow_signal_v2(
            database,
            settings,
            submission=_manual_submission(run, snapshot),
            accepted_at=datetime(2026, 8, 12, 20, 35, tzinfo=TZ),
        )
        observed_at = datetime(2026, 8, 13, 15, 5, tzinfo=TZ)
        source = OutcomePriceSourceStampV2(
            instrument=CSI1000,
            start_trade_date=snapshot.base_session,
            end_trade_date=snapshot.future_sessions[0],
            source_url="https://example.com/market",
            source_hash="3" * 64,
            observed_at=observed_at,
            ingested_at=observed_at,
        )
        calendar = OutcomeCalendarSourceStampV2(
            sessions=[snapshot.base_session, snapshot.future_sessions[0]],
            source_url="https://example.com/market",
            source_hash="4" * 64,
            observed_at=observed_at,
            ingested_at=observed_at,
        )
        outcome = seal_outcome_observation(
            {
                "program_hash": run.program_hash,
                "mode": run.mode,
                "target_id": CSI1000_D1_TARGET,
                "anchor_date": snapshot.base_session,
                "target_date": snapshot.future_sessions[0],
                "primary_start_close": 100,
                "primary_end_close": 101,
                "observed_at": observed_at,
                "primary_source": source,
                "calendar_source": calendar,
                "source_hashes": {CSI1000: "3" * 64, "calendar": "4" * 64},
            }
        )
        path = tmp_path / "outcome.json"
        path.write_text(outcome.model_dump_json(), encoding="utf-8")

        rows = evaluate_research_target(database, settings, observation_path=path)

        assert [row.signal_id for row in rows] == [signal.id]
        assert rows[0].actual_label == "up"
    finally:
        database.dispose()


def test_quant_v2_uses_exact_csi1000_d1_candidate_and_missing_input_creates_nothing(
    tmp_path: Path,
) -> None:
    settings, database, run, snapshot = _completed_run(tmp_path, mode="demo")
    try:
        with database.session_factory() as session:
            before = session.scalar(select(func.count()).select_from(AgentSignalV2Record))
        # Missing Quant means no admission call and therefore no synthetic row.
        with database.session_factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(AgentSignalV2Record)) == before == 0
            )

        candidate = _FakeCandidate(snapshot, run.snapshot_hash)
        source = _FakeQuantSource(candidate)
        import app.services.research_v2_shadow as shadow_service

        original = shadow_service.LocalJsonQuantSignalSource
        shadow_service.LocalJsonQuantSignalSource = lambda **_kwargs: source
        try:
            row = admit_quant_shadow_signal_v2(
                database,
                settings,
                run_id=run.id,
                quant_root=tmp_path,
                manifest_path=Path("manifest.json"),
                accepted_at=datetime(2026, 8, 12, 20, 35, tzinfo=TZ),
            )
        finally:
            shadow_service.LocalJsonQuantSignalSource = original

        assert source.requested_target.index_code == CSI1000
        assert source.requested_target.horizon.value == "D1"
        assert row.agent_id == "quant_agent"
        assert row.agent_version == "0.3.0"
        assert row.target_id == CSI1000_D1_TARGET
        assert row.natural_horizon == "D1"
        assert row.decision_horizon is None
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AgentSignalV2Record)) == 1
            assert session.scalar(select(func.count()).select_from(ForecastV2)) == 1
            assert not session.scalars(
                select(AgentSignalV2Record).where(AgentSignalV2Record.natural_horizon == "W1")
            ).all()
    finally:
        database.dispose()


class _FakeCandidate:
    def __init__(self, snapshot, snapshot_hash: str) -> None:
        from app.agent_contracts import AgentSignalDraft, SignalProvenance
        from app.domain import AgentSourceType
        from app.services.research_v2_shadow import d1_shadow_target

        self.target = d1_shadow_target(snapshot)
        self.evidence_snapshot_hash = snapshot_hash
        self.bundle_content_hash = "7" * 64
        self.manifest_sha256 = "8" * 64
        self.market_universe_hash = None
        self.test_weight = None
        self.draft = AgentSignalDraft(
            signal_id="quant-csi1000-d1",
            submitted_at=datetime(2026, 8, 12, 18, 20, tzinfo=TZ),
            direction="up",
            probabilities={"up": 0.58, "neutral": 0.27, "down": 0.15},
            rationale="Frozen synthetic model output supports the D1 direction.",
            counter_evidence=("Volatility shock can dominate momentum.",),
            invalidation_conditions=("Momentum reverses before the next cutoff.",),
            payload_schema="forecast-loop.quant-signal/v1",
            source_payload={},
        )
        self.provenance = SignalProvenance(
            source_type=AgentSourceType.QUANT,
            producer="synthetic-quant",
            adapter="test-adapter",
            adapter_version="1.0.0",
            model_name="synthetic-model",
            model_version="1.0.0",
            code_version="synthetic-code-1.0.0",
            code_hash="5" * 64,
            artifact_hashes={"model": "6" * 64},
        )


class _FakeQuantSource:
    def __init__(self, candidate) -> None:
        self.candidate = candidate
        self.requested_target = None

    def load_candidate(self, *, target):
        self.requested_target = target
        return self.candidate


def _completed_run(tmp_path: Path, *, mode: str):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'shadow.sqlite3'}",
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
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    snapshot = _snapshot()
    run_input_hash = content_hash(
        {
            "schema_version": "forecast-loop.research-run/v2",
            "program_hash": snapshot.program_hash,
            "snapshot_hash": snapshot.content_hash,
            "mode": mode,
        }
    )
    run = ResearchRunV2(
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        schema_version="forecast-loop.research-run/v2",
        program_hash=snapshot.program_hash,
        snapshot_hash=snapshot.content_hash,
        input_hash=run_input_hash,
        request_hash="b" * 64,
        mode=mode,
        status="completed",
        anchor_date=snapshot.base_session,
        as_of=snapshot.as_of,
        data_cutoff=snapshot.data_cutoff,
        prepared_at=datetime(2026, 8, 12, 18, 30, tzinfo=TZ),
        completed_at=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
        error=None,
        program=DEFAULT_RESEARCH_PROGRAM_V2.model_dump(mode="json"),
        snapshot=snapshot.model_dump(mode="json"),
        receipt={},
    )
    baseline = {"up": 1 / 3, "neutral": 1 / 3, "down": 1 / 3}
    forecast_body = {
        "schema_version": "forecast-loop.forecast/v2",
        "run_id": run.id,
        "source_signal_id": "source-cio-d1",
        "program_hash": run.program_hash,
        "target_id": CSI1000_D1_TARGET,
        "horizon": "D1",
        "configured_lane": "formal",
        "effective_lane": "shadow",
        "anchor_date": snapshot.base_session,
        "target_date": snapshot.future_sessions[0],
        "probabilities": {"up": 0.5, "neutral": 0.3, "down": 0.2},
        "threshold": threshold_for_target(snapshot, CSI1000_D1_TARGET),
        "baseline_probabilities": baseline,
        "rationale": "Synthetic host context.",
        "counter_evidence": [],
        "invalidation_conditions": [],
        "input_hash": run.input_hash,
        "created_at": run.completed_at,
    }
    forecast = ForecastV2(
        id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        content_hash=content_hash(forecast_body),
        probability_up=0.5,
        probability_neutral=0.3,
        probability_down=0.2,
        **{key: value for key, value in forecast_body.items() if key != "probabilities"},
    )
    # This narrowly scoped service test uses a synthetic source id; disable FK
    # enforcement only around the fixture insert, then restore it immediately.
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            ResearchRunV2.__table__.insert(),
            [
                {
                    column.name: getattr(run, column.name)
                    for column in ResearchRunV2.__table__.columns
                }
            ],
        )
        connection.execute(
            ForecastV2.__table__.insert(),
            [
                {
                    column.name: getattr(forecast, column.name)
                    for column in ForecastV2.__table__.columns
                }
            ],
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    return settings, database, run, snapshot


def _manual_submission(run, snapshot) -> ManualShadowInputV2:
    return seal_manual_shadow_input_v2(
        {
            "submission_id": "manual-20260812-csi1000-d1",
            "run_id": run.id,
            "mode": run.mode,
            "program_hash": run.program_hash,
            "snapshot_hash": run.snapshot_hash,
            "run_input_hash": run.input_hash,
            "target_id": CSI1000_D1_TARGET,
            "index_code": CSI1000,
            "horizon": "D1",
            "anchor_date": snapshot.base_session,
            "target_date": snapshot.future_sessions[0],
            "data_cutoff": snapshot.data_cutoff,
            "submitted_at": datetime(2026, 8, 12, 20, 32, tzinfo=TZ),
            "direction": "up",
            "probabilities": {"up": 0.6, "neutral": 0.25, "down": 0.15},
            "rationale": "Independent pre-outcome judgment supports the upside class.",
            "counter_evidence": ["Liquidity conditions may reverse before the close."],
            "invalidation_conditions": ["The frozen catalyst reverses before target close."],
            "blind_attestation": True,
        }
    )


def _snapshot():
    base = date(2026, 8, 12)
    observed = datetime(2026, 8, 12, 15, tzinfo=TZ)
    stamp = SourceStampV2(
        source_url="https://example.com/market",
        source_hash="1" * 64,
        observed_at=observed,
        ingested_at=observed,
    )
    history = _business_days_ending(base, 20)
    primary = [0.001 * (-1 if index % 2 else 1) for index in range(20)]
    benchmark = [0.0004 * (-1 if index % 3 else 1) for index in range(20)]
    future_sessions = _future_business_days(base, 20)
    calendar_payload = {
        "schema_version": "forecast-loop.trading-calendar-payload/v2",
        "base_session": base,
        "sessions": future_sessions,
    }
    calendar_stamp = TradingCalendarStampV2(
        source_url=stamp.source_url,
        source_hash=content_hash(calendar_payload),
        observed_at=stamp.observed_at,
        ingested_at=stamp.ingested_at,
        base_session=base,
        sessions=future_sessions,
    )
    return seal_evidence_snapshot(
        EvidenceSnapshotBodyV2(
            program_hash=DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
            as_of=datetime(2026, 8, 12, 17, tzinfo=TZ),
            data_cutoff=datetime(2026, 8, 12, 16, tzinfo=TZ),
            created_at=datetime(2026, 8, 12, 17, tzinfo=TZ),
            base_session=base,
            future_sessions=future_sessions,
            calendar_source=calendar_stamp,
            instruments={
                CSI1000: InstrumentEvidenceV2(
                    code=CSI1000,
                    volatility_20d=statistics.stdev(primary),
                    returns=[
                        DailyReturnV2(trade_date=day, daily_return=value, source=stamp)
                        for day, value in zip(history, primary, strict=True)
                    ],
                ),
                CSI300: InstrumentEvidenceV2(
                    code=CSI300,
                    volatility_20d=statistics.stdev(benchmark),
                    returns=[
                        DailyReturnV2(trade_date=day, daily_return=value, source=stamp)
                        for day, value in zip(history, benchmark, strict=True)
                    ],
                ),
            },
        )
    )


def _business_days_ending(end: date, count: int) -> list[date]:
    values = []
    current = end
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current -= timedelta(days=1)
    return sorted(values)


def _future_business_days(start: date, count: int) -> list[date]:
    values = []
    current = start + timedelta(days=1)
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values
