"""Explicit opt-in coverage for attributed, independently unverified media reports."""

from typing import Literal

from pydantic import Field, model_validator

from .evidence_coverage import EvidenceCoverageV1, PolicyEventRequirementV1

MEDIA_WARNING = "未独立核实"
DEGRADED_FORECAST_WARNING = "降级预测：含未独立核实的媒体报道，不代表官方政策已获核实。"


class PolicyEventRequirementV2(PolicyEventRequirementV1):
    status: Literal["verified", "unresolved", "media_reported"]
    verification_warning: Literal["未独立核实"] | None = None

    @model_validator(mode="after")
    def media_shape(self):
        if self.status == "media_reported":
            if not self.evidence_item_ids or not self.missing_reason:
                raise ValueError(
                    "reported policy requires captured evidence and missing-original reason"
                )
            if self.verification_warning != MEDIA_WARNING:
                raise ValueError("reported policy requires the unverified-media warning")
        elif self.verification_warning is not None:
            raise ValueError("only reported policy may carry a media verification warning")
        return self


class EvidenceCoverageV2(EvidenceCoverageV1):
    schema_version: Literal["forecast-loop.evidence-coverage/v2"] = (
        "forecast-loop.evidence-coverage/v2"
    )
    media_policy: Literal["attributed-unverified-media/v1"]
    events: list[PolicyEventRequirementV2] = Field(max_length=1000)


def has_reported_policy(coverage) -> bool:
    return isinstance(coverage, EvidenceCoverageV2) and any(
        event.status == "media_reported" for event in coverage.events
    )
