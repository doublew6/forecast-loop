from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import app.services.handoff as handoff_service
import pytest
from app.agent_contracts import (
    SignalProbabilityVector,
    SignalTarget,
)
from app.config import Settings
from app.db import Database
from app.domain import INDEXES, Horizon, RunStatus
from app.main import create_app
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import AgentOpinion, Forecast, SignalEnvelopeRecord, WorkflowRun
from app.ports import AgentSignalValidationError
from app.quant_contracts import (
    QuantArtifactRef,
    QuantArtifactSet,
    QuantFeatureValue,
    QuantInputRow,
    QuantInputSnapshotBody,
    QuantSignalBundleBody,
    QuantSignalOutput,
    seal_quant_input_snapshot,
    seal_quant_signal_bundle,
)
from app.services.handoff import (
    HANDOFF_PROTOCOL_VERSION,
    LEGACY_HANDOFF_PROTOCOL_VERSION,
    PREVIOUS_HANDOFF_PROTOCOL_VERSION,
    finalize_handoff,
    prepare_handoff,
    retry_failed_handoff,
)
from app.services.schema_readiness import SchemaNotReadyError, upgrade_database
from app.services.snapshot import load_evidence_snapshot
from app.workflow import CommitteeWorkflow, StaleRunExecutionError
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

ZONE = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 13, 15, 0, tzinfo=ZONE)
PREPARED_AT = datetime(2026, 7, 13, 15, 1, tzinfo=ZONE)
FINALIZED_AT = datetime(2026, 7, 13, 15, 10, tzinfo=ZONE)
TEST_OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"
PRE_UPGRADE_V2_INPUT = Path(__file__).parent / "fixtures" / "handoff" / "pre-upgrade-v2-input.json"
PRE_UPGRADE_V2_DATABASE_SEAL = (
    Path(__file__).parent / "fixtures" / "handoff" / "pre-upgrade-v2-db-seal.json"
)
PRE_UPGRADE_CONFIGURABLE_V2_INPUT = (
    Path(__file__).parent / "fixtures" / "handoff" / "pre-upgrade-v2-configurable-input.json"
)
PRE_UPGRADE_CONFIGURABLE_V2_DATABASE_SEAL = (
    Path(__file__).parent / "fixtures" / "handoff" / "pre-upgrade-v2-configurable-db-seal.json"
)


def _settings(
    tmp_path: Path,
    *,
    execution_provider: str = "demo",
    operator_token: str | None = None,
) -> Settings:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir(exist_ok=True)
    (wiki_path / "methodology.md").write_text(
        """---
id: VC-WIKI-PREDICTION-LABELS
title: 预测标签、证据和风险方法
version: 1.0.0
updated_at: 2026-07-13
published_at: 2026-07-13T00:00:00+08:00
status: active
owners: [forecast-loop]
tags: [prediction, macro, market, risk, ai-storage]
source_urls: [https://example.com/official-source]
---
<!-- section:methodology -->
# 方法
所有研究判断必须只使用冻结证据和冻结 Wiki，并明确记录反证与失效条件。

<!-- section:labels -->
# 标签
方向只允许上涨或下跌，小波动仅作为真实结果概率桶。
""",
        encoding="utf-8",
    )
    (wiki_path / "market-strategy.md").write_text(
        """---
id: VC-WIKI-MARKET-STRATEGY
title: 市场策略与指数配置
version: 1.0.0
updated_at: 2026-07-13
published_at: 2026-07-13T00:00:00+08:00
status: active
owners: [strategy_agent]
tags: [strategy, allocation]
source_urls: [https://www.csindex.com.cn/]
---
<!-- section:synthesis -->
# 策略综合
综合三位研究员并比较五个指数，不重复计算同源证据。
""",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'handoff.sqlite3'}",
        checkpoint_path=tmp_path / "handoff-checkpoint.sqlite3",
        wiki_path=wiki_path,
        handoff_root=tmp_path / "handoffs",
        demo_mode=execution_provider == "demo",
        execution_provider=execution_provider,
        operator_token=operator_token,
        auto_seed=False,
    )
    upgrade_database(settings.database_url)
    return settings


def _read_request(job_dir: Path) -> dict[str, Any]:
    return json.loads((job_dir / "input.json").read_text(encoding="utf-8"))


def _assignment_sections(assignment: dict[str, Any]) -> list[str]:
    for key in ("allowed_wiki_sections", "wiki_sections", "sections"):
        value = assignment.get(key)
        if isinstance(value, list) and value:
            return [str(item) for item in value]
    wiki = assignment.get("wiki")
    if isinstance(wiki, dict):
        value = wiki.get("sections")
        if isinstance(value, list) and value:
            return [str(item) for item in value]
    section = assignment.get("wiki_section")
    if section:
        return [str(section)]
    raise AssertionError(f"assignment does not freeze Wiki sections: {assignment}")


def _assignment_wiki_id(assignment: dict[str, Any]) -> str:
    if assignment.get("wiki_entry_id"):
        return str(assignment["wiki_entry_id"])
    wiki = assignment.get("wiki")
    if isinstance(wiki, dict) and wiki.get("entry_id"):
        return str(wiki["entry_id"])
    raise AssertionError(f"assignment does not freeze a Wiki entry: {assignment}")


