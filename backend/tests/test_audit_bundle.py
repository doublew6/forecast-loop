from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from app.services.audit_bundle import (
    AUDIT_ARTIFACT_PATHS,
    MAX_INSTRUCTIONS_BYTES,
    AuditBundleError,
    export_audit_bundle,
    verify_audit_bundle,
)
from app.services.handoff import HandoffRequest, finalize_handoff, prepare_handoff
from app.services.run_bundle import export_run_bundle
from app.services.schema_readiness import upgrade_database

ZONE = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 13, 15, 0, tzinfo=ZONE)
PREPARED_AT = datetime(2026, 7, 13, 15, 1, tzinfo=ZONE)
FINALIZED_AT = datetime(2026, 7, 13, 15, 10, tzinfo=ZONE)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any, *, trailing_newline: bool = False) -> str:
    body = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if trailing_newline:
        body += "\n"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _draft_bundle(request: HandoffRequest) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for position, assignment in enumerate(request.assignments):
        bullish = position % 2 == 0
        records.append(
            {
                "agent_id": assignment.agent_id,
                "index_code": assignment.index_code,
                "horizon": assignment.horizon.value,
                **(
                    {"agent_brief": assignment.agent_brief}
                    if assignment.agent_brief is not None
                    else {}
                ),
                "draft": {
                    "direction": "up" if bullish else "down",
                    "probabilities": (
                        {"up": 0.56, "neutral": 0.24, "down": 0.20}
                        if bullish
                        else {"up": 0.20, "neutral": 0.24, "down": 0.56}
                    ),
                    "summary": (
                        f"基于冻结输入完成 {assignment.agent_id} 对 "
                        f"{assignment.index_code}/{assignment.horizon.value} 的判断。"
                    ),
                    "evidence": ["已核对交接包内的冻结证据与对应 Wiki 方法。"],
                    "counter_evidence": ["共同来源和信息遗漏可能削弱方向判断。"],
                    "invalidation_conditions": ["冻结证据或 Wiki 校验失败时判断失效。"],
                    "evidence_item_ids": assignment.allowed_evidence_item_ids[:1],
                    "wiki_entry_id": assignment.wiki_entry_id,
                    "wiki_section": assignment.wiki_sections[0],
                },
            }
        )
    return {
        "protocol_version": request.protocol_version,
        "run_id": str(request.run_id),
        "input_hash": request.input_hash,
        "request_hash": request.request_hash,
        "generated_at": PREPARED_AT.isoformat(),
        "generated_by": {
            "surface": "codex",
            "task_id": "audit-bundle-test",
            "model": "test-model",
        },
        "drafts": records,
    }


def _completed_sources(client, tmp_path: Path) -> tuple[Path, Path, Path]:
    handoff_root = tmp_path / "handoffs"
    settings = client.app.state.settings.model_copy(update={"handoff_root": handoff_root})
    upgrade_database(settings.database_url)
    job_dir = prepare_handoff(
        settings,
        as_of=AS_OF,
        now=PREPARED_AT,
    )
    request = HandoffRequest.model_validate_json((job_dir / "input.json").read_bytes())
    drafts_path = job_dir / "drafts.json"
    drafts_path.write_bytes(_json_bytes(_draft_bundle(request)))
    drafts_path.chmod(0o600)
    finalize_handoff(settings, job_dir, now=FINALIZED_AT)

    result_bundle = export_run_bundle(
        client.app.state.database,
        run_id=str(request.run_id),
        output_root=tmp_path / "run-bundles",
        exported_at=datetime(2026, 7, 24, 11, tzinfo=UTC),
    )
    return handoff_root, job_dir, result_bundle


def _export_completed_audit(client, tmp_path: Path) -> Path:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    return export_audit_bundle(
        handoff_root=handoff_root,
        job_dir=job_dir,
        run_bundle_path=result_bundle,
        output_root=tmp_path / "audit-bundles",
        exported_at=datetime(2026, 7, 24, 12, tzinfo=UTC),
    )


def _reseal_receipt(job_dir: Path, **updates: Any) -> None:
    receipt_path = job_dir / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.update(updates)
    receipt["receipt_hash"] = _canonical_hash(
        {key: value for key, value in receipt.items() if key != "receipt_hash"}
    )
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(_json_bytes(receipt))


