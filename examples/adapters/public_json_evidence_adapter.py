"""Keyless, read-only EvidenceSnapshotSource for a sealed public JSON fixture."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.agent_contracts import contract_content_hash
from app.ports import (
    EvidenceSnapshotAccessError,
    EvidenceSnapshotFormatError,
    EvidenceSnapshotValidationError,
)
from app.schemas import FrozenEvidenceSnapshot
from app.services.snapshot import (
    LiveEvidenceRequiredError,
    validate_live_snapshot,
    validate_snapshot_content_hash,
)
from app.testing.adapter_compat import (
    FROZEN_EVIDENCE_SNAPSHOT_CONTRACT,
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
        "examples/adapters/data"
    ),
    redistribution_allowed=True,
    notice=(
        "Synthetic values are dedicated to the public domain for adapter tests; "
        "they are not investment research or live market data."
    ),
)

EXAMPLE_EVIDENCE_MANIFEST = AdapterManifest(
    adapter_id="public-json-evidence-adapter",
    adapter_version="1.0.0",
    kind=AdapterKind.EVIDENCE_ADAPTER,
    contract_version=FROZEN_EVIDENCE_SNAPSHOT_CONTRACT,
    capabilities=AdapterCapabilities(
        direction=DirectionCapability.NOT_APPLICABLE,
        probability=ProbabilityCapability.NOT_APPLICABLE,
        reasoning=ReasoningCapability.NOT_APPLICABLE,
        citation=CitationCapability.FROZEN,
    ),
    license=EXAMPLE_DATA_LICENSE,
    source_url=(
        "https://example.org/forecast-loop/"
        "examples/adapters/public_json_evidence_adapter.py"
    ),
    read_only=True,
    writes=(),
    evidence_cutoff_responsibility="adapter",
    limitations=(
        "Reads one already assembled local fixture; it is not a crawler.",
        "The adapter validates cutoff and provenance but does not authorize "
        "third-party content for redistribution.",
        "The example values are synthetic and must never be used for live forecasts.",
    ),
)


class _EvidenceBundleBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["forecast-loop.example-public-evidence/v1"]
    observed_at: datetime
    source_name: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=1, max_length=2000)
    license: AdapterLicense
    snapshot: FrozenEvidenceSnapshot

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bundle observed_at must be timezone-aware")
        return value


class _EvidenceBundle(_EvidenceBundleBody):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublicJsonEvidenceAdapter:
    """Validate a public wrapper and return its canonical frozen snapshot."""

    source_path: Path
    max_bytes: int = 2 * 1024 * 1024
    manifest: AdapterManifest = EXAMPLE_EVIDENCE_MANIFEST

    @property
    def license_declaration(self) -> AdapterLicense:
        """Return the validated fixture license for compatibility inspection."""

        return self._load_bundle().license

    def load_snapshot(self, *, as_of: datetime) -> FrozenEvidenceSnapshot:
        bundle = self._load_bundle()
        try:
            if bundle.snapshot.as_of != as_of:
                raise ValueError(
                    "snapshot as_of must exactly match the requested as_of"
                )
            if bundle.observed_at > bundle.snapshot.data_cutoff:
                raise ValueError(
                    "public bundle was observed after the frozen evidence cutoff"
                )
            validate_live_snapshot(bundle.snapshot, as_of=as_of)
            validate_snapshot_content_hash(bundle.snapshot)
        except (LiveEvidenceRequiredError, ValueError) as exc:
            raise EvidenceSnapshotValidationError(
                f"public evidence bundle failed closed: {exc}"
            ) from exc
        return bundle.snapshot

    def _load_bundle(self) -> _EvidenceBundle:
        try:
            payload = read_json_document(self.source_path, max_bytes=self.max_bytes)
        except ExampleSourceAccessError as exc:
            raise EvidenceSnapshotAccessError(str(exc)) from exc
        except ExampleSourceFormatError as exc:
            raise EvidenceSnapshotFormatError(str(exc)) from exc
        try:
            bundle = _EvidenceBundle.model_validate(payload)
        except ValidationError as exc:
            raise EvidenceSnapshotFormatError(
                f"public evidence bundle does not match its schema: {exc}"
            ) from exc
        try:
            if bundle.license != self.manifest.license:
                raise ValueError(
                    "bundle license does not match the reviewed adapter manifest"
                )
            if bundle.source_url != self.manifest.license.source_url:
                raise ValueError(
                    "bundle source_url does not match the reviewed fixture source"
                )
            canonical = bundle.model_dump(mode="json", exclude={"content_hash"})
            if bundle.content_hash != contract_content_hash(canonical):
                raise ValueError(
                    "bundle content_hash does not match canonical content"
                )
        except ValueError as exc:
            raise EvidenceSnapshotValidationError(
                f"public evidence bundle failed closed: {exc}"
            ) from exc
        return bundle