def _draft_bundle(request: dict[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for position, assignment in enumerate(request["assignments"]):
        bullish = position % 2 == 0
        probabilities = (
            {"up": 0.56, "neutral": 0.24, "down": 0.20}
            if bullish
            else {"up": 0.20, "neutral": 0.24, "down": 0.56}
        )
        evidence_ids = assignment.get(
            "allowed_evidence_item_ids",
            assignment.get("evidence_item_ids", []),
        )
        # Demo mode has no mandatory dynamic citations. If the request exposes
        # frozen IDs, using one proves the finalizer accepts only that set.
        selected_ids = list(evidence_ids[:1]) if evidence_ids else []
        records.append(
            {
                "agent_id": assignment["agent_id"],
                "index_code": assignment["index_code"],
                "horizon": assignment["horizon"],
                **(
                    {"agent_brief": assignment["agent_brief"]}
                    if "agent_brief" in assignment
                    else {}
                ),
                "draft": {
                    "direction": "up" if bullish else "down",
                    "probabilities": probabilities,
                    "summary": (
                        f"基于冻结输入完成 {assignment['agent_id']} 对 "
                        f"{assignment['index_code']}/{assignment['horizon']} 的判断。"
                    ),
                    "evidence": ["已核对交接包内的冻结证据与对应 Wiki 方法。"],
                    "counter_evidence": ["共同来源和信息遗漏可能削弱方向判断。"],
                    "invalidation_conditions": ["冻结证据或 Wiki 校验失败时判断失效。"],
                    "evidence_item_ids": selected_ids,
                    "wiki_entry_id": _assignment_wiki_id(assignment),
                    "wiki_section": _assignment_sections(assignment)[0],
                },
            }
        )

    protocol_key = "protocol_version" if "protocol_version" in request else "protocol"
    return {
        protocol_key: request[protocol_key],
        "run_id": request["run_id"],
        "input_hash": request["input_hash"],
        "request_hash": request["request_hash"],
        "generated_at": PREPARED_AT.isoformat(),
        "generated_by": {
            "surface": "codex",
            "task_id": "codex-test-task",
            "model": "codex-desktop",
        },
        "drafts": records,
    }


def _write_drafts(job_dir: Path, bundle: dict[str, Any]) -> None:
    path = job_dir / "drafts.json"
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_json_artifact(path: Path, payload: Any) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()
    path.write_bytes(raw)
    return raw


def _artifact_ref(
    *,
    artifact_id: str,
    version: str,
    path: str,
    raw: bytes,
) -> QuantArtifactRef:
    return QuantArtifactRef(
        artifact_id=artifact_id,
        version=version,
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _write_quant_manifest(
    settings: Settings,
    root: Path,
    *,
    omit_last_target: bool = False,
    wrong_d1_session: bool = False,
    wrong_evidence_snapshot_hash: bool = False,
    wrong_market_universe_hash: bool = False,
    horizons: tuple[Horizon, ...] = (Horizon.D1,),
) -> Path:
    snapshot = load_evidence_snapshot(settings, as_of=AS_OF)
    quant_root = root / "quant-bundle"
    artifacts_root = quant_root / "artifacts"

    code_path = "artifacts/model.py"
    code_raw = b"# synthetic read-only quant model\n"
    (artifacts_root / "model.py").parent.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "model.py").write_bytes(code_raw)
    parameters_path = "artifacts/parameters.json"
    parameters_raw = _write_json_artifact(
        quant_root / parameters_path,
        {
            "schema_version": "example.quant-parameters/v1",
            "description": "Synthetic fixture with no production settings.",
        },
    )
    feature_set_path = "artifacts/feature-set.json"
    feature_set_raw = _write_json_artifact(
        quant_root / feature_set_path,
        {"features": ["synthetic_momentum"], "version": "features-1.0.0"},
    )
    model_path = "artifacts/model.json"
    model_version = "synthetic-model-1.0.0"
    model_targets = {
        f"{index.code}/{horizon.value}": (
            snapshot.target_sessions[1]
            if wrong_d1_session and horizon is Horizon.D1
            else snapshot.target_sessions[0 if horizon is Horizon.D1 else 1]
        )
        for index in INDEXES
        for horizon in horizons
    }
    model_raw = _write_json_artifact(
        quant_root / model_path,
        {
            "schema_version": "example.quant-model/v1",
            "model_version": model_version,
            "as_of": snapshot.as_of.isoformat(),
            "data_cutoff": snapshot.data_cutoff.isoformat(),
            "description": "Opaque synthetic artifact for adapter tests.",
        },
    )

    input_snapshot = seal_quant_input_snapshot(
        QuantInputSnapshotBody(
            snapshot_id=f"quant-input-{AS_OF.date().isoformat()}",
            as_of=snapshot.as_of,
            data_cutoff=snapshot.data_cutoff,
            created_at=PREPARED_AT,
            feature_set_version="features-1.0.0",
            rows=tuple(
                QuantInputRow(
                    index_code=index.code,
                    features=(
                        QuantFeatureValue(
                            name="synthetic_momentum",
                            value=position / 10,
                        ),
                    ),
                )
                for position, index in enumerate(INDEXES, start=1)
            ),
        )
    )
    input_snapshot_path = "artifacts/input-snapshot.json"
    input_snapshot_raw = _write_json_artifact(
        quant_root / input_snapshot_path,
        input_snapshot.model_dump(mode="json"),
    )
    artifacts = QuantArtifactSet(
        code=_artifact_ref(
            artifact_id="code",
            version="synthetic-code-1.0.0",
            path=code_path,
            raw=code_raw,
        ),
        parameters=_artifact_ref(
            artifact_id="parameters",
            version="synthetic-parameters-1.0.0",
            path=parameters_path,
            raw=parameters_raw,
        ),
        feature_set=_artifact_ref(
            artifact_id="feature_set",
            version="features-1.0.0",
            path=feature_set_path,
            raw=feature_set_raw,
        ),
        model=_artifact_ref(
            artifact_id="model",
            version=model_version,
            path=model_path,
            raw=model_raw,
        ),
        input_snapshot=_artifact_ref(
            artifact_id="input_snapshot",
            version="synthetic-input-1.0.0",
            path=input_snapshot_path,
            raw=input_snapshot_raw,
        ),
    )

    signals = [
        QuantSignalOutput(
            signal_id=(f"handoff-{index.code.replace('.', '-')}-{horizon.value}"),
            target=SignalTarget(
                index_code=index.code,
                horizon=horizon,
                base_trade_date=snapshot.base_session,
                target_date=(model_targets[f"{index.code}/{horizon.value}"]),
                as_of=snapshot.as_of,
                data_cutoff=snapshot.data_cutoff,
            ),
            direction="up",
            probabilities=SignalProbabilityVector(
                up=0.55,
                neutral=0.25,
                down=0.20,
            ),
            rationale="Synthetic test model favors the up class.",
            counter_evidence=("Synthetic feature may not generalize.",),
            invalidation_conditions=("Input snapshot hash changes.",),
        )
        for index in INDEXES
        for horizon in horizons
    ]
    if omit_last_target:
        signals.pop()
    bundle = seal_quant_signal_bundle(
        QuantSignalBundleBody(
            bundle_id=f"handoff-quant-{AS_OF.date().isoformat()}",
            as_of=snapshot.as_of,
            data_cutoff=snapshot.data_cutoff,
            generated_at=PREPARED_AT,
            evidence_snapshot_hash=(
                "f" * 64 if wrong_evidence_snapshot_hash else snapshot.content_hash
            ),
            market_universe_hash=(
                "f" * 64 if wrong_market_universe_hash else DEFAULT_MARKET_UNIVERSE.content_hash
            ),
            artifacts=artifacts,
            signals=tuple(signals),
        )
    )
    manifest_path = quant_root / "manifest.json"
    _write_json_artifact(manifest_path, bundle.model_dump(mode="json"))
    return manifest_path


def _row_counts(settings: Settings, run_id: str) -> tuple[str, int, int]:
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, run_id)
            assert row is not None
            opinions = session.scalar(
                select(func.count()).select_from(AgentOpinion).where(AgentOpinion.run_id == run_id)
            )
            forecasts = session.scalar(
                select(func.count()).select_from(Forecast).where(Forecast.run_id == run_id)
            )
            return row.status, int(opinions or 0), int(forecasts or 0)
    finally:
        database.dispose()


def _seed_pre_upgrade_v2_handoff(
    settings: Settings,
    *,
    input_fixture: Path = PRE_UPGRADE_V2_INPUT,
    database_fixture: Path = PRE_UPGRADE_V2_DATABASE_SEAL,
) -> tuple[Path, dict[str, Any]]:
    """Install the frozen pre-upgrade input and its original database seal."""

    input_bytes = input_fixture.read_bytes()
    request = json.loads(input_bytes)
    job_dir = settings.handoff_root / request["run_id"]
    job_dir.mkdir(parents=True, mode=0o700)
    input_path = job_dir / "input.json"
    input_path.write_bytes(input_bytes)
    input_path.chmod(0o400)

    database_seal = json.loads(database_fixture.read_bytes())
    assert database_seal["run_id"] == request["run_id"]
    assert database_seal["input_hash"] == request["input_hash"]
    assert (
        database_seal["data_quality"]["handoff"]["request_raw_hash"]
        == hashlib.sha256(input_bytes).hexdigest()
    )
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            session.add(
                WorkflowRun(
                    id=database_seal["run_id"],
                    as_of=datetime.fromisoformat(database_seal["as_of"]),
                    data_cutoff=datetime.fromisoformat(database_seal["data_cutoff"]),
                    status=database_seal["status"],
                    mode=database_seal["mode"],
                    started_at=datetime.fromisoformat(database_seal["started_at"]),
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality=database_seal["data_quality"],
                    workflow_steps=database_seal["workflow_steps"],
                    input_hash=database_seal["input_hash"],
                    market_universe_hash=database_seal["market_universe_hash"],
                )
            )
            session.commit()
    finally:
        database.dispose()
    return job_dir, request


def _prepare_valid_bundle(tmp_path: Path) -> tuple[Settings, Path, dict[str, Any]]:
    settings = _settings(tmp_path)
    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
    )
    request = _read_request(job_dir)
    bundle = _draft_bundle(request)
    _write_drafts(job_dir, bundle)
    return settings, job_dir, bundle


def _prepare_failed_quant_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Settings, Path, Path, dict[str, Any]]:
    settings = _settings(tmp_path)
    manifest_path = _write_quant_manifest(settings, tmp_path)
    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
        quant_manifest_path=manifest_path,
    )
    request = _read_request(job_dir)
    _write_drafts(job_dir, _draft_bundle(request))

    original = CommitteeWorkflow._persist_result

    def fail_persistence(_session, _state) -> None:
        raise RuntimeError("synthetic final persistence failure")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "_persist_result",
        staticmethod(fail_persistence),
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="synthetic final persistence failure",
        ):
            finalize_handoff(settings, job_dir, now=FINALIZED_AT)
    finally:
        monkeypatch.setattr(
            CommitteeWorkflow,
            "_persist_result",
            staticmethod(original),
        )
    return settings, job_dir, manifest_path, request


