from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.cli import main
from app.models import (
    AgentSpecRecord,
    EvaluationBatch,
    EvaluationResult,
    Forecast,
    UserJudgment,
    WorkflowRun,
)
from app.services.judgment_bundle import (
    EVALUATION_NAME,
    FORECAST_NAME,
    JUDGMENT_NAME,
    MANIFEST_NAME,
    JudgmentBundleError,
    _canonical_json_bytes,
    _seal_manifest,
    export_judgment_bundle,
    verify_judgment_bundle,
)
from app.services.run_bundle import export_run_bundle
from app.services.user_judgment import materialize_user_judgment_evaluation
from fastapi.testclient import TestClient
from pydantic import SecretStr

ZONE = ZoneInfo("Asia/Shanghai")
FIXED_EXPORT_TIME = datetime(2026, 7, 28, 9, 0, tzinfo=ZONE)
FIXTURE_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "judgment-bundle"
    / "v1"
    / "11111111-1111-4111-8111-111111111111"
)
TEST_OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"
OPERATOR_HEADERS = {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}


def _enable_live_http(client: TestClient) -> None:
    client.app.state.settings.execution_provider = "codex_file"
    client.app.state.settings.demo_mode = False
    client.app.state.settings.operator_token = SecretStr(TEST_OPERATOR_TOKEN)


def test_private_runtime_data_uses_catch_all_gitignore_policy() -> None:
    ignore_file = Path(__file__).parents[2] / ".gitignore"
    rules = {
        line.strip()
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "/data/*" in rules
    assert "!/data/README.md" in rules


def _request(forecast_id: str, *, blind: bool) -> dict:
    return {
        "forecast_id": forecast_id,
        "direction": "up",
        "confidence": 0.67,
        "rationale": "流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。",
        "counter_evidence": "海外利率重新上行可能压制成长估值，并削弱本地流动性改善的效果。",
        "invalidation_condition": "若成交额显著收缩且跌破基准日低点，则本判断失效。",
        "blind_attestation": blind,
    }


def _create_judgment(
    client: TestClient,
    *,
    record_class: str,
) -> str:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-25T15:00:00+08:00"},
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    now = datetime.now(ZONE).replace(microsecond=0)
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, run_id)
        assert run is not None
        forecast = sorted(
            run.forecasts,
            key=lambda item: (item.index_code, item.horizon),
        )[0]
        if record_class != "demo":
            run.mode = "live"
            run.completed_at = now - timedelta(minutes=5)
            forecast.target_date = (now + timedelta(days=2)).date()
        forecast_id = forecast.id
        session.commit()

    if record_class != "demo":
        _enable_live_http(client)
    created = client.post(
        "/api/user-judgments",
        json=_request(
            forecast_id,
            blind=record_class == "formal_shadow",
        ),
        headers=OPERATOR_HEADERS if record_class != "demo" else None,
    )
    assert created.status_code == 201, created.text
    judgment_id = created.json()["id"]
    if record_class == "formal_shadow":
        _complete_evaluation(client, judgment_id=judgment_id, now=now)
    return judgment_id


