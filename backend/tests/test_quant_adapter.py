from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.adapters import LocalJsonQuantSignalSource
from app.agent_contracts import (
    EvaluationMetric,
    InfluenceMode,
    ParticipationMode,
    SignalInputBinding,
    SignalTarget,
    agent_spec,
)
from app.db import Database
from app.domain import Direction
from app.models import SignalEnvelopeRecord, WorkflowRun
from app.ports import (
    AgentSignalAccessError,
    AgentSignalSource,
    AgentSignalValidationError,
    QuantSignalSource,
)
from app.services.evaluation_facade import evaluate_signal, route_signal
from app.services.quant_signal import accept_quant_candidate
from app.services.signal_contract import (
    persist_signal_envelope,
    verify_signal_envelope_record,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend" / "tests" / "fixtures" / "quant" / "read-only-v1"
ZONE = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 27, 15, 0, tzinfo=ZONE)
DATA_CUTOFF = datetime(2026, 7, 27, 14, 55, tzinfo=ZONE)
ACCEPTED_AT = datetime(2026, 7, 27, 15, 3, tzinfo=ZONE)
DEADLINE = datetime(2026, 7, 27, 15, 30, tzinfo=ZONE)
TARGET = SignalTarget(
    index_code="000300.SH",
    horizon="D1",
    base_trade_date=date(2026, 7, 27),
    target_date=date(2026, 7, 28),
    as_of=AS_OF,
    data_cutoff=DATA_CUTOFF,
)


def _copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "quant-fixture"
    shutil.copytree(FIXTURE, destination)
    return destination


def _rewrite_json(path: Path, payload: dict[str, object]) -> bytes:
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode()
    path.write_bytes(raw)
    return raw


def _reseal(payload: dict[str, object]) -> None:
    from app.agent_contracts import contract_content_hash

    payload["content_hash"] = contract_content_hash(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )


def _add_run(database: Database) -> None:
    with database.session_factory() as session:
        session.add(
            WorkflowRun(
                id="quant-adapter-run",
                as_of=AS_OF,
                data_cutoff=DATA_CUTOFF,
                status="completed",
                mode="live",
                started_at=AS_OF,
                completed_at=ACCEPTED_AT,
                duration_seconds=180.0,
                error=None,
                data_quality={},
                workflow_steps=[],
                input_hash="a" * 64,
            )
        )
        session.commit()


def test_quant_fixture_is_synthetic_and_redistributable() -> None:
    notice = (FIXTURE / "README.md").read_text(encoding="utf-8")
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FIXTURE.rglob("*"))
        if path.is_file()
    )

    assert "Apache-2.0" in notice
    assert "synthetic" in fixture_text.lower()
    for forbidden in (
        "/Users/",
        "account_id",
        "api_key",
        "broker",
        "order_id",
        "private_key",
    ):
        assert forbidden not in fixture_text


def test_load_candidates_verifies_the_bundle_only_once(monkeypatch) -> None:
    source = LocalJsonQuantSignalSource(FIXTURE, Path("manifest.json"))
    original = LocalJsonQuantSignalSource._load_verified_bundle
    calls = 0

    def observed_load(instance, *, as_of):
        nonlocal calls
        calls += 1
        return original(instance, as_of=as_of)

    monkeypatch.setattr(
        LocalJsonQuantSignalSource,
        "_load_verified_bundle",
        observed_load,
    )

    candidates = source.load_candidates(as_of=AS_OF)

    assert len(candidates) == 1
    assert candidates[0].target == TARGET
    assert calls == 1