def test_prepare_creates_awaiting_run_and_complete_file_package(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    job_dir = prepare_handoff(settings, as_of=AS_OF, now=PREPARED_AT)

    assert job_dir.parent == settings.handoff_root.resolve()
    assert {"input.json", "INSTRUCTIONS.md", "drafts.template.json"}.issubset(
        {path.name for path in job_dir.iterdir()}
    )
    request = _read_request(job_dir)
    assert request["run_id"] == job_dir.name
    assert request["input_hash"]
    assert request["request_hash"]
    assert request["mode"] == "demo"
    assert request["protocol_version"] == HANDOFF_PROTOCOL_VERSION
    assert request["provider"] == "codex-file-handoff-v3"
    assert request["initial_state"]["forecast_horizons"] == ["D1"]
    instructions = (job_dir / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert instructions.startswith("# forecast-loop Codex 文件交接任务")
    assert len(request["assignments"]) == 25
    assert (
        len(
            {
                (item["agent_id"], item["index_code"], item["horizon"])
                for item in request["assignments"]
            }
        )
        == 25
    )
    assert {item["horizon"] for item in request["assignments"]} == {"D1"}
    assert {item["agent_id"] for item in request["assignments"]} == {
        "macro_policy_agent",
        "market_news_agent",
        "ai_storage_industry_agent",
        "strategy_agent",
        "risk_critic_agent",
    }
    assert _row_counts(settings, request["run_id"]) == ("awaiting_draft", 0, 0)


def test_v2_handoff_freezes_custom_briefs_in_request_template_and_drafts(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    custom_briefs = {
        "ai_storage_industry_agent": "研究该标的的产品周期、竞争格局与估值催化。",
        "strategy_agent": "综合该标的研究输入，判断相对强弱和组合配置优先级。",
        "risk_critic_agent": "检查该标的特有的监管、流动性与拥挤交易风险。",
    }
    universe_payload = DEFAULT_MARKET_UNIVERSE.model_dump(
        mode="json",
        exclude={"content_hash"},
    )
    universe_payload["universe_id"] = "custom-briefs"
    universe_payload["version"] = "1.0.0"
    universe_payload["instruments"][0]["agent_briefs"] = custom_briefs
    universe_path = tmp_path / "custom-briefs.json"
    universe_path.write_text(
        json.dumps(universe_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    custom_settings = settings.model_copy(update={"market_universe_path": universe_path})

    job_dir = prepare_handoff(
        custom_settings,
        as_of=AS_OF,
        now=PREPARED_AT,
    )
    request = _read_request(job_dir)
    first_code = DEFAULT_MARKET_UNIVERSE.codes[0]
    assignments = {
        (item["agent_id"], item["index_code"], item["horizon"]): item
        for item in request["assignments"]
    }
    template = json.loads((job_dir / "drafts.template.json").read_text(encoding="utf-8"))
    template_records = {
        (item["agent_id"], item["index_code"], item["horizon"]): item for item in template["drafts"]
    }

    for agent_id, expected_brief in custom_briefs.items():
        identity = (agent_id, first_code, "D1")
        assert assignments[identity]["agent_brief"] == expected_brief
        assert template_records[identity]["agent_brief"] == expected_brief

    unsigned_request = dict(request)
    request_hash = unsigned_request.pop("request_hash")
    assert (
        request_hash
        == hashlib.sha256(
            json.dumps(
                unsigned_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert "严格履行对应 assignment 冻结的 `agent_brief`" in (
        job_dir / "INSTRUCTIONS.md"
    ).read_text(encoding="utf-8")

    bundle = _draft_bundle(request)
    target = next(
        item
        for item in bundle["drafts"]
        if (
            item["agent_id"],
            item["index_code"],
            item["horizon"],
        )
        == ("strategy_agent", first_code, "D1")
    )
    target["agent_brief"] = "试图替换冻结职责"
    _write_drafts(job_dir, bundle)

    with pytest.raises(ValueError, match="changed the assigned agent brief"):
        finalize_handoff(custom_settings, job_dir, now=FINALIZED_AT)


def test_quant_manifest_is_bound_persisted_as_shadow_and_survives_finalize(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_quant_manifest(settings, tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frozen_evidence = load_evidence_snapshot(settings, as_of=AS_OF)
    input_snapshot_path = manifest_path.parent / manifest["artifacts"]["input_snapshot"]["path"]
    input_snapshot = json.loads(input_snapshot_path.read_text(encoding="utf-8"))

    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
        quant_manifest_path=manifest_path,
    )
    request = _read_request(job_dir)
    external = request["initial_state"]["external_input_bindings"]["quant_agent"]
    quant_audit = request["initial_state"]["data_quality"]["quant"]

    assert len(request["assignments"]) == 25
    assert "quant_agent" not in {assignment["agent_id"] for assignment in request["assignments"]}
    assert external["agent_version"] == "0.3.0"
    assert external["participation_mode"] == "shadow"
    assert external["schema_version"] == "forecast-loop.quant-run-input-binding/v1"
    assert external["signal_count"] == 5
    assert external["bundle_content_hash"] == manifest["content_hash"]
    assert external["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert (
        external["input_snapshot_sha256"]
        == hashlib.sha256(input_snapshot_path.read_bytes()).hexdigest()
    )
    assert external["input_snapshot_content_hash"] == input_snapshot["content_hash"]
    assert external["evidence_snapshot_content_hash"] == frozen_evidence.content_hash
    assert external["market_universe_content_hash"] == DEFAULT_MARKET_UNIVERSE.content_hash
    assert external["decision_weight_total"] == 0
    assert external["activation_status"] == "shadow_locked"
    assert quant_audit["routing_lane"] == "shadow_benchmark"
    assert quant_audit["signal_count"] == 5
    assert quant_audit["decision_weight_total"] == 0
    assert quant_audit["market_universe_content_hash"] == DEFAULT_MARKET_UNIVERSE.content_hash

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            run = session.get(WorkflowRun, request["run_id"])
            assert run is not None
            assert (run.data_quality or {})["quant"] == quant_audit
            records = session.scalars(
                select(SignalEnvelopeRecord)
                .where(SignalEnvelopeRecord.run_id == request["run_id"])
                .order_by(
                    SignalEnvelopeRecord.index_code,
                    SignalEnvelopeRecord.horizon,
                )
            ).all()
            assert len(records) == 5
            assert {(record.index_code, record.horizon) for record in records} == {
                (index.code, Horizon.D1.value) for index in INDEXES
            }
            assert {record.agent_id for record in records} == {"quant_agent"}
            assert {record.agent_version for record in records} == {"0.3.0"}
            assert {record.participation_mode for record in records} == {"shadow"}
            assert {record.routing_lane for record in records} == {"shadow_benchmark"}
            assert not any(record.formal_aggregation for record in records)
            assert all(record.shadow_benchmark for record in records)
            assert {
                record.envelope["input_binding"]["evidence_snapshot_hash"] for record in records
            } == {frozen_evidence.content_hash}
            assert all(
                "quant_test_weight_ref" not in record.envelope["source_payload"]
                for record in records
            )
    finally:
        database.dispose()

    bundle = _draft_bundle(request)
    _write_drafts(job_dir, bundle)
    finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            run = session.get(WorkflowRun, request["run_id"])
            assert run is not None
            quality = run.data_quality or {}
            assert quality["quant"] == quant_audit
            assert quality["handoff"]["status"] == "completed"
            signal_count = session.scalar(
                select(func.count())
                .select_from(SignalEnvelopeRecord)
                .where(SignalEnvelopeRecord.run_id == request["run_id"])
            )
            assert signal_count == 5
            forecasts = session.scalars(
                select(Forecast).where(Forecast.run_id == request["run_id"])
            ).all()
            assert len(forecasts) == 5
            assert all(
                "只读 shadow 输入" in forecast.rationale and "正式决策权重为0" in forecast.rationale
                for forecast in forecasts
            )
    finally:
        database.dispose()
    assert _row_counts(settings, request["run_id"]) == ("completed", 30, 5)


def test_quant_handoff_evening_deadline_precedes_d1_target(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_quant_manifest(settings, tmp_path)
    evening_prepare = PREPARED_AT.replace(hour=16, minute=0)

    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=evening_prepare,
        quant_manifest_path=manifest_path,
    )

    request = _read_request(job_dir)
    deadline = datetime.fromisoformat(request["finalize_deadline"])
    target_date = date.fromisoformat(
        request["initial_state"]["evidence_snapshot"]["target_sessions"][0]
    )
    assert deadline.date() < target_date

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            records = session.scalars(
                select(SignalEnvelopeRecord).where(SignalEnvelopeRecord.run_id == request["run_id"])
            ).all()
            assert len(records) == 5
            assert {
                datetime.fromisoformat(record.envelope["submission_deadline"]).date()
                for record in records
            } == {deadline.date()}
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("omit_last_target", "wrong_d1_session", "error_pattern"),
    [
        (True, False, "exactly match bundle target indexes|exactly 5 indexes"),
        (False, True, "frozen evidence sessions"),
    ],
)
def test_quant_manifest_target_matrix_must_match_frozen_evidence(
    tmp_path: Path,
    omit_last_target: bool,
    wrong_d1_session: bool,
    error_pattern: str,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_quant_manifest(
        settings,
        tmp_path,
        omit_last_target=omit_last_target,
        wrong_d1_session=wrong_d1_session,
    )

    with pytest.raises(
        (ValueError, AgentSignalValidationError),
        match=error_pattern,
    ):
        prepare_handoff(
            settings,
            as_of=AS_OF,
            now=PREPARED_AT,
            quant_manifest_path=manifest_path,
        )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            run_count = session.scalar(select(func.count()).select_from(WorkflowRun))
            signal_count = session.scalar(select(func.count()).select_from(SignalEnvelopeRecord))
            assert run_count == 0
            assert signal_count == 0
    finally:
        database.dispose()


def test_quant_manifest_must_be_sealed_to_the_same_evidence_snapshot(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_quant_manifest(
        settings,
        tmp_path,
        wrong_evidence_snapshot_hash=True,
    )

    with pytest.raises(
        ValueError,
        match="different Evidence Snapshot",
    ):
        prepare_handoff(
            settings,
            as_of=AS_OF,
            now=PREPARED_AT,
            quant_manifest_path=manifest_path,
        )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            run_count = session.scalar(select(func.count()).select_from(WorkflowRun))
            signal_count = session.scalar(select(func.count()).select_from(SignalEnvelopeRecord))
            assert run_count == 0
            assert signal_count == 0
    finally:
        database.dispose()


def test_quant_manifest_must_be_sealed_to_the_same_market_universe(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_quant_manifest(
        settings,
        tmp_path,
        wrong_market_universe_hash=True,
    )

    with pytest.raises(ValueError, match="different market universe"):
        prepare_handoff(
            settings,
            as_of=AS_OF,
            now=PREPARED_AT,
            quant_manifest_path=manifest_path,
        )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkflowRun)) == 0
            assert session.scalar(select(func.count()).select_from(SignalEnvelopeRecord)) == 0
    finally:
        database.dispose()


def test_quant_manifest_cannot_be_replayed_into_another_run(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest_path = _write_quant_manifest(settings, tmp_path)
    first_job = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
        quant_manifest_path=manifest_path,
    )
    first_run_id = _read_request(first_job)["run_id"]

    with pytest.raises(ValueError, match="already bound|already admitted"):
        prepare_handoff(
            settings,
            as_of=AS_OF,
            now=PREPARED_AT + timedelta(minutes=1),
            quant_manifest_path=manifest_path,
        )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            rows = session.scalars(select(WorkflowRun)).all()
            records = session.scalars(select(SignalEnvelopeRecord)).all()
            assert len(rows) == 2
            assert {row.id: row.status for row in rows}[first_run_id] == "awaiting_draft"
            assert sorted(row.status for row in rows) == [
                "awaiting_draft",
                "failed",
            ]
            assert len(records) == 5
            assert {record.run_id for record in records} == {first_run_id}
    finally:
        database.dispose()


def test_app_restart_preserves_awaiting_handoff_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    job_dir = prepare_handoff(settings, as_of=AS_OF, now=PREPARED_AT)
    run_id = _read_request(job_dir)["run_id"]

    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as client:
        runs = client.get("/api/runs").json()["items"]

    persisted = next(item for item in runs if item["id"] == run_id)
    assert persisted["status"] == "awaiting_draft"
    assert persisted["error"] is None
    assert _row_counts(settings, run_id) == ("awaiting_draft", 0, 0)


@pytest.mark.parametrize(
    "runtime_constant",
    ["WORKFLOW_VERSION", "DECISION_SCHEMA_VERSION"],
)
def test_finalize_rejects_runtime_version_drift_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_constant: str,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    monkeypatch.setattr(f"app.workflow.{runtime_constant}", "99.0.0")

    with pytest.raises(ValueError, match="runtime versions changed after prepare"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, bundle["run_id"]) == ("awaiting_draft", 0, 0)


def test_finalize_persists_exact_committee_and_binary_forecasts(tmp_path: Path) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)

    receipt = finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    run_id = bundle["run_id"]
    assert _row_counts(settings, run_id) == ("completed", 30, 5)
    assert (job_dir / "receipt.json").is_file()
    receipt_payload = receipt.model_dump(mode="json") if hasattr(receipt, "model_dump") else receipt
    assert receipt_payload["run_id"] == run_id
    assert receipt_payload["status"] == "completed"
    assert receipt_payload["input_hash"] == bundle["input_hash"]
    assert receipt_payload["request_hash"] == bundle["request_hash"]
    assert receipt_payload["receipt_hash"]

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            forecasts = session.scalars(select(Forecast).where(Forecast.run_id == run_id)).all()
            opinions = session.scalars(
                select(AgentOpinion).where(AgentOpinion.run_id == run_id)
            ).all()
            assert {forecast.direction for forecast in forecasts} <= {"up", "down"}
            assert {opinion.direction for opinion in opinions} <= {"up", "down"}
            file_opinions = [opinion for opinion in opinions if opinion.agent_id != "cio_agent"]
            assert len(file_opinions) == 25
            assert {opinion.model_name for opinion in file_opinions} == {"codex-file-handoff-v3"}
    finally:
        database.dispose()


def test_finalize_refuses_to_bootstrap_an_unmigrated_database(
    tmp_path: Path,
) -> None:
    settings, job_dir, _ = _prepare_valid_bundle(tmp_path)
    unmigrated_path = tmp_path / "unmigrated-finalize.sqlite3"
    unmigrated = settings.model_copy(update={"database_url": f"sqlite:///{unmigrated_path}"})

    with pytest.raises(SchemaNotReadyError, match="database schema is not ready"):
        finalize_handoff(unmigrated, job_dir, now=FINALIZED_AT)

    connection = sqlite3.connect(unmigrated_path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    assert tables == []


def test_legacy_v1_package_keeps_original_instructions_and_finalizes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
        protocol_version=LEGACY_HANDOFF_PROTOCOL_VERSION,
    )
    request = _read_request(job_dir)
    assert request["protocol_version"] == "1.0.0"
    assert request["provider"] == "codex-file-handoff-v1"
    assert all("agent_brief" not in item for item in request["assignments"])
    instructions = (job_dir / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert instructions.startswith("# VeriCouncil Codex 文件交接任务")
    assert instructions.endswith("CIO 结论由 VeriCouncil 本地聚合器生成。\n")
    assert "forecast-loop" not in instructions

    bundle = _draft_bundle(request)
    _write_drafts(job_dir, bundle)
    receipt = finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert receipt.protocol_version == "1.0.0"
    assert receipt.provider == "codex-file-handoff-v1"
    assert "attempt_number" not in receipt.model_dump(mode="json")
    assert "previous_receipt_hash" not in receipt.model_dump(mode="json")
    assert _row_counts(settings, request["run_id"]) == ("completed", 60, 10)


def test_frozen_v2_package_keeps_dual_horizon_runtime_and_finalizes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    job_dir, request = _seed_pre_upgrade_v2_handoff(settings)

    assert request["protocol_version"] == "2.0.0"
    assert request["provider"] == "codex-file-handoff-v2"
    assert request["workflow_version"] == "0.3.0"
    assert request["decision_schema_version"] == "0.4.0"
    assert "forecast_horizons" not in request["initial_state"]
    assert len(request["assignments"]) == 50
    assert {item["horizon"] for item in request["assignments"]} == {"D1", "D2"}
    assert all(item["agent_brief"] for item in request["assignments"])

    bundle = _draft_bundle(request)
    _write_drafts(job_dir, bundle)
    receipt = finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert receipt.protocol_version == "2.0.0"
    assert receipt.provider == "codex-file-handoff-v2"
    assert "attempt_number" not in receipt.model_dump(mode="json")
    assert "previous_receipt_hash" not in receipt.model_dump(mode="json")
    assert _row_counts(settings, request["run_id"]) == ("completed", 60, 10)


@pytest.mark.parametrize(
    "protocol_version",
    [LEGACY_HANDOFF_PROTOCOL_VERSION, PREVIOUS_HANDOFF_PROTOCOL_VERSION],
)
def test_fresh_legacy_protocols_keep_legacy_audit_and_checkpoint_identity(
    tmp_path: Path,
    protocol_version: str,
) -> None:
    settings = _settings(tmp_path)
    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
        protocol_version=protocol_version,
    )
    request = _read_request(job_dir)
    v3_only_fields = {
        "attempt_number",
        "checkpoint_thread_id",
        "previous_receipt_hash",
        "attempt_history",
        "attempt_history_hash",
        "retry_transitions",
        "retry_transitions_hash",
        "execution_token",
    }
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            prepared = session.get(WorkflowRun, request["run_id"])
            assert prepared is not None
            assert not v3_only_fields.intersection(prepared.data_quality["handoff"])
    finally:
        database.dispose()
    _write_drafts(job_dir, _draft_bundle(request))

    receipt = finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert receipt.status == "completed"
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, request["run_id"])
            assert row is not None
            audit = row.data_quality["handoff"]
            assert not v3_only_fields.intersection(audit)
    finally:
        database.dispose()

    checkpoint = sqlite3.connect(settings.checkpoint_path)
    try:
        thread_ids = {
            item[0]
            for item in checkpoint.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        }
    finally:
        checkpoint.close()
    assert request["run_id"] in thread_ids
    assert f"handoff:{request['run_id']}:1" not in thread_ids


def test_frozen_v2_duplicate_and_missing_receipt_recover_without_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    job_dir, request = _seed_pre_upgrade_v2_handoff(settings)
    _write_drafts(job_dir, _draft_bundle(request))
    first = finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    def forbid_graph_construction(*_args, **_kwargs):
        raise AssertionError("legacy receipt recovery rebuilt the graph")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "__init__",
        forbid_graph_construction,
    )
    duplicate = finalize_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(days=1),
    )
    assert duplicate == first

    (job_dir / "receipt.json").unlink()
    recovered = finalize_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(days=2),
    )
    assert recovered == first
    assert (job_dir / "receipt.json").is_file()
    assert _row_counts(settings, request["run_id"]) == (
        "completed",
        60,
        10,
    )


def test_frozen_v2_configurable_universe_uses_its_known_version_pair(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    job_dir, request = _seed_pre_upgrade_v2_handoff(
        settings,
        input_fixture=PRE_UPGRADE_CONFIGURABLE_V2_INPUT,
        database_fixture=PRE_UPGRADE_CONFIGURABLE_V2_DATABASE_SEAL,
    )

    assert request["protocol_version"] == "2.0.0"
    assert request["provider"] == "codex-file-handoff-v2"
    assert request["workflow_version"] == "0.4.0"
    assert request["decision_schema_version"] == "0.5.0"
    assert request["initial_state"]["market_universe"]["universe_id"] == ("v2-compatible-universe")
    assert len(request["assignments"]) == 50
    assert {item["horizon"] for item in request["assignments"]} == {"D1", "D2"}

    bundle = _draft_bundle(request)
    _write_drafts(job_dir, bundle)
    finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, request["run_id"]) == ("completed", 60, 10)


def test_unknown_frozen_v2_runtime_pair_fails_before_execution_or_receipt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
        protocol_version=PREVIOUS_HANDOFF_PROTOCOL_VERSION,
    )
    request = _read_request(job_dir)
    request["workflow_version"] = "99.0.0"
    input_path = job_dir / "input.json"
    input_path.chmod(0o600)
    input_path.write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime versions changed after prepare"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, request["run_id"]) == ("awaiting_draft", 0, 0)
    assert not (job_dir / "receipt.json").exists()


def test_duplicate_finalize_returns_same_receipt_without_duplicate_rows(
    tmp_path: Path,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    first = finalize_handoff(settings, job_dir, now=FINALIZED_AT)
    before = _row_counts(settings, bundle["run_id"])
    receipt_bytes = (job_dir / "receipt.json").read_bytes()

    duplicate = finalize_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(days=1),
    )

    assert duplicate == first
    assert (job_dir / "receipt.json").read_bytes() == receipt_bytes
    assert _row_counts(settings, bundle["run_id"]) == before == ("completed", 30, 5)


def test_finalize_recovers_receipt_after_completed_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_writer = handoff_service._write_new_file

    def fail_receipt_publish(path: Path, payload: bytes, *, mode: int) -> None:
        if path.name == "receipt.json":
            raise OSError("synthetic receipt publish failure")
        original_writer(path, payload, mode=mode)

    monkeypatch.setattr(
        handoff_service,
        "_write_new_file",
        fail_receipt_publish,
    )
    with pytest.raises(OSError, match="synthetic receipt publish failure"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, bundle["run_id"]) == ("completed", 30, 5)
    assert not (job_dir / "receipt.json").exists()

    def simulate_concurrent_receipt_winner(
        path: Path,
        payload: bytes,
        *,
        mode: int,
    ) -> None:
        original_writer(path, payload, mode=mode)
        raise FileExistsError("synthetic concurrent receipt winner")

    monkeypatch.setattr(
        handoff_service,
        "_write_new_file",
        simulate_concurrent_receipt_winner,
    )

    def forbid_graph_rerun(*_args, **_kwargs):
        raise AssertionError("completed receipt recovery reran the graph")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "execute_prepared",
        forbid_graph_rerun,
    )
    recovered = finalize_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(days=1),
    )

    assert recovered.status == "completed"
    assert recovered.attempt_number == 1
    assert (job_dir / "receipt.json").is_file()
    assert _row_counts(settings, bundle["run_id"]) == ("completed", 30, 5)


def test_completed_existing_receipt_recovers_drafts_chmod_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_chmod = handoff_service._chmod_regular_file

    def crash_before_drafts_chmod(path: Path, mode: int) -> None:
        if path.name == "drafts.json":
            raise OSError("synthetic crash before drafts chmod")
        original_chmod(path, mode)

    monkeypatch.setattr(
        handoff_service,
        "_chmod_regular_file",
        crash_before_drafts_chmod,
    )
    with pytest.raises(OSError, match="before drafts chmod"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, bundle["run_id"]) == ("completed", 30, 5)
    assert (job_dir / "receipt.json").is_file()
    assert stat.S_IMODE((job_dir / "drafts.json").stat().st_mode) == 0o600

    chmod_calls: list[tuple[Path, int]] = []

    def track_recovery_chmod(path: Path, mode: int) -> None:
        chmod_calls.append((path, mode))
        original_chmod(path, mode)

    monkeypatch.setattr(
        handoff_service,
        "_chmod_regular_file",
        track_recovery_chmod,
    )

    def forbid_graph_construction(*_args, **_kwargs):
        raise AssertionError("completed chmod recovery rebuilt the graph")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "__init__",
        forbid_graph_construction,
    )
    recovered = finalize_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(days=1),
    )

    assert recovered.status == "completed"
    assert chmod_calls == [(job_dir / "drafts.json", 0o400)]
    assert stat.S_IMODE((job_dir / "drafts.json").stat().st_mode) == 0o400


