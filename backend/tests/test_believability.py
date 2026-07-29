from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.domain import AGENT_BY_ID
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import (
    AgentOpinion,
    EvaluationBatch,
    Forecast,
    ReflectionFinding,
    ReflectionHumanReview,
    ReflectionRun,
    WorkflowRun,
)
from app.services.believability import (
    BelievabilityAgentScope,
    believability_run_binding_hash,
    build_believability_snapshot,
    validate_believability_snapshot,
)
from app.services.evaluation import evaluate_forecast
from app.services.reflection import (
    MarketSnapshotFact,
    create_reflection_run,
    materialize_evaluation_batch,
)
from app.services.reflection_governance import record_reflection_human_review
from app.services.run_bundle import RunBundleError, export_run_bundle
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

ZONE = ZoneInfo("Asia/Shanghai")
INDEX_CODE = "000300.SH"
HORIZON = "D1"


def test_believability_snapshot_is_shadow_only_and_cutoff_bound(
    client: TestClient,
) -> None:
    history = _create_reviewed_history(client)
    scopes = _agent_scopes(history["models"])

    with client.app.state.database.session_factory() as session:
        before = build_believability_snapshot(
            session,
            mode="live",
            as_of=history["evaluated_at"],
            data_cutoff=history["evaluated_at"] - timedelta(seconds=1),
            agent_scopes=scopes,
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )
        after = build_believability_snapshot(
            session,
            mode="live",
            as_of=history["reviewed_at"] + timedelta(minutes=1),
            data_cutoff=history["reviewed_at"],
            agent_scopes=scopes,
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )

    before_macro = _profile(before, "macro_policy_agent")
    after_macro = _profile(after, "macro_policy_agent")
    assert before_macro.evaluation_sample_size == 0
    assert before_macro.human_reviewed_right_reason_proxy_count == 0
    assert after_macro.evaluation_sample_size == 1
    assert after_macro.independent_evaluation_target_dates == 1
    assert after_macro.human_reviewed_right_reason_proxy_count == 1
    assert after_macro.right_reason_supported_count == 1
    assert after.phase == "shadow"
    assert after.applied_to_decision is False
    assert after.activation_supported is False
    assert after_macro.proposed_stage_multiplier is None
    assert after_macro.applied_stage_multiplier == 1.0


def test_demo_snapshot_never_imports_formal_history(client: TestClient) -> None:
    history = _create_reviewed_history(client)
    with client.app.state.database.session_factory() as session:
        snapshot = build_believability_snapshot(
            session,
            mode="demo",
            as_of=history["reviewed_at"] + timedelta(minutes=1),
            data_cutoff=history["reviewed_at"],
            agent_scopes=_agent_scopes(history["models"]),
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )

    assert snapshot.phase == "demo_excluded"
    assert snapshot.gate.completed_live_evaluation_target_dates == 0
    assert snapshot.gate.completed_live_reflection_target_dates == 0
    assert snapshot.gate.approved_initial_reflection_prefix == 0
    assert all(item.evaluation_sample_size == 0 for item in snapshot.profiles)
    assert all(item.evidence_status == "demo_excluded" for item in snapshot.profiles)


def test_snapshot_ignores_self_reported_weight_raw_response_and_confidence(
    client: TestClient,
) -> None:
    history = _create_reviewed_history(client)
    cutoff = history["reviewed_at"]
    as_of = cutoff + timedelta(minutes=1)
    scopes = _agent_scopes(history["models"])
    with client.app.state.database.session_factory() as session:
        original = build_believability_snapshot(
            session,
            mode="live",
            as_of=as_of,
            data_cutoff=cutoff,
            agent_scopes=scopes,
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )
        opinion = session.get(AgentOpinion, history["macro_opinion_id"])
        finding = session.get(ReflectionFinding, history["finding_id"])
        assert opinion is not None and finding is not None
        opinion.weight = 999.0
        opinion.raw_response = {
            **opinion.raw_response,
            "self_reported_believability": 1.0,
        }
        finding.confidence = 0.01
        session.commit()
        rebuilt = build_believability_snapshot(
            session,
            mode="live",
            as_of=as_of,
            data_cutoff=cutoff,
            agent_scopes=scopes,
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )

    assert rebuilt.content_hash == original.content_hash


