"""Additive Responses API contracts; historical Codex contracts stay unchanged."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .research_v2 import (
    CodexDraftRecordV3,
    CodexHandoffRequestV3,
    ContractModel,
    HandoffAssignmentV3,
    ReasoningReviewInputV2,
    ReasoningRubricDraftV2,
    content_hash,
)

RESPONSES_HANDOFF_SCHEMA = "forecast-loop.responses-handoff/v1"
RESPONSES_HANDOFF_SCHEMA_V2 = "forecast-loop.responses-handoff/v2"
RESPONSES_HANDOFF_SCHEMA_V3 = "forecast-loop.responses-handoff/v3"
RESPONSES_REVIEW_TASK_SCHEMA = "forecast-loop.responses-reasoning-task/v1"
RESPONSES_MODEL = "gpt-6.1-sol"
RESPONSES_EFFORT = "high"
RESPONSES_FALLBACK_MODEL = "gpt-5.6-sol"
RESPONSES_FALLBACK_REASONS = frozenset(
    {"quota_exhausted", "connection", "timeout", "service_unavailable"}
)


def validate_responses_generator(value: dict[str, str]) -> None:
    expected = {
        "surface": "openai_responses",
        "model": RESPONSES_MODEL,
        "reasoning_effort": RESPONSES_EFFORT,
    }
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("Responses drafts must identify the frozen API provider, model and effort")
    if not re.fullmatch(r"resp_[A-Za-z0-9_-]+", value.get("response_id", "")):
        raise ValueError("Responses drafts require an API response identity")
    if not re.fullmatch(
        re.escape(RESPONSES_MODEL) + r"(?:-\d{4}-\d{2}-\d{2})?",
        value.get("response_model", ""),
    ):
        raise ValueError("Responses drafts require the actual response model identity")


def validate_routed_generator(value: dict[str, str], generated_at: datetime) -> None:
    if value.get("surface") == "openai_responses":
        validate_responses_generator(value)
    else:
        expected = {
            "surface": "codex",
            "model": RESPONSES_FALLBACK_MODEL,
            "reasoning_effort": RESPONSES_EFFORT,
            "primary_surface": "openai_responses",
            "primary_model": RESPONSES_MODEL,
        }
        if (
            any(value.get(k) != v for k, v in expected.items())
            or set(value) != set(expected) | {"fallback_reason", "primary_failed_at"}
            or value.get("fallback_reason") not in RESPONSES_FALLBACK_REASONS
        ):
            raise ValueError("Codex fallback requires the sealed policy and actual provenance")
        failed_at = datetime.fromisoformat(value["primary_failed_at"])
        if failed_at.utcoffset() is None or generated_at.utcoffset() is None:
            raise ValueError("fallback times must be timezone-aware")
        if failed_at > generated_at:
            raise ValueError("fallback cannot predate the failed primary attempt")
    if generated_at.utcoffset() is None:
        raise ValueError("Responses generation time must be timezone-aware")


class ResponsesContractModel(ContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class ResponsesAssignmentV1(HandoffAssignmentV3):
    model_config = ConfigDict(hide_input_in_errors=True)
    producer: Literal["openai_responses", "deterministic"]

    @model_validator(mode="after")
    def validate_provider(self) -> ResponsesAssignmentV1:
        if self.producer == "openai_responses" and (
            self.model_name != RESPONSES_MODEL
            or self.prompt_version
            not in {
                RESPONSES_HANDOFF_SCHEMA,
                RESPONSES_HANDOFF_SCHEMA_V2,
                RESPONSES_HANDOFF_SCHEMA_V3,
            }
        ):
            raise ValueError("Responses assignment changed its frozen model or prompt version")
        return self


class ResponsesHandoffRequestV1(CodexHandoffRequestV3):
    model_config = ConfigDict(hide_input_in_errors=True)
    schema_version: Literal["forecast-loop.responses-handoff/v1"] = RESPONSES_HANDOFF_SCHEMA
    assignments: list[ResponsesAssignmentV1] = Field(min_length=1)
    reasoning_effort: Literal["high"] = RESPONSES_EFFORT

    @model_validator(mode="after")
    def validate_prompt_protocol(self) -> ResponsesHandoffRequestV1:
        if any(
            item.producer == "openai_responses" and item.prompt_version != self.schema_version
            for item in self.assignments
        ):
            raise ValueError("Responses prompt version does not match its handoff protocol")
        return self


class ResponsesHandoffRequestV2(ResponsesHandoffRequestV1):
    schema_version: Literal["forecast-loop.responses-handoff/v2"] = RESPONSES_HANDOFF_SCHEMA_V2
    mode: Literal["demo", "live"]


class ResponsesHandoffRequestV3(ResponsesHandoffRequestV2):
    """Seal API preference and the only permitted fallback before any generation."""

    schema_version: Literal["forecast-loop.responses-handoff/v3"] = RESPONSES_HANDOFF_SCHEMA_V3
    fallback_policy: Literal["quota-or-availability/v1"] = "quota-or-availability/v1"
    fallback_model: Literal["gpt-5.6-sol"] = RESPONSES_FALLBACK_MODEL
    fallback_reasoning_effort: Literal["high"] = RESPONSES_EFFORT


class ResponsesPredictionContent(ResponsesContractModel):
    drafts: list[CodexDraftRecordV3] = Field(min_length=1)


class ResponsesDraftBundleV1(ResponsesPredictionContent):
    schema_version: Literal["forecast-loop.responses-handoff/v1"] = RESPONSES_HANDOFF_SCHEMA
    run_id: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: dict[str, str]

    @model_validator(mode="after")
    def validate_generator(self) -> ResponsesDraftBundleV1:
        validate_responses_generator(self.generated_by)
        if len({item.assignment_id for item in self.drafts}) != len(self.drafts):
            raise ValueError("draft assignment IDs must be unique")
        if self.generated_at.utcoffset() is None:
            raise ValueError("Responses generation time must be timezone-aware")
        return self


class ResponsesDraftBundleV2(ResponsesDraftBundleV1):
    schema_version: Literal["forecast-loop.responses-handoff/v2"] = RESPONSES_HANDOFF_SCHEMA_V2


class ResponsesDraftBundleV3(ResponsesDraftBundleV2):
    schema_version: Literal["forecast-loop.responses-handoff/v3"] = RESPONSES_HANDOFF_SCHEMA_V3

    @model_validator(mode="after")
    def validate_generator(self) -> ResponsesDraftBundleV3:
        validate_routed_generator(self.generated_by, self.generated_at)
        if len({item.assignment_id for item in self.drafts}) != len(self.drafts):
            raise ValueError("draft assignment IDs must be unique")
        if self.generated_at.utcoffset() is None:
            raise ValueError("Responses generation time must be timezone-aware")
        return self


class ResponsesReasoningTaskV1(ResponsesContractModel):
    schema_version: Literal["forecast-loop.responses-reasoning-task/v1"] = (
        RESPONSES_REVIEW_TASK_SCHEMA
    )
    run_id: str
    prediction_request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: Literal["gpt-6.1-sol"] = RESPONSES_MODEL
    reasoning_effort: Literal["high"] = RESPONSES_EFFORT
    outcomes_included: Literal[False] = False
    reviews: list[ReasoningReviewInputV2] = Field(min_length=1)
    task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_seal(self) -> ResponsesReasoningTaskV1:
        if self.task_hash != content_hash(self, exclude=("task_hash",)):
            raise ValueError("Responses reasoning task hash mismatch")
        if len({item.signal_id for item in self.reviews}) != len(self.reviews):
            raise ValueError("reasoning task signal IDs must be unique")
        return self


class ResponsesReasoningTaskV2(ResponsesReasoningTaskV1):
    schema_version: Literal["forecast-loop.responses-reasoning-task/v2"] = (
        "forecast-loop.responses-reasoning-task/v2"
    )
    fallback_policy: Literal["quota-or-availability/v1"] = "quota-or-availability/v1"
    fallback_model: Literal["gpt-5.6-sol"] = RESPONSES_FALLBACK_MODEL
    fallback_reasoning_effort: Literal["high"] = RESPONSES_EFFORT


class ResponsesRubricV1(ReasoningRubricDraftV2):
    model_config = ConfigDict(hide_input_in_errors=True)
    model: Literal["gpt-6.1-sol"]


class ResponsesReviewRecordV1(ResponsesContractModel):
    signal_id: str
    review_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric: ResponsesRubricV1


class ResponsesReviewContent(ResponsesContractModel):
    reviews: list[ResponsesReviewRecordV1] = Field(min_length=1)


class ResponsesReviewBundleV1(ResponsesReviewContent):
    schema_version: Literal["forecast-loop.responses-reasoning-drafts/v1"] = (
        "forecast-loop.responses-reasoning-drafts/v1"
    )
    run_id: str
    task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: dict[str, str]

    @model_validator(mode="after")
    def validate_generator(self) -> ResponsesReviewBundleV1:
        validate_responses_generator(self.generated_by)
        if len({item.signal_id for item in self.reviews}) != len(self.reviews):
            raise ValueError("reasoning review signal IDs must be unique")
        if self.generated_at.utcoffset() is None:
            raise ValueError("Responses generation time must be timezone-aware")
        return self


class RoutedRubricV2(ReasoningRubricDraftV2):
    model: Literal["gpt-6.1-sol", "gpt-5.6-sol"]


class RoutedReviewRecordV2(ResponsesContractModel):
    signal_id: str
    review_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric: RoutedRubricV2


class RoutedReviewContentV2(ResponsesContractModel):
    reviews: list[RoutedReviewRecordV2] = Field(min_length=1)


class ResponsesReviewBundleV2(ResponsesReviewBundleV1):
    schema_version: Literal["forecast-loop.responses-reasoning-drafts/v2"] = (
        "forecast-loop.responses-reasoning-drafts/v2"
    )
    reviews: list[RoutedReviewRecordV2] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_generator(self) -> ResponsesReviewBundleV2:
        validate_routed_generator(self.generated_by, self.generated_at)
        if any(item.rubric.model != self.generated_by["model"] for item in self.reviews):
            raise ValueError("blind review rubric must identify its actual generator")
        if len({item.signal_id for item in self.reviews}) != len(self.reviews):
            raise ValueError("reasoning review signal IDs must be unique")
        return self
