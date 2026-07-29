from __future__ import annotations

import json
import sqlite3
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from app.agent_contracts import (
    SignalEnvelopeBody,
    SignalInputBinding,
    SignalProvenance,
    SignalTarget,
    agent_spec,
    seal_signal_envelope,
)
from app.cli import main
from app.domain import AgentSourceType
from app.services.schema_readiness import SchemaNotReadyError, upgrade_database

ROOT = Path(__file__).resolve().parents[2]


def test_distribution_exposes_forecast_loop_and_legacy_cli_aliases() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["project"]["name"] == "forecast-loop"
    assert configuration["project"]["scripts"] == {
        "forecast-loop": "app.cli:main",
        "signalrace": "app.cli:main",
        "vericouncil": "app.cli:main",
    }


def test_cli_help_uses_forecast_loop_brand(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: forecast-loop")


def test_persistent_worker_once_reports_idle(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setenv(
        "VERICOUNCIL_CHECKPOINT_PATH",
        str(tmp_path / "worker-checkpoint.sqlite3"),
    )
    monkeypatch.setenv("VERICOUNCIL_WIKI_PATH", str(tmp_path / "wiki"))
    monkeypatch.setenv("VERICOUNCIL_EXECUTION_PROVIDER", "demo")
    monkeypatch.setenv("VERICOUNCIL_AUTO_SEED", "false")

    database_url = f"sqlite:///{tmp_path / 'worker.sqlite3'}"
    upgrade_database(database_url)

    assert (
        main(
            [
                "worker",
                "run",
                "--once",
                "--worker-id",
                "test-worker",
                "--database-url",
                database_url,
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {
        "status": "idle",
        "worker_id": "test-worker",
    }


def test_cli_lists_and_shows_agent_contracts(capsys) -> None:
    assert main(["agent", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["items"]) == 8
    assert all(len(item["content_hash"]) == 64 for item in listed["items"])

    assert main(["agent", "show", "user_judgment_agent"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["capabilities"]["probability_mode"] == "confidence"
    assert shown["participation"]["mode"] == "shadow"

    with pytest.raises(SystemExit, match="unknown Agent"):
        main(["agent", "show", "missing-agent"])


def test_cli_prints_contract_json_schema(capsys) -> None:
    assert main(["contract", "schema", "signal-envelope"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "SignalEnvelope"
    assert "content_hash" in schema["properties"]

    assert main(["contract", "schema", "quant-signal-bundle"]) == 0
    quant_schema = json.loads(capsys.readouterr().out)
    assert quant_schema["title"] == "QuantSignalBundle"
    assert "artifacts" in quant_schema["properties"]

def test_cli_validates_signal_without_writing(
    tmp_path,
    capsys,
) -> None:
    spec = agent_spec("user_judgment_agent")
    zone = ZoneInfo("Asia/Shanghai")
    signal = seal_signal_envelope(
        SignalEnvelopeBody(
            signal_id="cli-manual-signal",
            agent_id=spec.agent_id,
            agent_version=spec.agent_version,
            mode="live",
            target=SignalTarget(
                index_code="000300.SH",
                horizon="D1",
                base_trade_date=date(2026, 7, 27),
                target_date=date(2026, 7, 28),
                as_of=datetime(2026, 7, 27, 15, tzinfo=zone),
                data_cutoff=datetime(2026, 7, 27, 14, 55, tzinfo=zone),
            ),
            submitted_at=datetime(2026, 7, 27, 15, 2, tzinfo=zone),
            accepted_at=datetime(2026, 7, 27, 15, 3, tzinfo=zone),
            submission_deadline=datetime(2026, 7, 27, 15, 30, tzinfo=zone),
            input_binding=SignalInputBinding(
                run_id="cli-fixture-run",
                run_input_hash="a" * 64,
                agent_spec_hash=spec.content_hash,
            ),
            participation=spec.participation,
            provenance=SignalProvenance(
                source_type=AgentSourceType.MANUAL,
                producer="local-user-interface",
                adapter="manual-form",
                adapter_version="1.0.0",
            ),
            direction="up",
            direction_confidence=0.64,
            rationale="流动性改善和风险偏好回升可能共同推动目标指数走强。",
            counter_evidence=("海外利率重新上行可能压制风险资产。",),
            invalidation_conditions=("若跌破基准日低点，则当前判断失效。",),
            blind_attestation=True,
            payload_schema="forecast-loop.manual/v1",
            source_payload={"entry_format": "private-wiki"},
        )
    )
    path = tmp_path / "signal.json"
    path.write_text(signal.model_dump_json(indent=2), encoding="utf-8")

    assert main(["agent", "validate", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "valid"
    assert output["content_hash"] == signal.content_hash

    symlink = tmp_path / "signal-link.json"
    symlink.symlink_to(path)
    with pytest.raises(SystemExit, match="non-symlink"):
        main(["agent", "validate", str(symlink)])


def test_cli_prepares_demo_handoff_as_json(monkeypatch, tmp_path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'cli.sqlite3'}"
    monkeypatch.setenv(
        "VERICOUNCIL_DATABASE_URL",
        database_url,
    )
    monkeypatch.setenv(
        "VERICOUNCIL_CHECKPOINT_PATH",
        str(tmp_path / "checkpoint.sqlite3"),
    )
    handoffs = tmp_path / "handoffs"
    upgrade_database(database_url)

    assert (
        main(
            [
                "forecast",
                "prepare",
                "--mode",
                "demo",
                "--output-root",
                str(handoffs),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "awaiting_draft"
    assert output["job_dir"].startswith(str(handoffs))
    assert output["drafts_file"].endswith("/drafts.json")


def test_cli_prepare_refuses_to_bootstrap_an_unmigrated_database(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "unmigrated-cli.sqlite3"
    monkeypatch.setenv(
        "VERICOUNCIL_DATABASE_URL",
        f"sqlite:///{database_path}",
    )
    monkeypatch.setenv(
        "VERICOUNCIL_CHECKPOINT_PATH",
        str(tmp_path / "checkpoint.sqlite3"),
    )

    with pytest.raises(SchemaNotReadyError, match="Run `forecast-loop database migrate`"):
        main(
            [
                "forecast",
                "prepare",
                "--mode",
                "demo",
                "--output-root",
                str(tmp_path / "handoffs"),
            ]
        )

    connection = sqlite3.connect(database_path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    assert tables == []


def test_cli_exports_and_verifies_run_bundle(client, tmp_path, capsys) -> None:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-13T15:30:00+08:00"},
    )
    assert response.status_code == 201
    run_id = response.json()["id"]
    output_root = tmp_path / "exports"

    assert (
        main(
            [
                "run",
                "export",
                run_id,
                "--database-url",
                client.app.state.settings.database_url,
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["status"] == "exported"
    assert exported["run_id"] == run_id

    assert main(["run", "verify", exported["bundle_path"]]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "completed"
    assert verified["verification_status"] == "verified"
    assert verified["run_id"] == run_id
    assert verified["bundle_hash"] == exported["bundle_hash"]


def test_cli_records_and_verifies_user_judgment(
    client,
    tmp_path,
    capsys,
) -> None:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-25T15:00:00+08:00"},
    )
    assert response.status_code == 201
    forecast_id = client.get("/api/user-judgments/targets").json()["items"][0][
        "forecast_id"
    ]
    rationale = tmp_path / "rationale.md"
    counter = tmp_path / "counter.md"
    invalidation = tmp_path / "invalidation.md"
    rationale.write_text(
        "流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。",
        encoding="utf-8",
    )
    counter.write_text(
        "海外利率重新上行可能压制成长估值，并削弱流动性改善效果。",
        encoding="utf-8",
    )
    invalidation.write_text(
        "若成交额显著收缩且跌破基准日低点，则本判断失效。",
        encoding="utf-8",
    )
    common = [
        "--database-url",
        client.app.state.settings.database_url,
        "--wiki-root",
        str(client.app.state.settings.user_judgment_wiki_root),
    ]

    assert main([
        "judgment",
        "record",
        "--forecast-id",
        forecast_id,
        "--direction",
        "up",
        "--confidence",
        "0.63",
        "--rationale-file",
        str(rationale),
        "--counter-evidence-file",
        str(counter),
        "--invalidation-file",
        str(invalidation),
        *common,
    ]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["status"] == "sealed"
    assert recorded["formal_score_eligible"] is False

    assert main([
        "judgment",
        "verify",
        recorded["judgment_id"],
        *common,
    ]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified"
    assert verified["content_hash"] == recorded["content_hash"]


def test_cli_validates_and_renders_job_manifest(tmp_path, capsys) -> None:
    manifest = ROOT / "jobs" / "daily-forecast.example.json"

    assert (
        main(
            [
                "jobs",
                "validate",
                str(manifest),
                "--project-root",
                str(ROOT),
            ]
        )
        == 0
    )
    validated = json.loads(capsys.readouterr().out)
    assert validated["status"] == "valid"
    assert validated["schema"] == "vericouncil.job/v1"
    assert validated["name"] == "daily-forecast"

    output_dir = tmp_path / "systemd"
    assert (
        main(
            [
                "jobs",
                "render",
                str(manifest),
                "--target",
                "systemd",
                "--dispatcher",
                "forecast-loop-job-dispatcher",
                "--output-dir",
                str(output_dir),
                "--project-root",
                str(ROOT),
            ]
        )
        == 0
    )
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["status"] == "rendered"
    assert {Path(item).name for item in rendered["files"]} == {
        "vericouncil-job-daily-forecast.service",
        "vericouncil-job-daily-forecast.timer",
    }
    assert all(Path(item).is_file() for item in rendered["files"])


def test_cli_opens_and_reads_append_only_job_execution(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    manifest = ROOT / "jobs" / "daily-forecast.example.json"
    captured: dict[str, object] = {}

    class FakeState:
        def model_dump(self, **_):
            return {
                "schema": "vericouncil.job-execution/v1",
                "execution_id": "a" * 64,
                "phase": "prepare_pending",
            }

    class FakeStore:
        def __init__(self, **kwargs) -> None:
            captured["store"] = kwargs

        def begin(self, loaded_manifest, *, idempotency_key: str):
            captured["manifest"] = loaded_manifest
            captured["idempotency_key"] = idempotency_key
            return FakeState()

        def resume(self, execution_id: str):
            captured["execution_id"] = execution_id
            return FakeState()

    monkeypatch.setattr("app.cli.JobExecutionStore", FakeStore)
    common = [
        "--state-root",
        str(tmp_path / "state"),
        "--handoff-root",
        str(tmp_path / "handoffs"),
        "--project-root",
        str(ROOT),
    ]

    assert (
        main(
            [
                "jobs",
                "begin",
                str(manifest),
                "--idempotency-key",
                "2026-07-24-evening",
                *common,
            ]
        )
        == 0
    )
    opened = json.loads(capsys.readouterr().out)
    assert opened["phase"] == "prepare_pending"
    assert captured["idempotency_key"] == "2026-07-24-evening"

    assert main(["jobs", "status", "a" * 64, *common]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["execution_id"] == "a" * 64
    assert captured["execution_id"] == "a" * 64


def test_cli_exports_and_verifies_audit_bundle(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    expected_run_id = "5ed4d481-fc6c-4f93-8686-c8d730a54fd8"
    bundle = tmp_path / "audit" / expected_run_id
    captured: dict[str, object] = {}

    class FakeManifest:
        run_id = expected_run_id
        bundle_hash = "b" * 64
        publisher_authentication = "none"

        def model_dump(self, **_):
            return {
                "schema_version": "vericouncil.audit-bundle/v1",
                "run_id": self.run_id,
                "bundle_hash": self.bundle_hash,
                "publisher_authentication": self.publisher_authentication,
            }

    def fake_export(**kwargs):
        captured.update(kwargs)
        return bundle

    monkeypatch.setattr("app.cli.export_audit_bundle", fake_export)
    monkeypatch.setattr("app.cli.verify_audit_bundle", lambda _path: FakeManifest())

    assert (
        main(
            [
                "audit",
                "export",
                expected_run_id,
                "--handoff-root",
                str(tmp_path / "handoffs"),
                "--run-bundle",
                str(tmp_path / "results" / expected_run_id),
                "--output-root",
                str(tmp_path / "audit"),
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["status"] == "exported"
    assert exported["publisher_authentication"] == "none"
    assert captured["job_dir"] == Path(expected_run_id)

    assert main(["audit", "verify", str(bundle)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["verification_status"] == "verified"
    assert verified["run_id"] == expected_run_id


def test_cli_export_does_not_create_a_missing_database(tmp_path) -> None:
    database_path = tmp_path / "missing.sqlite3"

    with pytest.raises(SystemExit, match="database does not exist"):
        main(
            [
                "run",
                "export",
                "missing-run",
                "--database-url",
                f"sqlite:///{database_path}",
                "--output-root",
                str(tmp_path / "exports"),
            ]
        )

    assert not database_path.exists()


def test_cli_snapshot_path_is_not_rebased_twice(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    snapshot_path = tmp_path / "snapshots" / "snapshot.json"
    snapshot_path.parent.mkdir()
    snapshot_path.write_text("{}\n", encoding="utf-8")
    captured: dict[str, Path] = {}

    class FakeSource:
        def __init__(self, *, root: Path, snapshot_path: Path) -> None:
            captured["root"] = root
            captured["snapshot_path"] = snapshot_path

        def load_snapshot(self, *, as_of: datetime):
            return SimpleNamespace(
                content_hash="a" * 64,
                as_of=as_of,
                data_cutoff=as_of,
                items=[],
            )

    monkeypatch.setattr("app.cli.LocalJsonEvidenceSnapshotSource", FakeSource)

    assert (
        main(
            [
                "snapshot",
                "validate",
                str(snapshot_path),
                "--root",
                str(snapshot_path.parent),
                "--as-of",
                datetime(2026, 7, 24, 15, tzinfo=UTC).isoformat(),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "valid"
    assert captured["root"] == snapshot_path.parent
    assert captured["snapshot_path"] == snapshot_path
