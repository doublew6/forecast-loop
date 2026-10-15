"""File-first preparation, finalization, and owner brief for pre-market runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import REPOSITORY_ROOT, Settings
from ..premarket import (
    PREMARKET_HANDOFF_SCHEMA_V1,
    PREMARKET_TIMEZONE,
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
PREMARKET_RECEIPT_SCHEMA_V1 = "forecast-loop.premarket-receipt/v1"


class PremarketServiceError(RuntimeError):
    """Raised when a pre-market file handoff or delivery cannot be trusted."""


class _PremarketReceiptBodyV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["forecast-loop.premarket-receipt/v1"] = (
        PREMARKET_RECEIPT_SCHEMA_V1
    )
    run_id: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    forecast_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("premarket receipt completed_at must be timezone-aware")
        return value


class _PremarketReceiptV1(_PremarketReceiptBodyV1):
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_hash(self) -> _PremarketReceiptV1:
        payload = self.model_dump(mode="json", exclude={"receipt_hash"})
        expected = hashlib.sha256(canonical_json(payload)).hexdigest()
        if self.receipt_hash != expected:
            raise ValueError("premarket receipt_hash mismatch")
        return self


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
    root = _handoff_root(settings)
    job_dir = root / run_id
    if job_dir.exists() or job_dir.is_symlink():
        existing_dir = _validated_job_dir(settings, job_dir)
        existing = _read_model(existing_dir / "input.json", PremarketHandoffV1)
        if (
            existing.prepared_at > prepared_at
            or existing.prepared_at >= snapshot.finalization_deadline
        ):
            raise PremarketServiceError(
                "existing premarket handoff has invalid preparation time"
            )
        expected = build_premarket_handoff(
            run_id=run_id,
            snapshot=snapshot,
            prepared_at=existing.prepared_at,
        )
        if existing.request_hash != expected.request_hash:
            raise PremarketServiceError("existing premarket handoff has different content")
        return existing_dir
    handoff = build_premarket_handoff(
        run_id=run_id,
        snapshot=snapshot,
        prepared_at=prepared_at,
    )
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
    clock: Callable[[], datetime] | None = None,
) -> PremarketForecastV1:
    zone = ZoneInfo(settings.timezone)
    if now is not None and clock is not None:
        raise PremarketServiceError("premarket finalization time is ambiguous")
    with _locked_premarket_job(settings, job_dir) as (resolved, directory_fd):
        handoff = _read_model_at(
            directory_fd,
            "input.json",
            PremarketHandoffV1,
        )
        drafts = _read_model_at(
            directory_fd,
            "drafts.json",
            PremarketDraftBundleV1,
        )
        forecast_exists = _artifact_exists_at(directory_fd, "forecast.json")
        receipt_exists = _artifact_exists_at(directory_fd, "receipt.json")
        if receipt_exists and not forecast_exists:
            raise PremarketServiceError("premarket receipt exists without forecast")
        if handoff.run_id != resolved.name:
            raise PremarketServiceError(
                "premarket job directory does not match run_id"
            )
        accepted_at = _sample_finalization_time(
            zone,
            now=now,
            clock=clock,
        )

        candidate = _finalize_forecast_or_error(
            handoff,
            drafts,
            accepted_at=accepted_at,
        )
        if forecast_exists:
            existing = _read_model_at(
                directory_fd,
                "forecast.json",
                PremarketForecastV1,
            )
            if existing.created_at > accepted_at:
                raise PremarketServiceError(
                    "existing premarket forecast follows the finalization attempt"
                )
            reproduced = _finalize_forecast_or_error(
                handoff,
                drafts,
                accepted_at=existing.created_at,
            )
            if existing.content_hash != reproduced.content_hash:
                raise PremarketServiceError(
                    "existing premarket forecast has different content"
                )
            if receipt_exists:
                _validate_premarket_receipt_at(
                    directory_fd,
                    "receipt.json",
                    existing,
                )
                _require_finalization_window_open(
                    handoff,
                    _sample_finalization_time(zone, now=now, clock=clock),
                )
                return existing
            receipt = _build_premarket_receipt(existing)
            _require_finalization_window_open(
                handoff,
                _sample_finalization_time(zone, now=now, clock=clock),
            )
            _atomic_write_at(
                directory_fd,
                "receipt.json",
                canonical_json(receipt),
                mode=0o400,
            )
            _validate_premarket_receipt_at(
                directory_fd,
                "receipt.json",
                existing,
            )
            _require_finalization_window_open(
                handoff,
                _sample_finalization_time(zone, now=now, clock=clock),
            )
            return existing

        _require_finalization_window_open(
            handoff,
            _sample_finalization_time(zone, now=now, clock=clock),
        )
        _atomic_write_at(
            directory_fd,
            "forecast.json",
            canonical_json(candidate),
            mode=0o400,
        )
        receipt = _build_premarket_receipt(candidate)
        _require_finalization_window_open(
            handoff,
            _sample_finalization_time(zone, now=now, clock=clock),
        )
        _atomic_write_at(
            directory_fd,
            "receipt.json",
            canonical_json(receipt),
            mode=0o400,
        )
        sealed = _read_model_at(
            directory_fd,
            "forecast.json",
            PremarketForecastV1,
        )
        if sealed.content_hash != candidate.content_hash:
            raise PremarketServiceError(
                "written premarket forecast has different content"
            )
        _validate_premarket_receipt_at(
            directory_fd,
            "receipt.json",
            sealed,
        )
        _require_finalization_window_open(
            handoff,
            _sample_finalization_time(zone, now=now, clock=clock),
        )
        return sealed


def load_premarket_forecast(
    settings: Settings,
    *,
    job_dir: Path,
) -> PremarketForecastV1:
    job_dir = _validated_job_dir(settings, job_dir)
    return _read_completed_premarket_forecast(
        job_dir,
        job_name=job_dir.name,
    )


def evaluate_premarket_run(
    settings: Settings,
    *,
    job_dir: Path,
    outcome_path: Path,
    now: datetime | None = None,
) -> PremarketEvaluationV1:
    """Seal one externally collected open-to-open outcome and its score."""

    outcome = _read_model(outcome_path, PremarketOutcomeV1)
    evaluated_at = now or datetime.now(ZoneInfo(settings.timezone))
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise PremarketServiceError("premarket evaluation time must be timezone-aware")
    evaluated_at = evaluated_at.astimezone(ZoneInfo(PREMARKET_TIMEZONE))
    with _locked_premarket_job(settings, job_dir) as (resolved, directory_fd):
        forecast = _read_completed_premarket_forecast_at(
            directory_fd,
            job_name=resolved.name,
        )
        outcome_exists = _artifact_exists_at(directory_fd, "outcome.json")
        evaluation_exists = _artifact_exists_at(directory_fd, "evaluation.json")
        if evaluation_exists and not outcome_exists:
            raise PremarketServiceError("premarket evaluation exists without outcome")

        if outcome_exists:
            sealed_outcome = _read_model_at(
                directory_fd,
                "outcome.json",
                PremarketOutcomeV1,
            )
            if sealed_outcome.content_hash != outcome.content_hash:
                raise PremarketServiceError("existing outcome.json has different content")
        else:
            sealed_outcome = outcome

        if evaluation_exists:
            existing = _read_model_at(
                directory_fd,
                "evaluation.json",
                PremarketEvaluationV1,
            )
            reproduced = _evaluate_premarket_forecast_or_error(
                forecast,
                sealed_outcome,
                evaluated_at=existing.evaluated_at,
            )
            if existing.content_hash != reproduced.content_hash:
                raise PremarketServiceError("premarket evaluation does not reproduce")
            return existing

        evaluation = _evaluate_premarket_forecast_or_error(
            forecast,
            sealed_outcome,
            evaluated_at=evaluated_at,
        )
        if not outcome_exists:
            _atomic_write_at(
                directory_fd,
                "outcome.json",
                canonical_json(sealed_outcome),
                mode=0o400,
            )
        _atomic_write_at(
            directory_fd,
            "evaluation.json",
            canonical_json(evaluation),
            mode=0o400,
        )
        sealed_evaluation = _read_model_at(
            directory_fd,
            "evaluation.json",
            PremarketEvaluationV1,
        )
        if sealed_evaluation.content_hash != evaluation.content_hash:
            raise PremarketServiceError("written premarket evaluation has different content")
        return sealed_evaluation


def load_premarket_history(
    settings: Settings,
    *,
    settled_before: date | None = None,
) -> list[dict[str, Any]]:
    """Load sealed episodes and derive deterministic direction/strategy history.

    Incomplete jobs are ignored. Once an evaluation exists, the complete
    forecast/outcome/evaluation chain must validate and reproduce exactly.
    Strategy returns are gross open-to-open returns with neutral signals in
    cash; they intentionally exclude fees, slippage, and shorting costs.
    """

    episodes: list[tuple[PremarketForecastV1, PremarketEvaluationV1]] = []
    seen_sessions: set[date] = set()
    for job_dir in sorted(_handoff_root(settings).iterdir(), key=lambda path: path.name):
        if job_dir.is_symlink():
            raise PremarketServiceError("premarket history contains a symlink")
        if not job_dir.is_dir():
            continue
        evaluation_path = job_dir / "evaluation.json"
        if not evaluation_path.exists():
            continue
        outcome_path = job_dir / "outcome.json"
        if not outcome_path.exists():
            raise PremarketServiceError("evaluated premarket job is incomplete")
        forecast = _read_completed_premarket_forecast(
            job_dir,
            job_name=job_dir.name,
        )
        outcome = _read_model(outcome_path, PremarketOutcomeV1)
        evaluation = _read_model(evaluation_path, PremarketEvaluationV1)
        reproduced = _evaluate_premarket_forecast_or_error(
            forecast,
            outcome,
            evaluated_at=evaluation.evaluated_at,
        )
        if reproduced.content_hash != evaluation.content_hash:
            raise PremarketServiceError("premarket evaluation does not reproduce")
        if settled_before is not None and forecast.target_session >= settled_before:
            continue
        if forecast.forecast_session in seen_sessions:
            raise PremarketServiceError("duplicate evaluated premarket forecast session")
        seen_sessions.add(forecast.forecast_session)
        episodes.append((forecast, evaluation))

    episodes.sort(key=lambda item: (item[0].forecast_session, item[0].content_hash))
    material: list[bool] = []
    long_only_nav = 1.0
    long_short_nav = 1.0
    history: list[dict[str, Any]] = []
    for forecast, evaluation in episodes:
        direction_correct: bool | None = None
        if evaluation.actual_label != "neutral":
            direction_correct = forecast.direction == evaluation.actual_label
            material.append(direction_correct)
        realized_return = evaluation.realized_return
        long_only_period_return = realized_return if forecast.direction == "up" else 0.0
        long_short_period_return = (
            realized_return
            if forecast.direction == "up"
            else -realized_return
            if forecast.direction == "down"
            else 0.0
        )
        long_only_nav *= 1.0 + long_only_period_return
        long_short_nav *= 1.0 + long_short_period_return
        rolling = material[-20:]
        history.append(
            {
                "forecast_hash": forecast.content_hash,
                "forecast_session": forecast.forecast_session,
                "target_session": forecast.target_session,
                "predicted_direction": forecast.direction,
                "realized_return": realized_return,
                "actual_label": evaluation.actual_label,
                "direction_correct": direction_correct,
                "cumulative_sample_size": len(material),
                "cumulative_hits": sum(material),
                "cumulative_win_rate": sum(material) / len(material) if material else None,
                "rolling_20_win_rate": sum(rolling) / len(rolling) if rolling else None,
                "long_only_period_return": long_only_period_return,
                "long_short_period_return": long_short_period_return,
                "long_only_cumulative_return": long_only_nav - 1.0,
                "long_short_cumulative_return": long_short_nav - 1.0,
            }
        )
    return history


def _build_premarket_receipt(forecast: PremarketForecastV1) -> _PremarketReceiptV1:
    body = _PremarketReceiptBodyV1(
        schema_version=PREMARKET_RECEIPT_SCHEMA_V1,
        run_id=forecast.run_id,
        request_hash=forecast.request_hash,
        snapshot_hash=forecast.snapshot_hash,
        forecast_hash=forecast.content_hash,
        completed_at=forecast.created_at,
    )
    payload = body.model_dump(mode="json")
    receipt_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
    return _PremarketReceiptV1(**payload, receipt_hash=receipt_hash)

def build_premarket_brief(
    settings: Settings,
    *,
    job_dir: Path,
    title: str = DEFAULT_PREMARKET_TITLE,
) -> PremarketBrief:
    job_dir = _validated_job_dir(settings, job_dir)
    handoff = _read_model(job_dir / "input.json", PremarketHandoffV1)
    forecast = load_premarket_forecast(settings, job_dir=job_dir)
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
    history = load_premarket_history(
        settings,
        settled_before=forecast.forecast_session,
    )
    feedback = _brief_feedback_line(history[-1]) if history else None
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
        *([feedback] if feedback else []),
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


def _brief_feedback_line(point: dict[str, Any]) -> str:
    predicted = {"up": "涨", "neutral": "小波动", "down": "跌"}[point["predicted_direction"]]
    verdict = (
        "小波动"
        if point["direction_correct"] is None
        else "命中"
        if point["direction_correct"]
        else "未命中"
    )
    realized = point["realized_return"]
    win_rate = point["cumulative_win_rate"]
    rate = "—" if win_rate is None else f"{win_rate:.0%}"
    return (
        f"反馈：{point['forecast_session']:%m-%d}→{point['target_session']:%m-%d} "
        f"预测{predicted}/实际{realized:+.2%}/{verdict}｜"
        f"胜率 {rate}（{point['cumulative_hits']}/{point['cumulative_sample_size']}）"
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


def _sample_finalization_time(
    zone: ZoneInfo,
    *,
    now: datetime | None,
    clock: Callable[[], datetime] | None,
) -> datetime:
    sampled = clock() if clock is not None else now or datetime.now(zone)
    if sampled.tzinfo is None or sampled.utcoffset() is None:
        raise PremarketServiceError(
            "premarket finalization time must be timezone-aware"
        )
    return sampled.astimezone(zone)


def _require_finalization_window_open(
    handoff: PremarketHandoffV1,
    sampled_at: datetime,
) -> None:
    if sampled_at >= handoff.snapshot.finalization_deadline:
        raise PremarketServiceError("premarket finalization deadline has passed")


def _finalize_forecast_or_error(
    handoff: PremarketHandoffV1,
    drafts: PremarketDraftBundleV1,
    *,
    accepted_at: datetime,
) -> PremarketForecastV1:
    try:
        return finalize_premarket_forecast(
            handoff,
            drafts,
            accepted_at=accepted_at,
        )
    except ValueError as exc:
        raise PremarketServiceError(str(exc)) from exc


def _evaluate_premarket_forecast_or_error(
    forecast: PremarketForecastV1,
    outcome: PremarketOutcomeV1,
    *,
    evaluated_at: datetime,
) -> PremarketEvaluationV1:
    try:
        return evaluate_premarket_forecast(
            forecast,
            outcome,
            evaluated_at=evaluated_at,
        )
    except ValueError as exc:
        raise PremarketServiceError(str(exc)) from exc


def _read_completed_premarket_forecast_at(
    directory_fd: int,
    *,
    job_name: str,
) -> PremarketForecastV1:
    handoff = _read_model_at(
        directory_fd,
        "input.json",
        PremarketHandoffV1,
    )
    drafts = _read_model_at(
        directory_fd,
        "drafts.json",
        PremarketDraftBundleV1,
    )
    forecast = _read_model_at(
        directory_fd,
        "forecast.json",
        PremarketForecastV1,
    )
    _validate_completed_premarket_forecast(
        handoff,
        drafts,
        forecast,
        job_name=job_name,
    )
    _validate_premarket_receipt_at(
        directory_fd,
        "receipt.json",
        forecast,
    )
    return forecast


def _read_completed_premarket_forecast(
    job_dir: Path,
    *,
    job_name: str,
) -> PremarketForecastV1:
    handoff = _read_model(job_dir / "input.json", PremarketHandoffV1)
    drafts = _read_model(job_dir / "drafts.json", PremarketDraftBundleV1)
    forecast = _read_model(job_dir / "forecast.json", PremarketForecastV1)
    _validate_completed_premarket_forecast(
        handoff,
        drafts,
        forecast,
        job_name=job_name,
    )
    _validate_premarket_receipt(job_dir / "receipt.json", forecast)
    return forecast


def _validate_completed_premarket_forecast(
    handoff: PremarketHandoffV1,
    drafts: PremarketDraftBundleV1,
    forecast: PremarketForecastV1,
    *,
    job_name: str,
) -> None:
    if handoff.run_id != job_name or forecast.run_id != job_name:
        raise PremarketServiceError("premarket job directory does not match run_id")
    reproduced = _finalize_forecast_or_error(
        handoff,
        drafts,
        accepted_at=forecast.created_at,
    )
    if reproduced.content_hash != forecast.content_hash:
        raise PremarketServiceError(
            "completed premarket forecast has different content"
        )


@contextmanager
def _locked_premarket_job(
    settings: Settings,
    job_dir: Path,
) -> Iterator[tuple[Path, int]]:
    """Open and lock one validated job without following descendant symlinks."""

    configured_input = settings.handoff_root.expanduser()
    requested_input = job_dir.expanduser()
    try:
        configured_before = os.stat(configured_input, follow_symlinks=False)
        requested_before = os.stat(requested_input, follow_symlinks=False)
    except OSError as exc:
        raise PremarketServiceError("premarket job directory is unavailable") from exc
    if (
        not stat.S_ISDIR(configured_before.st_mode)
        or not stat.S_ISDIR(requested_before.st_mode)
    ):
        raise PremarketServiceError("premarket job directory is unavailable")
    try:
        configured_root = configured_input.resolve(strict=True)
        requested_job = requested_input.resolve(strict=True)
        premarket_before = os.stat(
            configured_input / "premarket",
            follow_symlinks=False,
        )
    except (OSError, RuntimeError) as exc:
        raise PremarketServiceError("premarket job directory is unavailable") from exc
    if not stat.S_ISDIR(premarket_before.st_mode):
        raise PremarketServiceError("premarket job directory is unavailable")
    expected_parent = configured_root / "premarket"
    if configured_root == Path("/") or requested_job.parent != expected_parent:
        raise PremarketServiceError(
            "premarket job must be a direct child of the configured handoff root"
        )
    _require_leaf_name(requested_job.name)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    configured_descriptor: int | None = None
    premarket_descriptor: int | None = None
    job_descriptor: int | None = None
    lock_descriptor: int | None = None
    try:
        configured_descriptor = _open_absolute_directory_without_symlinks(
            configured_root,
            flags=directory_flags,
        )
        root_status = os.fstat(configured_descriptor)
        configured_after = os.stat(configured_input, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_status.st_mode)
            or _file_identity(root_status) != _file_identity(configured_before)
            or _file_identity(configured_after) != _file_identity(configured_before)
        ):
            raise PremarketServiceError("premarket handoff root is not a directory")
        premarket_descriptor = os.open(
            "premarket",
            directory_flags,
            dir_fd=configured_descriptor,
        )
        premarket_status = os.fstat(premarket_descriptor)
        premarket_after = os.stat(
            configured_input / "premarket",
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(premarket_status.st_mode)
            or _file_identity(premarket_status) != _file_identity(premarket_before)
            or _file_identity(premarket_after) != _file_identity(premarket_before)
        ):
            raise PremarketServiceError(
                "premarket handoff root is not a directory"
            )
        job_descriptor = os.open(
            requested_job.name,
            directory_flags,
            dir_fd=premarket_descriptor,
        )
        job_status = os.fstat(job_descriptor)
        requested_after = os.stat(requested_input, follow_symlinks=False)
        if (
            not stat.S_ISDIR(job_status.st_mode)
            or _file_identity(job_status) != _file_identity(requested_before)
            or _file_identity(requested_after) != _file_identity(requested_before)
        ):
            raise PremarketServiceError("premarket job is not a directory")
        lock_descriptor = os.open(
            ".finalize.lock",
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=job_descriptor,
        )
        lock_status = os.fstat(lock_descriptor)
        if not stat.S_ISREG(lock_status.st_mode) or lock_status.st_nlink != 1:
            raise PremarketServiceError("premarket finalization lock is invalid")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    except PremarketServiceError:
        for descriptor in (
            lock_descriptor,
            job_descriptor,
            premarket_descriptor,
            configured_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)
        raise
    except OSError as exc:
        for descriptor in (
            lock_descriptor,
            job_descriptor,
            premarket_descriptor,
            configured_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)
        raise PremarketServiceError("premarket job could not be locked safely") from exc

    assert job_descriptor is not None
    try:
        yield requested_job, job_descriptor
    finally:
        assert lock_descriptor is not None
        assert premarket_descriptor is not None
        assert configured_descriptor is not None
        os.close(lock_descriptor)
        os.close(job_descriptor)
        os.close(premarket_descriptor)
        os.close(configured_descriptor)


def _open_absolute_directory_without_symlinks(path: Path, *, flags: int) -> int:
    if not path.is_absolute():
        raise PremarketServiceError("premarket handoff root must be absolute")
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _require_leaf_name(name: str) -> None:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise PremarketServiceError("premarket artifact name is invalid")


def _artifact_exists_at(directory_fd: int, name: str) -> bool:
    _require_leaf_name(name)
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PremarketServiceError(f"unable to inspect {name}") from exc
    return True


def _safe_read_at(directory_fd: int, name: str) -> bytes:
    _require_leaf_name(name)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise PremarketServiceError(
            f"required regular file is missing: {name}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_JSON_BYTES:
            raise PremarketServiceError(f"invalid file size: {name}")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise PremarketServiceError(f"file changed while being read: {name}")
        return raw
    except OSError as exc:
        raise PremarketServiceError(f"unable to read {name}") from exc
    finally:
        os.close(descriptor)


def _read_model_at(directory_fd: int, name: str, model_type):
    raw = _safe_read_at(directory_fd, name)
    try:
        return model_type.model_validate_json(raw)
    except ValueError as exc:
        raise PremarketServiceError(f"invalid {name}") from exc


def _atomic_write_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    _require_leaf_name(name)
    if _artifact_exists_at(directory_fd, name):
        raise PremarketServiceError(f"refusing to overwrite {name}")

    temporary_name: str | None = None
    descriptor: int | None = None
    directory_changed = False
    phase = "allocate"
    primary_error: PremarketServiceError | None = None
    cleanup_error: PremarketServiceError | None = None
    try:
        for _ in range(8):
            candidate = f".{name}.{secrets.token_hex(16)}"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    mode,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            directory_changed = True
            break
        if descriptor is None or temporary_name is None:
            raise PremarketServiceError(
                f"unable to allocate temporary file for {name}"
            )

        phase = "write"
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        phase = "publish"
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise PremarketServiceError(f"refusing to overwrite {name}") from exc
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        os.fsync(directory_fd)
        directory_changed = False
    except PremarketServiceError as exc:
        primary_error = exc
    except OSError as exc:
        operation = "write" if phase in {"allocate", "write"} else "publish"
        primary_error = PremarketServiceError(f"unable to {operation} {name}")
        primary_error.__cause__ = exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = PremarketServiceError(
                    f"unable to clean up temporary file for {name}"
                )
                cleanup_error.__cause__ = exc
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
                directory_changed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = PremarketServiceError(
                    f"unable to clean up temporary file for {name}"
                )
                cleanup_error.__cause__ = exc
        if directory_changed:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                cleanup_error = PremarketServiceError(
                    f"unable to synchronize artifact directory for {name}"
                )
                cleanup_error.__cause__ = exc
    if primary_error is not None:
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error


def _validate_premarket_receipt_at(
    directory_fd: int,
    name: str,
    forecast: PremarketForecastV1,
) -> None:
    existing = _read_model_at(directory_fd, name, _PremarketReceiptV1)
    expected = _build_premarket_receipt(forecast)
    if existing.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise PremarketServiceError("existing premarket receipt does not match forecast")


def _validate_premarket_receipt(
    path: Path,
    forecast: PremarketForecastV1,
) -> None:
    existing = _read_model(path, _PremarketReceiptV1)
    expected = _build_premarket_receipt(forecast)
    if existing.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise PremarketServiceError("existing premarket receipt does not match forecast")


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
