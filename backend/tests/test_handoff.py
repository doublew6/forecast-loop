from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from app.agent_contracts import (
    SignalProbabilityVector,
    SignalTarget,
)
from app.config import Settings
from app.db import Database
from app.domain import INDEXES, Horizon
from app.main import create_app
from app.market_universe import DEFAULT_MARKET_UNIVERSE
from app.models import AgentOpinion, Forecast, SignalEnvelopeRecord, WorkflowRun
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
    finalize_handoff,
    prepare_handoff,
)
from app.services.schema_readiness import SchemaNotReadyError, upgrade_database
from app.services.snapshot import load_evidence_snapshot
from fastapi.testclient import TestClient
from sqlalchemy import func, select

ZONE = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 13, 15, 0, tzinfo=ZONE)
PREPARED_AT = datetime(2026, 7, 13, 15, 1, tzinfo=ZONE)
FINALIZED_AT = datetime(2026, 7, 13, 15, 10, tzinfo=ZONE)
TEST_OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"


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
        for horizon in Horizon
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
                target_date=(
                    model_targets[f"{index.code}/{horizon.value}"]
                ),
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
        for horizon in Horizon
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
                "f" * 64
                if wrong_evidence_snapshot_hash
                else snapshot.content_hash
            ),
            market_universe_hash=(
                "f" * 64
                if wrong_market_universe_hash
                else DEFAULT_MARKET_UNIVERSE.content_hash
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
    assert request["provider"] == "codex-file-handoff-v2"
    instructions = (job_dir / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert instructions.startswith("# forecast-loop Codex 文件交接任务")
    assert len(request["assignments"]) == 50
    assert (
        len(
            {
                (item["agent_id"], item["index_code"], item["horizon"])
                for item in request["assignments"]
            }
        )
        == 50
    )
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
    custom_settings = settings.model_copy(
        update={"market_universe_path": universe_path}
    )

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
    template = json.loads(
        (job_dir / "drafts.template.json").read_text(encoding="utf-8")
    )
    template_records = {
        (item["agent_id"], item["index_code"], item["horizon"]): item
        for item in template["drafts"]
    }

    for agent_id, expected_brief in custom_briefs.items():
        identity = (agent_id, first_code, "D1")
        assert assignments[identity]["agent_brief"] == expected_brief
        assert template_records[identity]["agent_brief"] == expected_brief

    unsigned_request = dict(request)
    request_hash = unsigned_request.pop("request_hash")
    assert request_hash == hashlib.sha256(
        json.dumps(
            unsigned_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
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

    assert len(request["assignments"]) == 50
    assert "quant_agent" not in {assignment["agent_id"] for assignment in request["assignments"]}
    assert external["agent_version"] == "0.3.0"
    assert external["participation_mode"] == "shadow"
    assert external["schema_version"] == "forecast-loop.quant-run-input-binding/v1"
    assert external["signal_count"] == 10
    assert external["bundle_content_hash"] == manifest["content_hash"]
    assert external["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert (
        external["input_snapshot_sha256"]
        == hashlib.sha256(input_snapshot_path.read_bytes()).hexdigest()
    )
    assert external["input_snapshot_content_hash"] == input_snapshot["content_hash"]
    assert external["evidence_snapshot_content_hash"] == frozen_evidence.content_hash
    assert (
        external["market_universe_content_hash"]
        == DEFAULT_MARKET_UNIVERSE.content_hash
    )
    assert external["decision_weight_total"] == 0
    assert external["activation_status"] == "shadow_locked"
    assert quant_audit["routing_lane"] == "shadow_benchmark"
    assert quant_audit["signal_count"] == 10
    assert quant_audit["decision_weight_total"] == 0
    assert (
        quant_audit["market_universe_content_hash"]
        == DEFAULT_MARKET_UNIVERSE.content_hash
    )

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
            assert len(records) == 10
            assert {(record.index_code, record.horizon) for record in records} == {
                (index.code, horizon.value) for index in INDEXES for horizon in Horizon
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
            assert signal_count == 10
            forecasts = session.scalars(
                select(Forecast).where(Forecast.run_id == request["run_id"])
            ).all()
            assert len(forecasts) == 10
            assert all(
                "只读 shadow 输入" in forecast.rationale
                and "正式决策权重为0" in forecast.rationale
                for forecast in forecasts
            )
    finally:
        database.dispose()
    assert _row_counts(settings, request["run_id"]) == ("completed", 60, 10)


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
                select(SignalEnvelopeRecord).where(
                    SignalEnvelopeRecord.run_id == request["run_id"]
                )
            ).all()
            assert len(records) == 10
            assert {
                datetime.fromisoformat(record.envelope["submission_deadline"]).date()
                for record in records
            } == {deadline.date()}
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("omit_last_target", "wrong_d1_session", "error_pattern"),
    [
        (True, False, "exactly 5 indexes"),
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

    with pytest.raises(ValueError, match=error_pattern):
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
            signal_count = session.scalar(
                select(func.count()).select_from(SignalEnvelopeRecord)
            )
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
            assert (
                session.scalar(select(func.count()).select_from(SignalEnvelopeRecord))
                == 0
            )
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
            assert len(records) == 10
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
    assert _row_counts(settings, run_id) == ("completed", 60, 10)
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
            assert len(file_opinions) == 50
            assert {opinion.model_name for opinion in file_opinions} == {"codex-file-handoff-v2"}
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
    assert _row_counts(settings, request["run_id"]) == ("completed", 60, 10)


def test_duplicate_finalize_is_rejected_without_duplicate_rows(tmp_path: Path) -> None:
    settings, job_dir, bundle = _prepare_valid_bundle(tmp_path)
    finalize_handoff(settings, job_dir, now=FINALIZED_AT)
    before = _row_counts(settings, bundle["run_id"])

    with pytest.raises((RuntimeError, ValueError), match="already|finalized|status|claim"):
        finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    assert _row_counts(settings, bundle["run_id"]) == before == ("completed", 60, 10)


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
        ("missing", "50|missing|identit"),
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