def _complete_evaluation(
    client: TestClient,
    *,
    judgment_id: str,
    now: datetime,
) -> None:
    with client.app.state.database.session_factory() as session:
        judgment = session.get(UserJudgment, judgment_id)
        assert judgment is not None
        forecast = session.get(Forecast, judgment.forecast_id)
        assert forecast is not None
        evaluation = EvaluationResult(
            id=f"outcome-{judgment_id}",
            forecast_id=forecast.id,
            actual_return=0.02,
            actual_label="up",
            correct=forecast.direction == "up",
            brier_score=0.2,
            evaluated_at=now,
            price_source="test-fixture",
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
            id=f"batch-{judgment_id}",
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
        rows = materialize_user_judgment_evaluation(
            session,
            batch=batch,
            forecast=forecast,
            evaluation=evaluation,
            material_outcome=True,
            now=now,
        )
        assert len(rows) == 1
        session.commit()


def _export(
    client: TestClient,
    tmp_path: Path,
    judgment_id: str,
    *,
    include_actor_id: bool = False,
) -> Path:
    return export_judgment_bundle(
        client.app.state.database,
        judgment_id=judgment_id,
        output_root=tmp_path / "judgment-bundles",
        wiki_root=Path(client.app.state.settings.user_judgment_wiki_root),
        timezone=client.app.state.settings.timezone,
        include_actor_id=include_actor_id,
        exported_at=FIXED_EXPORT_TIME,
    )


@pytest.mark.parametrize(
    ("record_class", "evaluation_status"),
    [
        ("demo", "not_applicable"),
        ("non_blind_archive", "not_applicable"),
        ("formal_shadow", "completed"),
    ],
)
def test_bundle_distinguishes_record_classes_and_verifies(
    client: TestClient,
    tmp_path: Path,
    record_class: str,
    evaluation_status: str,
) -> None:
    judgment_id = _create_judgment(client, record_class=record_class)
    bundle = _export(client, tmp_path, judgment_id)

    manifest = verify_judgment_bundle(bundle)
    judgment = json.loads((bundle / JUDGMENT_NAME).read_bytes())
    evaluation = json.loads((bundle / EVALUATION_NAME).read_bytes())

    assert manifest.record_class == record_class
    assert manifest.evaluation_status == evaluation_status
    assert manifest.actor_privacy == "omitted"
    assert "actor_id" not in judgment
    assert "source_content_hash" not in judgment
    assert "source_wiki_artifact_hash" not in judgment
    assert len(manifest.manifest_hash) == 64
    assert len(manifest.bundle_hash) == 64
    assert [item.path for item in manifest.artifacts] == [
        "agent-spec.json",
        "forecast.json",
        "judgment.json",
        "evaluation.json",
    ]
    assert all(item.size > 0 and len(item.sha256) == 64 for item in manifest.artifacts)
    if record_class == "formal_shadow":
        assert evaluation["source"]["observation_hash"] == "c" * 64
        assert "user_judgment_content_hash" not in evaluation["source"]
        assert "source_content_hash" not in evaluation
    else:
        assert evaluation == {
            "not_applicable_reason": record_class,
            "schema_version": "forecast-loop.judgment-evaluation-export/v1",
            "status": "not_applicable",
        }


def test_bundle_actor_id_requires_explicit_opt_in(
    client: TestClient,
    tmp_path: Path,
) -> None:
    judgment_id = _create_judgment(client, record_class="demo")
    bundle = _export(
        client,
        tmp_path,
        judgment_id,
        include_actor_id=True,
    )

    manifest = verify_judgment_bundle(bundle)
    judgment = json.loads((bundle / JUDGMENT_NAME).read_bytes())

    assert manifest.actor_privacy == "included"
    assert judgment["actor_id"] == client.app.state.settings.user_judgment_actor_id


def test_bundle_uses_the_creation_time_agent_spec_snapshot(
    client: TestClient,
    tmp_path: Path,
) -> None:
    judgment_id = _create_judgment(client, record_class="demo")
    with client.app.state.database.session_factory() as session:
        judgment = session.get(UserJudgment, judgment_id)
        assert judgment is not None
        assert judgment.agent_spec_hash is not None
        specification = session.get(AgentSpecRecord, judgment.agent_spec_hash)
        assert specification is not None
        frozen_hash = specification.content_hash

    bundle = _export(client, tmp_path, judgment_id)
    embedded = json.loads((bundle / "agent-spec.json").read_bytes())

    assert embedded["content_hash"] == frozen_hash
    assert verify_judgment_bundle(bundle).agent_spec_hash == frozen_hash


def test_v1_demo_fixture_remains_byte_and_hash_compatible() -> None:
    manifest = verify_judgment_bundle(FIXTURE_ROOT)

    assert manifest.manifest_hash == (
        "9eb959de3e1f4e4df115f1b607eca56d82365e7b7842498d927833df3b28b083"
    )
    assert manifest.bundle_hash == (
        "23324d049b08c6e92e4953c682037cee739d7295b51edbdf6914d84b97c8879e"
    )
    assert manifest.record_class == "demo"
    assert manifest.actor_privacy == "omitted"


def test_formal_shadow_export_fails_until_evaluation_is_complete(
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-25T15:00:00+08:00"},
    )
    assert response.status_code == 201
    run_id = response.json()["id"]
    now = datetime.now(ZONE).replace(microsecond=0)
    with client.app.state.database.session_factory() as session:
        run = session.get(WorkflowRun, run_id)
        assert run is not None
        run.mode = "live"
        run.completed_at = now - timedelta(minutes=5)
        forecast = run.forecasts[0]
        forecast.target_date = (now + timedelta(days=2)).date()
        forecast_id = forecast.id
        session.commit()
    _enable_live_http(client)
    created = client.post(
        "/api/user-judgments",
        json=_request(forecast_id, blind=True),
        headers=OPERATOR_HEADERS,
    )
    assert created.status_code == 201

    with pytest.raises(
        JudgmentBundleError,
        match="require a completed trusted evaluation",
    ):
        _export(client, tmp_path, created.json()["id"])


def test_verifier_rejects_tampering_missing_files_and_resealed_mismatches(
    client: TestClient,
    tmp_path: Path,
) -> None:
    judgment_id = _create_judgment(client, record_class="formal_shadow")

    tampered = _export(client, tmp_path / "tampered", judgment_id)
    judgment_path = tampered / JUDGMENT_NAME
    judgment_path.write_bytes(judgment_path.read_bytes() + b" ")
    with pytest.raises(JudgmentBundleError, match="hash mismatch"):
        verify_judgment_bundle(tampered)

    missing = _export(client, tmp_path / "missing", judgment_id)
    (missing / FORECAST_NAME).unlink()
    with pytest.raises(JudgmentBundleError, match="missing or unexpected"):
        verify_judgment_bundle(missing)

    mismatched = _export(client, tmp_path / "mismatch", judgment_id)
    forecast = json.loads((mismatched / FORECAST_NAME).read_bytes())
    forecast["forecast_id"] = "FORECAST-FROM-ANOTHER-RUN"
    _rewrite_and_reseal(mismatched, FORECAST_NAME, forecast)
    with pytest.raises(JudgmentBundleError, match="Forecast binding"):
        verify_judgment_bundle(mismatched)

    incomplete = _export(client, tmp_path / "incomplete", judgment_id)
    evaluation = {
        "schema_version": "forecast-loop.judgment-evaluation-export/v1",
        "status": "not_applicable",
        "not_applicable_reason": "non_blind_archive",
    }
    _rewrite_and_reseal(
        incomplete,
        EVALUATION_NAME,
        evaluation,
        manifest_updates={"evaluation_status": "not_applicable"},
    )
    with pytest.raises(JudgmentBundleError, match="evaluation is incomplete"):
        verify_judgment_bundle(incomplete)


def test_export_never_writes_into_committee_run_bundle(
    client: TestClient,
    tmp_path: Path,
) -> None:
    judgment_id = _create_judgment(client, record_class="demo")
    with client.app.state.database.session_factory() as session:
        judgment = session.get(UserJudgment, judgment_id)
        assert judgment is not None
        run_id = judgment.run_id
    run_bundle = export_run_bundle(
        client.app.state.database,
        run_id=run_id,
        output_root=tmp_path / "run-bundles",
        exported_at=FIXED_EXPORT_TIME,
    )
    before = {
        path.name: path.read_bytes()
        for path in sorted(run_bundle.iterdir())
    }

    with pytest.raises(JudgmentBundleError, match="committee run bundle"):
        export_judgment_bundle(
            client.app.state.database,
            judgment_id=judgment_id,
            output_root=run_bundle,
            wiki_root=Path(
                client.app.state.settings.user_judgment_wiki_root
            ),
            timezone=client.app.state.settings.timezone,
        )

    after = {
        path.name: path.read_bytes()
        for path in sorted(run_bundle.iterdir())
    }
    assert after == before


def test_judgment_export_and_bundle_verify_cli(
    client: TestClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    judgment_id = _create_judgment(client, record_class="demo")
    arguments = [
        "--database-url",
        client.app.state.settings.database_url,
        "--wiki-root",
        str(client.app.state.settings.user_judgment_wiki_root),
    ]
    assert main(
        [
            "judgment",
            "export",
            judgment_id,
            "--output-root",
            str(tmp_path / "cli-bundles"),
            *arguments,
        ]
    ) == 0
    exported = json.loads(capsys.readouterr().out)

    assert exported["status"] == "exported"
    assert exported["actor_privacy"] == "omitted"
    assert exported["record_class"] == "demo"
    assert main(["judgment", "verify", exported["bundle_path"]]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["verification_status"] == "verified"
    assert verified["bundle_hash"] == exported["bundle_hash"]


def _rewrite_and_reseal(
    bundle: Path,
    artifact_name: str,
    payload: dict,
    *,
    manifest_updates: dict | None = None,
) -> None:
    body = _canonical_json_bytes(payload)
    (bundle / artifact_name).write_bytes(body)
    manifest = json.loads((bundle / MANIFEST_NAME).read_bytes())
    for artifact in manifest["artifacts"]:
        if artifact["path"] == artifact_name:
            artifact["sha256"] = hashlib.sha256(body).hexdigest()
            artifact["size"] = len(body)
    manifest.pop("manifest_hash")
    manifest.pop("bundle_hash")
    manifest.update(manifest_updates or {})
    sealed = _seal_manifest(manifest)
    (bundle / MANIFEST_NAME).write_bytes(
        _canonical_json_bytes(sealed.model_dump(mode="json"))
    )