def test_export_and_verify_completed_handoff_audit_bundle(client, tmp_path) -> None:
    audit_bundle = _export_completed_audit(client, tmp_path)

    manifest = verify_audit_bundle(audit_bundle)

    assert audit_bundle.name == manifest.run_id
    assert manifest.status == "completed"
    assert manifest.integrity_algorithm == "sha256"
    assert manifest.publisher_authentication == "none"
    assert manifest.reproducibility_scope == "frozen-inputs-and-output-linkage"
    assert manifest.external_orchestration_captured is False
    assert manifest.runtime_environment_captured is False
    assert manifest.input_hash_verification == "cross-artifact-linkage"
    assert manifest.output_hash_verification == "recomputed"
    assert [artifact.path for artifact in manifest.artifacts] == list(AUDIT_ARTIFACT_PATHS)
    instructions = audit_bundle / "handoff" / "INSTRUCTIONS.md"
    assert instructions.is_file()
    assert f"任务 ID：`{manifest.run_id}`" in instructions.read_text(encoding="utf-8")
    input_payload = json.loads(
        (audit_bundle / "handoff" / "input.json").read_text(encoding="utf-8")
    )
    evidence_payload = json.loads(
        (audit_bundle / "handoff" / "evidence_snapshot.json").read_text(encoding="utf-8")
    )
    receipt_payload = json.loads(
        (audit_bundle / "handoff" / "receipt.json").read_text(encoding="utf-8")
    )
    assert evidence_payload == input_payload["initial_state"]["evidence_snapshot"]
    assert manifest.evidence_content_hash == evidence_payload["content_hash"]
    assert manifest.output_hash == receipt_payload["output_hash"]


