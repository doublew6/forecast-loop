"""Public test helpers for third-party forecast-loop integrations."""

from .adapter_compat import (
    FROZEN_EVIDENCE_SNAPSHOT_CONTRACT,
    AdapterCapabilities,
    AdapterCompatibilityError,
    AdapterKind,
    AdapterLicense,
    AdapterManifest,
    CitationCapability,
    CompatibilityReport,
    DirectionCapability,
    ProbabilityCapability,
    ReasoningCapability,
    assert_evidence_adapter_compatible,
    assert_fails_closed,
    assert_public_example_manifest,
    assert_signal_provider_compatible,
)

__all__ = [
    "FROZEN_EVIDENCE_SNAPSHOT_CONTRACT",
    "AdapterCapabilities",
    "AdapterCompatibilityError",
    "AdapterKind",
    "AdapterLicense",
    "AdapterManifest",
    "CitationCapability",
    "CompatibilityReport",
    "DirectionCapability",
    "ProbabilityCapability",
    "ReasoningCapability",
    "assert_evidence_adapter_compatible",
    "assert_fails_closed",
    "assert_public_example_manifest",
    "assert_signal_provider_compatible",
]