def test_snapshot_isolated_by_model_identity_and_rejects_tampering(
    client: TestClient,
) -> None:
    history = _create_reviewed_history(client)
    scopes = list(_agent_scopes(history["models"]))
    macro = next(item for item in scopes if item.agent_id == "macro_policy_agent")
    scopes[scopes.index(macro)] = BelievabilityAgentScope(
        agent_id=macro.agent_id,
        agent_version=macro.agent_version,
        model_name="different-model",
        role_domain=macro.role_domain,
        stage=macro.stage,
        current_stage_weight_metadata=macro.current_stage_weight_metadata,
    )
    with client.app.state.database.session_factory() as session:
        snapshot = build_believability_snapshot(
            session,
            mode="live",
            as_of=history["reviewed_at"] + timedelta(minutes=1),
            data_cutoff=history["reviewed_at"],
            agent_scopes=tuple(scopes),
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )

    assert _profile(snapshot, "macro_policy_agent").evaluation_sample_size == 0
    tampered = snapshot.model_dump(mode="json")
    tampered["profiles"][0]["average_brier"] = 0.0
    with pytest.raises(ValueError, match="content hash"):
        validate_believability_snapshot(tampered)


def test_snapshot_excludes_same_code_history_from_another_market_universe(
    client: TestClient,
) -> None:
    history = _create_reviewed_history(client)
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, history["run_id"])
        assert run is not None
        run.market_universe_hash = "f" * 64
        session.commit()
        snapshot = build_believability_snapshot(
            session,
            mode="live",
            as_of=history["reviewed_at"] + timedelta(minutes=1),
            data_cutoff=history["reviewed_at"],
            agent_scopes=_agent_scopes(history["models"]),
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )

    macro = _profile(snapshot, "macro_policy_agent")
    assert macro.evaluation_sample_size == 0
    assert macro.human_reviewed_right_reason_proxy_count == 0
    assert snapshot.gate.completed_live_evaluation_target_dates == 0
    assert snapshot.gate.completed_live_reflection_target_dates == 0
    assert snapshot.gate.approved_initial_reflection_prefix == 0


def test_global_gates_cannot_promote_an_empty_exact_identity_profile(
    client: TestClient,
) -> None:
    completed_at = datetime(2026, 7, 17, 18, 0, tzinfo=ZONE)
    with client.app.state.database.session_factory() as session:
        run = WorkflowRun(
            id="believability-global-gate-run",
            as_of=completed_at,
            data_cutoff=completed_at,
            status="completed",
            mode="live",
            started_at=completed_at,
            completed_at=completed_at,
            duration_seconds=1.0,
            error=None,
            data_quality={},
            workflow_steps=[],
            input_hash="1" * 64,
        )
        session.add(run)
        for offset in range(20):
            target_date = date(2026, 6, 1) + timedelta(days=offset)
            batch = EvaluationBatch(
                id=f"believability-global-batch-{offset}",
                target_date=target_date,
                horizon=HORIZON,
                status="completed",
                evaluation_set_hash=f"{offset + 2:064x}",
                source_hash=f"{offset + 40:064x}",
                data_quality={},
                started_at=completed_at,
                completed_at=completed_at,
                error=None,
            )
            reflection = ReflectionRun(
                id=f"believability-global-reflection-{offset:02d}",
                source_run_id=run.id,
                source_batch_id=batch.id,
                horizon=HORIZON,
                target_date=target_date,
                schema_version="1.0.0",
                evaluation_set_hash=batch.evaluation_set_hash,
                status="completed",
                supersedes_id=None,
                created_at=completed_at,
                completed_at=completed_at,
                error=None,
                input_hash=f"{offset + 60:064x}",
                source_snapshot_hash=f"{offset + 80:064x}",
                output_hash=f"{offset + 100:064x}",
                receipt_hash=f"{offset + 120:064x}",
            )
            session.add_all([batch, reflection])
            if offset < 10:
                session.add(
                    ReflectionHumanReview(
                        id=f"believability-global-review-{offset}",
                        reflection_run_id=reflection.id,
                        decision="approved",
                        reviewer="operator",
                        notes="checked",
                        notes_hash=_digest(f"global-review-{offset}"),
                        reviewed_at=completed_at,
                    )
                )
        session.commit()
        agent = AGENT_BY_ID["macro_policy_agent"]
        snapshot = build_believability_snapshot(
            session,
            mode="live",
            as_of=datetime(2026, 7, 24, 15, 0, tzinfo=ZONE),
            data_cutoff=datetime(2026, 7, 24, 14, 59, tzinfo=ZONE),
            agent_scopes=(
                BelievabilityAgentScope(
                    agent_id=agent.id,
                    agent_version=agent.version,
                    model_name="brand-new-model",
                    role_domain="research",
                    stage="research_to_strategy",
                    current_stage_weight_metadata=agent.weight,
                ),
            ),
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )

    assert snapshot.gate.completed_live_reflection_target_dates == 20
    assert snapshot.gate.approved_initial_reflection_prefix == 10
    assert snapshot.phase == "shadow"
    assert "exact_identity_history_below_minimum" in snapshot.gate.blockers
    assert "exact_identity_reasoning_proxy_below_minimum" in snapshot.gate.blockers