def test_export_rejects_mixed_draft_protocol_version(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    drafts_path = job_dir / "drafts.json"
    drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
    drafts["protocol_version"] = "1.0.0"
    drafts_body = _json_bytes(drafts)
    drafts_path.chmod(0o600)
    drafts_path.write_bytes(drafts_body)
    _reseal_receipt(
        job_dir,
        drafts_hash=_canonical_hash(drafts),
        drafts_raw_hash=hashlib.sha256(drafts_body).hexdigest(),
    )

    with pytest.raises(AuditBundleError, match="drafts.json protocol_version"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=tmp_path / "audit-bundles",
        )


def test_export_rejects_mixed_receipt_protocol_and_provider(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    _reseal_receipt(
        job_dir,
        protocol_version="1.0.0",
        provider="codex-file-handoff-v1",
    )

    with pytest.raises(AuditBundleError, match="receipt.json does not match"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=tmp_path / "audit-bundles",
        )


def test_export_rejects_legacy_v1_result_bundle(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    manifest_path = result_bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "vericouncil.run-bundle/v1"
    manifest["bundle_hash"] = _canonical_hash(
        {key: value for key, value in manifest.items() if key != "bundle_hash"},
        trailing_newline=True,
    )
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(AuditBundleError, match="run-bundle/v2"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=tmp_path / "audit-bundles",
        )


def test_verify_keeps_legacy_v1_result_bundle_read_only_compatibility(
    client,
    tmp_path,
) -> None:
    audit_bundle = _export_completed_audit(client, tmp_path)
    result_manifest_path = audit_bundle / "results" / "manifest.json"
    result_manifest = json.loads(result_manifest_path.read_text(encoding="utf-8"))
    result_manifest["schema_version"] = "vericouncil.run-bundle/v1"
    result_manifest["bundle_hash"] = _canonical_hash(
        {key: value for key, value in result_manifest.items() if key != "bundle_hash"},
        trailing_newline=True,
    )
    result_manifest_body = _json_bytes(result_manifest)
    result_manifest_path.write_bytes(result_manifest_body)

    audit_manifest_path = audit_bundle / "manifest.json"
    audit_manifest = json.loads(audit_manifest_path.read_text(encoding="utf-8"))
    audit_manifest["run_bundle_hash"] = result_manifest["bundle_hash"]
    artifact = next(
        item
        for item in audit_manifest["artifacts"]
        if item["path"] == "results/manifest.json"
    )
    artifact["sha256"] = hashlib.sha256(result_manifest_body).hexdigest()
    artifact["size"] = len(result_manifest_body)
    audit_manifest["bundle_hash"] = _canonical_hash(
        {key: value for key, value in audit_manifest.items() if key != "bundle_hash"}
    )
    audit_manifest_path.write_bytes(_json_bytes(audit_manifest))

    manifest = verify_audit_bundle(audit_bundle)
    assert manifest.run_bundle_hash == result_manifest["bundle_hash"]


def test_verify_rejects_resealed_result_that_breaks_receipt_output_link(client, tmp_path) -> None:
    audit_bundle = _export_completed_audit(client, tmp_path)
    opinions_path = audit_bundle / "results" / "opinions.json"
    opinions = json.loads(opinions_path.read_text(encoding="utf-8"))
    opinions[0]["summary"] += " tampered"
    opinions_body = _json_bytes(opinions)
    opinions_path.write_bytes(opinions_body)

    result_manifest_path = audit_bundle / "results" / "manifest.json"
    result_manifest = json.loads(result_manifest_path.read_text(encoding="utf-8"))
    opinion_artifact = next(
        item for item in result_manifest["artifacts"] if item["path"] == "opinions.json"
    )
    opinion_artifact["sha256"] = hashlib.sha256(opinions_body).hexdigest()
    opinion_artifact["size"] = len(opinions_body)
    result_manifest["bundle_hash"] = _canonical_hash(
        {key: value for key, value in result_manifest.items() if key != "bundle_hash"},
        trailing_newline=True,
    )
    result_manifest_body = _json_bytes(result_manifest)
    result_manifest_path.write_bytes(result_manifest_body)

    audit_manifest_path = audit_bundle / "manifest.json"
    audit_manifest = json.loads(audit_manifest_path.read_text(encoding="utf-8"))
    audit_manifest["run_bundle_hash"] = result_manifest["bundle_hash"]
    for artifact in audit_manifest["artifacts"]:
        if artifact["path"] == "results/opinions.json":
            artifact["sha256"] = hashlib.sha256(opinions_body).hexdigest()
            artifact["size"] = len(opinions_body)
        elif artifact["path"] == "results/manifest.json":
            artifact["sha256"] = hashlib.sha256(result_manifest_body).hexdigest()
            artifact["size"] = len(result_manifest_body)
    audit_manifest["bundle_hash"] = _canonical_hash(
        {key: value for key, value in audit_manifest.items() if key != "bundle_hash"}
    )
    audit_manifest_path.write_bytes(_json_bytes(audit_manifest))

    with pytest.raises(AuditBundleError, match="output hash"):
        verify_audit_bundle(audit_bundle)


def test_export_rejects_symlinked_handoff_artifact(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    template = job_dir / "drafts.template.json"
    copied_template = tmp_path / "copied-template.json"
    copied_template.write_bytes(template.read_bytes())
    template.unlink()
    template.symlink_to(copied_template)

    with pytest.raises(AuditBundleError, match="symlink|opened safely"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=tmp_path / "audit-bundles",
        )


def test_export_rejects_oversized_handoff_instructions(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    instructions = job_dir / "INSTRUCTIONS.md"
    instructions.chmod(0o600)
    with instructions.open("wb") as stream:
        stream.truncate(MAX_INSTRUCTIONS_BYTES + 1)

    with pytest.raises(AuditBundleError, match="invalid size"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=tmp_path / "audit-bundles",
        )


def test_export_never_overwrites_existing_audit_bundle(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    output_root = tmp_path / "audit-bundles"
    export_audit_bundle(
        handoff_root=handoff_root,
        job_dir=job_dir,
        run_bundle_path=result_bundle,
        output_root=output_root,
    )

    with pytest.raises(AuditBundleError, match="already exists"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=output_root,
        )


def test_export_rejects_output_root_inside_handoff_source(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)

    with pytest.raises(AuditBundleError, match="inside a source"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=job_dir / "exports",
        )


def test_export_rejects_group_or_world_writable_output_root(client, tmp_path) -> None:
    handoff_root, job_dir, result_bundle = _completed_sources(client, tmp_path)
    output_root = tmp_path / "shared-audit-output"
    output_root.mkdir()
    output_root.chmod(0o777)

    with pytest.raises(AuditBundleError, match="group/world writable"):
        export_audit_bundle(
            handoff_root=handoff_root,
            job_dir=job_dir,
            run_bundle_path=result_bundle,
            output_root=output_root,
        )
