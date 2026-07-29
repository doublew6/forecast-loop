"""Reusable compatibility assertions for provider and data-adapter authors.

The helpers deliberately depend only on forecast-loop's runtime dependencies.
They can be called from pytest, unittest, or an external adapter repository.
They validate the shared boundary while leaving transport-specific fault
injection to the adapter's own tests via :func:`assert_fails_closed`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..agent_contracts import (
    SIGNAL_ENVELOPE_SCHEMA,
    AgentSignalDraft,
    SignalProbabilityVector,
)
from ..domain import predicted_direction
from ..ports import (
    AgentSignalSource,
    AgentSignalSourceError,
    EvidenceSnapshotSource,
    EvidenceSnapshotSourceError,
)
from ..schemas import FrozenEvidenceSnapshot
from ..services.snapshot import (
    LiveEvidenceRequiredError,
    validate_live_snapshot,
    validate_snapshot_content_hash,
)

FROZEN_EVIDENCE_SNAPSHOT_CONTRACT = "forecast-loop.frozen-evidence-snapshot/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AdapterCompatibilityError(AssertionError):
    """Raised when an adapter does not satisfy its declared public contract."""


class AdapterKind(StrEnum):
    SIGNAL_PROVIDER = "signal_provider"
    EVIDENCE_ADAPTER = "evidence_adapter"


class DirectionCapability(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class ProbabilityCapability(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NONE = "none"
    CONFIDENCE = "confidence"
    MULTICLASS = "multiclass"


class ReasoningCapability(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NONE = "none"
    STRUCTURED = "structured"


class CitationCapability(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NONE = "none"
    DECLARED = "declared"
    FROZEN = "frozen"


class AdapterCapabilities(BaseModel):
    """Capability cells used by the machine-readable compatibility matrix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: DirectionCapability
    probability: ProbabilityCapability
    reasoning: ReasoningCapability
    citation: CitationCapability


class AdapterLicense(BaseModel):
    """License statement for the checked-in example data, not application code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spdx_id: str = Field(min_length=1, max_length=64)
    data_origin: str = Field(min_length=1, max_length=240)
    source_url: str = Field(min_length=1, max_length=2000)
    redistribution_allowed: bool
    notice: str = Field(min_length=1, max_length=1000)


class AdapterManifest(BaseModel):
    """Public metadata required by forecast-loop compatibility tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["forecast-loop.adapter-manifest/v1"] = (
        "forecast-loop.adapter-manifest/v1"
    )
    adapter_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    adapter_version: str = Field(min_length=1, max_length=64)
    kind: AdapterKind
    contract_version: str = Field(min_length=1, max_length=160)
    capabilities: AdapterCapabilities
    license: AdapterLicense
    source_url: str = Field(min_length=1, max_length=2000)
    read_only: bool = True
    writes: tuple[str, ...] = ()
    evidence_cutoff_responsibility: Literal["adapter", "host", "shared"]
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def boundary_matches_kind(self) -> AdapterManifest:
        if self.read_only and self.writes:
            raise ValueError("read-only adapters may not declare write targets")
        if self.kind is AdapterKind.SIGNAL_PROVIDER:
            if self.contract_version != SIGNAL_ENVELOPE_SCHEMA:
                raise ValueError(
                    "signal providers must target the current SignalEnvelope contract"
                )
            if (
                self.capabilities.direction is DirectionCapability.NOT_APPLICABLE
                or self.capabilities.probability
                is ProbabilityCapability.NOT_APPLICABLE
                or self.capabilities.reasoning is ReasoningCapability.NOT_APPLICABLE
                or self.capabilities.citation is CitationCapability.NOT_APPLICABLE
            ):
                raise ValueError(
                    "signal-provider capability cells may not be not_applicable"
                )
        else:
            if self.contract_version != FROZEN_EVIDENCE_SNAPSHOT_CONTRACT:
                raise ValueError(
                    "evidence adapters must target the current frozen snapshot contract"
                )
            if not (
                self.capabilities.direction is DirectionCapability.NOT_APPLICABLE
                and self.capabilities.probability
                is ProbabilityCapability.NOT_APPLICABLE
                and self.capabilities.reasoning
                is ReasoningCapability.NOT_APPLICABLE
            ):
                raise ValueError(
                    "evidence adapters must mark signal capabilities not_applicable"
                )
        return self


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """Stable, inspectable list of checks completed by the test kit."""

    adapter_id: str
    contract_version: str
    checks: tuple[str, ...]