def test_workflow_success_refreshes_quality_after_fence_before_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_finalize_fence = CommitteeWorkflow._finalize_task_execution

    def inject_unrelated_quality(
        self,
        session,
        fence,
        *,
        run_id: str,
    ) -> None:
        original_finalize_fence(
            self,
            session,
            fence,
            run_id=run_id,
        )
        row = session.get(WorkflowRun, run_id)
        assert row is not None
        quality = json.loads(json.dumps(row.data_quality))
        quality["concurrent_unrelated_namespace"] = {
            "writer": "external",
            "preserve": True,
        }
        changed = session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status == RunStatus.RUNNING.value,
            )
            .values(data_quality=quality)
            .execution_options(synchronize_session=False)
        )
        assert changed.rowcount == 1

    monkeypatch.setattr(
        CommitteeWorkflow,
        "_finalize_task_execution",
        inject_unrelated_quality,
    )
    receipt = finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert receipt.status == "completed"
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.data_quality["concurrent_unrelated_namespace"] == {
                "writer": "external",
                "preserve": True,
            }
            assert row.data_quality["handoff"]["status"] == "completed"
    finally:
        database.dispose()


def test_finalize_recovers_completed_commit_before_output_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_seal = handoff_service._seal_output

    def crash_before_output_seal(*_args, **_kwargs):
        raise OSError("synthetic output seal crash")

    monkeypatch.setattr(
        handoff_service,
        "_seal_output",
        crash_before_output_seal,
    )
    with pytest.raises(OSError, match="synthetic output seal crash"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.status == RunStatus.COMPLETED.value
            assert row.data_quality["handoff"]["status"] == "validating"
    finally:
        database.dispose()
    assert not (job_dir / "receipt.json").exists()

    monkeypatch.setattr(handoff_service, "_seal_output", original_seal)

    def forbid_graph_rerun(*_args, **_kwargs):
        raise AssertionError("completed seal recovery reran the graph")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "execute_prepared",
        forbid_graph_rerun,
    )
    recovered = finalize_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(days=1),
    )

    assert recovered.status == "completed"
    assert _row_counts(settings, bundle["run_id"]) == ("completed", 30, 5)
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.data_quality["handoff"]["status"] == "completed"
    finally:
        database.dispose()


