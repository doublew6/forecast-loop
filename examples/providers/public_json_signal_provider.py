"""Keyless, read-only AgentSignalSource backed by a sealed public JSON fixture."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.agent_contracts import (
    SIGNAL_ENVELOPE_SCHEMA,
    AgentSignalDraft,
    contract_content_hash,
)
from app.domain import predicted_direction
from app.ports import (
    AgentSignalAccessError,
    AgentSignalFormatError,
    AgentSignalValidationError,
)
from app.testing.adapter_compat import (
    AdapterCapabilities,
    AdapterKind,
    AdapterLicense,
    AdapterManifest,
    CitationCapability,
    DirectionCapability,
    ProbabilityCapability,
    ReasoningCapability,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from examples._sealed_json import (
    ExampleSourceAccessError,
    ExampleSourceFormatError,
    read_json_document,
)

EXAMPLE_DATA_LICENSE = AdapterLicense(
    spdx_id="CC0-1.0",
    data_origin="Synthetic forecast-loop compatibility fixture",
    source_url=(
        "https://example.org/forecast-loop/"
        "examples/providers/data"
    ),
    redistribution_allowed=True,
    notice=(
        "Synthetic values are dedicated to the public domain for adapter tests; "
        "they are not investment research or live market data."
    ),
)

EXAMPLE_SIGNAL_MANIFEST = AdapterManifest(
    adapter_id="public-json-signal-provider",
    adapter_version="1.0.0",
    kind=AdapterKind.SIGNAL_PROVIDER,
    contract_version=SIGNAL_ENVELOPE_SCHEMA,
    capabilities=AdapterCapabilities(
        direction=DirectionCapability.REQUIRED,
        probability=ProbabilityCapability.MULTICLASS,
        reasoning=ReasoningCapability.STRUCTURED,
        citation=CitationCapability.FROZEN,
    ),
    license=EXAMPLE_DATA_LICENSE,
    source_url=(
        "https://example.org/forecast-loop/"
        "examples/providers/public_json_signal_provider.py"
    ),
    read_only=True,
    writes=(),
    evidence_cutoff_responsibility="shared",
    limitations=(
        "Reads one local sealed fixture; it does not fetch a remote feed.",
        "Produces untrusted drafts only; the host must bind target, run, policy, "
        "deadline, provenance, and acceptance time.",
        "The example values are synthetic and must never be treated as live signals.",
    ),
)


class _SignalBundleBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["forecast-loop.example-public-signals/v1"]
    as_of: datetime
    observed_at: datetime
    source_name: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=1, max_length=2000)
    license: AdapterLicense
    signals: tuple[AgentSignalDraft, ...] = Field(min_length=1)

    @field_validator("as_of", "observed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bundle timestamps must be timezone-aware")
        return value


class _SignalBundle(_SignalBundleBody):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublicJsonSignalProvider:
    """Return a complete draft batch or fail closed without partial records."""

    source_path: Path
    max_bytes: int = 2 * 1024 * 1024
    manifest: AdapterManifest = EXAMPLE_SIGNAL_MANIFEST

    def load_signal_drafts(self, *, as_of: datetime) -> tuple[AgentSignalDraft, ...]:
        try:
            payload = read_json_document(self.source_path, max_bytes=self.max_bytes)
        except ExampleSourceAccessError as exc:
            raise AgentSignalAccessError(str(exc)) from exc
        except ExampleSourceFormatError as exc:
            raise AgentSignalFormatError(str(exc)) from exc
        try:
            bundle = _SignalBundle.model_validate(payload)
            self._validate_bundle(bundle, as_of=as_of)
        except (ValidationError, ValueError) as exc:
            raise AgentSignalValidationError(
                f"public signal bundle failed closed: {exc}"
            ) from exc

        drafts: list[AgentSignalDraft] = []
        for raw_draft in bundle.signals:
            source_payload = raw_draft.model_dump(mode="json")["source_payload"]
            source_payload.update(
                {
                    "source_bundle_hash": bundle.content_hash,
                    "source_url": bundle.source_url,
                    "data_license": bundle.license.spdx_id,
                }
            )
            drafts.append(
                AgentSignalDraft.model_validate(
                    {
                        **raw_draft.model_dump(mode="json", exclude={"source_payload"}),
                        "source_payload": source_payload,
                    }
                )
            )
        return tuple(drafts)

    def _validate_bundle(self, bundle: _SignalBundle, *, as_of: datetime) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("requested as_of must be timezone-aware")
        if bundle.as_of != as_of:
            raise ValueError("bundle as_of must exactly match the requested as_of")
        if bundle.observed_at > bundle.as_of:
            raise ValueError("bundle source was observed after its evidence cutoff")
        if bundle.license != self.manifest.license:
            raise ValueError("bundle license does not match the reviewed adapter manifest")
        if bundle.source_url != self.manifest.license.source_url:
            raise ValueError("bundle source_url does not match the reviewed fixture source")
        canonical = bundle.model_dump(mode="json", exclude={"content_hash"})
        if bundle.content_hash != contract_content_hash(canonical):
            raise ValueError("bundle content_hash does not match canonical content")
        if len({draft.signal_id for draft in bundle.signals}) != len(bundle.signals):
            raise ValueError("signal_id values must be unique within a source batch")
        for draft in bundle.signals:
            if draft.submitted_at < as_of:
                raise ValueError("draft submitted_at may not predate as_of")
            if draft.direction is None:
                raise ValueError("public example drafts require direction")
            if draft.probabilities is None:
                raise ValueError("public example drafts require multiclass probabilities")
            if predicted_direction(draft.probabilities.as_dict()).value != draft.direction:
                raise ValueError("draft direction conflicts with its probability vector")
            if not (
                draft.rationale
                and draft.counter_evidence
                and draft.invalidation_conditions
            ):
                raise ValueError("public example drafts require structured reasoning")
            if not draft.citations:
                raise ValueError("public example drafts require frozen citations")
            if any(citation.observed_at > as_of for citation in draft.citations):
                raise ValueError("draft citation was observed after the evidence cutoff")
