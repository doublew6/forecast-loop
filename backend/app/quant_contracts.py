"""Versioned, content-addressed contracts for read-only Quant signal bundles."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .agent_contracts import (
    HASH_PATTERN,
    ContractModel,
    SignalProbabilityVector,
    SignalTarget,
    contract_content_hash,
)
from .domain import predicted_direction

QUANT_SIGNAL_BUNDLE_SCHEMA = "forecast-loop.quant-signal-bundle/v1"
QUANT_INPUT_SNAPSHOT_SCHEMA = "forecast-loop.quant-input-snapshot/v1"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


def _reject_placeholder_hash(value: str) -> str:
    if value == "0" * 64:
        raise ValueError("SHA-256 digest may not be a placeholder")
    return value


class QuantArtifactRef(ContractModel):
    """One immutable file referenced by a Quant bundle."""

    artifact_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    version: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=HASH_PATTERN)

    @field_validator("version")
    @classmethod
    def version_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("artifact version may not be blank")
        return stripped

    @field_validator("path")
    @classmethod
    def path_is_relative_and_normalized(cls, value: str) -> str:
        if (
            value.startswith(("/", "\\"))
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("artifact path must be a normalized relative POSIX path")
        return value

    @field_validator("sha256")
    @classmethod
    def digest_is_not_placeholder(cls, value: str) -> str:
        return _reject_placeholder_hash(value)


class QuantArtifactSet(ContractModel):
    """The five reproducibility inputs required by the first Quant adapter."""

    code: QuantArtifactRef
    parameters: QuantArtifactRef
    feature_set: QuantArtifactRef
    model: QuantArtifactRef
    input_snapshot: QuantArtifactRef

    @model_validator(mode="after")
    def artifact_identities_are_unambiguous(self) -> QuantArtifactSet:
        values = self.as_dict()
        for name, artifact in values.items():
            if artifact.artifact_id != name:
                raise ValueError(
                    f"artifact_id for {name} must equal the manifest slot name"
                )
        paths = [artifact.path for artifact in values.values()]
        if len(paths) != len(set(paths)):
            raise ValueError("Quant artifacts must use distinct files")
        return self

    def as_dict(self) -> dict[str, QuantArtifactRef]:
        return {
            "code": self.code,
            "parameters": self.parameters,
            "feature_set": self.feature_set,
            "model": self.model,
            "input_snapshot": self.input_snapshot,
        }


class QuantFeatureValue(ContractModel):
    name: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    value: float = Field(allow_inf_nan=False)


class QuantInputRow(ContractModel):
    index_code: str = Field(min_length=1, max_length=32)
    features: tuple[QuantFeatureValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def feature_names_are_unique(self) -> QuantInputRow:
        names = [feature.name for feature in self.features]
        if len(names) != len(set(names)):
            raise ValueError("input snapshot feature names must be unique per index")
        return self


class QuantInputSnapshotBody(ContractModel):
    schema_version: Literal["forecast-loop.quant-input-snapshot/v1"] = (
        QUANT_INPUT_SNAPSHOT_SCHEMA
    )
    snapshot_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    as_of: datetime
    data_cutoff: datetime
    created_at: datetime
    feature_set_version: str = Field(min_length=1, max_length=120)
    rows: tuple[QuantInputRow, ...] = Field(min_length=1)

    @field_validator("as_of", "data_cutoff", "created_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def snapshot_times_and_rows_are_consistent(self) -> QuantInputSnapshotBody:
        if not self.data_cutoff <= self.as_of <= self.created_at:
            raise ValueError(
                "input snapshot requires data_cutoff <= as_of <= created_at"
            )
        index_codes = [row.index_code for row in self.rows]
        if len(index_codes) != len(set(index_codes)):
            raise ValueError("input snapshot index rows must be unique")
        return self


class QuantInputSnapshot(QuantInputSnapshotBody):
    content_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("content_hash")
    @classmethod
    def digest_is_not_placeholder(cls, value: str) -> str:
        return _reject_placeholder_hash(value)

    @model_validator(mode="after")
    def content_hash_matches_body(self) -> QuantInputSnapshot:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != contract_content_hash(payload):
            raise ValueError("Quant input snapshot content_hash does not match payload")
        return self


class QuantSignalOutput(ContractModel):
    """One model result before the host binds run and receipt facts."""

    signal_id: str = Field(min_length=1, max_length=64)
    target: SignalTarget
    direction: Literal["up", "down"]
    probabilities: SignalProbabilityVector
    rationale: str = Field(min_length=1, max_length=8000)
    counter_evidence: tuple[str, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)

    @field_validator("rationale")
    @classmethod
    def rationale_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Quant rationale may not be blank")
        return stripped

    @field_validator("counter_evidence", "invalidation_conditions")
    @classmethod
    def reasoning_items_are_not_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        stripped = tuple(item.strip() for item in values)
        if any(not item for item in stripped):
            raise ValueError("Quant reasoning items may not be blank")
        return stripped

    @model_validator(mode="after")
    def direction_matches_probabilities(self) -> QuantSignalOutput:
        expected = predicted_direction(self.probabilities.as_dict())
        if self.direction != expected.value:
            raise ValueError("Quant direction must match the probability vector")
        return self


class QuantSignalBundleBody(ContractModel):
    schema_version: Literal["forecast-loop.quant-signal-bundle/v1"] = (
        QUANT_SIGNAL_BUNDLE_SCHEMA
    )
    bundle_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    as_of: datetime
    data_cutoff: datetime
    generated_at: datetime
    evidence_snapshot_hash: str | None = Field(
        default=None,
        pattern=HASH_PATTERN,
    )
    market_universe_hash: str | None = Field(
        default=None,
        pattern=HASH_PATTERN,
    )
    artifacts: QuantArtifactSet
    signals: tuple[QuantSignalOutput, ...] = Field(min_length=1)

    @field_validator("as_of", "data_cutoff", "generated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @field_validator("evidence_snapshot_hash", "market_universe_hash")
    @classmethod
    def binding_digest_is_not_placeholder(cls, value: str | None) -> str | None:
        return _reject_placeholder_hash(value) if value is not None else None

    @model_validator(mode="after")
    def bundle_time_and_target_semantics_are_complete(self) -> QuantSignalBundleBody:
        if not self.data_cutoff <= self.as_of <= self.generated_at:
            raise ValueError(
                "Quant bundle requires data_cutoff <= as_of <= generated_at"
            )
        signal_ids = [signal.signal_id for signal in self.signals]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("Quant signal_id values must be unique within a bundle")
        target_keys: list[tuple[str, str, object]] = []
        for signal in self.signals:
            if (
                signal.target.as_of != self.as_of
                or signal.target.data_cutoff != self.data_cutoff
            ):
                raise ValueError(
                    "Quant target as_of and data_cutoff must match the bundle"
                )
            if signal.target.base_trade_date != self.as_of.date():
                raise ValueError(
                    "Quant target base_trade_date must match the as_of date"
                )
            target_keys.append(
                (
                    signal.target.index_code,
                    signal.target.horizon.value,
                    signal.target.target_date,
                )
            )
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("Quant bundle targets must be unique")
        return self


class QuantSignalBundle(QuantSignalBundleBody):
    content_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("content_hash")
    @classmethod
    def digest_is_not_placeholder(cls, value: str) -> str:
        return _reject_placeholder_hash(value)

    @model_validator(mode="after")
    def content_hash_matches_body(self) -> QuantSignalBundle:
        payload = self.model_dump(
            mode="json",
            exclude={"content_hash"},
            exclude_none=True,
        )
        if self.content_hash != contract_content_hash(payload):
            raise ValueError("Quant signal bundle content_hash does not match payload")
        return self


def seal_quant_input_snapshot(
    value: QuantInputSnapshotBody | dict[str, object],
) -> QuantInputSnapshot:
    body = (
        value
        if isinstance(value, QuantInputSnapshotBody)
        else QuantInputSnapshotBody.model_validate(value)
    )
    payload = body.model_dump(mode="json")
    return QuantInputSnapshot.model_validate(
        {**payload, "content_hash": contract_content_hash(payload)}
    )


def seal_quant_signal_bundle(
    value: QuantSignalBundleBody | dict[str, object],
) -> QuantSignalBundle:
    body = (
        value
        if isinstance(value, QuantSignalBundleBody)
        else QuantSignalBundleBody.model_validate(value)
    )
    payload = body.model_dump(mode="json", exclude_none=True)
    return QuantSignalBundle.model_validate(
        {**payload, "content_hash": contract_content_hash(payload)}
    )