def assert_public_example_manifest(manifest: AdapterManifest) -> CompatibilityReport:
    """Check public-fixture licensing, source identity, and read-only boundaries."""

    _require_https(manifest.source_url, label="adapter source_url")
    _require_https(manifest.license.source_url, label="license source_url")
    if not manifest.license.redistribution_allowed:
        raise AdapterCompatibilityError(
            "public example data must explicitly permit redistribution"
        )
    if not manifest.read_only or manifest.writes:
        raise AdapterCompatibilityError(
            "official provider and data-adapter examples must be read-only"
        )
    if not all(item.strip() for item in manifest.limitations):
        raise AdapterCompatibilityError("adapter limitations may not be blank")
    return CompatibilityReport(
        adapter_id=manifest.adapter_id,
        contract_version=manifest.contract_version,
        checks=("source", "license", "read_only_boundary", "known_limitations"),
    )


def assert_signal_provider_compatible(
    source: AgentSignalSource,
    *,
    as_of: datetime,
    manifest: AdapterManifest,
) -> CompatibilityReport:
    """Load and inspect one complete signal-provider batch.

    The source must either return a fully valid batch or raise
    :class:`AgentSignalSourceError`; tests for malformed transport records use
    :func:`assert_fails_closed`.
    """

    assert_public_example_manifest(manifest)
    if manifest.kind is not AdapterKind.SIGNAL_PROVIDER:
        raise AdapterCompatibilityError("manifest kind must be signal_provider")
    if not isinstance(source, AgentSignalSource):
        raise AdapterCompatibilityError("source does not implement AgentSignalSource")
    _require_aware(as_of, label="requested as_of")

    try:
        drafts = tuple(source.load_signal_drafts(as_of=as_of))
    except AgentSignalSourceError:
        raise
    except Exception as exc:
        raise AdapterCompatibilityError(
            "signal provider must translate source failures to AgentSignalSourceError"
        ) from exc
    if not drafts:
        raise AdapterCompatibilityError("signal provider returned an empty batch")
    if any(not isinstance(draft, AgentSignalDraft) for draft in drafts):
        raise AdapterCompatibilityError("signal provider returned a non-AgentSignalDraft")
    if len({draft.signal_id for draft in drafts}) != len(drafts):
        raise AdapterCompatibilityError("signal_id values must be unique per batch")

    for draft in drafts:
        _assert_signal_draft(
            draft,
            as_of=as_of,
            manifest=manifest,
        )
    return CompatibilityReport(
        adapter_id=manifest.adapter_id,
        contract_version=manifest.contract_version,
        checks=(
            "source",
            "time_semantics",
            "timezone",
            "missing_values",
            "license",
            "content_hash",
            "capabilities",
        ),
    )


def assert_evidence_adapter_compatible(
    source: EvidenceSnapshotSource,
    *,
    as_of: datetime,
    manifest: AdapterManifest,
) -> CompatibilityReport:
    """Load and independently validate a frozen evidence snapshot."""

    assert_public_example_manifest(manifest)
    if manifest.kind is not AdapterKind.EVIDENCE_ADAPTER:
        raise AdapterCompatibilityError("manifest kind must be evidence_adapter")
    if not isinstance(source, EvidenceSnapshotSource):
        raise AdapterCompatibilityError(
            "source does not implement EvidenceSnapshotSource"
        )
    _require_aware(as_of, label="requested as_of")
    try:
        snapshot = source.load_snapshot(as_of=as_of)
    except EvidenceSnapshotSourceError:
        raise
    except Exception as exc:
        raise AdapterCompatibilityError(
            "evidence adapter must translate source failures to "
            "EvidenceSnapshotSourceError"
        ) from exc
    if not isinstance(snapshot, FrozenEvidenceSnapshot):
        raise AdapterCompatibilityError(
            "evidence adapter returned a non-FrozenEvidenceSnapshot"
        )
    try:
        validate_live_snapshot(snapshot, as_of=as_of)
        validate_snapshot_content_hash(snapshot)
    except LiveEvidenceRequiredError as exc:
        raise AdapterCompatibilityError(
            f"evidence adapter returned an invalid snapshot: {exc}"
        ) from exc

    declared_license = getattr(source, "license_declaration", None)
    if declared_license != manifest.license:
        raise AdapterCompatibilityError(
            "adapter did not expose the validated fixture license declaration"
        )
    return CompatibilityReport(
        adapter_id=manifest.adapter_id,
        contract_version=manifest.contract_version,
        checks=(
            "source",
            "time_semantics",
            "timezone",
            "missing_values",
            "license",
            "content_hash",
            "evidence_cutoff",
        ),
    )


