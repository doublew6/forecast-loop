from __future__ import annotations

import hashlib
import os
from datetime import datetime, time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from app.models import LessonProposal, ReflectionFinding, ReflectionRun, WorkflowRun
from app.services import reflection_markdown as reflection_markdown_service
from app.services.evaluation import evaluate_forecast
from app.services.reflection import (
    MarketSnapshotFact,
    create_reflection_run,
    materialize_evaluation_batch,
)
from app.services.reflection_markdown import (
    ReflectionMarkdownError,
    write_reflection_markdown,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

ZONE = ZoneInfo("Asia/Shanghai")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _archivable_reflection(client: TestClient) -> tuple[object, ReflectionRun]:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-10T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    session = client.app.state.database.session_factory()
    run = session.scalar(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.forecasts))
        .where(WorkflowRun.id == response.json()["id"])
    )
    assert run is not None
    run.mode = "live"
    forecast = next(
        item
        for item in run.forecasts
        if item.index_code == "000300.SH" and item.horizon == "D1"
    )
    observed_at = datetime.combine(forecast.target_date, time(15, 10), tzinfo=ZONE)
    evaluation = evaluate_forecast(
        session,
        forecast=forecast,
        price_source="synthetic-market-source",
        observed_at=observed_at,
        start_trade_date=forecast.base_trade_date,
        start_close=100.0,
        start_source_url="https://example.org/market/start",
        start_source_hash=_digest("start"),
        end_trade_date=forecast.target_date,
        end_close=96.0,
        end_source_url="https://example.org/market/end",
        end_source_hash=_digest("end"),
        now=observed_at,
    )
    session.commit()
    session.refresh(forecast)
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
                amount=123_000_000.0,
                advancers=500,
                decliners=4_500,
                unchanged=100,
                limit_down_count=80,
                breadth_down_ratio=4_500 / 5_100,
                sector_contributions=[{"name": "金融", "contribution": -0.012}],
                weight_contributions=[{"name": "样本股", "contribution": -0.004}],
                historical_abs_return_percentile=0.995,
                history_sample_size=1_250,
                source_url="https://example.org/market/session",
                source_hash=_digest("market-source"),
                captured_at=observed_at,
            )
        ],
        source_hash=_digest("evaluation-source"),
        now=observed_at,
        data_quality={"errors": 0, "warnings": 0},
    )
    reflection = create_reflection_run(
        session,
        source_run=run,
        source_batch=batch,
        input_hash=_digest("reflection-input"),
        now=observed_at,
    )
    reflection.status = "completed"
    reflection.completed_at = observed_at
    reflection.source_snapshot_hash = _digest("source-snapshot")
    reflection.output_hash = _digest("output")
    reflection.receipt_hash = _digest("receipt")
    finding = ReflectionFinding(
        id=str(uuid4()),
        reflection_run_id=reflection.id,
        scope_type="committee",
        subject_id="committee",
        index_code=forecast.index_code,
        horizon=forecast.horizon,
        verdict="wrong",
        primary_error_type="reasoning_or_weighting_failure",
        secondary_error_types=["attention_omission"],
        evidence_ids=["source-1"],
        availability_class="available_missed",
        causal_status="supported",
        counterfactual={
            "direction": "down",
            "would_flip": True,
            "basis": "pre_cutoff_only",
        },
        remediation=["提高反证权重"],
        confidence=0.82,
        summary="原判断低估了已冻结风险，但不把相关性写成已证实因果。",
        created_at=observed_at,
    )
    session.add(finding)
    session.flush()
    session.add(
        LessonProposal(
            id=str(uuid4()),
            reflection_run_id=reflection.id,
            episode_key=forecast.target_date.isoformat(),
            cluster_key="committee|risk-gate|broad-market",
            title="极端下跌风险门禁候选",
            summary="单次极端事件只提出风险检查表，不修改方向权重。",
            status="candidate",
            proposal_type="risk_checklist",
            evidence_finding_ids=[finding.id],
            independent_episode_count=1,
            replay_target_dates=0,
            replay_metrics={},
            half_life_sessions=60,
            created_at=observed_at,
        )
    )
    session.commit()
    return session, reflection


def test_completed_live_reflection_writes_hash_sealed_markdown_idempotently(
    client: TestClient,
    tmp_path: Path,
) -> None:
    session, reflection = _archivable_reflection(client)
    try:
        reflections_root = tmp_path / "reflections"
        lessons_root = tmp_path / "lessons"
        first = write_reflection_markdown(
            session,
            reflection,
            reflections_root=reflections_root,
            lessons_root=lessons_root,
        )
        second = write_reflection_markdown(
            session,
            reflection.id,
            reflections_root=reflections_root,
            lessons_root=lessons_root,
        )

        assert first == second
        assert first.reflection.path.name == (
            f"{reflection.target_date.isoformat()}-D1-{reflection.id}.md"
        )
        assert len(first.lessons) == 1
        reflection_text = first.reflection.path.read_text(encoding="utf-8")
        lesson_text = first.lessons[0].path.read_text(encoding="utf-8")
        assert "artifact_type: \"vericouncil_reflection\"" in reflection_text
        assert f'artifact_payload_sha256: "{first.reflection.payload_hash}"' in reflection_text
        assert f'receipt_hash: "{_digest("receipt")}"' in reflection_text
        assert "## 预测与实际行情" in reflection_text
        assert "reasoning_or_weighting_failure" in reflection_text
        assert "artifact_type: \"vericouncil_lesson_proposal\"" in lesson_text
        assert "不是正式 Wiki 条目" in lesson_text
        assert hashlib.sha256(first.reflection.path.read_bytes()).hexdigest() == (
            first.reflection.file_hash
        )
        assert first.reflection.path.stat().st_mode & 0o222 == 0
        assert first.lessons[0].path.stat().st_mode & 0o222 == 0
    finally:
        session.close()


