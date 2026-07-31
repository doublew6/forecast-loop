"""Deterministic, append-only Markdown archives for completed reflections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import REPOSITORY_ROOT
from ..models import (
    EvaluationBatch,
    ForecastDiagnostic,
    LessonProposal,
    ReflectionFinding,
    ReflectionRun,
)

ARTIFACT_SCHEMA_VERSION = "1.0.0"
DEFAULT_REFLECTIONS_ROOT = REPOSITORY_ROOT / "data" / "reflection-archives"
DEFAULT_LESSONS_ROOT = REPOSITORY_ROOT / "data" / "lesson-archives"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReflectionMarkdownError(ValueError):
    """Raised when an immutable reflection archive cannot be published safely."""


@dataclass(frozen=True, slots=True)
class MarkdownArtifact:
    path: Path
    payload_hash: str
    file_hash: str


@dataclass(frozen=True, slots=True)
class ReflectionMarkdownArtifacts:
    reflection: MarkdownArtifact
    lessons: tuple[MarkdownArtifact, ...]


def write_reflection_markdown(
    session: Session,
    reflection: ReflectionRun | str,
    *,
    reflections_root: Path | None = None,
    lessons_root: Path | None = None,
) -> ReflectionMarkdownArtifacts:
    """Publish immutable Markdown for one finalized live reflection.

    This is intentionally a post-finalize function: it reads persisted,
    deterministically validated rows and does not update the database or Wiki.
    Publishing the same bytes is idempotent; an existing different artifact is
    an append-only conflict and is never overwritten.
    """

    reflection_id = reflection.id if isinstance(reflection, ReflectionRun) else reflection
    _validate_component(reflection_id, label="reflection ID")
    row = _load_reflection(session, reflection_id)
    _validate_reflection(row)

    reflection_directory = _prepare_root(
        reflections_root or DEFAULT_REFLECTIONS_ROOT,
        label="reflections root",
    )
    lesson_directory = _prepare_root(
        lessons_root or DEFAULT_LESSONS_ROOT,
        label="lessons root",
    )
    reflection_filename = (
        f"{row.target_date.isoformat()}-{row.horizon}-{row.id}.md"
    )
    reflection_path = _child_path(reflection_directory, reflection_filename)
    reflection_reference = Path(
        os.path.relpath(reflection_path, start=lesson_directory)
    ).as_posix()

    reflection_payload = _reflection_payload(row)
    reflection_payload_hash = _canonical_hash(reflection_payload)
    reflection_bytes = _render_reflection(
        reflection_payload,
        payload_hash=reflection_payload_hash,
    ).encode("utf-8")

    lesson_publications: list[tuple[Path, bytes, str]] = []
    for lesson in sorted(row.lesson_proposals, key=lambda item: item.id):
        _validate_component(lesson.id, label="lesson ID")
        lesson_path = _child_path(lesson_directory, f"{lesson.id}.md")
        lesson_payload = _lesson_payload(
            row,
            lesson,
            reflection_file=reflection_reference,
        )
        lesson_payload_hash = _canonical_hash(lesson_payload)
        lesson_bytes = _render_lesson(
            lesson_payload,
            payload_hash=lesson_payload_hash,
        ).encode("utf-8")
        lesson_publications.append((lesson_path, lesson_bytes, lesson_payload_hash))

    publications = [
        (reflection_path, reflection_bytes, reflection_payload_hash),
        *lesson_publications,
    ]
    for path, content, _ in publications:
        _assert_publishable(path, content)
    for path, content, _ in publications:
        _write_once(path, content)

    reflection_artifact = MarkdownArtifact(
        path=reflection_path,
        payload_hash=reflection_payload_hash,
        file_hash=_sha256(reflection_bytes),
    )
    lesson_artifacts = tuple(
        MarkdownArtifact(
            path=path,
            payload_hash=payload_hash,
            file_hash=_sha256(content),
        )
        for path, content, payload_hash in lesson_publications
    )
    return ReflectionMarkdownArtifacts(
        reflection=reflection_artifact,
        lessons=lesson_artifacts,
    )


def _load_reflection(session: Session, reflection_id: str) -> ReflectionRun:
    row = session.scalar(
        select(ReflectionRun)
        .options(
            selectinload(ReflectionRun.source_run),
            selectinload(ReflectionRun.findings),
            selectinload(ReflectionRun.lesson_proposals),
            selectinload(ReflectionRun.source_batch).selectinload(
                EvaluationBatch.market_snapshots
            ),
            selectinload(ReflectionRun.source_batch)
            .selectinload(EvaluationBatch.diagnostics)
            .selectinload(ForecastDiagnostic.forecast),
            selectinload(ReflectionRun.source_batch)
            .selectinload(EvaluationBatch.diagnostics)
            .selectinload(ForecastDiagnostic.evaluation),
        )
        .where(ReflectionRun.id == reflection_id)
    )
    if row is None:
        raise ReflectionMarkdownError("reflection was not found")
    return row


def _validate_reflection(row: ReflectionRun) -> None:
    if row.source_run.mode != "live" or row.source_run.status != "completed":
        raise ReflectionMarkdownError(
            "Markdown archives require a completed live prediction run"
        )
    if row.status != "completed" or row.completed_at is None:
        raise ReflectionMarkdownError("reflection must be completed before archiving")
    if row.source_batch.status != "completed":
        raise ReflectionMarkdownError("reflection evaluation batch is not completed")
    if row.horizon not in {"D1", "D2"}:
        raise ReflectionMarkdownError("reflection horizon must be D1 or D2")
    if row.source_batch.horizon != row.horizon:
        raise ReflectionMarkdownError("reflection and evaluation batch horizons differ")
    if row.source_batch.target_date != row.target_date:
        raise ReflectionMarkdownError("reflection and evaluation target dates differ")
    if row.source_batch.evaluation_set_hash != row.evaluation_set_hash:
        raise ReflectionMarkdownError("reflection evaluation set seal is inconsistent")
    for label, value in (
        ("input_hash", row.input_hash),
        ("source_snapshot_hash", row.source_snapshot_hash),
        ("output_hash", row.output_hash),
        ("receipt_hash", row.receipt_hash),
        ("evaluation_set_hash", row.evaluation_set_hash),
        ("evaluation source_hash", row.source_batch.source_hash),
    ):
        _validate_digest(value, label=label)
    if not row.source_batch.diagnostics:
        raise ReflectionMarkdownError("completed reflection has no forecast diagnostics")
    if not row.findings:
        raise ReflectionMarkdownError("completed reflection has no findings")

    finding_ids = {item.id for item in row.findings}
    for finding in row.findings:
        if finding.reflection_run_id != row.id or finding.horizon != row.horizon:
            raise ReflectionMarkdownError("finding identity does not match reflection")
    for lesson in row.lesson_proposals:
        if lesson.reflection_run_id != row.id:
            raise ReflectionMarkdownError("lesson identity does not match reflection")
        if not set(lesson.evidence_finding_ids).issubset(finding_ids):
            raise ReflectionMarkdownError("lesson cites a finding outside its reflection")
    for diagnostic in row.source_batch.diagnostics:
        if diagnostic.forecast.run_id != row.source_run_id:
            raise ReflectionMarkdownError("diagnostic belongs to another prediction run")
        if diagnostic.forecast.horizon != row.horizon:
            raise ReflectionMarkdownError("diagnostic horizon does not match reflection")
        if diagnostic.forecast.target_date != row.target_date:
            raise ReflectionMarkdownError("diagnostic target date does not match reflection")
    for snapshot in row.source_batch.market_snapshots:
        _validate_digest(snapshot.source_hash, label="market source_hash")
        _validate_digest(snapshot.content_hash, label="market content_hash")


def _reflection_payload(row: ReflectionRun) -> dict[str, Any]:
    snapshots = sorted(
        row.source_batch.market_snapshots,
        key=lambda item: item.index_code,
    )
    diagnostics = sorted(
        row.source_batch.diagnostics,
        key=lambda item: (item.forecast.index_code, item.id),
    )
    findings = sorted(
        row.findings,
        key=lambda item: (
            item.scope_type,
            item.index_code or "",
            item.subject_id,
            item.id,
        ),
    )
    lessons = sorted(row.lesson_proposals, key=lambda item: item.id)
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "reflection": {
            "id": row.id,
            "status": row.status,
            "schema_version": row.schema_version,
            "target_date": row.target_date.isoformat(),
            "horizon": row.horizon,
            "created_at": _iso(row.created_at),
            "completed_at": _iso(row.completed_at),
            "supersedes_id": row.supersedes_id,
        },
        "source": {
            "mode": row.source_run.mode,
            "run_id": row.source_run_id,
            "batch_id": row.source_batch_id,
            "prediction_data_cutoff": _iso(row.source_run.data_cutoff),
            "prediction_input_hash": row.source_run.input_hash,
            "reflection_input_hash": row.input_hash,
            "evaluation_set_hash": row.evaluation_set_hash,
            "evaluation_source_hash": row.source_batch.source_hash,
            "source_snapshot_hash": row.source_snapshot_hash,
            "output_hash": row.output_hash,
            "receipt_hash": row.receipt_hash,
            "data_quality": row.source_batch.data_quality or {},
        },
        "market_snapshots": [
            {
                "id": item.id,
                "index_code": item.index_code,
                "index_name": item.index_name,
                "target_date": item.target_date.isoformat(),
                "base_trade_date": item.base_trade_date.isoformat(),
                "base_close": item.base_close,
                "target_close": item.target_close,
                "actual_return": item.actual_return,
                "amount": item.amount,
                "advancers": item.advancers,
                "decliners": item.decliners,
                "unchanged": item.unchanged,
                "limit_down_count": item.limit_down_count,
                "breadth_down_ratio": item.breadth_down_ratio,
                "sector_contributions": item.sector_contributions or [],
                "weight_contributions": item.weight_contributions or [],
                "historical_abs_return_percentile": (
                    item.historical_abs_return_percentile
                ),
                "history_sample_size": item.history_sample_size,
                "source_url": item.source_url,
                "source_hash": item.source_hash,
                "captured_at": _iso(item.captured_at),
                "content_hash": item.content_hash,
            }
            for item in snapshots
        ],
        "diagnostics": [
            {
                "id": item.id,
                "forecast_id": item.forecast_id,
                "evaluation_result_id": item.evaluation_result_id,
                "index_code": item.forecast.index_code,
                "index_name": item.forecast.index_name,
                "predicted_direction": item.forecast.direction,
                "probabilities": {
                    "up": item.forecast.probability_up,
                    "neutral": item.forecast.probability_neutral,
                    "down": item.forecast.probability_down,
                },
                "threshold": item.forecast.threshold,
                "actual_return": item.evaluation.actual_return,
                "actual_label": item.evaluation.actual_label,
                "observation_hash": item.evaluation.observation_hash,
                "signed_sigma": item.signed_sigma,
                "severity": item.severity,
                "systemic_extreme_down": item.systemic_extreme_down,
                "historical_abs_return_percentile": (
                    item.historical_abs_return_percentile
                ),
                "history_sample_size": item.history_sample_size,
                "data_incomplete": item.data_incomplete,
                "sign_correct": item.sign_correct,
                "material_direction_correct": item.material_direction_correct,
                "brier_score": item.brier_score,
                "policy_version": item.policy_version,
                "created_at": _iso(item.created_at),
            }
            for item in diagnostics
        ],
        "findings": [_finding_payload(item) for item in findings],
        "lessons": [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status,
                "proposal_type": item.proposal_type,
                "episode_key": item.episode_key,
                "cluster_key": item.cluster_key,
            }
            for item in lessons
        ],
    }


def _lesson_payload(
    row: ReflectionRun,
    lesson: LessonProposal,
    *,
    reflection_file: str,
) -> dict[str, Any]:
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "lesson": {
            "id": lesson.id,
            "title": lesson.title,
            "summary": lesson.summary,
            "status": lesson.status,
            "proposal_type": lesson.proposal_type,
            "episode_key": lesson.episode_key,
            "cluster_key": lesson.cluster_key,
            "evidence_finding_ids": sorted(lesson.evidence_finding_ids),
            "independent_episode_count": lesson.independent_episode_count,
            "replay_target_dates": lesson.replay_target_dates,
            "replay_metrics": lesson.replay_metrics or {},
            "half_life_sessions": lesson.half_life_sessions,
            "created_at": _iso(lesson.created_at),
            "reviewed_at": _iso(lesson.reviewed_at),
            "supersedes_id": lesson.supersedes_id,
        },
        "source": {
            "reflection_id": row.id,
            "reflection_file": reflection_file,
            "source_run_id": row.source_run_id,
            "source_batch_id": row.source_batch_id,
            "target_date": row.target_date.isoformat(),
            "horizon": row.horizon,
            "evaluation_set_hash": row.evaluation_set_hash,
            "source_snapshot_hash": row.source_snapshot_hash,
            "output_hash": row.output_hash,
            "receipt_hash": row.receipt_hash,
        },
    }


def _finding_payload(item: ReflectionFinding) -> dict[str, Any]:
    return {
        "id": item.id,
        "scope_type": item.scope_type,
        "subject_id": item.subject_id,
        "index_code": item.index_code,
        "horizon": item.horizon,
        "verdict": item.verdict,
        "primary_error_type": item.primary_error_type,
        "secondary_error_types": item.secondary_error_types or [],
        "evidence_ids": item.evidence_ids or [],
        "availability_class": item.availability_class,
        "causal_status": item.causal_status,
        "counterfactual": item.counterfactual or {},
        "remediation": item.remediation or [],
        "confidence": item.confidence,
        "summary": item.summary,
        "created_at": _iso(item.created_at),
    }


def _render_reflection(payload: dict[str, Any], *, payload_hash: str) -> str:
    reflection = payload["reflection"]
    source = payload["source"]
    snapshots = payload["market_snapshots"]
    diagnostics = payload["diagnostics"]
    findings = payload["findings"]
    lessons = payload["lessons"]
    snapshot_hashes = {
        item["index_code"]: item["content_hash"] for item in snapshots
    }
    overall_severity = _overall_severity(diagnostics)
    metadata = {
        "artifact_type": "vericouncil_reflection",
        "artifact_schema_version": payload["artifact_schema_version"],
        "artifact_payload_sha256": payload_hash,
        "id": reflection["id"],
        "status": reflection["status"],
        "target_date": reflection["target_date"],
        "horizon": reflection["horizon"],
        "source_mode": source["mode"],
        "source_run_id": source["run_id"],
        "source_batch_id": source["batch_id"],
        "evaluation_set_hash": source["evaluation_set_hash"],
        "source_snapshot_hash": source["source_snapshot_hash"],
        "output_hash": source["output_hash"],
        "receipt_hash": source["receipt_hash"],
        "market_snapshot_hashes": snapshot_hashes,
        "supersedes_id": reflection["supersedes_id"],
    }
    lines = [
        _frontmatter(metadata),
        f"# 每日反省：{reflection['target_date']} {reflection['horizon']}",
        "",
        "> 这是已完成 Live 预测的事后审计归档，不属于预测 Wiki，"
        "也不会反向改写历史预测。",
        "",
        "## 归档状态",
        "",
        f"- Reflection：`{_md(reflection['id'])}`",
        f"- 状态：`{_md(reflection['status'])}`",
        f"- 总体严重度：`{_md(overall_severity)}`",
        f"- 原预测截止：`{_md(source['prediction_data_cutoff'])}`",
        f"- 完成时间：`{_md(reflection['completed_at'])}`",
        f"- 评价集哈希：`{_md(source['evaluation_set_hash'])}`",
        f"- 来源快照哈希：`{_md(source['source_snapshot_hash'])}`",
        f"- 不可变回执哈希：`{_md(source['receipt_hash'])}`",
        "",
        "## 预测与实际行情",
        "",
        "| 指数 | 预测 | 实际收益 | 严重度 | z | 符号命中 | 重大方向命中 | Brier | 数据完整 |",
        "| --- | --- | ---: | --- | ---: | --- | --- | ---: | --- |",
    ]
    for item in diagnostics:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(f"{item['index_name']} ({item['index_code']})"),
                    _md(item["predicted_direction"]),
                    _percent(item["actual_return"]),
                    _md(
                        "systemic_extreme_down"
                        if item["systemic_extreme_down"]
                        else item["severity"]
                    ),
                    _number(item["signed_sigma"], 3),
                    _optional_bool(item["sign_correct"]),
                    _optional_bool(item["material_direction_correct"]),
                    _number(item["brier_score"], 4),
                    "否" if item["data_incomplete"] else "是",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 冻结行情来源",
            "",
            "| 指数 | 基准/目标收盘 | 市场宽度 | 来源 | 来源哈希 | 内容哈希 |",
            "| --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for item in snapshots:
        source_link = (
            f"[{_md(item['source_url'])}]({_markdown_url(item['source_url'])})"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(item["index_code"]),
                    _md(f"{item['base_close']} / {item['target_close']}"),
                    _percent(item["breadth_down_ratio"]),
                    source_link,
                    f"`{item['source_hash']}`",
                    f"`{item['content_hash']}`",
                ]
            )
            + " |"
        )
    lines.extend(["", "## 反省发现", ""])
    for item in findings:
        subject = f"{item['scope_type']} / {item['subject_id']}"
        if item["index_code"]:
            subject += f" / {item['index_code']}"
        lines.extend(
            [
                f"### {_md(subject)}",
                "",
                f"- 结论：`{_md(item['verdict'])}`",
                f"- 主要错误类型：`{_md(item['primary_error_type'])}`",
                f"- 信息时间分类：`{_md(item['availability_class'])}`",
                f"- 因果可信等级：`{_md(item['causal_status'])}`",
                f"- 置信度：`{_number(item['confidence'], 3)}`",
                f"- 证据 ID：{_inline_code_list(item['evidence_ids'])}",
                "",
                _md(item["summary"]),
                "",
            ]
        )
        if item["remediation"]:
            lines.append("整改建议：")
            lines.append("")
            lines.extend(f"- {_md(value)}" for value in item["remediation"])
            lines.append("")
        lines.extend(
            [
                "反事实敏感度：",
                "",
                "```json",
                _json_block(item["counterfactual"]),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Lesson 候选",
            "",
            "| ID | 标题 | 类型 | 状态 | 独立事件 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in lessons:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"[`{_md(item['id'])}`](../lessons/{item['id']}.md)",
                    _md(item["title"]),
                    _md(item["proposal_type"]),
                    _md(item["status"]),
                    _md(item["episode_key"]),
                ]
            )
            + " |"
        )
    if not lessons:
        lines.append("| — | 本次未生成经验候选 | — | — | — |")
    lines.extend(
        [
            "",
            "## 完整性封印",
            "",
            f"- 归档负载 SHA-256：`{payload_hash}`",
            f"- 反省输出 SHA-256：`{source['output_hash']}`",
            f"- 反省回执 SHA-256：`{source['receipt_hash']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _render_lesson(payload: dict[str, Any], *, payload_hash: str) -> str:
    lesson = payload["lesson"]
    source = payload["source"]
    metadata = {
        "artifact_type": "vericouncil_lesson_proposal",
        "artifact_schema_version": payload["artifact_schema_version"],
        "artifact_payload_sha256": payload_hash,
        "id": lesson["id"],
        "title": lesson["title"],
        "status": lesson["status"],
        "proposal_type": lesson["proposal_type"],
        "episode_key": lesson["episode_key"],
        "cluster_key": lesson["cluster_key"],
        "source_reflection_id": source["reflection_id"],
        "source_run_id": source["source_run_id"],
        "source_snapshot_hash": source["source_snapshot_hash"],
        "receipt_hash": source["receipt_hash"],
        "supersedes_id": lesson["supersedes_id"],
    }
    lines = [
        _frontmatter(metadata),
        f"# {_md(lesson['title'])}",
        "",
        "> 这是待人工审核的经验候选，不是正式 Wiki 条目，"
        "不能自动调整预测权重。",
        "",
        "## 候选内容",
        "",
        _md(lesson["summary"]),
        "",
        "## 验证状态",
        "",
        f"- 状态：`{_md(lesson['status'])}`",
        f"- 类型：`{_md(lesson['proposal_type'])}`",
        f"- 独立市场事件数：`{lesson['independent_episode_count']}`",
        f"- 重放目标日期数：`{lesson['replay_target_dates']}`",
        f"- 默认半衰期：`{lesson['half_life_sessions']}` 个交易日",
        f"- Episode：`{_md(lesson['episode_key'])}`",
        f"- Cluster：`{_md(lesson['cluster_key'])}`",
        "",
        "重放指标：",
        "",
        "```json",
        _json_block(lesson["replay_metrics"]),
        "```",
        "",
        "## 来源与证据",
        "",
        f"- 反省案例：[{_md(source['reflection_id'])}]"
        f"({_markdown_url(source['reflection_file'])})",
        f"- 目标交易日 / 周期：`{source['target_date']} / {source['horizon']}`",
        f"- Evidence findings：{_inline_code_list(lesson['evidence_finding_ids'])}",
        f"- 评价集 SHA-256：`{source['evaluation_set_hash']}`",
        f"- 来源快照 SHA-256：`{source['source_snapshot_hash']}`",
        f"- 回执 SHA-256：`{source['receipt_hash']}`",
        f"- 候选负载 SHA-256：`{payload_hash}`",
        "",
        "## 晋升约束",
        "",
        "- 正式 Wiki 晋升必须人工审核，并新增 Wiki 版本、更新索引和维护日志。",
        "- 单次极端事件只能直接提出数据门禁、风险检查表或失效条件，"
        "不能直接形成方向权重规律。",
        "- 普通方向规律须满足独立事件、跨日期重放和校准改善门槛。",
        "",
    ]
    return "\n".join(lines)


def _frontmatter(values: dict[str, Any]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {_yaml_scalar(value)}" for key, value in values.items())
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _overall_severity(diagnostics: list[dict[str, Any]]) -> str:
    if any(item["systemic_extreme_down"] for item in diagnostics):
        return "systemic_extreme_down"
    priority = {"noise": 0, "directional": 1, "large": 2, "extreme": 3}
    return max(diagnostics, key=lambda item: priority[item["severity"]])["severity"]


def _prepare_root(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ReflectionMarkdownError(f"{label} may not be a symlink")
    try:
        expanded.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReflectionMarkdownError(f"cannot create {label}: {exc}") from exc
    if expanded.is_symlink() or not expanded.is_dir():
        raise ReflectionMarkdownError(f"{label} must be a real directory")
    return expanded.resolve(strict=True)


def _child_path(root: Path, filename: str) -> Path:
    _validate_component(filename, label="artifact filename")
    path = root / filename
    try:
        if path.parent.resolve(strict=True) != root:
            raise ReflectionMarkdownError("artifact path escapes its configured root")
    except OSError as exc:
        raise ReflectionMarkdownError("artifact parent cannot be resolved") from exc
    return path


def _validate_component(value: str, *, label: str) -> None:
    if not _SAFE_COMPONENT.fullmatch(value):
        raise ReflectionMarkdownError(f"{label} is not a safe path component")


def _validate_digest(value: str | None, *, label: str) -> None:
    if value is None or not _SHA256.fullmatch(value):
        raise ReflectionMarkdownError(f"{label} is not a canonical SHA-256 digest")


def _assert_publishable(path: Path, content: bytes) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ReflectionMarkdownError(
            f"artifact destination cannot be opened safely: {path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReflectionMarkdownError(
                f"artifact destination is not a regular file: {path}"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            existing = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if existing != content:
        raise ReflectionMarkdownError(
            f"immutable artifact already exists with different content: {path}"
        )


def _write_once(path: Path, content: bytes) -> None:
    if path.exists():
        _assert_publishable(path, content)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".vericouncil-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o444)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            _assert_publishable(path, content)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(raw)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _md(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _markdown_url(value: str) -> str:
    return str(value).replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def _number(value: float | None, places: int) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _optional_bool(value: bool | None) -> str:
    if value is None:
        return "—"
    return "是" if value else "否"


def _inline_code_list(values: list[str]) -> str:
    if not values:
        return "—"
    return "、".join(f"`{_md(value)}`" for value in values)


def _json_block(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).replace("`", "\\u0060")