@pytest.mark.parametrize("artifact", ["drafts", "receipt", "output"])
def test_completed_finalize_rejects_conflicting_artifacts(
    tmp_path: Path,
    artifact: str,
) -> None:
    settings, job_dir, _ = _prepare_valid_bundle(tmp_path)
    finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    if artifact == "drafts":
        drafts_path = job_dir / "drafts.json"
        drafts_path.chmod(0o600)
        drafts_path.write_bytes(drafts_path.read_bytes() + b" ")
        error_pattern = "drafts.json.*database seal"
    elif artifact == "receipt":
        receipt_path = job_dir / "receipt.json"
        payload = json.loads(receipt_path.read_text())
        payload["finalized_at"] = (FINALIZED_AT + timedelta(seconds=1)).isoformat()
        payload["receipt_hash"] = handoff_service._canonical_hash(
            {key: value for key, value in payload.items() if key != "receipt_hash"}
        )
        receipt_path.chmod(0o600)
        receipt_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        error_pattern = "conflicts with the completed database seal"
    else:
        database = Database(settings.database_url)
        try:
            with database.session_factory() as session:
                opinion = session.scalar(
                    select(AgentOpinion).where(
                        AgentOpinion.run_id == _read_request(job_dir)["run_id"]
                    )
                )
                assert opinion is not None
                opinion.summary = "tampered completed output"
                session.commit()
        finally:
            database.dispose()
        error_pattern = "committee output.*handoff seal"

    with pytest.raises(ValueError, match=error_pattern):
        finalize_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT + timedelta(minutes=1),
        )


def test_first_v3_attempt_rejects_finalize_before_prepare_without_sealing(
    tmp_path: Path,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)

    with pytest.raises(ValueError, match="finalized before.*prepared"):
        finalize_handoff(
            settings,
            job_dir,
            now=PREPARED_AT - timedelta(minutes=1),
        )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.status == RunStatus.AWAITING_DRAFT.value
            assert row.data_quality["handoff"]["status"] == RunStatus.AWAITING_DRAFT.value
            assert "execution_token" not in row.data_quality["handoff"]
    finally:
        database.dispose()
    assert not (job_dir / "receipt.json").exists()


@pytest.mark.parametrize("backdated_field", ["generated_at", "finalized_at"])
def test_retry_attempt_rejects_time_before_latest_transition_without_sealing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backdated_field: str,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    transitioned_at = FINALIZED_AT + timedelta(minutes=1)
    retry_failed_handoff(
        settings,
        job_dir,
        now=transitioned_at,
    )
    bundle = _draft_bundle(request)
    if backdated_field == "generated_at":
        bundle["generated_at"] = FINALIZED_AT.isoformat()
        finalized_at = FINALIZED_AT + timedelta(minutes=2)
        error_pattern = "generated before.*retry transition"
    else:
        bundle["generated_at"] = (FINALIZED_AT + timedelta(minutes=2)).isoformat()
        finalized_at = FINALIZED_AT + timedelta(seconds=30)
        error_pattern = "finalized before.*retry transition"
    _write_drafts(job_dir, bundle)

    with pytest.raises(ValueError, match=error_pattern):
        finalize_handoff(settings, job_dir, now=finalized_at)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, request["run_id"])
            assert row is not None
            assert row.status == RunStatus.AWAITING_DRAFT.value
            assert row.data_quality["handoff"]["status"] == RunStatus.AWAITING_DRAFT.value
            assert "execution_token" not in row.data_quality["handoff"]
    finally:
        database.dispose()
    assert not (job_dir / "receipt.json").exists()


def test_failed_quant_handoff_retries_same_run_with_isolated_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, manifest_path, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    run_id = request["run_id"]
    assert _row_counts(settings, run_id) == ("failed", 0, 0)
    failed_receipt = json.loads((job_dir / "receipt.json").read_text())
    assert failed_receipt["attempt_number"] == 1

    assert (
        retry_failed_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT + timedelta(minutes=1),
        )
        == job_dir
    )
    archive = job_dir / "attempts" / "0001"
    assert {item.name for item in archive.iterdir()} == {
        "drafts.json",
        "receipt.json",
    }
    assert not (job_dir / "drafts.json").exists()
    assert not (job_dir / "receipt.json").exists()
    assert _row_counts(settings, run_id) == ("awaiting_draft", 0, 0)

    retry_bundle = _draft_bundle(request)
    retry_bundle["generated_at"] = (FINALIZED_AT + timedelta(minutes=2)).isoformat()
    _write_drafts(job_dir, retry_bundle)
    completed_receipt = finalize_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=3),
    )

    assert completed_receipt.status == "completed"
    assert completed_receipt.attempt_number == 2
    assert completed_receipt.previous_receipt_hash == failed_receipt["receipt_hash"]
    assert _row_counts(settings, run_id) == ("completed", 30, 5)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            records = session.scalars(
                select(SignalEnvelopeRecord).order_by(SignalEnvelopeRecord.id)
            ).all()
            row = session.get(WorkflowRun, run_id)
            assert row is not None
            history = row.data_quality["handoff"]["attempt_history"]
            assert len(history) == 1
            assert history[0]["receipt_hash"] == failed_receipt["receipt_hash"]
            transitions = row.data_quality["handoff"]["retry_transitions"]
            assert len(transitions) == 1
            assert transitions[0]["from_attempt"] == 1
            assert transitions[0]["to_attempt"] == 2
            assert transitions[0]["previous_attempt_hash"] == history[0]["attempt_hash"]
            assert row.data_quality["handoff"][
                "retry_transitions_hash"
            ] == handoff_service._canonical_hash(transitions)
            assert "retried_at" not in row.data_quality["handoff"]
            assert {record.run_id for record in records} == {run_id}
            assert len(records) == 5
    finally:
        database.dispose()

    with pytest.raises(ValueError, match="already bound|already admitted"):
        prepare_handoff(
            settings,
            as_of=AS_OF,
            now=PREPARED_AT + timedelta(minutes=10),
            quant_manifest_path=manifest_path,
        )

    checkpoint = sqlite3.connect(settings.checkpoint_path)
    try:
        thread_ids = {
            row[0]
            for row in checkpoint.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        }
    finally:
        checkpoint.close()
    assert f"handoff:{run_id}:1" in thread_ids
    assert f"handoff:{run_id}:2" in thread_ids