def test_completed_successor_excludes_approved_old_reflection(
    client: TestClient,
) -> None:
    history = _create_reviewed_history(client)
    successor_completed_at = history["reviewed_at"] + timedelta(minutes=5)
    with client.app.state.database.session_factory() as session:
        old = session.get(ReflectionRun, history["reflection_id"])
        assert old is not None
        successor = ReflectionRun(
            id="believability-successor",
            source_run_id=old.source_run_id,
            source_batch_id=old.source_batch_id,
            horizon=old.horizon,
            target_date=old.target_date,
            schema_version="1.1.0",
            evaluation_set_hash=old.evaluation_set_hash,
            status="completed",
            supersedes_id=old.id,
            created_at=successor_completed_at,
            completed_at=successor_completed_at,
            error=None,
            input_hash="8" * 64,
            source_snapshot_hash="9" * 64,
            output_hash="a" * 64,
            receipt_hash="b" * 64,
        )
        session.add(successor)
        session.commit()
        snapshot = build_believability_snapshot(
            session,
            mode="live",
            as_of=successor_completed_at + timedelta(minutes=1),
            data_cutoff=successor_completed_at,
            agent_scopes=_agent_scopes(history["models"]),
            index_codes=(INDEX_CODE,),
            horizons=(HORIZON,),
            market_universe_hash=DEFAULT_MARKET_UNIVERSE.content_hash,
            required_live_target_dates=20,
            required_approved_reflections=10,
        )

    macro = _profile(snapshot, "macro_policy_agent")
    assert macro.approved_reflection_count == 0
    assert macro.human_reviewed_right_reason_proxy_count == 0
    assert snapshot.gate.approved_initial_reflection_prefix == 0