@pytest.mark.parametrize("use_default_roots", [True, False])
def test_lesson_link_resolves_across_default_and_custom_archive_roots(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_default_roots: bool,
) -> None:
    session, reflection = _archivable_reflection(client)
    try:
        if use_default_roots:
            reflections_root = tmp_path / "default" / "reflection-archives"
            lessons_root = tmp_path / "default" / "lesson-archives"
            monkeypatch.setattr(
                reflection_markdown_service,
                "DEFAULT_REFLECTIONS_ROOT",
                reflections_root,
            )
            monkeypatch.setattr(
                reflection_markdown_service,
                "DEFAULT_LESSONS_ROOT",
                lessons_root,
            )
            artifacts = write_reflection_markdown(session, reflection.id)
        else:
            reflections_root = tmp_path / "configured" / "records" / "reflections"
            lessons_root = tmp_path / "configured" / "published" / "lessons"
            artifacts = write_reflection_markdown(
                session,
                reflection.id,
                reflections_root=reflections_root,
                lessons_root=lessons_root,
            )

        lesson = artifacts.lessons[0]
        relative_reference = Path(
            os.path.relpath(artifacts.reflection.path, start=lesson.path.parent)
        ).as_posix()
        lesson_text = lesson.path.read_text(encoding="utf-8")

        assert f"]({relative_reference})" in lesson_text
        assert (lesson.path.parent / relative_reference).resolve() == (
            artifacts.reflection.path.resolve()
        )
    finally:
        session.close()


@pytest.mark.parametrize(
    ("source_mode", "reflection_status", "message"),
    [
        ("demo", "completed", "completed live prediction"),
        ("live", "awaiting_analysis", "must be completed"),
    ],
)
def test_demo_and_noncompleted_reflections_are_rejected(
    client: TestClient,
    tmp_path: Path,
    source_mode: str,
    reflection_status: str,
    message: str,
) -> None:
    session, reflection = _archivable_reflection(client)
    try:
        reflection.source_run.mode = source_mode
        reflection.status = reflection_status
        session.commit()
        with pytest.raises(ReflectionMarkdownError, match=message):
            write_reflection_markdown(
                session,
                reflection.id,
                reflections_root=tmp_path / "reflections",
                lessons_root=tmp_path / "lessons",
            )
        assert not (tmp_path / "reflections").exists()
        assert not (tmp_path / "lessons").exists()
    finally:
        session.close()


def test_symlink_root_and_unsafe_horizon_are_rejected(
    client: TestClient,
    tmp_path: Path,
) -> None:
    session, reflection = _archivable_reflection(client)
    try:
        real_root = tmp_path / "real-reflections"
        real_root.mkdir()
        linked_root = tmp_path / "linked-reflections"
        linked_root.symlink_to(real_root, target_is_directory=True)
        with pytest.raises(ReflectionMarkdownError, match="may not be a symlink"):
            write_reflection_markdown(
                session,
                reflection.id,
                reflections_root=linked_root,
                lessons_root=tmp_path / "lessons",
            )

        reflection.horizon = "../D1"
        session.commit()
        with pytest.raises(ReflectionMarkdownError, match="horizon must be D1 or D2"):
            write_reflection_markdown(
                session,
                reflection.id,
                reflections_root=real_root,
                lessons_root=tmp_path / "lessons-2",
            )
    finally:
        session.close()


def test_existing_different_markdown_is_never_overwritten(
    client: TestClient,
    tmp_path: Path,
) -> None:
    session, reflection = _archivable_reflection(client)
    try:
        artifacts = write_reflection_markdown(
            session,
            reflection.id,
            reflections_root=tmp_path / "reflections",
            lessons_root=tmp_path / "lessons",
        )
        path = artifacts.reflection.path
        path.chmod(0o644)
        path.write_text("tampered\n", encoding="utf-8")
        path.chmod(0o444)

        with pytest.raises(ReflectionMarkdownError, match="different content"):
            write_reflection_markdown(
                session,
                reflection.id,
                reflections_root=tmp_path / "reflections",
                lessons_root=tmp_path / "lessons",
            )
        assert path.read_text(encoding="utf-8") == "tampered\n"
    finally:
        session.close()
