from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.agent_contracts import contract_content_hash
from app.ports import AgentSignalSourceError, EvidenceSnapshotSourceError
from app.schemas import EvidenceItem, FrozenEvidenceSnapshot
from app.services.snapshot import canonical_hash, evidence_item_hash
from app.testing.adapter_compat import (
    AdapterCompatibilityError,
    assert_evidence_adapter_compatible,
    assert_fails_closed,
    assert_signal_provider_compatible,
)

from examples.adapters.public_json_evidence_adapter import (
    EXAMPLE_EVIDENCE_MANIFEST,
    PublicJsonEvidenceAdapter,
    _EvidenceBundleBody,
)
from examples.providers.public_json_signal_provider import (
    EXAMPLE_SIGNAL_MANIFEST,
    PublicJsonSignalProvider,
    _SignalBundleBody,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SIGNAL_FIXTURE = (
    REPOSITORY_ROOT / "examples/providers/data/public-signals.json"
)
EVIDENCE_FIXTURE = (
    REPOSITORY_ROOT / "examples/adapters/data/public-evidence.json"
)
AS_OF = datetime(2026, 7, 27, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _reseal_signal_bundle(payload: dict) -> None:
    body_payload = dict(payload)
    body_payload.pop("content_hash", None)
    body = _SignalBundleBody.model_validate(body_payload)
    payload["content_hash"] = contract_content_hash(body.model_dump(mode="json"))


def _reseal_evidence_bundle(
    payload: dict,
    *,
    reseal_item: bool = False,
    reseal_snapshot: bool = True,
) -> None:
    if reseal_item:
        item = EvidenceItem.model_validate(payload["snapshot"]["items"][0])
        payload["snapshot"]["items"][0]["content_hash"] = evidence_item_hash(item)
    if reseal_snapshot:
        snapshot = FrozenEvidenceSnapshot.model_validate(payload["snapshot"])
        payload["snapshot"]["content_hash"] = canonical_hash(
            snapshot.model_dump(mode="json", exclude={"content_hash"})
        )
    body_payload = dict(payload)
    body_payload.pop("content_hash", None)
    body = _EvidenceBundleBody.model_validate(body_payload)
    payload["content_hash"] = contract_content_hash(body.model_dump(mode="json"))


def test_official_signal_provider_passes_reusable_compatibility_kit() -> None:
    report = assert_signal_provider_compatible(
        PublicJsonSignalProvider(SIGNAL_FIXTURE),
        as_of=AS_OF,
        manifest=EXAMPLE_SIGNAL_MANIFEST,
    )

    assert report.adapter_id == "public-json-signal-provider"
    assert {
        "source",
        "time_semantics",
        "timezone",
        "missing_values",
        "license",
        "content_hash",
        "capabilities",
    }.issubset(report.checks)


def test_official_evidence_adapter_passes_reusable_compatibility_kit() -> None:
    report = assert_evidence_adapter_compatible(
        PublicJsonEvidenceAdapter(EVIDENCE_FIXTURE),
        as_of=AS_OF,
        manifest=EXAMPLE_EVIDENCE_MANIFEST,
    )

    assert report.adapter_id == "public-json-evidence-adapter"
    assert {
        "source",
        "time_semantics",
        "timezone",
        "missing_values",
        "license",
        "content_hash",
        "evidence_cutoff",
    }.issubset(report.checks)


@pytest.mark.parametrize(
    ("case_name", "mutate", "reseal"),
    [
        (
            "missing direction",
            lambda payload: payload["signals"][0].update({"direction": None}),
            True,
        ),
        (
            "naive submitted_at",
            lambda payload: payload["signals"][0].update(
                {"submitted_at": "2026-07-27T15:02:00"}
            ),
            False,
        ),
        (
            "missing license",
            lambda payload: payload.pop("license"),
            False,
        ),
        (
            "license forbids redistribution",
            lambda payload: payload["license"].update(
                {"redistribution_allowed": False}
            ),
            True,
        ),
        (
            "citation after cutoff",
            lambda payload: payload["signals"][0]["citations"][0].update(
                {"observed_at": "2026-07-27T15:01:00+08:00"}
            ),
            True,
        ),
        (
            "tampered content",
            lambda payload: payload["signals"][0].update(
                {"rationale": "Tampered without resealing."}
            ),
            False,
        ),
    ],
)
def test_signal_provider_fails_closed_for_contract_violations(
    tmp_path: Path,
    case_name: str,
    mutate: Callable[[dict], object],
    reseal: bool,
) -> None:
    payload = _load(SIGNAL_FIXTURE)
    mutate(payload)
    if reseal:
        _reseal_signal_bundle(payload)
    source = tmp_path / "signals.json"
    _write(source, payload)

    report = assert_fails_closed(
        lambda: PublicJsonSignalProvider(source).load_signal_drafts(as_of=AS_OF),
        expected_error=AgentSignalSourceError,
        label=case_name,
    )

    assert report.checks == ("fail_closed",)


def test_signal_provider_rejects_entire_batch_when_one_record_is_missing(
    tmp_path: Path,
) -> None:
    payload = _load(SIGNAL_FIXTURE)
    invalid = dict(payload["signals"][0])
    invalid["signal_id"] = "example-invalid-second-record"
    invalid["probabilities"] = None
    payload["signals"].append(invalid)
    _reseal_signal_bundle(payload)
    source = tmp_path / "signals.json"
    _write(source, payload)

    assert_fails_closed(
        lambda: PublicJsonSignalProvider(source).load_signal_drafts(as_of=AS_OF),
        expected_error=AgentSignalSourceError,
        label="all-or-nothing signal batch",
    )


def test_examples_fail_closed_for_unavailable_or_symlinked_sources(
    tmp_path: Path,
) -> None:
    assert_fails_closed(
        lambda: PublicJsonSignalProvider(
            tmp_path / "missing-signals.json"
        ).load_signal_drafts(as_of=AS_OF),
        expected_error=AgentSignalSourceError,
        label="unavailable signal source",
    )

    target = tmp_path / "evidence.json"
    target.write_bytes(EVIDENCE_FIXTURE.read_bytes())
    link = tmp_path / "evidence-link.json"
    link.symlink_to(target)
    assert_fails_closed(
        lambda: PublicJsonEvidenceAdapter(link).load_snapshot(as_of=AS_OF),
        expected_error=EvidenceSnapshotSourceError,
        label="symlinked evidence source",
    )


@pytest.mark.parametrize(
    ("case_name", "mutate", "reseal_item", "reseal_snapshot", "reseal_bundle"),
    [
        (
            "missing market value",
            lambda payload: payload["snapshot"]["market_data"].pop("000300.SH"),
            False,
            True,
            True,
        ),
        (
            "naive market time",
            lambda payload: payload["snapshot"]["market_data"]["000300.SH"].update(
                {"observed_at": "2026-07-27T14:50:00"}
            ),
            False,
            True,
            True,
        ),
        (
            "missing license",
            lambda payload: payload.pop("license"),
            False,
            False,
            False,
        ),
        (
            "license forbids redistribution",
            lambda payload: payload["license"].update(
                {"redistribution_allowed": False}
            ),
            False,
            True,
            True,
        ),
        (
            "evidence after cutoff",
            lambda payload: payload["snapshot"]["items"][0].update(
                {"ingested_at": "2026-07-27T15:01:00+08:00"}
            ),
            True,
            True,
            True,
        ),
        (
            "tampered snapshot hash",
            lambda payload: payload["snapshot"]["volatility_20d"].update(
                {"000300.SH": 0.012}
            ),
            False,
            False,
            True,
        ),
        (
            "tampered wrapper hash",
            lambda payload: payload.update(
                {"source_name": "Tampered without resealing"}
            ),
            False,
            False,
            False,
        ),
    ],
)
def test_evidence_adapter_fails_closed_for_contract_violations(
    tmp_path: Path,
    case_name: str,
    mutate: Callable[[dict], object],
    reseal_item: bool,
    reseal_snapshot: bool,
    reseal_bundle: bool,
) -> None:
    payload = _load(EVIDENCE_FIXTURE)
    mutate(payload)
    if reseal_bundle:
        _reseal_evidence_bundle(
            payload,
            reseal_item=reseal_item,
            reseal_snapshot=reseal_snapshot,
        )
    source = tmp_path / "evidence.json"
    _write(source, payload)

    report = assert_fails_closed(
        lambda: PublicJsonEvidenceAdapter(source).load_snapshot(as_of=AS_OF),
        expected_error=EvidenceSnapshotSourceError,
        label=case_name,
    )

    assert report.checks == ("fail_closed",)


def test_examples_are_read_only_and_require_no_secret_or_personal_path(
    tmp_path: Path,
) -> None:
    signal_copy = tmp_path / "signals.json"
    evidence_copy = tmp_path / "evidence.json"
    signal_copy.write_bytes(SIGNAL_FIXTURE.read_bytes())
    evidence_copy.write_bytes(EVIDENCE_FIXTURE.read_bytes())
    before = {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    }

    PublicJsonSignalProvider(signal_copy).load_signal_drafts(as_of=AS_OF)
    PublicJsonEvidenceAdapter(evidence_copy).load_snapshot(as_of=AS_OF)

    after = {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    }
    assert after == before
    assert EXAMPLE_SIGNAL_MANIFEST.writes == ()
    assert EXAMPLE_EVIDENCE_MANIFEST.writes == ()
    assert "key" not in json.dumps(_load(SIGNAL_FIXTURE)).lower()
    assert "/Users/" not in SIGNAL_FIXTURE.read_text(encoding="utf-8")
    assert "/Users/" not in EVIDENCE_FIXTURE.read_text(encoding="utf-8")


def test_compatibility_kit_detects_a_declared_capability_mismatch() -> None:
    class MissingProbabilitySource:
        def load_signal_drafts(self, *, as_of: datetime):
            draft = PublicJsonSignalProvider(SIGNAL_FIXTURE).load_signal_drafts(
                as_of=as_of
            )[0]
            return (draft.model_copy(update={"probabilities": None}),)

    with pytest.raises(
        AdapterCompatibilityError,
        match="complete probability vector",
    ):
        assert_signal_provider_compatible(
            MissingProbabilitySource(),
            as_of=AS_OF,
            manifest=EXAMPLE_SIGNAL_MANIFEST,
        )