def test_workflow_persists_and_seals_audit_only_snapshot(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-20T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    full = body["data_quality"]["believability_snapshot"]
    runtime = body["data_quality"]["believability"]
    snapshot = validate_believability_snapshot(full)
    assert snapshot.mode == "demo"
    assert snapshot.applied_to_decision is False
    assert runtime == {
        "policy_version": "1.0.0-shadow",
        "snapshot_hash": snapshot.content_hash,
        "run_binding_hash": believability_run_binding_hash(
            body["id"], snapshot.content_hash
        ),
        "mode": "shadow_only",
        "applied_to_decision": False,
    }


def test_workflow_rejects_database_side_snapshot_tampering(
    client: TestClient,
) -> None:
    workflow = client.app.state.workflow
    prepared = workflow.prepare_run(
        as_of=datetime(2026, 7, 21, 15, 0, tzinfo=ZONE),
    )
    with client.app.state.database.session_factory() as session:
        row = session.get(WorkflowRun, prepared.row.id)
        assert row is not None
        quality = dict(row.data_quality)
        snapshot = dict(quality["believability_snapshot"])
        profiles = [dict(item) for item in snapshot["profiles"]]
        profiles[0]["evaluation_sample_size"] = 999
        snapshot["profiles"] = profiles
        quality["believability_snapshot"] = snapshot
        row.data_quality = quality
        session.commit()

    with pytest.raises(RuntimeError, match="content hash"):
        workflow.execute_prepared(prepared)
    with client.app.state.database.session_factory() as session:
        failed = session.get(WorkflowRun, prepared.row.id)
        assert failed is not None
        assert failed.status == "failed"
        assert "content hash" in (failed.error or "")


def test_workflow_rechecks_snapshot_after_graph_execution(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = client.app.state.workflow
    prepared = workflow.prepare_run(
        as_of=datetime(2026, 7, 21, 15, 0, tzinfo=ZONE),
    )
    invoke = workflow.graph.invoke

    def tampering_invoke(*args, **kwargs):
        result = invoke(*args, **kwargs)
        with client.app.state.database.session_factory() as session:
            row = session.get(WorkflowRun, prepared.row.id)
            assert row is not None
            quality = dict(row.data_quality)
            snapshot = dict(quality["believability_snapshot"])
            snapshot["phase"] = "shadow"
            quality["believability_snapshot"] = snapshot
            row.data_quality = quality
            session.commit()
        return result

    monkeypatch.setattr(workflow.graph, "invoke", tampering_invoke)
    with pytest.raises(RuntimeError, match="content hash"):
        workflow.execute_prepared(prepared)

    with client.app.state.database.session_factory() as session:
        failed = session.get(WorkflowRun, prepared.row.id)
        assert failed is not None
        assert failed.status == "failed"
        assert session.scalars(
            select(Forecast.id).where(Forecast.run_id == prepared.row.id)
        ).all() == []


def test_run_bundle_rejects_rehashed_bundle_with_invalid_internal_snapshot(
    client: TestClient,
    tmp_path,
) -> None:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-22T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    with client.app.state.database.session_factory() as session:
        row = session.get(WorkflowRun, run_id)
        assert row is not None
        quality = dict(row.data_quality)
        snapshot = dict(quality["believability_snapshot"])
        snapshot["phase"] = "governance_review_required"
        quality["believability_snapshot"] = snapshot
        row.data_quality = quality
        session.commit()

    with pytest.raises(RunBundleError, match="content hash"):
        export_run_bundle(
            client.app.state.database,
            run_id=run_id,
            output_root=tmp_path / "exports",
        )
    assert not (tmp_path / "exports" / run_id).exists()


def test_v2_run_bundle_cannot_drop_or_transplant_believability_seal(
    client: TestClient,
    tmp_path,
) -> None:
    first = client.post(
        "/api/runs",
        json={"as_of": "2026-07-22T15:00:00+08:00"},
    )
    second = client.post(
        "/api/runs",
        json={"as_of": "2026-07-23T15:00:00+08:00"},
    )
    assert first.status_code == 201 and second.status_code == 201
    with client.app.state.database.session_factory() as session:
        first_row = session.get(WorkflowRun, first.json()["id"])
        second_row = session.get(WorkflowRun, second.json()["id"])
        assert first_row is not None and second_row is not None
        first_quality = dict(first_row.data_quality)
        second_row.data_quality = {}
        session.commit()

    with pytest.raises(RunBundleError, match="missing its believability"):
        export_run_bundle(
            client.app.state.database,
            run_id=second.json()["id"],
            output_root=tmp_path / "missing",
        )
    assert not (tmp_path / "missing" / second.json()["id"]).exists()

    with client.app.state.database.session_factory() as session:
        second_row = session.get(WorkflowRun, second.json()["id"])
        assert second_row is not None
        second_row.data_quality = first_quality
        session.commit()
    with pytest.raises(
        RunBundleError,
        match="runtime seal|belongs to another run",
    ):
        export_run_bundle(
            client.app.state.database,
            run_id=second.json()["id"],
            output_root=tmp_path / "transplanted",
        )
    assert not (tmp_path / "transplanted" / second.json()["id"]).exists()


def _create_reviewed_history(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-10T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, run_id)
        assert run is not None
        run.mode = "live"
        run.completed_at = datetime(2026, 7, 10, 15, 1, tzinfo=ZONE)
        session.commit()

    with client.app.state.database.session_factory() as session:
        forecast = session.scalar(
            select(Forecast)
            .options(selectinload(Forecast.run), selectinload(Forecast.evaluation))
            .where(
                Forecast.run_id == run_id,
                Forecast.index_code == INDEX_CODE,
                Forecast.horizon == HORIZON,
            )
        )
        assert forecast is not None
        evaluated_at = datetime.combine(
            forecast.target_date,
            time(15, 10),
            tzinfo=ZONE,
        )
        evaluate_forecast(
            session,
            forecast=forecast,
            price_source="test-provider",
            observed_at=evaluated_at,
            start_trade_date=forecast.base_trade_date,
            start_close=100.0,
            start_source_url="https://example.com/believability-start",
            start_source_hash=_digest("believability-start"),
            end_trade_date=forecast.target_date,
            end_close=102.0,
            end_source_url="https://example.com/believability-end",
            end_source_hash=_digest("believability-end"),
            now=evaluated_at,
        )
        session.commit()

    with client.app.state.database.session_factory() as session:
        run = session.scalar(
            select(WorkflowRun)
            .options(selectinload(WorkflowRun.forecasts))
            .where(WorkflowRun.id == run_id)
        )
        forecast = session.scalar(
            select(Forecast)
            .options(selectinload(Forecast.evaluation))
            .where(
                Forecast.run_id == run_id,
                Forecast.index_code == INDEX_CODE,
                Forecast.horizon == HORIZON,
            )
        )
        assert run is not None and forecast is not None and forecast.evaluation is not None
        evaluation = forecast.evaluation
        batch = materialize_evaluation_batch(
            session,
            target_date=forecast.target_date,
            horizon=forecast.horizon,
            snapshots=[
                MarketSnapshotFact(
                    index_code=forecast.index_code,
                    index_name=forecast.index_name,
                    target_date=forecast.target_date,
                    base_trade_date=forecast.base_trade_date,
                    base_close=evaluation.start_close,
                    target_close=evaluation.end_close,
                    actual_return=evaluation.actual_return,
                    source_url="https://example.com/believability-market",
                    source_hash=_digest("believability-market"),
                    captured_at=evaluated_at,
                    breadth_down_ratio=0.4,
                    history_sample_size=500,
                )
            ],
            source_hash=_digest("believability-batch"),
            now=evaluated_at,
        )
        reflection = create_reflection_run(
            session,
            source_run=run,
            source_batch=batch,
            input_hash=_digest("believability-reflection-input"),
            now=evaluated_at,
        )
        reflection.status = "completed"
        reflection.completed_at = evaluated_at + timedelta(minutes=1)
        reflection.output_hash = _digest("believability-reflection-output")
        reflection.receipt_hash = _digest("believability-reflection-receipt")
        macro = session.scalar(
            select(AgentOpinion).where(
                AgentOpinion.run_id == run_id,
                AgentOpinion.agent_id == "macro_policy_agent",
                AgentOpinion.index_code == INDEX_CODE,
                AgentOpinion.horizon == HORIZON,
            )
        )
        assert macro is not None
        finding = ReflectionFinding(
            id="believability-finding",
            reflection_run_id=reflection.id,
            scope_type="agent",
            subject_id=macro.agent_id,
            index_code=macro.index_code,
            horizon=macro.horizon,
            verdict="right_reason",
            primary_error_type="unresolved",
            secondary_error_types=[],
            evidence_ids=["frozen-evidence", "frozen-outcome"],
            availability_class="available_used",
            causal_status="supported",
            counterfactual={"would_flip": False},
            remediation=[],
            confidence=0.95,
            summary="The original causal chain was supported.",
            created_at=reflection.completed_at,
        )
        session.add(finding)
        reviewed_at = evaluated_at + timedelta(minutes=2)
        record_reflection_human_review(
            session,
            reflection_id=reflection.id,
            decision="approved",
            reviewer="operator",
            notes="checked source and causal status",
            reviewed_at=reviewed_at,
        )
        models = {
            item.agent_id: item.model_name
            for item in session.scalars(
                select(AgentOpinion).where(
                    AgentOpinion.run_id == run_id,
                    AgentOpinion.agent_id.in_(
                        (
                            "macro_policy_agent",
                            "market_news_agent",
                            "ai_storage_industry_agent",
                            "strategy_agent",
                        )
                    ),
                )
            ).all()
        }
        session.commit()
        return {
            "run_id": run_id,
            "evaluated_at": evaluated_at,
            "reviewed_at": reviewed_at,
            "models": models,
            "macro_opinion_id": macro.id,
            "finding_id": finding.id,
            "reflection_id": reflection.id,
        }


def _agent_scopes(models: object) -> tuple[BelievabilityAgentScope, ...]:
    assert isinstance(models, dict)
    scopes = []
    for agent_id in (
        "macro_policy_agent",
        "market_news_agent",
        "ai_storage_industry_agent",
        "strategy_agent",
    ):
        agent = AGENT_BY_ID[agent_id]
        is_strategy = agent_id == "strategy_agent"
        scopes.append(
            BelievabilityAgentScope(
                agent_id=agent_id,
                agent_version=agent.version,
                model_name=str(models[agent_id]),
                role_domain="strategy" if is_strategy else "research",
                stage=(
                    "strategy_to_cio"
                    if is_strategy
                    else "research_to_strategy"
                ),
                current_stage_weight_metadata=agent.weight,
            )
        )
    return tuple(scopes)


def _profile(snapshot, agent_id: str):
    return next(item for item in snapshot.profiles if item.agent_id == agent_id)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