def assert_fails_closed(
    operation: Callable[[], Any],
    *,
    expected_error: type[Exception] | tuple[type[Exception], ...],
    label: str,
) -> CompatibilityReport:
    """Assert a malformed or unavailable source never returns partial output."""

    try:
        operation()
    except expected_error:
        return CompatibilityReport(
            adapter_id=label,
            contract_version="failure-probe",
            checks=("fail_closed",),
        )
    except Exception as exc:
        raise AdapterCompatibilityError(
            f"{label} raised {type(exc).__name__}, not its public source error"
        ) from exc
    raise AdapterCompatibilityError(f"{label} did not fail closed")


def _assert_signal_draft(
    draft: AgentSignalDraft,
    *,
    as_of: datetime,
    manifest: AdapterManifest,
) -> None:
    _require_aware(draft.submitted_at, label=f"{draft.signal_id}.submitted_at")
    if draft.submitted_at < as_of:
        raise AdapterCompatibilityError(
            f"{draft.signal_id} was submitted before the requested as_of"
        )

    capabilities = manifest.capabilities
    if (
        capabilities.direction is DirectionCapability.REQUIRED
        and draft.direction is None
    ):
        raise AdapterCompatibilityError(
            f"{draft.signal_id} is missing its required direction"
        )
    if capabilities.direction is DirectionCapability.NONE and draft.direction is not None:
        raise AdapterCompatibilityError(
            f"{draft.signal_id} declares an unsupported direction"
        )

    if capabilities.probability is ProbabilityCapability.NONE:
        if draft.probabilities is not None or draft.direction_confidence is not None:
            raise AdapterCompatibilityError(
                f"{draft.signal_id} declares unsupported probability fields"
            )
    elif capabilities.probability is ProbabilityCapability.CONFIDENCE:
        if draft.direction_confidence is None or draft.probabilities is not None:
            raise AdapterCompatibilityError(
                f"{draft.signal_id} must provide confidence without a probability vector"
            )
    elif capabilities.probability is ProbabilityCapability.MULTICLASS:
        if not isinstance(draft.probabilities, SignalProbabilityVector):
            raise AdapterCompatibilityError(
                f"{draft.signal_id} must provide a complete probability vector"
            )
        if (
            draft.direction is not None
            and predicted_direction(draft.probabilities.as_dict()).value
            != draft.direction
        ):
            raise AdapterCompatibilityError(
                f"{draft.signal_id} direction conflicts with its probability vector"
            )

    if capabilities.reasoning is ReasoningCapability.STRUCTURED and not (
        draft.rationale
        and draft.counter_evidence
        and draft.invalidation_conditions
    ):
        raise AdapterCompatibilityError(
            f"{draft.signal_id} is missing structured reasoning"
        )
    if capabilities.reasoning is ReasoningCapability.NONE and (
        draft.rationale or draft.counter_evidence or draft.invalidation_conditions
    ):
        raise AdapterCompatibilityError(
            f"{draft.signal_id} declares unsupported reasoning fields"
        )

    if capabilities.citation is CitationCapability.FROZEN and not draft.citations:
        raise AdapterCompatibilityError(
            f"{draft.signal_id} is missing frozen citations"
        )
    if capabilities.citation is CitationCapability.NONE and draft.citations:
        raise AdapterCompatibilityError(
            f"{draft.signal_id} declares unsupported citations"
        )
    for citation in draft.citations:
        _require_https(citation.source_url, label=f"{draft.signal_id} citation")
        _require_aware(citation.observed_at, label=f"{draft.signal_id} citation")
        if citation.observed_at > as_of:
            raise AdapterCompatibilityError(
                f"{draft.signal_id} citation exceeds the requested evidence cutoff"
            )

    bundle_hash = draft.source_payload.get("source_bundle_hash")
    if not isinstance(bundle_hash, str) or not SHA256_RE.fullmatch(bundle_hash):
        raise AdapterCompatibilityError(
            f"{draft.signal_id} is missing its source bundle SHA-256"
        )
    data_license = draft.source_payload.get("data_license")
    if data_license != manifest.license.spdx_id:
        raise AdapterCompatibilityError(
            f"{draft.signal_id} data license does not match the adapter manifest"
        )
    source_url = draft.source_payload.get("source_url")
    if source_url != manifest.license.source_url:
        raise AdapterCompatibilityError(
            f"{draft.signal_id} source URL does not match the adapter manifest"
        )
    assert isinstance(source_url, str)
    _require_https(source_url, label=f"{draft.signal_id} source URL")


def _require_aware(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdapterCompatibilityError(f"{label} must be timezone-aware")


def _require_https(url: str, *, label: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AdapterCompatibilityError(f"{label} must be an HTTPS URL without credentials")
