"""File-first preparation, finalization, and owner brief for pre-market runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from ..config import REPOSITORY_ROOT, Settings
from ..premarket import (
    PREMARKET_HANDOFF_SCHEMA_V1,
    PremarketDraftBundleV1,
    PremarketEvaluationV1,
    PremarketEvidenceSnapshotV1,
    PremarketForecastV1,
    PremarketHandoffV1,
    PremarketOutcomeV1,
    build_premarket_handoff,
    canonical_json,
    evaluate_premarket_forecast,
    finalize_premarket_forecast,
)
from .daily_brief_v2 import FeishuOwnerConfig, FeishuOwnerSender

MAX_JSON_BYTES = 32 * 1024 * 1024
DEFAULT_PREMARKET_DELIVERY_ROOT = REPOSITORY_ROOT / "data" / "notification-delivery"
DEFAULT_PREMARKET_TITLE = "forecast-loop｜中证1000盘前预测"


class PremarketServiceError(RuntimeError):
    """Raised when a pre-market file handoff or delivery cannot be trusted."""


@dataclass(frozen=True)
class PremarketBrief:
    forecast_hash: str
    forecast_session: str
    target_session: str
    text: str
    content_hash: str


@dataclass(frozen=True)
class PremarketDeliveryResult:
    status: Literal["sent", "already_sent"]
    forecast_hash: str
    marker: Path


def prepare_premarket_run(
    settings: Settings,
    *,
    snapshot_path: Path,
    now: datetime | None = None,
) -> Path:
    snapshot = _read_model(snapshot_path, PremarketEvidenceSnapshotV1)
    prepared_at = now or datetime.now(ZoneInfo(settings.timezone))
    if prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
        raise PremarketServiceError("premarket prepare time must be timezone-aware")
    prepared_at = prepared_at.astimezone(ZoneInfo(settings.timezone))
    if prepared_at < snapshot.created_at:
        raise PremarketServiceError("premarket prepare cannot precede snapshot creation")
    if prepared_at >= snapshot.finalization_deadline:
        raise PremarketServiceError("premarket prepare deadline has passed")
    run_id = str(
        uuid5(
            NAMESPACE_URL,
            "forecast-loop:premarket:"
            f"{snapshot.forecast_session.isoformat()}:{snapshot.content_hash}",
        )
    )
    handoff = build_premarket_handoff(
        run_id=run_id,
        snapshot=snapshot,
        prepared_at=prepared_at,
    )
    root = _handoff_root(settings)
    job_dir = root / run_id
    if job_dir.exists():
        existing = _read_model(job_dir / "input.json", PremarketHandoffV1)
        if existing.request_hash != handoff.request_hash:
            raise PremarketServiceError("existing premarket handoff has different content")
        return job_dir
    job_dir.mkdir(mode=0o700)
    _atomic_write(job_dir / "input.json", canonical_json(handoff), mode=0o400)
    template = {
        "schema_version": PREMARKET_HANDOFF_SCHEMA_V1,
        "run_id": handoff.run_id,
        "request_hash": handoff.request_hash,
        "generated_at": None,
        "generated_by": {
            "surface": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        },
        "drafts": [
            {
                "assignment_id": item.assignment_id,
                "agent_id": item.agent_id,
                "role": item.role,
                "draft": None,
            }
            for item in handoff.assignments
        ],
    }
    _atomic_write(
        job_dir / "drafts.template.json",
        json.dumps(template, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        mode=0o400,
    )
    _atomic_write(
        job_dir / "INSTRUCTIONS.md",
        _instructions(handoff).encode("utf-8"),
        mode=0o400,
    )
    return job_dir


def finalize_premarket_run(
    settings: Settings,
    *,
    job_dir: Path,
    now: datetime | None = None,
) -> PremarketForecastV1:
    job_dir = _validated_job_dir(settings, job_dir)
    handoff = _read_model(job_dir / "input.json", PremarketHandoffV1)
    drafts = _read_model(job_dir / "drafts.json", PremarketDraftBundleV1)
    accepted_at = now or datetime.now(ZoneInfo(settings.timezone))
    forecast = finalize_premarket_forecast(
        handoff,
        drafts,
        accepted_at=accepted_at,
    )
    destination = job_dir / "forecast.json"
    if destination.exists():
        existing = _read_model(destination, PremarketForecastV1)
        if existing.content_hash != forecast.content_hash:
            raise PremarketServiceError("existing premarket forecast has different content")
        return existing
    _atomic_write(destination, canonical_json(forecast), mode=0o400)
    receipt = {
        "schema_version": "forecast-loop.premarket-receipt/v1",
        "run_id": forecast.run_id,
        "request_hash": forecast.request_hash,
        "snapshot_hash": forecast.snapshot_hash,
        "forecast_hash": forecast.content_hash,
        "completed_at": forecast.created_at.isoformat(),
    }
    receipt["receipt_hash"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    _atomic_write(job_dir / "receipt.json", canonical_json(receipt), mode=0o400)
    return forecast


def load_premarket_forecast(
    settings: Settings,
    *,
    job_dir: Path,
) -> PremarketForecastV1:
    job_dir = _validated_job_dir(settings, job_dir)
    return _read_model(job_dir / "forecast.json", PremarketForecastV1)


def evaluate_premarket_run(
    settings: Settings,
    *,
    job_dir: Path,
    outcome_path: Path,
    now: datetime | None = None,
) -> PremarketEvaluationV1:
    """Seal one externally collected open-to-open outcome and its score."""

    job_dir = _validated_job_dir(settings, job_dir)
    forecast = _read_model(job_dir / "forecast.json", PremarketForecastV1)
    outcome = _read_model(outcome_path, PremarketOutcomeV1)
    evaluated_at = now or datetime.now(ZoneInfo(settings.timezone))
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise PremarketServiceError("premarket evaluation time must be timezone-aware")
    evaluation = evaluate_premarket_forecast(
        forecast,
        outcome,
        evaluated_at=evaluated_at,
    )
    _write_or_match(job_dir / "outcome.json", outcome, PremarketOutcomeV1)
    _write_or_match(
        job_dir / "evaluation.json",
        evaluation,
        PremarketEvaluationV1,
    )
    return evaluation


def _write_or_match(path: Path, value, model_type) -> None:
    if path.exists():
        existing = _read_model(path, model_type)
        if existing.content_hash != value.content_hash:
            raise PremarketServiceError(f"existing {path.name} has different content")
        return
    _atomic_write(path, canonical_json(value), mode=0o400)


def build_premarket_brief(
    settings: Settings,
    *,
    job_dir: Path,
    title: str = DEFAULT_PREMARKET_TITLE,
) -> PremarketBrief:
    job_dir = _validated_job_dir(settings, job_dir)
    handoff = _read_model(job_dir / "input.json", PremarketHandoffV1)
    forecast = _read_model(job_dir / "forecast.json", PremarketForecastV1)
    direction = {"up": "上涨", "neutral": "小幅波动", "down": "下跌"}[forecast.direction]
    risk = {"none": "无", "low": "低", "medium": "中", "high": "高"}[forecast.risk_severity]
    grouped = []
    for category, label in (
        ("global_equity", "外盘"),
        ("fx_rates", "汇率/利率"),
        ("news", "资讯"),
    ):
        items = [item.title for item in handoff.snapshot.items if item.category.value == category][
            :2
        ]
        if items:
            grouped.append(f"{label}：{'；'.join(items)}")
    probabilities = forecast.probabilities
    lines = [
        f"【{title.strip()}】",
        (
            f"窗口：{forecast.forecast_session.isoformat()} 开盘 → "
            f"{forecast.target_session.isoformat()} 开盘（Shadow）"
        ),
        (
            f"判断：{direction}｜上涨 {probabilities.up:.0%}｜"
            f"小幅波动 {probabilities.neutral:.0%}｜下跌 {probabilities.down:.0%}"
        ),
        f"证据截止：{forecast.evidence_cutoff:%H:%M}｜Risk Critic：{risk}",
        *grouped,
        f"策略摘要：{_one_line(forecast.rationale, limit=180)}",
        "仅为研究信号，不构成交易指令；开盘跳空和集合竞价可能使执行价偏离指数开盘价。",
    ]
    text = "\n".join(lines)
    return PremarketBrief(
        forecast_hash=forecast.content_hash,
        forecast_session=forecast.forecast_session.isoformat(),
        target_session=forecast.target_session.isoformat(),
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def publish_premarket_brief(
    brief: PremarketBrief,
    config: FeishuOwnerConfig,
    *,
    state_root: Path = DEFAULT_PREMARKET_DELIVERY_ROOT,
    sender: FeishuOwnerSender | None = None,
) -> PremarketDeliveryResult:
    destination_hash = hashlib.sha256(
        f"{config.receive_id_type}:{config.receive_id}".encode()
    ).hexdigest()[:16]
    marker = (
        state_root.expanduser().resolve()
        / "feishu-owner-premarket"
        / brief.forecast_session
        / f"owner-{destination_hash}.json"
    )
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = marker.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if marker.exists():
            payload = _read_json(marker)
            if payload.get("forecast_hash") != brief.forecast_hash:
                raise PremarketServiceError(
                    "a different premarket forecast was already delivered for this session"
                )
            return PremarketDeliveryResult("already_sent", brief.forecast_hash, marker)
        message_uuid = str(
            uuid5(
                NAMESPACE_URL,
                "forecast-loop:feishu-owner:csi1000-open-to-open-d1:"
                f"{brief.forecast_session}:{destination_hash}",
            )
        )
        active_sender = sender or FeishuOwnerSender(config)
        try:
            active_sender.send(brief, message_uuid=message_uuid)  # type: ignore[arg-type]
        finally:
            if sender is None:
                active_sender.close()
        marker_payload = {
            "schema_version": "forecast-loop.premarket-delivery/v1",
            "forecast_hash": brief.forecast_hash,
            "brief_hash": brief.content_hash,
            "forecast_session": brief.forecast_session,
            "target_session": brief.target_session,
            "message_uuid": message_uuid,
        }
        _atomic_write(marker, canonical_json(marker_payload), mode=0o400)
        return PremarketDeliveryResult("sent", brief.forecast_hash, marker)


def _instructions(handoff: PremarketHandoffV1) -> str:
    return f"""# Premarket open-to-open research drafts

