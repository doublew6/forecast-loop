"""Frozen evidence snapshots guard live runs against future-information leakage."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..config import Settings
from ..domain import INDEXES
from ..market_universe import MarketUniverseSpec, load_market_universe
from ..ports import EvidenceSnapshotSource, EvidenceSnapshotSourceError
from ..schemas import (
    EvidenceItem,
    FrozenEvidenceSnapshot,
    MarketDataProvenance,
    TradingCalendarProvenance,
)

DEMO_VOLATILITY = {
    "000300.SH": 0.011,
    "000905.SH": 0.014,
    "000852.SH": 0.017,
    "399006.SZ": 0.018,
    "000688.SH": 0.020,
}

# Live snapshots are an explicit trust boundary. A URL merely being HTTPS is not
# enough: sources must belong to a reviewed first-party/regulatory/market-data
# domain. Subdomains are accepted, lookalike suffixes are not.
TRUSTED_SOURCE_DOMAINS = frozenset(
    {
        "alphavantage.co",
        "cnindex.com.cn",
        "cninfo.com.cn",
        "computeexpresslink.org",
        "csrc.gov.cn",
        "csindex.com.cn",
        "eastmoney.com",
        "federalreserve.gov",
        "gov.cn",
        "hkex.com.hk",
        "hkexnews.hk",
        "micron.com",
        "nasdaq.com",
        "nist.gov",
        "nvidia.com",
        "nyse.com",
        "pbc.gov.cn",
        "reuters.com",
        "safe.gov.cn",
        "samsung.com",
        "sec.gov",
        "sina.com.cn",
        "skhynix.com",
        "sse.com.cn",
        "stats.gov.cn",
        "stooq.com",
        "szse.cn",
        "tsmc.com",
        "yahoo.com",
    }
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class LiveEvidenceRequiredError(RuntimeError):
    pass


def load_evidence_snapshot(
    settings: Settings,
    *,
    as_of: datetime,
    source: EvidenceSnapshotSource | None = None,
    universe: MarketUniverseSpec | None = None,
) -> FrozenEvidenceSnapshot:
    resolved_universe = universe or load_market_universe(settings.market_universe_path)
    if settings.use_demo_provider:
        return _demo_snapshot(settings, as_of, universe=resolved_universe)
    if source is None and settings.evidence_snapshot_path is None:
        raise LiveEvidenceRequiredError(
            "Live mode is blocked: set VERICOUNCIL_EVIDENCE_SNAPSHOT_PATH to a frozen JSON "
            "snapshot containing per-index volatility and timestamped evidence."
        )
    if source is None:
        from ..adapters import LocalJsonEvidenceSnapshotSource

        configured_path = settings.evidence_snapshot_path
        if configured_path is None:  # pragma: no cover - guarded above
            raise LiveEvidenceRequiredError("Live evidence snapshot path is missing")
        path = Path(configured_path)
        source = LocalJsonEvidenceSnapshotSource(
            root=path.parent,
            snapshot_path=Path(path.name),
            instrument_codes=resolved_universe.codes,
        )
    try:
        snapshot = source.load_snapshot(as_of=as_of)
        validate_live_snapshot(
            snapshot,
            as_of=as_of,
            instrument_codes=resolved_universe.codes,
        )
        validate_snapshot_content_hash(snapshot)
        return snapshot
    except EvidenceSnapshotSourceError as exc:
        raise LiveEvidenceRequiredError(f"Invalid live evidence snapshot: {exc}") from exc


def canonical_hash(value: Any) -> str:
    """Hash JSON using the single canonical representation used by snapshots."""

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def evidence_item_hash(item: EvidenceItem) -> str:
    return canonical_hash(item.model_dump(mode="json", exclude={"content_hash"}))


def validate_snapshot_content_hash(snapshot: FrozenEvidenceSnapshot) -> None:
    """Verify the outer seal for demo and live snapshots alike."""

    canonical = snapshot.model_dump(mode="json", exclude={"content_hash"})
    if snapshot.content_hash != canonical_hash(canonical):
        raise LiveEvidenceRequiredError(
            "Evidence snapshot content_hash does not match payload"
        )


def validate_trusted_source_url(url: str, *, label: str = "source") -> None:
    """Validate a live provenance URL against the reviewed domain allowlist."""

    _validate_source_url(url, label=label)


def validate_live_snapshot(
    snapshot: FrozenEvidenceSnapshot,
    *,
    as_of: datetime,
    instrument_codes: Iterable[str] | None = None,
) -> None:
    """Validate freshness, provenance and hashes at the live-run trust boundary."""

    _require_aware("requested as_of", as_of)
    for label, timestamp in (
        ("snapshot.as_of", snapshot.as_of),
        ("snapshot.data_cutoff", snapshot.data_cutoff),
        ("snapshot.created_at", snapshot.created_at),
    ):
        _require_aware(label, timestamp)
    if snapshot.as_of != as_of:
        raise LiveEvidenceRequiredError(
            "Snapshot as_of does not exactly match the requested as_of time"
        )
    if snapshot.data_cutoff > as_of:
        raise LiveEvidenceRequiredError("Snapshot cutoff is newer than the requested as_of time")
    if snapshot.created_at < as_of:
        raise LiveEvidenceRequiredError(
            "Snapshot cannot be created before its as_of time"
        )
    requested_zone = as_of.tzinfo
    assert requested_zone is not None
    if snapshot.data_cutoff.astimezone(requested_zone).date() != as_of.date():
        raise LiveEvidenceRequiredError(
            "Snapshot cutoff is stale: it must be from the requested as_of date"
        )
    if snapshot.base_session != as_of.date() or snapshot.base_session.weekday() >= 5:
        raise LiveEvidenceRequiredError(
            "Snapshot base_session must be the requested as_of trading weekday"
        )
    calendar = snapshot.trading_calendar
    _validate_source_url(calendar.source_url, label="trading calendar")
    _validate_hash(calendar.source_hash, label="trading calendar")
    _require_aware("trading calendar observed_at", calendar.observed_at)
    _require_aware("trading calendar ingested_at", calendar.ingested_at)
    if calendar.observed_at > calendar.ingested_at:
        raise LiveEvidenceRequiredError("Trading calendar was ingested before it was observed")
    if calendar.observed_at > snapshot.data_cutoff or calendar.ingested_at > snapshot.data_cutoff:
        raise LiveEvidenceRequiredError(
            "Trading calendar was not visible at the frozen data cutoff"
        )
    expected_sessions = [snapshot.base_session, *snapshot.target_sessions]
    if calendar.sessions != expected_sessions:
        raise LiveEvidenceRequiredError(
            "Trading calendar sessions must exactly match base_session and D1/D2 targets"
        )
    required_codes = set(instrument_codes or (index.code for index in INDEXES))
    if not required_codes:
        raise LiveEvidenceRequiredError("Configured market universe has no instruments")
    if set(snapshot.volatility_20d) != required_codes:
        missing = sorted(required_codes - set(snapshot.volatility_20d))
        extra = sorted(set(snapshot.volatility_20d) - required_codes)
        raise LiveEvidenceRequiredError(
            "Snapshot volatility keys do not match configured instruments; "
            f"missing={missing}, extra={extra}"
        )
    if any(
        not math.isfinite(value) or value <= 0 or value > 1
        for value in snapshot.volatility_20d.values()
    ):
        raise LiveEvidenceRequiredError(
            "Snapshot volatility values must be finite, greater than zero and at most one"
        )
    if set(snapshot.market_data) != required_codes:
        missing = sorted(required_codes - set(snapshot.market_data))
        extra = sorted(set(snapshot.market_data) - required_codes)
        raise LiveEvidenceRequiredError(
            "Snapshot market provenance keys do not match configured instruments; "
            f"missing={missing}, extra={extra}"
        )
    for code, provenance in snapshot.market_data.items():
        if provenance.trade_date != snapshot.base_session:
            raise LiveEvidenceRequiredError(
                f"Market data {code} trade_date must equal the frozen base_session"
            )
        _validate_source_url(provenance.source_url, label=f"market data {code}")
        _validate_hash(provenance.source_hash, label=f"market data {code}")
        _require_aware(f"market data {code} observed_at", provenance.observed_at)
        _require_aware(f"market data {code} ingested_at", provenance.ingested_at)
        if provenance.observed_at > provenance.ingested_at:
            raise LiveEvidenceRequiredError(
                f"Market data {code} was ingested before it was observed"
            )
        if (
            provenance.observed_at > snapshot.data_cutoff
            or provenance.ingested_at > snapshot.data_cutoff
        ):
            raise LiveEvidenceRequiredError(
                f"Market data {code} was not visible at the frozen data cutoff"
            )
    _validate_target_sessions(snapshot.target_sessions, as_of=as_of)
    if not snapshot.items:
        raise LiveEvidenceRequiredError("Live snapshot must contain at least one evidence item")
    if len({item.id for item in snapshot.items}) != len(snapshot.items):
        raise LiveEvidenceRequiredError("Evidence item IDs must be unique within a snapshot")
    for item in snapshot.items:
        _validate_source_url(item.source_url, label=f"evidence {item.id}")
        _validate_hash(item.content_hash, label=f"evidence {item.id}")
        for label, timestamp in (
            ("event_time", item.event_time),
            ("published_at", item.published_at),
            ("ingested_at", item.ingested_at),
        ):
            _require_aware(f"evidence {item.id} {label}", timestamp)
        if not (item.event_time <= item.published_at <= item.ingested_at):
            raise LiveEvidenceRequiredError(
                f"Evidence {item.id} has inconsistent event/published/ingested times"
            )
        if item.ingested_at > snapshot.data_cutoff:
            raise LiveEvidenceRequiredError(
                f"Evidence {item.id} was not visible at the frozen data cutoff"
            )
        expected_item_hash = evidence_item_hash(item)
        if item.content_hash != expected_item_hash:
            raise LiveEvidenceRequiredError(
                f"Evidence {item.id} content_hash does not match its canonical payload"
            )


def _demo_snapshot(
    settings: Settings,
    as_of: datetime,
    *,
    universe: MarketUniverseSpec,
) -> FrozenEvidenceSnapshot:
    timezone = ZoneInfo(settings.timezone)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone)
    item_payload = {
        "id": "DEMO-EVIDENCE-ONLY",
        "title": "离线演示占位事实",
        "summary": "离线演示没有实时行情和资讯，仅验证工作流、引用与评分链路。",
        "quote": "离线演示没有实时行情和资讯，仅验证工作流、引用与评分链路。",
        "source_url": "https://example.invalid/forecast-loop-demo",
        "event_time": as_of,
        "published_at": as_of,
        "ingested_at": as_of,
        "entities": list(universe.codes),
        "event_type": "demo",
    }
    item = EvidenceItem(
        **item_payload,
        content_hash=canonical_hash(
            EvidenceItem(**item_payload, content_hash="0" * 64).model_dump(
                mode="json", exclude={"content_hash"}
            )
        ),
    )
    market_data = {
        index.code: MarketDataProvenance(
            trade_date=as_of.date(),
            source_url="https://example.invalid/forecast-loop-demo-market",
            source_hash=hashlib.sha256(
                f"demo-market|{index.code}|{as_of.isoformat()}".encode()
            ).hexdigest(),
            observed_at=as_of,
            ingested_at=as_of,
        )
        for index in universe.definitions()
    }
    base = {
        "as_of": as_of,
        "data_cutoff": as_of,
        "created_at": as_of,
        "base_session": as_of.date(),
        "trading_calendar": TradingCalendarProvenance(
            sessions=[as_of.date(), *_next_weekdays(as_of.date(), count=2)],
            source_url="https://example.invalid/forecast-loop-demo-calendar",
            source_hash=hashlib.sha256(
                f"demo-calendar|{as_of.date().isoformat()}".encode()
            ).hexdigest(),
            observed_at=as_of,
            ingested_at=as_of,
        ),
        "volatility_20d": {
            code: DEMO_VOLATILITY.get(code, 0.02) for code in universe.codes
        },
        "market_data": market_data,
        "target_sessions": _next_weekdays(as_of.date(), count=2),
        "items": [item],
    }
    canonical = FrozenEvidenceSnapshot(**base, content_hash="0" * 64).model_dump(
        mode="json", exclude={"content_hash"}
    )
    digest = canonical_hash(canonical)
    return FrozenEvidenceSnapshot(**base, content_hash=digest)


def _validate_source_url(url: str, *, label: str) -> None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LiveEvidenceRequiredError(f"{label} must use a valid HTTPS source URL")
    if not any(
        hostname == trusted or hostname.endswith(f".{trusted}")
        for trusted in TRUSTED_SOURCE_DOMAINS
    ):
        raise LiveEvidenceRequiredError(
            f"{label} source domain is not in the trusted allowlist: {hostname}"
        )


def _validate_hash(value: str, *, label: str) -> None:
    if not HASH_RE.fullmatch(value):
        raise LiveEvidenceRequiredError(f"{label} hash must be 64 lowercase hex characters")
    if value == "0" * 64:
        raise LiveEvidenceRequiredError(f"{label} hash cannot be a placeholder digest")


def _require_aware(label: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveEvidenceRequiredError(f"{label} must be timezone-aware")


def _validate_target_sessions(targets: list[date], *, as_of: datetime) -> None:
    if len(targets) != 2:
        raise LiveEvidenceRequiredError("Snapshot must freeze exactly two target sessions")
    if targets != sorted(set(targets)):
        raise LiveEvidenceRequiredError("Snapshot target sessions must be unique and increasing")
    if any(target <= as_of.date() for target in targets):
        raise LiveEvidenceRequiredError("Snapshot target sessions must be after as_of")
    if any(target.weekday() >= 5 for target in targets):
        raise LiveEvidenceRequiredError("Snapshot target sessions must be trading weekdays")


def _next_weekdays(start: date, *, count: int) -> list[date]:
    targets: list[date] = []
    candidate = start
    while len(targets) < count:
        candidate += timedelta(days=1)
        if candidate.weekday() < 5:
            targets.append(candidate)
    return targets