def test_retry_seals_failed_commit_after_attempt_seal_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_persist = CommitteeWorkflow._persist_result
    original_failed_seal = handoff_service._seal_failed_attempt

    def fail_persistence(_session, _state) -> None:
        raise RuntimeError("synthetic failed workflow commit")

    def crash_failed_seal(*_args, **_kwargs) -> None:
        raise OSError("synthetic failed attempt seal crash")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "_persist_result",
        staticmethod(fail_persistence),
    )
    monkeypatch.setattr(
        handoff_service,
        "_seal_failed_attempt",
        crash_failed_seal,
    )
    with pytest.raises(OSError, match="synthetic failed attempt seal crash"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.status == RunStatus.FAILED.value
            assert row.data_quality["handoff"]["status"] == "validating"
            assert row.data_quality["handoff"]["attempt_history"] == []
    finally:
        database.dispose()
    assert (job_dir / "receipt.json").is_file()

    monkeypatch.setattr(
        CommitteeWorkflow,
        "_persist_result",
        staticmethod(original_persist),
    )
    monkeypatch.setattr(
        handoff_service,
        "_seal_failed_attempt",
        original_failed_seal,
    )
    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )

    assert _row_counts(settings, bundle["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )
    archived = json.loads((job_dir / "attempts" / "0001" / "receipt.json").read_text())
    assert archived["status"] == "failed"
    assert archived["error"] == "synthetic failed workflow commit"


def test_retry_reconstructs_receipt_after_failed_publish_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_persist = CommitteeWorkflow._persist_result
    original_publish = handoff_service._write_or_verify_receipt

    def fail_persistence(_session, _state) -> None:
        raise RuntimeError("synthetic failure before receipt publication")

    def crash_receipt_publish(*_args, **_kwargs) -> None:
        raise OSError("synthetic failed receipt publish crash")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "_persist_result",
        staticmethod(fail_persistence),
    )
    monkeypatch.setattr(
        handoff_service,
        "_write_or_verify_receipt",
        crash_receipt_publish,
    )
    with pytest.raises(OSError, match="failed receipt publish crash"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)
    assert _row_counts(settings, bundle["run_id"]) == ("failed", 0, 0)
    assert not (job_dir / "receipt.json").exists()

    monkeypatch.setattr(
        CommitteeWorkflow,
        "_persist_result",
        staticmethod(original_persist),
    )
    monkeypatch.setattr(
        handoff_service,
        "_write_or_verify_receipt",
        original_publish,
    )
    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )

    archived = json.loads((job_dir / "attempts" / "0001" / "receipt.json").read_text())
    assert archived["error"] == "synthetic failure before receipt publication"
    assert _row_counts(settings, bundle["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )


def test_retry_recovers_exclusively_locked_interrupted_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_execute = CommitteeWorkflow.execute_prepared

    def strand_after_claim(self, prepared, **_kwargs):
        with self.database.session_factory() as session:
            row = session.get(WorkflowRun, prepared.row.id)
            assert row is not None
            assert row.data_quality["handoff"]["status"] == "validating"
            row.status = RunStatus.RUNNING.value
            row.started_at = FINALIZED_AT
            session.commit()
        raise SystemExit("synthetic hard exit after running claim")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "execute_prepared",
        strand_after_claim,
    )
    with pytest.raises(SystemExit, match="synthetic hard exit"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)
    assert _row_counts(settings, bundle["run_id"]) == ("running", 0, 0)

    monkeypatch.setattr(
        CommitteeWorkflow,
        "execute_prepared",
        original_execute,
    )
    with pytest.raises(ValueError, match="out of order"):
        retry_failed_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT - timedelta(seconds=1),
        )
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            stranded = session.get(WorkflowRun, bundle["run_id"])
            assert stranded is not None
            assert stranded.status == RunStatus.RUNNING.value
            assert stranded.data_quality["handoff"]["status"] == "validating"
            assert stranded.completed_at is None
    finally:
        database.dispose()
    assert not (job_dir / "receipt.json").exists()

    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )

    assert _row_counts(settings, bundle["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )
    archived = json.loads((job_dir / "attempts" / "0001" / "receipt.json").read_text())
    assert "Interrupted file handoff attempt" in archived["error"]
    assert archived["attempt_number"] == 1


def test_running_recovery_serializes_token_replacement_without_mixed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    request_payload = _read_request(job_dir)
    request = handoff_service.HandoffRequest.model_validate(request_payload)
    original_execute = CommitteeWorkflow.execute_prepared

    def strand_after_claim(self, prepared, **_kwargs):
        with self.database.session_factory() as session:
            row = session.get(WorkflowRun, prepared.row.id)
            assert row is not None
            row.status = RunStatus.RUNNING.value
            row.started_at = FINALIZED_AT
            quality = json.loads(json.dumps(row.data_quality))
            quality["concurrent_operator_note"] = {"preserve": True}
            row.data_quality = quality
            session.commit()
        raise SystemExit("synthetic hard exit for recovery CAS")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "execute_prepared",
        strand_after_claim,
    )
    with pytest.raises(SystemExit, match="recovery CAS"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)
    monkeypatch.setattr(
        CommitteeWorkflow,
        "execute_prepared",
        original_execute,
    )

    database = Database(settings.database_url)
    entered_recovery = threading.Event()
    release_recovery = threading.Event()
    stale_writer_finished = threading.Event()
    failures: list[BaseException] = []
    stale_rowcounts: list[int] = []
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            old_token = row.data_quality["handoff"]["execution_token"]
            stale_quality = json.loads(json.dumps(row.data_quality))
            stale_quality["handoff"]["execution_token"] = "d" * 64
            stale_quality["handoff"]["successor_marker"] = "replacement"

        original_verify_quant = handoff_service._verify_quant_records

        def pause_after_write_reservation(*args, **kwargs) -> None:
            original_verify_quant(*args, **kwargs)
            entered_recovery.set()
            if not release_recovery.wait(timeout=5):
                raise TimeoutError("test did not release running recovery")

        monkeypatch.setattr(
            handoff_service,
            "_verify_quant_records",
            pause_after_write_reservation,
        )

        def recover_in_thread() -> None:
            try:
                handoff_service._recover_interrupted_running_attempt(
                    database,
                    request=request,
                    recovered_at=FINALIZED_AT + timedelta(minutes=1),
                    settings=settings,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def replace_token_in_thread() -> None:
            try:
                with database.session_factory() as session:
                    changed = session.execute(
                        update(WorkflowRun)
                        .where(
                            WorkflowRun.id == bundle["run_id"],
                            WorkflowRun.status == RunStatus.RUNNING.value,
                        )
                        .values(data_quality=stale_quality)
                        .execution_options(synchronize_session=False)
                    )
                    stale_rowcounts.append(int(changed.rowcount or 0))
                    session.commit()
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                stale_writer_finished.set()

        recovery_thread = threading.Thread(target=recover_in_thread)
        recovery_thread.start()
        assert entered_recovery.wait(timeout=5)
        stale_thread = threading.Thread(target=replace_token_in_thread)
        stale_thread.start()
        assert not stale_writer_finished.wait(timeout=0.1)
        release_recovery.set()
        recovery_thread.join(timeout=10)
        stale_thread.join(timeout=10)

        assert not recovery_thread.is_alive()
        assert not stale_thread.is_alive()
        assert failures == []
        assert stale_rowcounts == [0]
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.status == RunStatus.FAILED.value
            assert row.data_quality["handoff"]["execution_token"] == old_token
            assert "successor_marker" not in row.data_quality["handoff"]
            assert row.data_quality["concurrent_operator_note"] == {"preserve": True}
    finally:
        release_recovery.set()
        database.dispose()


def test_retry_transition_chain_survives_a_later_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    first_receipt = json.loads((job_dir / "receipt.json").read_text())
    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )
    retry_bundle = _draft_bundle(request)
    retry_bundle["generated_at"] = (FINALIZED_AT + timedelta(minutes=2)).isoformat()
    _write_drafts(job_dir, retry_bundle)

    original_persist = CommitteeWorkflow._persist_result

    def fail_second_persistence(_session, _state) -> None:
        raise RuntimeError("synthetic second-attempt failure")

    monkeypatch.setattr(
        CommitteeWorkflow,
        "_persist_result",
        staticmethod(fail_second_persistence),
    )
    try:
        with pytest.raises(RuntimeError, match="second-attempt failure"):
            finalize_handoff(
                settings,
                job_dir,
                now=FINALIZED_AT + timedelta(minutes=3),
            )
    finally:
        monkeypatch.setattr(
            CommitteeWorkflow,
            "_persist_result",
            staticmethod(original_persist),
        )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, request["run_id"])
            assert row is not None
            handoff = row.data_quality["handoff"]
            assert row.status == RunStatus.FAILED.value
            assert len(handoff["attempt_history"]) == 2
            assert len(handoff["retry_transitions"]) == 1
            assert (
                handoff["retry_transitions"][0]["previous_receipt_hash"]
                == first_receipt["receipt_hash"]
            )
            assert (
                handoff["attempt_history"][1]["receipt"]["previous_receipt_hash"]
                == first_receipt["receipt_hash"]
            )
    finally:
        database.dispose()


