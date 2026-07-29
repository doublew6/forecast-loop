"""Immutable daily prediction-preparation receipts and read-only status views."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..market_universe import (
    DEFAULT_MARKET_UNIVERSE,
    MarketUniverseError,
    MarketUniverseSpec,
    load_market_universe,
)
from ..models import WorkflowRun
from ..schemas import (
    APIModel,
    PredictionDailyStatusRead,
    PredictionPrepareAttemptRead,
    PredictionStatusResponse,
)

PREDICTION_PREPARE_PROTOCOL_VERSION = "1.1.0"
MAX_RECEIPT_BYTES = 256 * 1024
PREDICTION_COMPLETION_SLA = time(23, 59)
PREPARE_STATUSES = {
    "prepared",
    "already_prepared",
    "no_open_session",
    "blocked_upstream",
    "failed",
}


class PredictionPrepareReceipt(APIModel):
    protocol_version: Literal["1.0.0", "1.1.0"] = (
        PREDICTION_PREPARE_PROTOCOL_VERSION
    )
    attempt_id: str
    base_session: date
    attempted_at: datetime
    status: Literal[
        "prepared",
        "already_prepared",
        "no_open_session",
        "blocked_upstream",
        "failed",
    ]
    run_id: str | None = None
    run_status: str | None = None
    snapshot_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    market_universe_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    error_code: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=500)
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("attempted_at")
    @classmethod
    def attempted_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prediction prepare attempted_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def successful_attempt_has_immutable_bindings(self) -> PredictionPrepareReceipt:
        if self.protocol_version == "1.1.0" and not self.market_universe_hash:
            raise ValueError(
                "prediction prepare receipt v1.1 must bind a market universe"
            )
        if self.protocol_version == "1.0.0" and self.market_universe_hash is not None:
            raise ValueError(
                "legacy prediction prepare receipt v1.0 cannot bind a market universe"
            )
        if self.status in {"prepared", "already_prepared"} and not self.run_id:
            raise ValueError("successful prediction prepare receipt must bind a run_id")
        if self.status == "prepared" and not self.snapshot_hash:
            raise ValueError("prepared prediction receipt must bind a snapshot_hash")
        return self


def write_prediction_prepare_receipt(
    settings: Settings,
    *,
    base_session: date,
    attempted_at: datetime,
    result: dict[str, object],
) -> PredictionPrepareReceipt:
    """Append one hash-sealed attempt without storing private local paths."""

    zone = ZoneInfo(settings.timezone)
    timestamp = _aware(attempted_at, zone)
    if timestamp.date() != base_session:
        raise ValueError(
            "prediction prepare attempted_at date must equal base_session "
            "in the configured timezone"
        )
    status = str(result.get("status", "failed"))
    if status not in PREPARE_STATUSES:
        status = "failed"
    error = str(result.get("error") or result.get("reason") or "").strip()
    error_code, message = _public_error(status, error)
    attempt_id = str(uuid4())
    try:
        universe = load_market_universe(settings.market_universe_path)
    except MarketUniverseError as exc:
        raise ValueError(
            "prediction prepare receipt cannot bind an invalid market universe"
        ) from exc
    unsigned = PredictionPrepareReceipt(
        attempt_id=attempt_id,
        base_session=base_session,
        attempted_at=timestamp,
        status=status,  # type: ignore[arg-type]
        run_id=_optional_text(result.get("run_id")),
        run_status=_optional_text(result.get("run_status")),
        snapshot_hash=_optional_hash(result.get("snapshot_hash")),
        market_universe_hash=universe.content_hash,
        error_code=error_code,
        message=message,
        receipt_hash="0" * 64,
    )
    receipt = unsigned.model_copy(
        update={
            "receipt_hash": _canonical_hash(
                unsigned.model_dump(mode="json", exclude={"receipt_hash"})
            )
        }
    )
    root = _prepare_root(settings.prediction_status_root)
    day_root = root / base_session.isoformat()
    if day_root.is_symlink():
        raise ValueError("prediction status day directory may not be a symlink")
    day_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(day_root, 0o700)
    filename = (
        timestamp.strftime("%Y%m%dT%H%M%S%f%z")
        + f"-{attempt_id}.json"
    )
    _atomic_create(
        day_root / filename,
        _json_bytes(receipt.model_dump(mode="json")),
    )
    return receipt


def load_prediction_prepare_receipts(
    settings: Settings,
    *,
    limit: int = 50,
) -> list[PredictionPrepareReceipt]:
    configured_root = settings.prediction_status_root.expanduser()
    if configured_root.is_symlink():
        raise ValueError("prediction status root may not be a symlink")
    if not configured_root.exists():
        return []
    root = configured_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("prediction status root must be a directory")
    paths = sorted(
        root.glob("????-??-??/*.json"),
        key=lambda item: item.name,
        reverse=True,
    )[: max(1, min(limit, 200))]
    zone = ZoneInfo(settings.timezone)
    receipts = [_read_receipt(path, root=root, zone=zone) for path in paths]
    receipts.sort(key=lambda item: (item.attempted_at, item.attempt_id), reverse=True)
    return receipts


def build_prediction_status(
    settings: Settings,
    session: Session,
    *,
    now: datetime | None = None,
    history_limit: int = 20,
    universe: MarketUniverseSpec | None = None,
) -> PredictionStatusResponse:
    current_universe = universe or load_market_universe(settings.market_universe_path)
    zone = ZoneInfo(settings.timezone)
    current = _aware(now or datetime.now(zone), zone)
    latest_completed = _latest_completed_run(session, universe=current_universe)
    if current_universe.content_hash != DEFAULT_MARKET_UNIVERSE.content_hash:
        return PredictionStatusResponse(
            today=PredictionDailyStatusRead(
                base_session=current.date(),
                state="blocked",
                message=(
                    "内置日预测状态仅支持默认 A 股 Universe；"
                    "当前自定义 Universe 需要独立调度与状态目录。"
                ),
            ),
            latest_completed_run_id=(
                None if latest_completed is None else latest_completed.id
            ),
            latest_completed_as_of=(
                None
                if latest_completed is None
                else _database_time(latest_completed.as_of, zone)
            ),
            latest_completed_data_cutoff=(
                None
                if latest_completed is None
                else _database_time(latest_completed.data_cutoff, zone)
            ),
            history=[],
        )
    receipts = load_prediction_prepare_receipts(settings, limit=200)
    history_receipts: list[PredictionPrepareReceipt] = []
    seen_sessions: set[date] = set()
    for receipt in receipts:
        if not receipt_uses_market_universe(receipt, current_universe):
            continue
        if receipt.base_session in seen_sessions:
            continue
        seen_sessions.add(receipt.base_session)
        history_receipts.append(receipt)
    current_date = current.date()
    today_attempt = next(
        (item for item in history_receipts if item.base_session == current_date),
        None,
    )
    today = _today_status(
        session,
        current=current,
        attempt=today_attempt,
        timezone=settings.timezone,
        universe=current_universe,
    )
    return PredictionStatusResponse(
        today=today,
        latest_completed_run_id=(
            None if latest_completed is None else latest_completed.id
        ),
        latest_completed_as_of=(
            None
            if latest_completed is None
            else _database_time(latest_completed.as_of, zone)
        ),
        latest_completed_data_cutoff=(
            None
            if latest_completed is None
            else _database_time(latest_completed.data_cutoff, zone)
        ),
        history=[
            _attempt_read(
                item,
                session=session,
                current=current,
                timezone=settings.timezone,
                universe=current_universe,
            )
            for item in history_receipts[: max(1, min(history_limit, 200))]
        ],
    )


def _today_status(
    session: Session,
    *,
    current: datetime,
    attempt: PredictionPrepareReceipt | None,
    timezone: str,
    universe: MarketUniverseSpec,
) -> PredictionDailyStatusRead:
    base_session = current.date()
    if attempt is None:
        if current.weekday() >= 5:
            return PredictionDailyStatusRead(
                base_session=base_session,
                state="holiday",
                message="今日为周末，不应生成 A 股正式预测。",
            )
        state = "pending" if current.time() < time(17, 55) else "stale"
        message = (
            "今日确定性准备任务将在配置的时间运行。"
            if state == "pending"
            else "今日确定性准备回执缺失，任务可能未运行。"
        )
        return PredictionDailyStatusRead(
            base_session=base_session,
            state=state,
            message=message,
        )
    return _attempt_status(
        session,
        current=current,
        attempt=attempt,
        timezone=timezone,
        universe=universe,
    )


def _attempt_status(
    session: Session,
    *,
    current: datetime,
    attempt: PredictionPrepareReceipt,
    timezone: str,
    universe: MarketUniverseSpec,
) -> PredictionDailyStatusRead:
    base_session = attempt.base_session
    is_today = base_session == current.date()
    day_label = "今日" if is_today else "该日"
    common = {
        "base_session": base_session,
        "attempted_at": attempt.attempted_at,
        "attempt_status": attempt.status,
        "run_id": attempt.run_id,
        "run_status": attempt.run_status,
    }
    if attempt.status == "no_open_session":
        return PredictionDailyStatusRead(
            **common,
            state="holiday",
            message=(
                _status_message_for_day(attempt.message, is_today=is_today)
                or f"上交所交易日历确认{day_label}休市。"
            ),
        )
    if attempt.status in {"blocked_upstream", "failed"}:
        return PredictionDailyStatusRead(
            **common,
            state="blocked",
            message=(
                _status_message_for_day(attempt.message, is_today=is_today)
                or f"{day_label}预测准备已被阻断。"
            ),
        )
    row = session.get(WorkflowRun, attempt.run_id) if attempt.run_id else None
    if row is None:
        return PredictionDailyStatusRead(
            **common,
            state="blocked",
            message="准备回执绑定的运行不存在于本地审计数据库。",
        )
    if not run_uses_market_universe(row, universe):
        return PredictionDailyStatusRead(
            **common,
            state="blocked",
            message="准备回执绑定的运行不属于当前 Market Universe。",
        )
    common["run_status"] = row.status
    if row.status == "completed":
        return PredictionDailyStatusRead(
            **common,
            state="completed",
            message=f"{day_label} Live 预测已完成并发布。",
        )
    if row.status == "failed":
        return PredictionDailyStatusRead(
            **common,
            state="blocked",
            message=f"{day_label} Live 预测未通过确定性终检。",
        )
    hard_deadline = _handoff_deadline(row, timezone=timezone)
    sla_deadline = datetime.combine(
        base_session,
        PREDICTION_COMPLETION_SLA,
        tzinfo=ZoneInfo(timezone),
    )
    if current > sla_deadline or (
        hard_deadline is not None and current > hard_deadline
    ):
        return PredictionDailyStatusRead(
            **common,
            state="overdue",
            message=f"{day_label} Live 预测未在配置的运行时限内完成。",
        )
    return PredictionDailyStatusRead(
        **common,
        state="awaiting",
        message=f"{day_label} Live 预测正在等待 Codex 草稿与确定性终检。",
    )


def _handoff_deadline(row: WorkflowRun, *, timezone: str) -> datetime | None:
    raw = ((row.data_quality or {}).get("handoff") or {}).get("finalize_deadline")
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(str(raw)), ZoneInfo(timezone))
    except ValueError:
        return None


def _attempt_read(
    receipt: PredictionPrepareReceipt,
    *,
    session: Session,
    current: datetime,
    timezone: str,
    universe: MarketUniverseSpec,
) -> PredictionPrepareAttemptRead:
    status = _attempt_status(
        session,
        current=current,
        attempt=receipt,
        timezone=timezone,
        universe=universe,
    )
    payload = receipt.model_dump(
        mode="json",
        exclude={"market_universe_hash", "protocol_version"},
    )
    payload.update(
        state=status.state,
        message=status.message,
        run_status=status.run_status,
    )
    return PredictionPrepareAttemptRead.model_validate(payload)


def run_uses_market_universe(
    row: WorkflowRun,
    universe: MarketUniverseSpec,
) -> bool:
    """Return whether a run belongs to the process-local market universe.

    The first-class identity column is authoritative. The JSON seal fallback is
    retained only for detached pre-migration fixtures; migration 0011 assigns
    every persisted legacy row to the checked-in default universe.
    """

    identity_hash = getattr(row, "market_universe_hash", None)
    if isinstance(identity_hash, str) and identity_hash:
        return identity_hash == universe.content_hash

    # Defensive compatibility for detached pre-migration fixtures. Migration
    # 0011 backfills all persisted legacy rows to the default identity.
    quality = row.data_quality if isinstance(row.data_quality, dict) else {}
    seal = quality.get("market_universe")
    if isinstance(seal, dict):
        content_hash = seal.get("content_hash")
        return (
            isinstance(content_hash, str)
            and content_hash == universe.content_hash
        )
    return universe.content_hash == DEFAULT_MARKET_UNIVERSE.content_hash


def receipt_uses_market_universe(
    receipt: PredictionPrepareReceipt,
    universe: MarketUniverseSpec,
) -> bool:
    """Apply the same default-only legacy rule to preparation receipts."""

    if receipt.market_universe_hash is None:
        return universe.content_hash == DEFAULT_MARKET_UNIVERSE.content_hash
    return receipt.market_universe_hash == universe.content_hash


def _latest_completed_run(
    session: Session,
    *,
    universe: MarketUniverseSpec,
) -> WorkflowRun | None:
    return session.scalar(
        select(WorkflowRun)
        .where(
            WorkflowRun.mode == "live",
            WorkflowRun.status == "completed",
            WorkflowRun.market_universe_hash == universe.content_hash,
        )
        .order_by(WorkflowRun.as_of.desc(), WorkflowRun.completed_at.desc())
        .limit(1)
    )


def _status_message_for_day(message: str | None, *, is_today: bool) -> str | None:
    if message is None or is_today:
        return message
    return message.replace("今日", "该日")


def _prepare_root(value: Path) -> Path:
    path = value.expanduser()
    if path.is_symlink():
        raise ValueError("prediction status root may not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = path.resolve()
    os.chmod(resolved, 0o700)
    return resolved


def _read_receipt(
    path: Path,
    *,
    root: Path,
    zone: ZoneInfo,
) -> PredictionPrepareReceipt:
    if path.is_symlink():
        raise ValueError("prediction prepare receipt may not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("prediction prepare receipt escaped its configured root")
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_RECEIPT_BYTES:
            raise ValueError("prediction prepare receipt has an invalid file type or size")
        raw = b""
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, before.st_size - len(raw))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
        ):
            raise ValueError("prediction prepare receipt changed while being read")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        receipt = PredictionPrepareReceipt.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid prediction prepare receipt: {path.name}") from exc
    if (
        receipt.protocol_version == "1.0.0"
        and "market_universe_hash" in payload
    ):
        raise ValueError(
            "legacy prediction prepare receipt has a v1.1 universe binding"
        )
    hash_payload = receipt.model_dump(mode="json", exclude={"receipt_hash"})
    if "market_universe_hash" not in payload:
        # Protocol 1.0 receipts written before Universe isolation did not carry
        # this optional field. Preserve their exact historical hash envelope;
        # callers will classify them as default-Universe receipts only.
        hash_payload.pop("market_universe_hash", None)
    expected = _canonical_hash(hash_payload)
    if receipt.receipt_hash != expected:
        raise ValueError(f"prediction prepare receipt hash mismatch: {path.name}")
    if resolved.parent.name != receipt.base_session.isoformat():
        raise ValueError("prediction prepare receipt directory/date mismatch")
    if receipt.attempted_at.astimezone(zone).date() != receipt.base_session:
        raise ValueError(
            "prediction prepare receipt attempted_at/base_session mismatch"
        )
    return receipt


def _atomic_create(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("prediction prepare receipt already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValueError("prediction prepare receipt already exists") from exc
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _aware(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prediction status timestamps must be timezone-aware")
    return value.astimezone(zone)


def _database_time(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _optional_text(value: object) -> str | None:
    normalized = "" if value is None else str(value).strip()
    return normalized or None


def _optional_hash(value: object) -> str | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    if len(normalized) != 64 or any(item not in "0123456789abcdef" for item in normalized):
        return None
    return normalized


def _public_error(status: str, error: str) -> tuple[str | None, str | None]:
    if status == "no_open_session":
        return "sse_session_closed", "上交所交易日历确认今日休市。"
    if status not in {"blocked_upstream", "failed"}:
        return None, None
    lowered = error.lower()
    if "quality" in lowered:
        return "quality_gate_failed", "上游数据质量闸门未通过。"
    if "manifest" in lowered:
        return "manifest_gate_failed", "上游发布清单闸门未通过。"
    if "calendar" in lowered or "session" in lowered:
        return "calendar_gate_failed", "上游交易日历闸门未通过。"
    if "index" in lowered or "partition" in lowered:
        return "index_gate_failed", "上游正式行情发布闸门未通过。"
    if status == "blocked_upstream":
        return "upstream_gate_failed", "上游正式数据尚未就绪。"
    return "prediction_prepare_failed", "每日预测准备在本地执行失败。"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")