Read only `input.json`. Create only `drafts.json` using the template identities.
Complete the four analysts first, then Strategy from their drafts, then Risk
Critic. Every draft must use its exact frozen Wiki reference and only evidence
IDs listed in `allowed_evidence_item_ids`. Treat repeated `independence_key`
values as revisions of one source, not independent corroboration. Price moves
show market reaction and do not by themselves prove a news cause. Risk Critic
must not cast a direction vote.

The target is `{handoff.program.target_id}`: the official index open on
{handoff.snapshot.forecast_session.isoformat()} to the official index open on
{handoff.snapshot.target_session.isoformat()}. Evidence is frozen at
{handoff.snapshot.evidence_cutoff.isoformat()}. The request hash is
`{handoff.request_hash}`. Python validates identities, timestamps, hashes,
routing, aggregation, publication deadline, and evaluation.
"""


def _handoff_root(settings: Settings) -> Path:
    configured = settings.handoff_root.expanduser()
    if configured.is_symlink():
        raise PremarketServiceError("handoff root may not be a symlink")
    configured.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = configured.resolve(strict=True) / "premarket"
    if root.is_symlink():
        raise PremarketServiceError("premarket handoff root may not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    return root.resolve(strict=True)


def _validated_job_dir(settings: Settings, job_dir: Path) -> Path:
    if job_dir.is_symlink():
        raise PremarketServiceError("premarket job directory may not be a symlink")
    try:
        resolved = job_dir.resolve(strict=True)
    except OSError as exc:
        raise PremarketServiceError("premarket job directory is unavailable") from exc
    if not resolved.is_dir() or resolved.parent != _handoff_root(settings):
        raise PremarketServiceError(
            "premarket job must be a direct child of the configured handoff root"
        )
    return resolved


def _read_model(path: Path, model_type):
    raw = _safe_read(path)
    try:
        return model_type.model_validate_json(raw)
    except ValueError as exc:
        raise PremarketServiceError(f"invalid {path.name}") from exc


def _read_json(path: Path) -> dict[str, object]:
    raw = _safe_read(path)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise PremarketServiceError(f"invalid {path.name}") from exc
    if not isinstance(payload, dict):
        raise PremarketServiceError(f"{path.name} root must be an object")
    return payload


def _safe_read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PremarketServiceError(f"required regular file is missing: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_JSON_BYTES:
            raise PremarketServiceError(f"invalid file size: {path.name}")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ino != before.st_ino
        ):
            raise PremarketServiceError(f"file changed while being read: {path.name}")
        return raw
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    if path.exists() or path.is_symlink():
        raise PremarketServiceError(f"refusing to overwrite {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _one_line(value: str, *, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"