def test_backdated_failed_retry_is_rejected_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            before = session.get(WorkflowRun, request["run_id"])
            assert before is not None
            before_quality = json.loads(json.dumps(before.data_quality))
            assert before.completed_at is not None
    finally:
        database.dispose()
    drafts_before = (job_dir / "drafts.json").read_bytes()
    receipt_before = (job_dir / "receipt.json").read_bytes()

    with pytest.raises(ValueError, match="out of order"):
        retry_failed_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT - timedelta(seconds=1),
        )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            after = session.get(WorkflowRun, request["run_id"])
            assert after is not None
            assert after.status == RunStatus.FAILED.value
            assert after.data_quality == before_quality
    finally:
        database.dispose()
    assert (job_dir / "drafts.json").read_bytes() == drafts_before
    assert (job_dir / "receipt.json").read_bytes() == receipt_before
    assert not (job_dir / "attempts").exists()


def test_finalize_rejects_retry_transition_before_failed_attempt_terminal_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, request["run_id"])
            assert row is not None
            quality = json.loads(json.dumps(row.data_quality))
            handoff = quality["handoff"]
            transition = dict(handoff["retry_transitions"][0])
            transition["transitioned_at"] = (FINALIZED_AT - timedelta(seconds=1)).isoformat()
            transition_without_hash = {
                key: value for key, value in transition.items() if key != "transition_hash"
            }
            transition["transition_hash"] = handoff_service._canonical_hash(transition_without_hash)
            handoff["retry_transitions"] = [transition]
            handoff["retry_transitions_hash"] = handoff_service._canonical_hash([transition])
            row.data_quality = quality
            session.commit()
    finally:
        database.dispose()

    with pytest.raises(ValueError, match="transition time is out of order"):
        finalize_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT + timedelta(minutes=2),
        )


def test_retry_rejects_tampered_failed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, _ = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    receipt_path = job_dir / "receipt.json"
    payload = json.loads(receipt_path.read_text())
    payload["error"] = "tampered"
    receipt_path.chmod(0o600)
    receipt_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="receipt hash|attempt seal"):
        retry_failed_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT + timedelta(minutes=1),
        )


