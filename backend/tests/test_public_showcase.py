from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from app.models import ForecastV2
from app.public_showcase import PublicSnapshot, export_snapshot, project_forecast, write_snapshot
from app.services.research_v2 import evaluate_research_target, finalize_research_run
from pydantic import ValidationError
from sqlalchemy import select
from test_research_v2 import TZ, _outcome, _prepared_demo_job


@pytest.fixture
def publication_source(tmp_path):
    settings, database, job_dir, _ = _prepared_demo_job(tmp_path)
    finalize_research_run(
        database,
        settings,
        job_dir=job_dir,
        now=datetime(2026, 8, 12, 20, 30, tzinfo=TZ),
    )
    yield settings, database, tmp_path / "v2.sqlite3"
    database.dispose()


def test_explicit_demo_export_is_read_only_and_omits_research(publication_source):
    _, _, path = publication_source
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = export_snapshot(path, mode="demo")
    assert snapshot.mode == "demo"
    assert len(snapshot.forecasts) == 2
    assert {item.lane for item in snapshot.forecasts} == {"shadow"}
    raw = snapshot.model_dump_json()
    for private_field in (
        "rationale",
        "source_url",
        "wiki",
        "model_name",
        "run_id",
        "input_hash",
        "counter_evidence",
        "evidence_item",
        "receipt",
        "operator",
        "prompt",
    ):
        assert private_field not in raw
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="no completed forecasts"):
        export_snapshot(path)


def test_export_reproduces_settlement(publication_source):
    settings, database, path = publication_source
    observation = _outcome(
        mode="demo",
        source_url="https://example.com/market-outcome",
        observed_at=datetime(2026, 8, 13, 16, 0, tzinfo=TZ),
    )
    observation_path = path.parent / "outcome.json"
    observation_path.write_text(observation.model_dump_json(), encoding="utf-8")
    evaluate_research_target(database, settings, observation_path=observation_path)
    snapshot = export_snapshot(path, mode="demo")
    settled = [item for item in snapshot.forecasts if item.result]
    assert len(settled) == 1
    assert settled[0].result.actual_label in {"up", "neutral", "down"}
    assert 0 <= settled[0].result.brier_score <= 2
    with database.session_factory() as session:
        row = session.scalar(select(ForecastV2).where(ForecastV2.horizon == "D1"))
        row.evaluation.brier_score = 0
        with pytest.raises(ValueError):
            project_forecast(row)


def test_reexporting_old_predictions_does_not_reset_freshness(publication_source):
    _, _, path = publication_source
    snapshot = export_snapshot(path, mode="demo")
    assert snapshot.next_update_at < snapshot.generated_at


def test_invalid_receipt_never_crosses_public_boundary(publication_source):
    _, database, _ = publication_source
    with database.session_factory() as session:
        row = session.scalar(select(ForecastV2))
        row.run.receipt = {**row.run.receipt, "receipt_hash": "0" * 64}
        with pytest.raises(ValueError):
            project_forecast(row)


def test_future_completion_never_publishes(publication_source):
    _, _, path = publication_source
    with pytest.raises(ValueError, match="future completion"):
        export_snapshot(path, mode="demo", now=datetime(2026, 8, 12, 20, 0, tzinfo=TZ))


@pytest.mark.parametrize(
    "field,value",
    [
        ("probability_up", 0.99),
        ("rationale", "altered"),
        ("content_hash", "0" * 64),
        ("effective_lane", "formal"),
    ],
)
def test_tampered_forecast_is_rejected(publication_source, field, value):
    _, database, _ = publication_source
    with database.session_factory() as session:
        row = session.scalar(select(ForecastV2))
        setattr(row, field, value)
        with pytest.raises(ValueError):
            project_forecast(row)


def test_missing_or_symlink_database_never_creates_source(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        export_snapshot(missing)
    assert not missing.exists()
    target = tmp_path / "source"
    target.write_text("not a database")
    link = tmp_path / "alias"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        export_snapshot(link)


def test_strict_contract_and_failed_export_preserve_previous(publication_source, tmp_path):
    _, _, path = publication_source
    snapshot = export_snapshot(path, mode="demo")
    output = tmp_path / "forecast.json"
    write_snapshot(snapshot, output)
    before = output.read_bytes()
    with pytest.raises(ValueError):
        export_snapshot(path)
    assert output.read_bytes() == before
    malformed = snapshot.model_dump(mode="json")
    malformed["api_key"] = "test-only"
    with pytest.raises(ValidationError):
        PublicSnapshot.model_validate(malformed)
    malformed.pop("api_key")
    malformed["forecasts"].append(malformed["forecasts"][0])
    with pytest.raises(ValidationError, match="duplicate"):
        PublicSnapshot.model_validate(malformed)
    link = tmp_path / "public-link.json"
    link.symlink_to(output)
    with pytest.raises(ValueError, match="symlink"):
        write_snapshot(snapshot, link)
    assert output.read_bytes() == before