def test_quant_adapter_to_persisted_shadow_evaluation_is_end_to_end(
    monkeypatch,
    tmp_path,
) -> None:
    fixture_hashes_before = {
        path.relative_to(FIXTURE): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in FIXTURE.rglob("*")
        if path.is_file()
    }
    opened_flags: list[int] = []
    original_open = os.open

    def observed_open(path, flags, *args, **kwargs):
        opened_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("app.adapters.local_quant_signal.os.open", observed_open)
    source = LocalJsonQuantSignalSource(
        root=FIXTURE,
        manifest_path=Path("manifest.json"),
    )
    assert isinstance(source, AgentSignalSource)
    assert isinstance(source, QuantSignalSource)
    candidate = source.load_candidate(target=TARGET)
    spec = agent_spec("quant_agent")

    assert spec.agent_version == "0.3.0"
    assert spec.participation.mode is ParticipationMode.SHADOW
    assert spec.participation.influence is InfluenceMode.NONE
    assert set(spec.participation.evaluation_metrics) >= {
        EvaluationMetric.DIRECTION,
        EvaluationMetric.MULTICLASS_BRIER,
        EvaluationMetric.CALIBRATION,
    }
    assert candidate.draft.direction == "up"
    assert candidate.draft.probabilities is not None
    artifact_manifest = candidate.draft.source_payload["artifact_manifest"]
    assert isinstance(artifact_manifest, dict)
    assert set(artifact_manifest) == {
        "code",
        "parameters",
        "feature_set",
        "model",
        "input_snapshot",
    }
    assert all(
        item["version"] and len(item["sha256"]) == 64
        for item in artifact_manifest.values()
    )
    assert candidate.provenance.code_version == "synthetic-code-1.0.0"
    assert len(candidate.provenance.code_hash or "") == 64
    assert set(candidate.provenance.artifact_hashes) == {
        "parameters",
        "feature_set",
        "model",
        "input_snapshot",
        "bundle_manifest",
    }

    signal = accept_quant_candidate(
        candidate=candidate,
        mode="live",
        target=TARGET,
        accepted_at=ACCEPTED_AT,
        submission_deadline=DEADLINE,
        input_binding=SignalInputBinding(
            run_id="quant-adapter-run",
            run_input_hash="a" * 64,
            agent_spec_hash=spec.content_hash,
            evidence_snapshot_hash=candidate.provenance.artifact_hashes[
                "input_snapshot"
            ],
        ),
    )
    route = route_signal(spec=spec, signal=signal)
    assert route.lane == "shadow_benchmark"
    assert route.formal_aggregation is False
    assert route.shadow_benchmark is True

    evaluation = evaluate_signal(
        spec=spec,
        signal=signal,
        actual_label=Direction.UP,
    )
    assert evaluation.direction_correct is True
    assert evaluation.brier_score == pytest.approx(
        ((1 - 0.58) ** 2 + 0.27**2 + 0.15**2) / 3
    )
    assert evaluation.calibration_eligible is True

    database = Database(f"sqlite:///{tmp_path / 'quant.sqlite3'}")
    database.create_all()
    try:
        _add_run(database)
        with database.session_factory() as session:
            row, created = persist_signal_envelope(
                session,
                signal=signal,
                authoritative_target=TARGET,
                authoritative_accepted_at=ACCEPTED_AT,
                authoritative_submission_deadline=DEADLINE,
                authoritative_provenance=candidate.provenance,
                run_timezone="Asia/Shanghai",
            )
            assert created is True
            assert row.routing_lane == "shadow_benchmark"
            assert row.formal_aggregation is False
            assert row.shadow_benchmark is True
            session.commit()
        with database.session_factory() as session:
            row = session.get(SignalEnvelopeRecord, signal.signal_id)
            assert row is not None
            assert verify_signal_envelope_record(row) == signal
    finally:
        database.dispose()

    write_mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    assert opened_flags
    assert all(flags & write_mask == 0 for flags in opened_flags)
    fixture_hashes_after = {
        path.relative_to(FIXTURE): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in FIXTURE.rglob("*")
        if path.is_file()
    }
    assert fixture_hashes_after == fixture_hashes_before


def test_quant_adapter_fails_closed_on_missing_or_tampered_artifact(
    tmp_path,
) -> None:
    manifest_root = _copy_fixture(tmp_path / "manifest")
    manifest_path = manifest_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_id"] = "tampered-without-reseal"
    _rewrite_json(manifest_path, manifest)
    source = LocalJsonQuantSignalSource(manifest_root, Path("manifest.json"))
    with pytest.raises(
        AgentSignalValidationError,
        match="content-hash validation",
    ):
        source.load_signal_drafts(as_of=AS_OF)

    root = _copy_fixture(tmp_path)
    source = LocalJsonQuantSignalSource(root, Path("manifest.json"))
    (root / "artifacts" / "parameters.json").write_text(
        '{"tampered": true}\n',
        encoding="utf-8",
    )
    with pytest.raises(AgentSignalValidationError, match="parameters artifact SHA-256"):
        source.load_signal_drafts(as_of=AS_OF)

    root = _copy_fixture(tmp_path / "missing")
    (root / "artifacts" / "model.json").unlink()
    source = LocalJsonQuantSignalSource(root, Path("manifest.json"))
    with pytest.raises(AgentSignalAccessError, match="model artifact is missing"):
        source.load_signal_drafts(as_of=AS_OF)