def test_retry_accepts_a_prearchived_failed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    archive = job_dir / "attempts" / "0001"
    archive.mkdir(parents=True)
    (job_dir / "drafts.json").rename(archive / "drafts.json")
    (job_dir / "receipt.json").rename(archive / "receipt.json")

    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )

    assert _row_counts(settings, request["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )
    assert {item.name for item in archive.iterdir()} == {
        "drafts.json",
        "receipt.json",
    }


def test_retry_resumes_after_archive_link_and_unlink_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    original_archive = handoff_service._archive_attempt_artifact
    crashed = False

    def crash_after_first_archive(*args, **kwargs):
        nonlocal crashed
        original_archive(*args, **kwargs)
        if kwargs["name"] == "drafts.json" and not crashed:
            crashed = True
            raise OSError("synthetic crash after archive unlink")

    monkeypatch.setattr(
        handoff_service,
        "_archive_attempt_artifact",
        crash_after_first_archive,
    )
    with pytest.raises(OSError, match="archive unlink"):
        retry_failed_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT + timedelta(minutes=1),
        )
    assert _row_counts(settings, request["run_id"]) == ("failed", 0, 0)
    assert (job_dir / "attempts" / "0001" / "drafts.json").is_file()
    assert not (job_dir / "drafts.json").exists()
    assert (job_dir / "receipt.json").is_file()

    monkeypatch.setattr(
        handoff_service,
        "_archive_attempt_artifact",
        original_archive,
    )
    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=2),
    )
    assert _row_counts(settings, request["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )


def test_retry_syncs_archive_namespaces_before_database_rearm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, _ = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    original_sync = handoff_service._fsync_directory
    original_rearm = handoff_service._rearm_failed_run
    synced: list[Path] = []

    def record_sync(path: Path) -> None:
        original_sync(path)
        synced.append(path.resolve())

    def assert_synced_before_rearm(*args, **kwargs) -> None:
        archive = (job_dir / "attempts" / "0001").resolve()
        assert archive in synced
        assert archive.parent in synced
        assert job_dir.resolve() in synced
        original_rearm(*args, **kwargs)

    monkeypatch.setattr(
        handoff_service,
        "_fsync_directory",
        record_sync,
    )
    monkeypatch.setattr(
        handoff_service,
        "_rearm_failed_run",
        assert_synced_before_rearm,
    )
    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )


def test_attempt_lock_blocks_retry_until_terminal_publisher_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    started = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def retry_in_thread() -> None:
        started.set()
        try:
            retry_failed_handoff(
                settings,
                job_dir,
                now=FINALIZED_AT + timedelta(minutes=1),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            finished.set()

    with handoff_service._handoff_attempt_lock(job_dir):
        # Replacing the historical named lock path must not create a second
        # lock domain: v3 now flocks the stable job-directory inode.
        obsolete_lock = job_dir / ".attempt.lock"
        obsolete_lock.write_text("old inode", encoding="utf-8")
        obsolete_lock.unlink()
        obsolete_lock.write_text("replacement inode", encoding="utf-8")
        thread = threading.Thread(target=retry_in_thread)
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert _row_counts(settings, request["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )


def test_sqlite_rearm_serializes_double_connection_cas_and_preserves_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _, _, request_payload = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    request = handoff_service.HandoffRequest.model_validate(request_payload)
    database = Database(settings.database_url)
    entered_rearm = threading.Event()
    release_rearm = threading.Event()
    stale_writer_finished = threading.Event()
    failures: list[BaseException] = []
    stale_rowcounts: list[int] = []

    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, request_payload["run_id"])
            assert row is not None
            quality = json.loads(json.dumps(row.data_quality))
            quality["concurrent_operator_note"] = {"preserve": True}
            row.data_quality = quality
            session.commit()
            expected_attempt, history = handoff_service._verify_attempt_state(
                row,
                request=request,
                expected_status=RunStatus.FAILED,
            )
            stale_quality = json.loads(json.dumps(row.data_quality))
            stale_quality["stale_writer"] = True

        original_verify_quant = handoff_service._verify_quant_records

        def pause_after_sqlite_write_reservation(*args, **kwargs) -> None:
            original_verify_quant(*args, **kwargs)
            entered_rearm.set()
            if not release_rearm.wait(timeout=5):
                raise TimeoutError("test did not release SQLite rearm")

        monkeypatch.setattr(
            handoff_service,
            "_verify_quant_records",
            pause_after_sqlite_write_reservation,
        )

        def rearm_in_thread() -> None:
            try:
                handoff_service._rearm_failed_run(
                    database,
                    request=request,
                    expected_attempt=expected_attempt,
                    attempt_history=history,
                    retried_at=FINALIZED_AT + timedelta(minutes=1),
                    settings=settings,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def stale_write_in_thread() -> None:
            try:
                with database.session_factory() as session:
                    changed = session.execute(
                        update(WorkflowRun)
                        .where(
                            WorkflowRun.id == request_payload["run_id"],
                            WorkflowRun.status == RunStatus.FAILED.value,
                        )
                        .values(data_quality=stale_quality)
                        .execution_options(synchronize_session=False)
                    )
                    stale_rowcounts.append(int(changed.rowcount or 0))
                    session.commit()
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                stale_writer_finished.set()

        rearm_thread = threading.Thread(target=rearm_in_thread)
        rearm_thread.start()
        assert entered_rearm.wait(timeout=5)
        stale_thread = threading.Thread(target=stale_write_in_thread)
        stale_thread.start()
        assert not stale_writer_finished.wait(timeout=0.1)
        release_rearm.set()
        rearm_thread.join(timeout=10)
        stale_thread.join(timeout=10)

        assert not rearm_thread.is_alive()
        assert not stale_thread.is_alive()
        assert failures == []
        assert stale_rowcounts == [0]
        with database.session_factory() as session:
            row = session.get(WorkflowRun, request_payload["run_id"])
            assert row is not None
            assert row.status == RunStatus.AWAITING_DRAFT.value
            assert row.data_quality["concurrent_operator_note"] == {"preserve": True}
            assert "stale_writer" not in row.data_quality
            assert row.data_quality["handoff"]["attempt_number"] == 2
    finally:
        release_rearm.set()
        database.dispose()


def test_stale_failed_publisher_cannot_write_into_rearmed_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_publish = handoff_service._publish_and_seal_failed_attempt
    captured: dict[str, Any] = {}

    def capture_publish(database: Database, **kwargs) -> None:
        captured.update(kwargs)
        original_publish(database, **kwargs)

    monkeypatch.setattr(
        handoff_service,
        "_publish_and_seal_failed_attempt",
        capture_publish,
    )
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setattr(
        handoff_service,
        "_publish_and_seal_failed_attempt",
        original_publish,
    )
    retry_failed_handoff(
        settings,
        job_dir,
        now=FINALIZED_AT + timedelta(minutes=1),
    )
    assert not (job_dir / "receipt.json").exists()

    database = Database(settings.database_url)
    try:
        with handoff_service._handoff_attempt_lock(job_dir):
            original_publish(database, **captured)
    finally:
        database.dispose()

    assert not (job_dir / "receipt.json").exists()
    assert _row_counts(settings, request["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )


def test_stale_attempt_token_rejects_graph_claim_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    original_seal = handoff_service._seal_validating_attempt

    def replace_token_after_seal(database: Database, **kwargs):
        detached = original_seal(database, **kwargs)
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            quality = json.loads(json.dumps(row.data_quality))
            quality["handoff"]["execution_token"] = "c" * 64
            quality["handoff"]["successor_marker"] = "claim-owner-replaced"
            row.data_quality = quality
            session.commit()
        return detached

    monkeypatch.setattr(
        handoff_service,
        "_seal_validating_attempt",
        replace_token_after_seal,
    )
    with pytest.raises(StaleRunExecutionError, match="execution fence is stale"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.status == RunStatus.AWAITING_DRAFT.value
            assert row.completed_at is None
            assert row.data_quality["handoff"]["execution_token"] == "c" * 64
            assert row.data_quality["handoff"]["successor_marker"] == "claim-owner-replaced"
    finally:
        database.dispose()
    assert _row_counts(settings, bundle["run_id"]) == (
        "awaiting_draft",
        0,
        0,
    )
    assert not (job_dir / "receipt.json").exists()


@pytest.mark.parametrize("graph_outcome", ["success", "failure"])
def test_stale_executor_cannot_publish_after_attempt_token_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_outcome: str,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    graph_finished = threading.Event()
    release_stale_executor = threading.Event()
    original_init = CommitteeWorkflow.__init__
    failures: list[BaseException] = []

    class BlockingGraph:
        def __init__(self, graph) -> None:
            self.graph = graph

        def invoke(self, *args, **kwargs):
            result = self.graph.invoke(*args, **kwargs)
            graph_finished.set()
            if not release_stale_executor.wait(timeout=5):
                raise TimeoutError("test did not release stale executor")
            if graph_outcome == "failure":
                raise RuntimeError("synthetic stale graph failure")
            return result

    def install_blocking_graph(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        self.graph = BlockingGraph(self.graph)

    monkeypatch.setattr(
        CommitteeWorkflow,
        "__init__",
        install_blocking_graph,
    )

    def finalize_in_thread() -> None:
        try:
            finalize_handoff(settings, job_dir, now=FINALIZED_AT)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=finalize_in_thread)
    thread.start()
    assert graph_finished.wait(timeout=5)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.status == RunStatus.RUNNING.value
            quality = json.loads(json.dumps(row.data_quality))
            handoff = quality["handoff"]
            old_token = handoff["execution_token"]
            handoff["attempt_number"] = 2
            handoff["checkpoint_thread_id"] = f"handoff:{bundle['run_id']}:2"
            handoff["execution_token"] = "b" * 64
            handoff["successor_marker"] = "attempt-2"
            row.data_quality = quality
            session.commit()
            assert handoff["execution_token"] != old_token
    finally:
        database.dispose()

    release_stale_executor.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], StaleRunExecutionError)

    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            row = session.get(WorkflowRun, bundle["run_id"])
            assert row is not None
            assert row.status == RunStatus.RUNNING.value
            assert row.data_quality["handoff"]["execution_token"] == "b" * 64
            assert row.data_quality["handoff"]["successor_marker"] == "attempt-2"
    finally:
        database.dispose()
    assert _row_counts(settings, bundle["run_id"]) == ("running", 0, 0)
    assert not (job_dir / "receipt.json").exists()


def test_retry_rejects_attempt_archive_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    outside = tmp_path / "outside-attempts"
    outside.mkdir()
    (job_dir / "attempts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="attempts directory.*symlink"):
        retry_failed_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT + timedelta(minutes=1),
        )
    assert _row_counts(settings, request["run_id"]) == ("failed", 0, 0)


def test_retry_rejects_expired_or_nonfailed_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    deadline = datetime.fromisoformat(request["finalize_deadline"])
    with pytest.raises(ValueError, match="retry deadline"):
        retry_failed_handoff(
            settings,
            job_dir,
            now=deadline + timedelta(seconds=1),
        )

    awaiting_root = tmp_path / "awaiting"
    awaiting_root.mkdir()
    awaiting_settings, awaiting_job, _ = _prepare_valid_bundle(awaiting_root)
    with pytest.raises(RuntimeError, match="status must be failed"):
        retry_failed_handoff(
            awaiting_settings,
            awaiting_job,
            now=FINALIZED_AT,
        )


def test_retry_rejects_partial_outputs_and_active_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, job_dir, _, request = _prepare_failed_quant_handoff(
        tmp_path,
        monkeypatch,
    )
    run_id = request["run_id"]
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            session.add(
                AgentOpinion(
                    id="partial-opinion",
                    run_id=run_id,
                    agent_id="macro_policy_agent",
                    agent_name="partial",
                    role="partial",
                    agent_version="0.1.0",
                    model_name="test",
                    status="completed",
                    index_code=INDEXES[0].code,
                    horizon=Horizon.D1.value,
                    target_date=date.fromisoformat(request["assignments"][0]["target_date"]),
                    direction="up",
                    probability_up=0.6,
                    probability_neutral=0.2,
                    probability_down=0.2,
                    summary="partial",
                    evidence=[],
                    counter_evidence=[],
                    invalidation_conditions=[],
                    citations=[],
                    contribution="",
                    weight=1.0,
                    raw_response={},
                )
            )
            session.commit()
    finally:
        database.dispose()

    with pytest.raises(RuntimeError, match="zero outputs"):
        retry_failed_handoff(
            settings,
            job_dir,
            now=FINALIZED_AT + timedelta(minutes=1),
        )

    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    replacement_settings, replacement_job, _, replacement_request = _prepare_failed_quant_handoff(
        replacement_root,
        monkeypatch,
    )
    database = Database(replacement_settings.database_url)
    try:
        with database.session_factory() as session:
            failed = session.get(WorkflowRun, replacement_request["run_id"])
            assert failed is not None
            session.add(
                WorkflowRun(
                    id="active-replacement-run",
                    as_of=failed.as_of,
                    data_cutoff=failed.data_cutoff,
                    status=RunStatus.AWAITING_DRAFT.value,
                    mode=failed.mode,
                    started_at=failed.started_at,
                    completed_at=None,
                    duration_seconds=None,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash="a" * 64,
                    market_universe_hash=failed.market_universe_hash,
                )
            )
            session.commit()
    finally:
        database.dispose()

    with pytest.raises(RuntimeError, match="active replacement"):
        retry_failed_handoff(
            replacement_settings,
            replacement_job,
            now=FINALIZED_AT + timedelta(minutes=1),
        )


def test_input_tamper_is_rejected_against_database_seal(tmp_path: Path) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    request = _read_request(job_dir)
    request["mode"] = "live"
    (job_dir / "input.json").chmod(0o600)
    (job_dir / "input.json").write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises((RuntimeError, ValueError), match="hash|seal|request|tamper"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, bundle["run_id"]) == ("awaiting_draft", 0, 0)


def test_expired_handoff_is_rejected_before_run_claim(tmp_path: Path) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    request = _read_request(job_dir)
    deadline = datetime.fromisoformat(request["finalize_deadline"])

    with pytest.raises(ValueError, match="deadline|passed"):
        finalize_handoff(
            settings,
            job_dir,
            now=deadline.replace(microsecond=0) + timedelta(seconds=1),
        )

    assert _row_counts(settings, bundle["run_id"]) == ("awaiting_draft", 0, 0)


def test_live_prepare_is_blocked_before_market_close(tmp_path: Path) -> None:
    settings = _settings(tmp_path, execution_provider="codex_file")

    with pytest.raises(ValueError, match="after the trading-day close"):
        prepare_handoff(
            settings,
            as_of=AS_OF,
            now=AS_OF.replace(hour=14),
        )


@pytest.mark.parametrize(
    ("mutation", "error_pattern"),
    [
        ("missing", "25|missing|identit"),
        ("wrong_wiki", "Wiki|wiki|entry"),
        ("wrong_protocol", "protocol"),
        ("neutral_direction", "direction|neutral"),
        ("directional_tie", "tie|tied|probabilit"),
    ],
)
def test_invalid_draft_packages_are_rejected_before_claim(
    tmp_path: Path,
    mutation: str,
    error_pattern: str,
) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    if mutation == "missing":
        bundle["drafts"].pop()
    elif mutation == "wrong_wiki":
        bundle["drafts"][0]["draft"]["wiki_entry_id"] = "VC-WIKI-NOT-FROZEN"
    elif mutation == "wrong_protocol":
        bundle["protocol_version"] = LEGACY_HANDOFF_PROTOCOL_VERSION
    elif mutation == "neutral_direction":
        bundle["drafts"][0]["draft"]["direction"] = "neutral"
    elif mutation == "directional_tie":
        bundle["drafts"][0]["draft"]["probabilities"] = {
            "up": 0.25,
            "neutral": 0.50,
            "down": 0.25,
        }
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    _write_drafts(job_dir, bundle)

    with pytest.raises((RuntimeError, ValueError), match=error_pattern):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, bundle["run_id"]) == ("awaiting_draft", 0, 0)


def test_codex_file_health_is_not_demo_and_http_run_is_blocked(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        execution_provider="codex_file",
        operator_token=TEST_OPERATOR_TOKEN,
    )

    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as client:
        health = client.get("/api/health")
        response = client.post(
            "/api/runs",
            json={"as_of": AS_OF.isoformat()},
            headers={"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"},
        )

    assert health.status_code == 200
    assert health.json()["mode"] == "codex-file"
    assert response.status_code == 409
    assert "codex_handoff.py" in response.text
