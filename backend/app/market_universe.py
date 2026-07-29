"""Versioned market-universe contracts for configurable forecast targets."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import Horizon, IndexDefinition

MARKET_UNIVERSE_SCHEMA = "forecast-loop.market-universe/v1"
MARKET_UNIVERSE_MAX_BYTES = 1024 * 1024
HASH_PATTERN = r"^[0-9a-f]{64}$"


class MarketUniverseError(RuntimeError):
    """Raised when a configured universe cannot be trusted or validated."""


class UniverseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentSpec(UniverseModel):
    """One stable forecast target inside a single-market universe."""

    code: str = Field(
        min_length=1,
        max_length=24,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$",
    )
    name: str = Field(min_length=1, max_length=120)
    asset_type: Literal["index", "equity"]
    exchange: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    sector: str | None = Field(default=None, max_length=120)
    strategy_bucket: Literal[
        "large_cap",
        "mid_small_cap",
        "growth",
        "balanced",
    ] = "balanced"
    tags: tuple[str, ...] = ()
    wiki_entry_ids: dict[str, str] = Field(default_factory=dict)
    agent_briefs: dict[str, str] = Field(default_factory=dict)

    @field_validator("code", "name", "exchange", "sector", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("tags")
    @classmethod
    def tags_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("instrument tags must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("instrument tags must be unique")
        return normalized

    @field_validator("wiki_entry_ids")
    @classmethod
    def wiki_bindings_are_explicit(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for agent_id, entry_id in value.items():
            agent = agent_id.strip()
            entry = entry_id.strip()
            if not agent or not entry:
                raise ValueError("Wiki binding agent and entry IDs must not be blank")
            normalized[agent] = entry
        return dict(sorted(normalized.items()))

    @field_validator("agent_briefs")
    @classmethod
    def agent_briefs_are_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for agent_id, brief in value.items():
            agent = agent_id.strip()
            text = brief.strip()
            if not agent or not text:
                raise ValueError("Agent brief IDs and text must not be blank")
            if len(agent) > 80 or len(text) > 1000:
                raise ValueError("Agent brief IDs or text exceed the supported length")
            if any(ord(character) < 32 and character not in "\n\t" for character in text):
                raise ValueError("Agent briefs must not contain control characters")
            normalized[agent] = text
        return dict(sorted(normalized.items()))

    def to_definition(self, *, market: str, timezone: str) -> IndexDefinition:
        """Convert the public contract into the legacy-compatible runtime shape."""

        return IndexDefinition(
            code=self.code,
            name=self.name,
            market=market,
            asset_type=self.asset_type,
            exchange=self.exchange,
            currency=self.currency,
            timezone=timezone,
            sector=self.sector,
            strategy_bucket=self.strategy_bucket,
            tags=self.tags,
            wiki_entry_ids=tuple(sorted(self.wiki_entry_ids.items())),
            agent_briefs=tuple(sorted(self.agent_briefs.items())),
        )


class MarketUniverseBody(UniverseModel):
    schema_version: Literal["forecast-loop.market-universe/v1"] = MARKET_UNIVERSE_SCHEMA
    universe_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    version: str = Field(min_length=1, max_length=32)
    market: str = Field(
        min_length=2,
        max_length=16,
        pattern=r"^[A-Z][A-Z0-9_-]*$",
    )
    timezone: str = Field(min_length=1, max_length=64)
    calendar_id: str = Field(min_length=1, max_length=64)
    session_close: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    horizons: tuple[Horizon, ...] = (Horizon.D1, Horizon.D2)
    instruments: tuple[InstrumentSpec, ...] = Field(min_length=1, max_length=100)

    @field_validator(
        "universe_id",
        "version",
        "market",
        "timezone",
        "calendar_id",
        "session_close",
        mode="before",
    )
    @classmethod
    def strip_universe_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def universe_is_coherent(self) -> MarketUniverseBody:
        if self.horizons != (Horizon.D1, Horizon.D2):
            raise ValueError("market-universe/v1 requires ordered D1 and D2 horizons")
        codes = [item.code for item in self.instruments]
        if len(codes) != len(set(codes)):
            raise ValueError("instrument codes must be unique within a universe")
        currencies = {item.currency for item in self.instruments}
        if len(currencies) != 1:
            raise ValueError("market-universe/v1 requires one settlement currency per run")
        return self


class MarketUniverseSpec(MarketUniverseBody):
    content_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def content_hash_matches_body(self) -> MarketUniverseSpec:
        expected = market_universe_hash(
            self.model_dump(mode="json", exclude={"content_hash"})
        )
        if self.content_hash != expected:
            raise ValueError("market universe content_hash does not match canonical content")
        return self

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.instruments)

    def definitions(self) -> tuple[IndexDefinition, ...]:
        return tuple(
            item.to_definition(market=self.market, timezone=self.timezone)
            for item in self.instruments
        )


def market_universe_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def seal_market_universe(body: MarketUniverseBody) -> MarketUniverseSpec:
    payload = body.model_dump(mode="json")
    return MarketUniverseSpec(
        **payload,
        content_hash=market_universe_hash(payload),
    )


def load_market_universe(path: Path | None) -> MarketUniverseSpec:
    """Load a sealed custom universe, or return the legacy A-share default."""

    if path is None:
        return DEFAULT_MARKET_UNIVERSE
    configured = path.expanduser()
    if configured.is_symlink():
        raise MarketUniverseError("market universe path may not be a symlink")
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise MarketUniverseError(f"market universe is unavailable: {configured}") from exc
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MarketUniverseError("market universe must be a regular file")
        if metadata.st_size <= 0 or metadata.st_size > MARKET_UNIVERSE_MAX_BYTES:
            raise MarketUniverseError(
                f"market universe must contain 1-{MARKET_UNIVERSE_MAX_BYTES} bytes"
            )
        raw = os.read(descriptor, metadata.st_size + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != metadata.st_size
            or metadata.st_size != after.st_size
            or metadata.st_mtime_ns != after.st_mtime_ns
            or metadata.st_ino != after.st_ino
        ):
            raise MarketUniverseError("market universe changed while being read")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise MarketUniverseError("market universe is not valid UTF-8") from exc
    except ValueError as exc:
        raise MarketUniverseError("market universe is not valid canonical JSON") from exc
    if not isinstance(payload, dict):
        raise MarketUniverseError("market universe root must be a JSON object")
    try:
        if "content_hash" not in payload:
            return seal_market_universe(MarketUniverseBody.model_validate(payload))
        return MarketUniverseSpec.model_validate(payload)
    except ValueError as exc:
        raise MarketUniverseError(f"invalid market universe: {exc}") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


DEFAULT_MARKET_UNIVERSE = seal_market_universe(
    MarketUniverseBody(
        universe_id="a-share-core-indexes",
        version="1.0.0",
        market="CN",
        timezone="Asia/Shanghai",
        calendar_id="SSE",
        session_close="15:00",
        instruments=tuple(
            InstrumentSpec(
                code=code,
                name=name,
                asset_type="index",
                exchange="SSE" if code.endswith(".SH") else "SZSE",
                currency="CNY",
                strategy_bucket=bucket,
                tags=tags,
            )
            for code, name, bucket, tags in (
                ("000300.SH", "沪深300", "large_cap", ("broad-market", "large-cap")),
                ("000905.SH", "中证500", "mid_small_cap", ("broad-market", "mid-cap")),
                ("000852.SH", "中证1000", "mid_small_cap", ("broad-market", "small-cap")),
                ("399006.SZ", "创业板指", "growth", ("growth", "technology")),
                ("000688.SH", "科创50", "growth", ("growth", "technology")),
            )
        ),
    )
)