def test_generic_shadow_model_artifact_may_remain_inert_binary(tmp_path) -> None:
    root = _copy_fixture(tmp_path)
    model_path = root / "artifacts" / "model.json"
    binary_model = b"\x00\xffsynthetic-inert-model\x01"
    model_path.write_bytes(binary_model)

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"]["model"]["version"] == "synthetic-model-1.0.0"
    manifest["artifacts"]["model"]["sha256"] = hashlib.sha256(binary_model).hexdigest()
    _reseal(manifest)
    _rewrite_json(manifest_path, manifest)

    source = LocalJsonQuantSignalSource(root, Path("manifest.json"))
    candidates = source.load_candidates(as_of=AS_OF)

    assert len(candidates) == 1


def test_quant_adapter_rejects_unknown_time_semantics_and_target_mismatch(
    tmp_path,
) -> None:
    source = LocalJsonQuantSignalSource(FIXTURE, Path("manifest.json"))
    with pytest.raises(AgentSignalValidationError, match="must include a timezone"):
        source.load_signal_drafts(as_of=datetime(2026, 7, 27, 15))
    with pytest.raises(AgentSignalValidationError, match="exactly one signal"):
        source.load_candidate(
            target=TARGET.model_copy(update={"index_code": "000905.SH"})
        )

    root = _copy_fixture(tmp_path)
    snapshot_path = root / "artifacts" / "input-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["as_of"] = "2026-07-27T15:00:00"
    _reseal(snapshot)
    raw_snapshot = _rewrite_json(snapshot_path, snapshot)

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["input_snapshot"]["sha256"] = hashlib.sha256(
        raw_snapshot
    ).hexdigest()
    _reseal(manifest)
    _rewrite_json(manifest_path, manifest)

    source = LocalJsonQuantSignalSource(root, Path("manifest.json"))
    with pytest.raises(AgentSignalValidationError, match="timestamps must include"):
        source.load_signal_drafts(as_of=AS_OF)


def test_quant_adapter_rejects_extra_input_snapshot_indexes(tmp_path) -> None:
    root = _copy_fixture(tmp_path)
    snapshot_path = root / "artifacts" / "input-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    extra_row = dict(snapshot["rows"][0])
    extra_row["index_code"] = "000905.SH"
    snapshot["rows"].append(extra_row)
    _reseal(snapshot)
    raw_snapshot = _rewrite_json(snapshot_path, snapshot)

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["input_snapshot"]["sha256"] = hashlib.sha256(
        raw_snapshot
    ).hexdigest()
    _reseal(manifest)
    _rewrite_json(manifest_path, manifest)

    source = LocalJsonQuantSignalSource(root, Path("manifest.json"))
    with pytest.raises(AgentSignalValidationError, match="exactly match"):
        source.load_signal_drafts(as_of=AS_OF)


def test_quant_adapter_rejects_path_escape_and_symlink(
    tmp_path,
) -> None:
    root = _copy_fixture(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["code"]["path"] = "../outside.py"
    _reseal(manifest)
    _rewrite_json(manifest_path, manifest)
    source = LocalJsonQuantSignalSource(root, Path("manifest.json"))
    with pytest.raises(
        AgentSignalValidationError,
        match="normalized relative POSIX path",
    ):
        source.load_signal_drafts(as_of=AS_OF)

    symlink_root = _copy_fixture(tmp_path / "symlink")
    code_path = symlink_root / "artifacts" / "strategy.py"
    outside = tmp_path / "outside-strategy.py"
    outside.write_bytes(code_path.read_bytes())
    code_path.unlink()
    code_path.symlink_to(outside)
    source = LocalJsonQuantSignalSource(symlink_root, Path("manifest.json"))
    with pytest.raises(AgentSignalAccessError, match="may not contain symlinks"):
        source.load_signal_drafts(as_of=AS_OF)


def test_quant_admission_requires_active_spec_and_exact_target() -> None:
    source = LocalJsonQuantSignalSource(FIXTURE, Path("manifest.json"))
    candidate = source.load_candidate(target=TARGET)
    spec = agent_spec("quant_agent")
    binding = SignalInputBinding(
        run_id="quant-adapter-run",
        run_input_hash="a" * 64,
        agent_spec_hash=spec.content_hash,
    )

    with pytest.raises(ValueError, match="candidate target"):
        accept_quant_candidate(
            candidate=candidate,
            mode="live",
            target=TARGET.model_copy(update={"index_code": "000905.SH"}),
            accepted_at=ACCEPTED_AT,
            submission_deadline=DEADLINE,
            input_binding=binding,
        )
    with pytest.raises(ValueError, match="active AgentSpec"):
        accept_quant_candidate(
            candidate=candidate,
            mode="live",
            target=TARGET,
            accepted_at=ACCEPTED_AT,
            submission_deadline=DEADLINE,
            input_binding=binding.model_copy(update={"agent_spec_hash": "f" * 64}),
        )
