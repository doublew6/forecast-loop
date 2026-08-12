"""Build sanitized, audit-linked trace views from immutable workflow receipts.

The projection deliberately returns identifiers, bounded summaries, timings and
content hashes only.  It never exposes prompts, raw model responses, frozen
source bodies or database payloads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import AgentTrace, AgentTraceSpan, WorkflowRun
from .agent_tracing import canonical_digest


@dataclass(slots=True)
class TraceViewProjection:
    audit_url: str | None = None
    audit_label: str | None = None
    span_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    synthetic_spans: list[dict[str, Any]] = field(default_factory=list)


_PREDICTION_TOOL_NAMES = {
    "freeze_snapshot": "snapshot.freeze",
    "wiki_read": "wiki.read_frozen_snapshot",
    "macro_policy_agent": "provider.research",
    "market_news_agent": "provider.research",
    "ai_storage_industry_agent": "provider.research",
    "strategy_agent": "provider.strategize",
    "risk_critic_agent": "provider.criticize",
    "evidence_validator": "evidence.validate",
    "cio_agent": "provider.decide",
    "persist_result": "database.persist_forecast_receipt",
}

_GENERIC_TOOL_NAMES = {
    "prepare": "snapshot.freeze",
    "source_discovery": "codex.source_discovery",
    "freeze_sources": "evidence.freeze_sources",
    "analysis": "codex.reflection_analysis",
    "finalize": "database.persist_reflection_receipt",
    "deterministic_evaluators": "evaluation.run_deterministic_suite",
}


def build_trace_view_projection(
    session: Session,
    trace: AgentTrace,
    spans: list[AgentTraceSpan],
) -> TraceViewProjection:
    if trace.workflow_kind == "prediction":
        return _prediction_projection(session, trace, spans)
    audit = {
        "reflection": ("/reflections", "查看反省审计详情"),
        "agent_eval": ("/evaluations", "查看评测审计详情"),
    }.get(trace.workflow_kind)
    projection = TraceViewProjection(
        audit_url=audit[0] if audit else None,
        audit_label=audit[1] if audit else None,
    )
    for span in spans:
        projection.span_overrides[span.span_id] = {
            "tool_name": _GENERIC_TOOL_NAMES.get(span.node_id, span.name),
            "input_summary": "输入已由工作流边界冻结；Trace 仅保留内容哈希。",
            "output_summary": span.summary,
            "references": [],
        }
    return projection


def _prediction_projection(
    session: Session,
    trace: AgentTrace,
    spans: list[AgentTraceSpan],
) -> TraceViewProjection:
    projection = TraceViewProjection(
        audit_url=f"/meeting/{trace.subject_id}",
        audit_label="查看投委会审计详情",
    )
    run = session.scalar(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.opinions), selectinload(WorkflowRun.forecasts))
        .where(WorkflowRun.id == trace.subject_id)
    )
    if run is None:
        return projection

    references_by_agent: dict[str, list[dict[str, Any]]] = {}
    opinion_digests_by_agent: dict[str, list[dict[str, Any]]] = {}
    opinion_counts: dict[str, int] = {}
    for opinion in run.opinions:
        opinion_counts[opinion.agent_id] = opinion_counts.get(opinion.agent_id, 0) + 1
        references_by_agent.setdefault(opinion.agent_id, []).extend(
            _sanitize_references(opinion.citations or [])
        )
        opinion_digests_by_agent.setdefault(opinion.agent_id, []).append(
            {
                "id": opinion.id,
                "index_code": opinion.index_code,
                "horizon": opinion.horizon,
                "direction": opinion.direction,
                "probabilities": [
                    opinion.probability_up,
                    opinion.probability_neutral,
                    opinion.probability_down,
                ],
                "citation_hashes": [
                    str(item.get("evidence_content_hash") or item.get("content_hash") or "")
                    for item in (opinion.citations or [])
                ],
            }
        )
    references_by_agent = {
        agent_id: _deduplicate_references(items)
        for agent_id, items in references_by_agent.items()
    }
    all_references = _deduplicate_references(
        [item for references in references_by_agent.values() for item in references]
    )
    evidence_count = len(
        {item["evidence_item_id"] for item in all_references if item["evidence_item_id"]}
    )
    wiki_count = len(
        {
            (item["wiki_entry_id"], item["wiki_version"], item["section"])
            for item in all_references
        }
    )
    data_quality = run.data_quality or {}
    index_count = len({opinion.index_code for opinion in run.opinions})

    for span in spans:
        references = references_by_agent.get(span.agent_id or "", [])
        input_summary: str | None
        output_summary: str | None
        input_digest = span.input_digest
        output_digest = span.output_digest
        if span.agent_id:
            agent_evidence_count = len(
                {
                    item["evidence_item_id"]
                    for item in references
                    if item["evidence_item_id"]
                }
            )
            agent_wiki_count = len(
                {
                    (item["wiki_entry_id"], item["wiki_version"], item["section"])
                    for item in references
                }
            )
            input_summary = (
                f"读取 {index_count} 个标的的冻结输入、Wiki 快照与上游角色结果。"
            )
            output_summary = (
                f"形成 {opinion_counts.get(span.agent_id, 0)} 条角色意见，绑定 "
                f"{agent_wiki_count} 个 Wiki section 与 {agent_evidence_count} 个证据 ID。"
            )
            if input_digest is None:
                input_digest = canonical_digest(
                    {
                        "run_input_hash": run.input_hash,
                        "agent_id": span.agent_id,
                        "workflow_node": span.node_id,
                    }
                )
            if output_digest is None:
                output_digest = canonical_digest(opinion_digests_by_agent.get(span.agent_id, []))
        elif span.node_id == "freeze_snapshot":
            input_summary = "校验运行输入封签、行情日历、证据截止时间与上游快照身份。"
            output_summary = (
                f"冻结 {data_quality.get('evidence_items', evidence_count)} 条证据与 "
                f"{data_quality.get('wiki_entries', wiki_count)} 个 Wiki 条目。"
            )
            input_digest = input_digest or run.input_hash
            output_digest = output_digest or canonical_digest(
                {
                    "evidence_snapshot_hash": data_quality.get("evidence_snapshot_hash"),
                    "market_universe": data_quality.get("market_universe"),
                    "data_cutoff": run.data_cutoff,
                }
            )
        elif span.node_id == "wiki_read":
            references = all_references
            input_summary = "按运行截止时间读取版本化 Wiki，不读取之后发布的内容。"
            output_summary = f"载入 {wiki_count} 个去重 Wiki section，全部保留版本与内容哈希。"
            input_digest = input_digest or run.input_hash
            output_digest = output_digest or canonical_digest(
                [
                    {
                        "wiki_entry_id": item["wiki_entry_id"],
                        "wiki_version": item["wiki_version"],
                        "section": item["section"],
                        "content_hash": item["content_hash"],
                    }
                    for item in all_references
                ]
            )
        elif span.node_id == "evidence_validator":
            references = all_references
            input_summary = f"复核 {len(all_references)} 条 Wiki/证据绑定及其冻结时间。"
            output_summary = (
                f"通过 {data_quality.get('citations_validated', len(all_references))} 条引用，"
                "并完成未来信息检查。"
            )
            input_digest = input_digest or canonical_digest(all_references)
            output_digest = output_digest or canonical_digest(
                {
                    "citations_validated": data_quality.get("citations_validated"),
                    "future_information_check": data_quality.get(
                        "future_information_check"
                    ),
                }
            )
        elif span.node_id == "persist_result":
            input_summary = (
                f"接收 {len(run.opinions)} 条已验证意见与 {len(run.forecasts)} 条最终预测。"
            )
            output_summary = "写入正式预测、Agent 意见、工作流回执与输入封签。"
            input_digest = input_digest or canonical_digest(
                {
                    "opinion_ids": sorted(opinion.id for opinion in run.opinions),
                    "forecast_ids": sorted(forecast.id for forecast in run.forecasts),
                }
            )
            output_digest = output_digest or canonical_digest(
                {
                    "run_id": run.id,
                    "status": run.status,
                    "input_hash": run.input_hash,
                    "completed_at": run.completed_at,
                }
            )
        else:
            input_summary = "输入由已冻结的工作流收据绑定。"
            output_summary = span.summary
        projection.span_overrides[span.span_id] = {
            "tool_name": _PREDICTION_TOOL_NAMES.get(span.node_id, span.name),
            "input_summary": input_summary,
            "output_summary": output_summary,
            "input_digest": input_digest,
            "output_digest": output_digest,
            "references": references,
        }

    node_ids = {span.node_id for span in spans}
    freeze_span = next((span for span in spans if span.node_id == "freeze_snapshot"), None)
    if freeze_span is not None and "wiki_read" not in node_ids:
        projection.synthetic_spans.append(
            _synthetic_span(
                trace_id=trace.id,
                node_id="wiki_read",
                name="读取冻结 Wiki 快照",
                span_kind="validator",
                status=freeze_span.status,
                started_at=freeze_span.started_at,
                completed_at=freeze_span.completed_at,
                duration_ms=freeze_span.duration_ms,
                tool_name=_PREDICTION_TOOL_NAMES["wiki_read"],
                input_summary="按运行截止时间读取版本化 Wiki，不读取之后发布的内容。",
                output_summary=f"载入 {wiki_count} 个去重 Wiki section，全部保留版本与内容哈希。",
                input_digest=run.input_hash,
                output_digest=canonical_digest(
                    [
                        {
                            "wiki_entry_id": item["wiki_entry_id"],
                            "wiki_version": item["wiki_version"],
                            "section": item["section"],
                            "content_hash": item["content_hash"],
                        }
                        for item in all_references
                    ]
                ),
                references=all_references,
                provenance="derived_from_immutable_receipt",
            )
        )
    if run.completed_at is not None and "persist_result" not in node_ids:
        latest_span_end = max(
            (span.completed_at for span in spans if span.completed_at is not None),
            default=run.started_at,
        )
        projection.synthetic_spans.append(
            _synthetic_span(
                trace_id=trace.id,
                node_id="persist_result",
                name="持久化预测与审计回执",
                span_kind="persistence",
                status="completed" if run.status == "completed" else "failed",
                started_at=latest_span_end,
                completed_at=run.completed_at,
                duration_ms=_duration_ms(latest_span_end, run.completed_at),
                tool_name=_PREDICTION_TOOL_NAMES["persist_result"],
                input_summary=(
                    f"接收 {len(run.opinions)} 条已验证意见与 {len(run.forecasts)} 条最终预测。"
                ),
                output_summary="写入正式预测、Agent 意见、工作流回执与输入封签。",
                input_digest=canonical_digest(
                    {
                        "opinion_ids": sorted(opinion.id for opinion in run.opinions),
                        "forecast_ids": sorted(forecast.id for forecast in run.forecasts),
                    }
                ),
                output_digest=canonical_digest(
                    {
                        "run_id": run.id,
                        "status": run.status,
                        "input_hash": run.input_hash,
                        "completed_at": run.completed_at,
                    }
                ),
                references=[],
                provenance="derived_from_immutable_receipt",
            )
        )
    return projection


def _sanitize_references(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for value in values:
        required = {
            "wiki_entry_id": value.get("wiki_entry_id"),
            "wiki_title": value.get("wiki_title"),
            "wiki_version": value.get("wiki_version"),
            "section": value.get("section"),
            "content_hash": value.get("content_hash"),
        }
        if not all(isinstance(item, str) and item for item in required.values()):
            continue
        references.append(
            {
                **required,
                "evidence_item_id": _optional_text(value.get("evidence_item_id"), 160),
                "evidence_content_hash": _optional_hash(
                    value.get("evidence_content_hash")
                ),
                "source_url": _optional_text(value.get("source_url"), 2_000),
                "published_at": value.get("published_at"),
            }
        )
    return references


def _deduplicate_references(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for value in values:
        key = (
            value["wiki_entry_id"],
            value["wiki_version"],
            value["section"],
            value["evidence_item_id"],
        )
        unique[key] = value
    return list(unique.values())


def _synthetic_span(
    *,
    trace_id: str,
    node_id: str,
    name: str,
    span_kind: str,
    status: str,
    started_at: datetime,
    completed_at: datetime | None,
    duration_ms: float | None,
    tool_name: str,
    input_summary: str,
    output_summary: str,
    input_digest: str | None,
    output_digest: str | None,
    references: list[dict[str, Any]],
    provenance: str,
) -> dict[str, Any]:
    return {
        "span_id": hashlib.sha256(f"{trace_id}:{node_id}".encode()).hexdigest()[:16],
        "parent_span_id": None,
        "node_id": node_id,
        "name": name,
        "span_kind": span_kind,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "agent_id": None,
        "agent_version": None,
        "model_name": None,
        "prompt_version": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "estimated_cost_usd": None,
        "input_digest": input_digest,
        "output_digest": output_digest,
        "tool_name": tool_name,
        "input_summary": input_summary,
        "output_summary": output_summary,
        "summary": output_summary,
        "error_code": None,
        "error_summary": None,
        "attributes": {"provenance": provenance},
        "references": references,
    }


def _duration_ms(started_at: datetime, completed_at: datetime) -> float:
    if started_at.tzinfo is None and completed_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=completed_at.tzinfo)
    elif completed_at.tzinfo is None and started_at.tzinfo is not None:
        completed_at = completed_at.replace(tzinfo=started_at.tzinfo)
    return max(0.0, (completed_at - started_at).total_seconds() * 1_000)


def _optional_text(value: Any, maximum: int) -> str | None:
    return value[:maximum] if isinstance(value, str) and value else None


def _optional_hash(value: Any) -> str | None:
    if isinstance(value, str) and len(value) == 64:
        return value
    return None
