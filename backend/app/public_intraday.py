"""Strict scalar publication for explicitly identified intraday supplements."""

from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .public_showcase import PublicModel, PublicSnapshot
from .research_v2 import ProbabilitiesV2


class PublicIntradayForecast(PublicModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: Literal["csi1000-open-to-open-d1"]
    publication_kind: Literal["intraday_supplement"]
    performance_included: Literal[False]
    evidence_status: Literal["partial"]
    anchor_date: date
    target_date: date
    data_cutoff: datetime
    published_at: datetime
    probabilities: ProbabilitiesV2
    threshold: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_publication(self):
        if any(t.utcoffset() is None for t in (self.data_cutoff, self.published_at)):
            raise ValueError("intraday timestamps require a timezone")
        local = self.published_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if (
            self.anchor_date >= self.target_date
            or local.date() != self.anchor_date
            or not time(9, 30) < local.time() < time(15, 0)
            or self.data_cutoff > self.published_at
        ):
            raise ValueError("intraday publication time is invalid")
        values = self.probabilities.as_dict()
        if sum(value == max(values.values()) for value in values.values()) != 1:
            raise ValueError("intraday probabilities require a unique maximum")
        return self


class PublicIntradaySnapshot(PublicModel):
    schema_version: Literal["forecast-loop.public-intraday/v1"]
    generated_at: datetime
    forecasts: list[PublicIntradayForecast] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_snapshot(self):
        if self.generated_at.utcoffset() is None:
            raise ValueError("export time requires a timezone")
        if len({item.id for item in self.forecasts}) != len(self.forecasts):
            raise ValueError("duplicate intraday publication")
        if any(item.published_at > self.generated_at for item in self.forecasts):
            raise ValueError("cannot publish future intraday updates")
        return self


class PublicSnapshotV2(PublicSnapshot):
    schema_version: Literal["forecast-loop.public-showcase/v2"]
    intraday_forecasts: list[PublicIntradayForecast] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_intraday(self):
        PublicIntradaySnapshot(
            schema_version="forecast-loop.public-intraday/v1",
            generated_at=self.generated_at,
            forecasts=self.intraday_forecasts,
        )
        if any(item.published_at >= self.next_update_at for item in self.intraday_forecasts):
            raise ValueError("refresh deadline must follow intraday publication")
        return self


def parse_publication(raw: str | bytes) -> PublicSnapshot:
    import json

    payload = json.loads(raw)
    model = (
        PublicSnapshotV2
        if isinstance(payload, dict)
        and payload.get("schema_version") == "forecast-loop.public-showcase/v2"
        else PublicSnapshot
    )
    return model.model_validate(payload)


def include_intraday(base: PublicSnapshot, updates: PublicIntradaySnapshot) -> PublicSnapshotV2:
    """Keep open-to-open updates separate from all close-return score records."""
    payload = base.model_dump(mode="json")
    payload.update(
        schema_version="forecast-loop.public-showcase/v2",
        generated_at=max(base.generated_at, updates.generated_at),
        next_update_at=max(
            [base.next_update_at]
            + [item.published_at + timedelta(hours=26) for item in updates.forecasts]
        ),
        intraday_forecasts=updates.forecasts,
    )
    return PublicSnapshotV2.model_validate(payload)
