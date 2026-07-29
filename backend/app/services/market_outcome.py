"""Import trusted market-outcome snapshots into immutable live evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..db import Database
from ..domain import INDEXES, Horizon
from ..market_universe import DEFAULT_MARKET_UNIVERSE
from ..models import EvaluationBatch, Forecast, WorkflowRun
from .evaluation import evaluate_forecast
from .reflection_handoff import MarketSnapshotBundleInput
from .schema_readiness import require_schema_current
from .snapshot import validate_trusted_source_url

MAX_SNAPSHOT_BYTES = 25 * 1024 * 1024
REQUIRED_QUALITY_CHECKS = frozenset(
    {
        "quality_policy_passed",
        "target_session_published",
        "target_calendar_open",
        "required_instruments_complete",
        "outcome_metrics_complete",
        "publication_freshness_passed",
    }
)
@dataclass(frozen=True, slots=True)
class MarketImportResult:
    status: str
    target_date: date
    horizon: str
    source_run_ids: tuple[str, ...]
    evaluated_forecasts: int
    existing_forecasts: int
    snapshot_hash: str


def import_market_snapshot(
    settings: Settings,
    snapshot_path: Path,
    *,
    now: datetime | None = None,
) -> MarketImportResult:
    """Evaluate every due formal forecast from one sealed five-index bundle."""

    require_schema_current(settings.database_url)
    zone = ZoneInfo(settings.timezone)
    current = _aware(now or datetime.now(zone), zone)
    bundle = load_market_snapshot(
        snapshot_path,
        now=current,
        root=settings.market_snapshot_root,
        timezone=settings.timezone,
    )
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            forecasts = pending_live_forecasts(
                session,
                target_date=bundle.target_date,
                horizon=bundle.horizon.value,
            )
            if not forecasts:
                return MarketImportResult(
                    status="no_due_live_forecast",
                    target_date=bundle.target_date,
                    horizon=bundle.horizon.value,
                    source_run_ids=(),
                    evaluated_forecasts=0,
                    existing_forecasts=0,
                    snapshot_hash=bundle.content_hash,
                )
            _validate_forecast_groups(forecasts)
            item_by_code = {item.index_code: item for item in bundle.items}
            evaluated = 0
            existing = 0
            for forecast in forecasts:
                item = item_by_code[forecast.index_code]
                _validate_item_matches_forecast(item, forecast)
                if forecast.evaluation is not None:
                    _validate_existing_evaluation(
                        forecast,
                        item,
                        source_id=bundle.publication.source_id,
                    )
                    existing += 1
                    continue
                evaluate_forecast(
                    session,
                    forecast=forecast,
                    price_source=bundle.publication.source_id,
                    observed_at=bundle.captured_at,
                    start_trade_date=item.base_trade_date,
                    start_close=item.base_close,
                    start_source_url=str(item.base_source_url),
                    start_source_hash=item.base_source_hash,
                    end_trade_date=item.target_date,
                    end_close=item.target_close,
                    end_source_url=str(item.target_source_url),
                    end_source_hash=item.target_source_hash,
                    timezone=settings.timezone,
                    now=current,
                    trusted_sources_only=True,
                )
                evaluated += 1
            session.commit()
            run_ids = tuple(sorted({forecast.run_id for forecast in forecasts}))
            return MarketImportResult(
                status="completed",
                target_date=bundle.target_date,
                horizon=bundle.horizon.value,
                source_run_ids=run_ids,
                evaluated_forecasts=evaluated,
                existing_forecasts=existing,
                snapshot_hash=bundle.content_hash,
            )
    finally:
        database.dispose()


def load_market_snapshot(
    path: Path,
    *,
    now: datetime,
    root: Path | None = None,
    timezone: str = "Asia/Shanghai",
) -> MarketSnapshotBundleInput:
    """Strictly validate the file, canonical seal, upstream gates and timing."""

    resolved = _resolve_snapshot_path(path, root=root)
    raw = _secure_read(resolved)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("market snapshot is not valid JSON") from exc
    bundle = MarketSnapshotBundleInput.model_validate(payload)
    expected_hash = _canonical_hash(
        bundle.model_dump(
            mode="json",
            exclude={"content_hash"},
            exclude_none=True,
        )
    )
    if bundle.content_hash != expected_hash or bundle.content_hash == "0" * 64:
        raise ValueError("market snapshot content_hash is invalid")
    if bundle.data_quality.status != "passed":
        raise ValueError("market snapshot data quality did not pass")
    if bundle.data_quality.source_id != bundle.publication.source_id:
        raise ValueError("market snapshot source IDs are inconsistent")
    missing_checks = sorted(REQUIRED_QUALITY_CHECKS - set(bundle.data_quality.checks))
    if missing_checks:
        raise ValueError(
            "market snapshot omits required quality checks: "
            + ", ".join(missing_checks)
        )
    if not bundle.trading_calendar.is_open:
        raise ValueError("target session is not open in the frozen calendar")
    if bundle.trading_calendar.target_date != bundle.target_date:
        raise ValueError("calendar target does not match the market snapshot")
    validate_trusted_source_url(
        str(bundle.trading_calendar.source_url),
        label="market snapshot calendar",
    )
    if bundle.trading_calendar.source_hash == "0" * 64:
        raise ValueError("market snapshot calendar hash cannot be a placeholder")
    zone = ZoneInfo(timezone)
    current = _aware(now, zone)
    captured_at = _aware(bundle.captured_at, zone)
    if bundle.captured_at > current + timedelta(minutes=5):
        raise ValueError("market snapshot capture time is in the future")
    maturity = datetime.combine(
        bundle.target_date,
        time(15, 5),
        tzinfo=zone,
    )
    if captured_at < maturity:
        raise ValueError("market snapshot was captured before target-session maturity")
    for item in bundle.items:
        validate_trusted_source_url(
            str(item.source_url),
            label=f"{item.index_code} market snapshot",
        )
        if item.source_hash == "0" * 64:
            raise ValueError("market snapshot source hashes cannot be placeholders")
        for label, url, digest in (
            ("base", item.base_source_url, item.base_source_hash),
            ("target", item.target_source_url, item.target_source_hash),
        ):
            validate_trusted_source_url(
                str(url),
                label=f"{item.index_code} {label} close",
            )
            if digest == "0" * 64:
                raise ValueError("price source hashes cannot be placeholders")
        if _aware(item.captured_at, zone) != captured_at:
            raise ValueError("market snapshot item capture time conflicts with bundle")
        if item.base_trade_date >= item.target_date:
            raise ValueError("market snapshot base date must precede target date")
        expected_return = item.target_close / item.base_close - 1.0
        if not math.isclose(
            item.actual_return,
            expected_return,
            rel_tol=0,
            abs_tol=1e-10,
        ):
            raise ValueError("market snapshot actual_return conflicts with closes")
    return bundle


def pending_live_forecasts(
    session: Session,
    *,
    target_date: date,
    horizon: str,
) -> list[Forecast]:
    """Return formal forecasts by frozen target session, including evaluated rows."""

    forecasts = list(
        session.scalars(
            select(Forecast)
            .join(WorkflowRun, WorkflowRun.id == Forecast.run_id)
            .options(
                selectinload(Forecast.run),
                selectinload(Forecast.evaluation),
            )
            .where(
                WorkflowRun.mode == "live",
                WorkflowRun.status == "completed",
                WorkflowRun.market_universe_hash
                == DEFAULT_MARKET_UNIVERSE.content_hash,
                Forecast.target_date == target_date,
                Forecast.horizon == horizon,
                Forecast.index_code.in_(DEFAULT_MARKET_UNIVERSE.codes),
            )
            .order_by(Forecast.run_id, Forecast.index_code)
        ).all()
    )
    return forecasts


def record_blocked_upstream(
    settings: Settings,
    *,
    target_date: date,
    horizon: Horizon | str,
    reason_code: str,
    error: str,
    now: datetime | None = None,
) -> EvaluationBatch | None:
    """Persist an idempotent blocked gate without creating a ReflectionRun."""

    require_schema_current(settings.database_url)
    normalized_horizon = Horizon(horizon).value
    zone = ZoneInfo(settings.timezone)
    current = _aware(now or datetime.now(zone), zone)
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            forecasts = pending_live_forecasts(
                session,
                target_date=target_date,
                horizon=normalized_horizon,
            )
            if not forecasts:
                return None
            evaluation_set_hash = _canonical_hash(
                [
                    {
                        "forecast_id": forecast.id,
                        "run_id": forecast.run_id,
                        "input_hash": forecast.input_hash,
                    }
                    for forecast in forecasts
                ]
            )
            existing = session.scalar(
                select(EvaluationBatch).where(
                    EvaluationBatch.target_date == target_date,
                    EvaluationBatch.horizon == normalized_horizon,
                    EvaluationBatch.evaluation_set_hash == evaluation_set_hash,
                    EvaluationBatch.status == "blocked_upstream",
                )
            )
            if existing is not None:
                return existing
            normalized_error = error.strip()[:4000] or reason_code
            source_hash = _canonical_hash(
                {
                    "target_date": target_date.isoformat(),
                    "horizon": normalized_horizon,
                    "reason_code": reason_code,
                    "error": normalized_error,
                }
            )
            batch = EvaluationBatch(
                id=_uuid(),
                target_date=target_date,
                horizon=normalized_horizon,
                status="blocked_upstream",
                evaluation_set_hash=evaluation_set_hash,
                source_hash=source_hash,
                data_quality={
                    "status": "blocked",
                    "reason_code": reason_code,
                },
                started_at=current,
                completed_at=current,
                error=normalized_error,
            )
            session.add(batch)
            session.commit()
            session.refresh(batch)
            return batch
    finally:
        database.dispose()


def _validate_forecast_groups(forecasts: list[Forecast]) -> None:
    expected_codes = {item.code for item in INDEXES}
    grouped: dict[str, set[str]] = {}
    for forecast in forecasts:
        grouped.setdefault(forecast.run_id, set()).add(forecast.index_code)
    if any(codes != expected_codes for codes in grouped.values()):
        raise ValueError("each due live run must contain all five index forecasts")


def _validate_item_matches_forecast(item: Any, forecast: Forecast) -> None:
    if item.target_date != forecast.target_date:
        raise ValueError("market snapshot target date conflicts with forecast")
    if item.base_trade_date != forecast.base_trade_date:
        raise ValueError("market snapshot base date conflicts with forecast")


def _validate_existing_evaluation(
    forecast: Forecast,
    item: Any,
    *,
    source_id: str,
) -> None:
    evaluation = forecast.evaluation
    if evaluation is None:  # pragma: no cover - guarded by caller
        raise ValueError("forecast evaluation disappeared")
    comparisons = (
        (evaluation.start_trade_date == item.base_trade_date, "base date"),
        (evaluation.end_trade_date == item.target_date, "target date"),
        (
            math.isclose(
                evaluation.start_close,
                item.base_close,
                rel_tol=0,
                abs_tol=1e-10,
            ),
            "base close",
        ),
        (
            math.isclose(
                evaluation.end_close,
                item.target_close,
                rel_tol=0,
                abs_tol=1e-10,
            ),
            "target close",
        ),
        (
            evaluation.start_source_hash == item.base_source_hash,
            "base source hash",
        ),
        (
            evaluation.end_source_hash == item.target_source_hash,
            "target source hash",
        ),
        (
            evaluation.start_source_url == str(item.base_source_url),
            "base source URL",
        ),
        (
            evaluation.end_source_url == str(item.target_source_url),
            "target source URL",
        ),
        (
            evaluation.price_source == source_id,
            "price source",
        ),
    )
    failed = [label for matches, label in comparisons if not matches]
    if failed:
        raise ValueError(
            "existing immutable evaluation conflicts with snapshot: "
            + ", ".join(failed)
        )


def _canonical_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aware(value: datetime, zone: Any) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _uuid() -> str:
    from uuid import uuid4

    return str(uuid4())


def _resolve_snapshot_path(path: Path, *, root: Path | None) -> Path:
    source = path.expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    source = Path(os.path.abspath(source))
    if source.is_symlink():
        raise ValueError("market snapshot may not be a symlink")
    if root is not None:
        configured_root = root.expanduser()
        if configured_root.is_symlink():
            raise ValueError("configured market snapshot root may not be a symlink")
        trusted_root = configured_root.resolve(strict=True)
        try:
            relative = source.relative_to(trusted_root)
        except ValueError as exc:
            raise ValueError("market snapshot escaped the configured root") from exc
        current = trusted_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("market snapshot path may not contain symlinks")
    resolved = source.resolve(strict=True)
    if root is not None and not resolved.is_relative_to(trusted_root):
        raise ValueError("market snapshot escaped the configured root")
    return resolved


def _secure_read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("market snapshot must be a regular file")
        if metadata.st_size <= 0 or metadata.st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("market snapshot size is invalid")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size:
            raise ValueError("market snapshot changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")
