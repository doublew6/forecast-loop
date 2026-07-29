"""Export and verify portable User Judgment bundles without mutating run artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..agent_contracts import (
    AgentSpec,
    EvaluationMetric,
    ParticipationMode,
    ProbabilityMode,
    ReasoningMode,
    verify_agent_spec_hash,
)
from ..db import Database
from ..domain import AgentSourceType
from ..models import (
    AgentSpecRecord,
    Forecast,
    UserJudgment,
    UserJudgmentEvaluation,
)
from .run_bundle import SUPPORTED_RUN_BUNDLE_SCHEMAS
from .user_judgment import (
    forecast_market_zone,
    verify_user_judgment,
    verify_user_judgment_evaluation_record,
)
from .user_judgment_markdown import UserJudgmentWikiError

JUDGMENT_BUNDLE_SCHEMA = "forecast-loop.judgment-bundle/v1"
JUDGMENT_ARTIFACT_SCHEMA = "forecast-loop.user-judgment-export/v1"
FORECAST_BINDING_SCHEMA = "forecast-loop.forecast-binding/v1"
JUDGMENT_EVALUATION_SCHEMA = "forecast-loop.judgment-evaluation-export/v1"
SOURCE_EVALUATION_SCHEMA = "vericouncil.user-judgment-evaluation/v1"

MANIFEST_NAME = "manifest.json"
AGENT_SPEC_NAME = "agent-spec.json"
FORECAST_NAME = "forecast.json"
JUDGMENT_NAME = "judgment.json"
EVALUATION_NAME = "evaluation.json"
ARTIFACT_NAMES = (
    AGENT_SPEC_NAME,
    FORECAST_NAME,
    JUDGMENT_NAME,
    EVALUATION_NAME,
)

HASH_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024

JudgmentClass = Literal["demo", "non_blind_archive", "formal_shadow"]
EvaluationStatus = Literal["not_applicable", "completed"]


class JudgmentBundleError(ValueError):
    """A judgment bundle failed an export, integrity, or safety check."""


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class JudgmentBundleArtifact(ContractModel):
    path: str
    media_type: Literal["application/json"] = "application/json"
    sha256: str = Field(pattern=HASH_PATTERN)
    size: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)


class ForecastBindingArtifact(ContractModel):
    """Minimal committee Forecast identity required to reject cross-record mixing."""

    schema_version: Literal["forecast-loop.forecast-binding/v1"] = (
        FORECAST_BINDING_SCHEMA
    )
    forecast_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    mode: Literal["demo", "live"]
    run_status: Literal["completed"]
    run_input_hash: str = Field(pattern=HASH_PATTERN)
    forecast_input_hash: str = Field(pattern=HASH_PATTERN)
    index_code: str = Field(min_length=1, max_length=32)
    index_name: str = Field(min_length=1, max_length=120)
    horizon: Literal["D1", "D2"]
    base_trade_date: date
    target_date: date
    as_of: datetime
    data_cutoff: datetime
    committee_direction: Literal["up", "neutral", "down"]
    committee_abstain: bool
    outcome_status: Literal["pending", "completed"]
    outcome_observation_hash: str | None = Field(
        default=None,
        pattern=HASH_PATTERN,
    )

    @field_validator("as_of", "data_cutoff")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)

    @field_validator("outcome_observation_hash")
    @classmethod
    def outcome_status_is_consistent(
        cls,
        value: str | None,
        info: Any,
    ) -> str | None:
        status = info.data.get("outcome_status")
        if (status == "completed") != (value is not None):
            raise ValueError(
                "completed Forecast outcomes require an observation hash"
            )
        return value


class JudgmentArtifact(ContractModel):
    """Privacy-minimized portable projection of one immutable judgment."""

    schema_version: Literal["forecast-loop.user-judgment-export/v1"] = (
        JUDGMENT_ARTIFACT_SCHEMA
    )
    judgment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    record_class: JudgmentClass
    mode: Literal["demo", "live"]
    agent_id: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(min_length=1, max_length=32)
    agent_spec_hash: str = Field(pattern=HASH_PATTERN)
    forecast_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    run_input_hash: str = Field(pattern=HASH_PATTERN)
    forecast_input_hash: str = Field(pattern=HASH_PATTERN)
    index_code: str = Field(min_length=1, max_length=32)
    horizon: Literal["D1", "D2"]
    target_date: date
    direction: Literal["up", "down"]
    confidence_hex: str = Field(min_length=3, max_length=32)
    rationale: str = Field(min_length=20, max_length=4000)
    counter_evidence: str = Field(min_length=10, max_length=2000)
    invalidation_condition: str = Field(min_length=10, max_length=2000)
    blind_attestation: bool
    submitted_at: datetime
    submission_deadline: datetime | None = None
    formal_score_eligible: bool
    policy_version: str = Field(min_length=1, max_length=64)
    source_content_hash: str | None = Field(default=None, pattern=HASH_PATTERN)
    source_wiki_artifact_hash: str | None = Field(
        default=None,
        pattern=HASH_PATTERN,
    )
    actor_id: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("submitted_at", "submission_deadline")
    @classmethod
    def timestamp_is_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return None if value is None else _aware_datetime(value)

    @field_validator("confidence_hex")
    @classmethod
    def confidence_is_valid(cls, value: str) -> str:
        try:
            confidence = float.fromhex(value)
        except ValueError as exc:
            raise ValueError("confidence_hex is not a Python float hex value") from exc
        if not math.isfinite(confidence) or not 0.5 <= confidence <= 1:
            raise ValueError("confidence_hex must encode a value between 0.5 and 1")
        return value


class SourceJudgmentEvaluation(ContractModel):
    """The exact source payload protected by UserJudgmentEvaluation.content_hash."""

    source_schema: Literal["vericouncil.user-judgment-evaluation/v1"] = Field(
        default=SOURCE_EVALUATION_SCHEMA,
        alias="schema",
        serialization_alias="schema",
    )
    id: str = Field(min_length=1, max_length=64)
    user_judgment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    user_judgment_content_hash: str | None = Field(
        default=None,
        pattern=HASH_PATTERN,
    )
    forecast_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    run_input_hash: str = Field(pattern=HASH_PATTERN)
    forecast_input_hash: str = Field(pattern=HASH_PATTERN)
    batch_id: str = Field(min_length=1, max_length=64)
    batch_evaluation_set_hash: str = Field(pattern=HASH_PATTERN)
    batch_source_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_result_id: str = Field(min_length=1, max_length=64)
    actual_return_hex: str = Field(min_length=3, max_length=32)
    actual_label: Literal["up", "neutral", "down"]
    sign_correct: bool | None
    material_direction_correct: bool | None
    observation_hash: str = Field(pattern=HASH_PATTERN)
    policy_version: str = Field(min_length=1, max_length=64)
    evaluated_at: str = Field(min_length=1, max_length=64)

    @field_validator("actual_return_hex")
    @classmethod
    def actual_return_is_finite(cls, value: str) -> str:
        try:
            actual_return = float.fromhex(value)
        except ValueError as exc:
            raise ValueError("actual_return_hex is invalid") from exc
        if not math.isfinite(actual_return):
            raise ValueError("actual_return_hex must encode a finite value")
        return value

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_aware(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("evaluated_at must be ISO-8601") from exc
        _aware_datetime(parsed)
        return value


class JudgmentEvaluationArtifact(ContractModel):
    schema_version: Literal[
        "forecast-loop.judgment-evaluation-export/v1"
    ] = JUDGMENT_EVALUATION_SCHEMA
    status: EvaluationStatus
    not_applicable_reason: Literal["demo", "non_blind_archive"] | None = None
    source: SourceJudgmentEvaluation | None = None
    source_content_hash: str | None = Field(default=None, pattern=HASH_PATTERN)


class JudgmentBundleManifest(ContractModel):
    schema_version: Literal["forecast-loop.judgment-bundle/v1"] = (
        JUDGMENT_BUNDLE_SCHEMA
    )
    judgment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    record_class: JudgmentClass
    mode: Literal["demo", "live"]
    forecast_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    agent_spec_hash: str = Field(pattern=HASH_PATTERN)
    actor_privacy: Literal["omitted", "included"]
    evaluation_status: EvaluationStatus
    exported_at: datetime
    artifacts: tuple[JudgmentBundleArtifact, ...]
    manifest_hash: str = Field(pattern=HASH_PATTERN)
    bundle_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("exported_at")
    @classmethod
    def exported_at_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)


def export_judgment_bundle(
    database: Database,
    *,
    judgment_id: str,
    output_root: Path,
    wiki_root: Path,
    timezone: str,
    include_actor_id: bool = False,
    exported_at: datetime | None = None,
) -> Path:
    """Export one verified judgment without changing its Forecast or run bundle."""

    normalized_id = _safe_judgment_id(judgment_id)
    with database.session_factory() as session:
        row = session.scalar(
            select(UserJudgment)
            .options(
                selectinload(UserJudgment.forecast).selectinload(Forecast.run),
                selectinload(UserJudgment.forecast).selectinload(
                    Forecast.evaluation
                ),
                selectinload(UserJudgment.evaluation).selectinload(
                    UserJudgmentEvaluation.batch
                ),
                selectinload(UserJudgment.evaluation).selectinload(
                    UserJudgmentEvaluation.evaluation_result
                ),
            )
            .where(UserJudgment.id == normalized_id)
        )
        if row is None:
            raise JudgmentBundleError(f"User Judgment not found: {normalized_id}")
        zone = forecast_market_zone(
            row.forecast,
            fallback_timezone=timezone,
        )
        try:
            verify_user_judgment(
                row,
                wiki_root=wiki_root,
                timezone=timezone,
            )
        except UserJudgmentWikiError as exc:
            raise JudgmentBundleError(
                f"User Judgment source verification failed: {exc}"
            ) from exc

        record_class = _record_class(row)
        specification = _agent_spec_for(session, row)
        payloads = _bundle_payloads(
            row,
            record_class=record_class,
            specification=specification,
            zone=zone,
            include_actor_id=include_actor_id,
        )
        manifest_fields = {
            "schema_version": JUDGMENT_BUNDLE_SCHEMA,
            "judgment_id": row.id,
            "record_class": record_class,
            "mode": row.mode,
            "forecast_id": row.forecast_id,
            "run_id": row.run_id,
            "agent_spec_hash": specification.content_hash,
            "actor_privacy": "included" if include_actor_id else "omitted",
            "evaluation_status": payloads[EVALUATION_NAME]["status"],
            "exported_at": _aware_utc(exported_at),
        }

    root = _prepare_output_root(output_root)
    destination = root / normalized_id
    if destination.exists() or destination.is_symlink():
        raise JudgmentBundleError(
            f"Judgment bundle destination already exists: {destination}"
        )
    temporary = root / f".{normalized_id}.{uuid4().hex}.tmp"
    try:
        temporary.mkdir(mode=0o700)
        artifacts: list[JudgmentBundleArtifact] = []
        for name in ARTIFACT_NAMES:
            body = _canonical_json_bytes(payloads[name])
            _write_new_file(temporary / name, body)
            artifacts.append(
                JudgmentBundleArtifact(
                    path=name,
                    sha256=_sha256(body),
                    size=len(body),
                )
            )
        manifest = _seal_manifest(
            {
                **manifest_fields,
                "artifacts": artifacts,
            }
        )
        _write_new_file(
            temporary / MANIFEST_NAME,
            _canonical_json_bytes(manifest.model_dump(mode="json")),
        )
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        raise
    return destination


def verify_judgment_bundle(bundle_path: Path) -> JudgmentBundleManifest:
    """Verify membership, file hashes, schemas, privacy, and cross-file bindings."""

    bundle = _resolve_bundle_directory(bundle_path)
    manifest_payload = _read_json_file(
        bundle / MANIFEST_NAME,
        max_bytes=MAX_MANIFEST_BYTES,
        label="Judgment bundle manifest",
    )
    try:
        manifest = JudgmentBundleManifest.model_validate(manifest_payload)
    except ValidationError as exc:
        raise JudgmentBundleError(
            f"Invalid judgment bundle manifest: {exc}"
        ) from exc
    if bundle.name != manifest.judgment_id:
        raise JudgmentBundleError(
            "Judgment bundle directory name does not match judgment_id"
        )
    artifact_paths = tuple(item.path for item in manifest.artifacts)
    if artifact_paths != ARTIFACT_NAMES:
        raise JudgmentBundleError(
            "Judgment bundle artifact list must be exactly: "
            + ", ".join(ARTIFACT_NAMES)
        )
    expected_members = {MANIFEST_NAME, *ARTIFACT_NAMES}
    if {item.name for item in bundle.iterdir()} != expected_members:
        raise JudgmentBundleError(
            "Judgment bundle contains missing or unexpected files"
        )

    payloads: dict[str, Any] = {}
    for artifact in manifest.artifacts:
        relative = PurePosixPath(artifact.path)
        if relative.is_absolute() or len(relative.parts) != 1:
            raise JudgmentBundleError(
                f"Unsafe judgment bundle artifact path: {artifact.path}"
            )
        body = _read_bounded_regular_file(
            bundle / artifact.path,
            max_bytes=MAX_ARTIFACT_BYTES,
            expected_size=artifact.size,
            label=f"Judgment bundle artifact {artifact.path}",
        )
        if _sha256(body) != artifact.sha256:
            raise JudgmentBundleError(
                f"Judgment bundle artifact hash mismatch: {artifact.path}"
            )
        payloads[artifact.path] = _parse_json(
            body,
            label=f"Judgment bundle artifact {artifact.path}",
        )

    _verify_manifest_hashes(manifest)
    _validate_payloads(payloads, manifest=manifest)
    return manifest


def _bundle_payloads(
    row: UserJudgment,
    *,
    record_class: JudgmentClass,
    specification: AgentSpec,
    zone: ZoneInfo,
    include_actor_id: bool,
) -> dict[str, Any]:
    forecast = row.forecast
    run = forecast.run
    if run.status != "completed":
        raise JudgmentBundleError("Judgment bundle requires a completed committee run")
    outcome = forecast.evaluation
    forecast_payload = ForecastBindingArtifact(
        forecast_id=forecast.id,
        run_id=forecast.run_id,
        mode=run.mode,
        run_status=run.status,
        run_input_hash=run.input_hash,
        forecast_input_hash=forecast.input_hash,
        index_code=forecast.index_code,
        index_name=forecast.index_name,
        horizon=forecast.horizon,
        base_trade_date=forecast.base_trade_date,
        target_date=forecast.target_date,
        as_of=_aware_in_zone(forecast.as_of, zone),
        data_cutoff=_aware_in_zone(forecast.data_cutoff, zone),
        committee_direction=forecast.direction,
        committee_abstain=forecast.abstain,
        outcome_status="completed" if outcome is not None else "pending",
        outcome_observation_hash=(
            outcome.observation_hash if outcome is not None else None
        ),
    )
    judgment_payload = JudgmentArtifact(
        judgment_id=row.id,
        record_class=record_class,
        mode=row.mode,
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        agent_spec_hash=specification.content_hash,
        forecast_id=row.forecast_id,
        run_id=row.run_id,
        run_input_hash=row.run_input_hash,
        forecast_input_hash=row.forecast_input_hash,
        index_code=row.index_code,
        horizon=row.horizon,
        target_date=row.target_date,
        direction=row.direction,
        confidence_hex=row.confidence.hex(),
        rationale=row.rationale,
        counter_evidence=row.counter_evidence,
        invalidation_condition=row.invalidation_condition,
        blind_attestation=row.blind_attestation,
        submitted_at=_aware_in_zone(row.submitted_at, zone),
        submission_deadline=(
            _aware_in_zone(row.submission_deadline, zone)
            if row.submission_deadline is not None
            else None
        ),
        formal_score_eligible=row.formal_score_eligible,
        policy_version=row.policy_version,
        actor_id=row.actor_id if include_actor_id else None,
    )
    evaluation_payload = _evaluation_payload(row, record_class=record_class)
    return {
        AGENT_SPEC_NAME: specification.model_dump(mode="json"),
        FORECAST_NAME: forecast_payload.model_dump(mode="json"),
        JUDGMENT_NAME: judgment_payload.model_dump(
            mode="json",
            exclude_none=True,
        ),
        EVALUATION_NAME: evaluation_payload.model_dump(
            mode="json",
            exclude_none=True,
            by_alias=True,
        ),
    }


def _evaluation_payload(
    row: UserJudgment,
    *,
    record_class: JudgmentClass,
) -> JudgmentEvaluationArtifact:
    if record_class != "formal_shadow":
        if row.evaluation is not None:
            raise JudgmentBundleError(
                "Demo and non-blind archives may not contain a formal evaluation"
            )
        return JudgmentEvaluationArtifact(
            status="not_applicable",
            not_applicable_reason=record_class,
        )
    if row.evaluation is None or row.forecast.evaluation is None:
        raise JudgmentBundleError(
            "Formal shadow judgments require a completed trusted evaluation"
        )
    try:
        source = verify_user_judgment_evaluation_record(
            row.evaluation,
            row,
        )
    except UserJudgmentWikiError as exc:
        raise JudgmentBundleError(
            f"User Judgment evaluation verification failed: {exc}"
        ) from exc
    source.pop("user_judgment_content_hash", None)
    return JudgmentEvaluationArtifact(
        status="completed",
        source=SourceJudgmentEvaluation.model_validate(source),
    )


def _validate_payloads(
    payloads: dict[str, Any],
    *,
    manifest: JudgmentBundleManifest,
) -> None:
    try:
        specification = AgentSpec.model_validate(payloads[AGENT_SPEC_NAME])
        forecast = ForecastBindingArtifact.model_validate(payloads[FORECAST_NAME])
        judgment = JudgmentArtifact.model_validate(payloads[JUDGMENT_NAME])
        evaluation = JudgmentEvaluationArtifact.model_validate(
            payloads[EVALUATION_NAME]
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise JudgmentBundleError(
            f"Judgment bundle payload schema validation failed: {exc}"
        ) from exc

    expected_manifest = {
        "judgment_id": judgment.judgment_id,
        "record_class": judgment.record_class,
        "mode": judgment.mode,
        "forecast_id": judgment.forecast_id,
        "run_id": judgment.run_id,
        "agent_spec_hash": judgment.agent_spec_hash,
        "actor_privacy": "included" if judgment.actor_id is not None else "omitted",
        "evaluation_status": evaluation.status,
    }
    mismatches = [
        field
        for field, expected in expected_manifest.items()
        if getattr(manifest, field) != expected
    ]
    if mismatches:
        raise JudgmentBundleError(
            "Judgment bundle manifest does not match artifacts: "
            + ", ".join(sorted(mismatches))
        )

    _validate_record_class(judgment)
    _validate_agent_spec(specification, judgment=judgment)
    _validate_forecast_binding(forecast, judgment=judgment)
    _validate_evaluation(
        evaluation,
        judgment=judgment,
        forecast=forecast,
    )


def _validate_record_class(judgment: JudgmentArtifact) -> None:
    expected = (
        "demo"
        if judgment.mode == "demo"
        else (
            "formal_shadow"
            if judgment.formal_score_eligible
            else "non_blind_archive"
        )
    )
    if judgment.record_class != expected:
        raise JudgmentBundleError(
            "Judgment record class does not match mode and score eligibility"
        )
    if judgment.record_class == "demo":
        if judgment.formal_score_eligible or judgment.submission_deadline is not None:
            raise JudgmentBundleError("Demo judgments cannot be formally score eligible")
    elif judgment.record_class == "non_blind_archive":
        if judgment.blind_attestation or judgment.formal_score_eligible:
            raise JudgmentBundleError(
                "Non-blind archives cannot claim blind or formal eligibility"
            )
        if judgment.submission_deadline is None:
            raise JudgmentBundleError(
                "Live non-blind archives require a submission deadline"
            )
    else:
        if (
            not judgment.blind_attestation
            or not judgment.formal_score_eligible
            or judgment.submission_deadline is None
            or judgment.submitted_at >= judgment.submission_deadline
        ):
            raise JudgmentBundleError(
                "Formal shadow identity is inconsistent or missed its deadline"
            )


def _validate_agent_spec(
    specification: AgentSpec,
    *,
    judgment: JudgmentArtifact,
) -> None:
    if (
        specification.agent_id != judgment.agent_id
        or specification.agent_version != judgment.agent_version
        or specification.content_hash != judgment.agent_spec_hash
    ):
        raise JudgmentBundleError("AgentSpec does not match the judgment identity")
    if (
        specification.source_type is not AgentSourceType.MANUAL
        or specification.participation.mode is not ParticipationMode.SHADOW
        or specification.capabilities.probability_mode is not ProbabilityMode.CONFIDENCE
        or specification.capabilities.reasoning_mode is not ReasoningMode.STRUCTURED
        or not specification.capabilities.direction
        or not specification.capabilities.supports_blind_submission
        or EvaluationMetric.DIRECTION
        not in specification.participation.evaluation_metrics
    ):
        raise JudgmentBundleError(
            "User Judgment AgentSpec lacks required shadow capabilities"
        )


def _validate_forecast_binding(
    forecast: ForecastBindingArtifact,
    *,
    judgment: JudgmentArtifact,
) -> None:
    comparisons = {
        "forecast_id": forecast.forecast_id,
        "run_id": forecast.run_id,
        "mode": forecast.mode,
        "run_input_hash": forecast.run_input_hash,
        "forecast_input_hash": forecast.forecast_input_hash,
        "index_code": forecast.index_code,
        "horizon": forecast.horizon,
        "target_date": forecast.target_date,
    }
    mismatches = [
        field
        for field, actual in comparisons.items()
        if getattr(judgment, field) != actual
    ]
    if mismatches:
        raise JudgmentBundleError(
            "Forecast binding does not match the judgment: "
            + ", ".join(sorted(mismatches))
        )


def _validate_evaluation(
    evaluation: JudgmentEvaluationArtifact,
    *,
    judgment: JudgmentArtifact,
    forecast: ForecastBindingArtifact,
) -> None:
    if judgment.record_class != "formal_shadow":
        if (
            evaluation.status != "not_applicable"
            or evaluation.not_applicable_reason != judgment.record_class
            or evaluation.source is not None
        ):
            raise JudgmentBundleError(
                "Demo and non-blind archives require an explicit "
                "not-applicable evaluation"
            )
        return
    if (
        evaluation.status != "completed"
        or evaluation.not_applicable_reason is not None
        or evaluation.source is None
    ):
        raise JudgmentBundleError(
            "Formal shadow judgment evaluation is incomplete"
        )
    source = evaluation.source
    legacy_hashes = (
        judgment.source_content_hash,
        source.user_judgment_content_hash,
        evaluation.source_content_hash,
    )
    if any(value is not None for value in legacy_hashes):
        if any(value is None for value in legacy_hashes):
            raise JudgmentBundleError(
                "Legacy source hashes must be present or omitted together"
            )
        if (
            source.user_judgment_content_hash
            != judgment.source_content_hash
            or _source_content_hash(source) != evaluation.source_content_hash
        ):
            raise JudgmentBundleError(
                "Legacy judgment evaluation source hash mismatch"
            )
    expected = {
        "user_judgment_id": judgment.judgment_id,
        "forecast_id": judgment.forecast_id,
        "run_id": judgment.run_id,
        "run_input_hash": judgment.run_input_hash,
        "forecast_input_hash": judgment.forecast_input_hash,
        "observation_hash": forecast.outcome_observation_hash,
    }
    mismatches = [
        field
        for field, value in expected.items()
        if getattr(source, field) != value
    ]
    if mismatches:
        raise JudgmentBundleError(
            "Judgment evaluation does not match Forecast or judgment: "
            + ", ".join(sorted(mismatches))
        )
    if forecast.outcome_status != "completed":
        raise JudgmentBundleError(
            "Formal shadow judgment Forecast evaluation is incomplete"
        )
    actual_return = float.fromhex(source.actual_return_hex)
    expected_sign = (
        None
        if actual_return == 0
        else (
            judgment.direction == ("up" if actual_return > 0 else "down")
        )
    )
    if source.sign_correct != expected_sign:
        raise JudgmentBundleError(
            "Judgment evaluation sign result is inconsistent"
        )
    if (
        source.material_direction_correct is not None
        and source.material_direction_correct != source.sign_correct
    ):
        raise JudgmentBundleError(
            "Judgment evaluation material result is inconsistent"
        )


def _record_class(row: UserJudgment) -> JudgmentClass:
    if row.mode == "demo":
        return "demo"
    if row.mode != "live":
        raise JudgmentBundleError(f"Unsupported User Judgment mode: {row.mode}")
    return "formal_shadow" if row.formal_score_eligible else "non_blind_archive"


def _agent_spec_for(session: Session, row: UserJudgment) -> AgentSpec:
    if row.agent_spec_hash is not None:
        record = session.get(AgentSpecRecord, row.agent_spec_hash)
        records = [] if record is None else [record]
    else:
        records = session.scalars(
            select(AgentSpecRecord).where(
                AgentSpecRecord.agent_id == row.agent_id,
                AgentSpecRecord.agent_version == row.agent_version,
            )
        ).all()
    if not records:
        raise JudgmentBundleError(
            "No frozen AgentSpec is stored for this judgment"
        )
    if len(records) != 1:
        raise JudgmentBundleError(
            "The historical AgentSpec is ambiguous for this judgment"
        )
    record = records[0]
    try:
        specification = AgentSpec.model_validate(record.spec)
        verify_agent_spec_hash(specification)
    except (ValidationError, ValueError) as exc:
        raise JudgmentBundleError(
            "The frozen AgentSpec record failed validation"
        ) from exc
    if (
        specification.content_hash != record.content_hash
        or specification.agent_id != record.agent_id
        or specification.agent_version != record.agent_version
        or specification.agent_id != row.agent_id
        or specification.agent_version != row.agent_version
        or (
            row.agent_spec_hash is not None
            and specification.content_hash != row.agent_spec_hash
        )
    ):
        raise JudgmentBundleError(
            "The frozen AgentSpec does not match the judgment identity"
        )
    return specification


def _source_content_hash(source: SourceJudgmentEvaluation) -> str:
    body = json.dumps(
        source.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(body)


def _seal_manifest(fields: dict[str, Any]) -> JudgmentBundleManifest:
    provisional = JudgmentBundleManifest.model_validate(
        {
            **fields,
            "manifest_hash": "0" * 64,
            "bundle_hash": "0" * 64,
        }
    )
    unsigned = provisional.model_dump(
        mode="json",
        exclude={"manifest_hash", "bundle_hash"},
    )
    manifest_hash = _canonical_hash(unsigned)
    bundle_hash = _canonical_hash(
        {
            "schema_version": JUDGMENT_BUNDLE_SCHEMA,
            "manifest_hash": manifest_hash,
            "artifacts": [
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                }
                for item in unsigned["artifacts"]
            ],
        }
    )
    return JudgmentBundleManifest.model_validate(
        {
            **unsigned,
            "manifest_hash": manifest_hash,
            "bundle_hash": bundle_hash,
        }
    )


def _verify_manifest_hashes(manifest: JudgmentBundleManifest) -> None:
    unsigned = manifest.model_dump(
        mode="json",
        exclude={"manifest_hash", "bundle_hash"},
    )
    if _canonical_hash(unsigned) != manifest.manifest_hash:
        raise JudgmentBundleError("Judgment bundle manifest hash mismatch")
    bundle_hash = _canonical_hash(
        {
            "schema_version": manifest.schema_version,
            "manifest_hash": manifest.manifest_hash,
            "artifacts": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in manifest.artifacts
            ],
        }
    )
    if bundle_hash != manifest.bundle_hash:
        raise JudgmentBundleError("Judgment bundle overall hash mismatch")


def _prepare_output_root(output_root: Path) -> Path:
    candidate = Path(os.path.abspath(output_root.expanduser()))
    _reject_existing_symlink_components(candidate)
    _reject_committee_bundle_ancestor(candidate)
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    if candidate.is_symlink() or not candidate.is_dir():
        raise JudgmentBundleError(
            f"Judgment bundle output root is not a real directory: {candidate}"
        )
    resolved = candidate.resolve()
    if stat.S_IMODE(resolved.stat().st_mode) & 0o022:
        raise JudgmentBundleError(
            "Judgment bundle output root must not be group/world writable"
        )
    _reject_committee_bundle_ancestor(resolved)
    return resolved


def _reject_committee_bundle_ancestor(path: Path) -> None:
    for ancestor in (path, *path.parents):
        manifest = ancestor / MANIFEST_NAME
        if not manifest.exists() and not manifest.is_symlink():
            continue
        if manifest.is_symlink() or not manifest.is_file():
            raise JudgmentBundleError(
                "Judgment output path may not traverse another bundle manifest"
            )
        try:
            payload = _read_json_file(
                manifest,
                max_bytes=MAX_MANIFEST_BYTES,
                label="Ancestor bundle manifest",
            )
        except JudgmentBundleError:
            continue
        if payload.get("schema_version") in SUPPORTED_RUN_BUNDLE_SCHEMAS:
            raise JudgmentBundleError(
                "Judgment bundle output may not be inside a committee run bundle"
            )


def _resolve_bundle_directory(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise JudgmentBundleError(
            f"Judgment bundle may not be a symlink: {candidate}"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise JudgmentBundleError(
            f"Judgment bundle does not exist: {candidate}"
        ) from exc
    if not resolved.is_dir():
        raise JudgmentBundleError(
            f"Judgment bundle must be a directory: {candidate}"
        )
    return resolved


def _reject_existing_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise JudgmentBundleError(
                f"Judgment output path may not traverse a symlink: {current}"
            )
        if not current.exists():
            break


def _write_new_file(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _read_json_file(path: Path, *, max_bytes: int, label: str) -> dict[str, Any]:
    payload = _parse_json(
        _read_bounded_regular_file(path, max_bytes=max_bytes, label=label),
        label=label,
    )
    if not isinstance(payload, dict):
        raise JudgmentBundleError(f"{label} must contain a JSON object")
    return payload


def _read_bounded_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    expected_size: int | None = None,
) -> bytes:
    if path.is_symlink():
        raise JudgmentBundleError(f"{label} may not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os,
        "O_NOFOLLOW",
        0,
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise JudgmentBundleError(
            f"{label} cannot be opened safely: {path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise JudgmentBundleError(f"{label} must be a regular file: {path}")
        if metadata.st_size > max_bytes:
            raise JudgmentBundleError(f"{label} exceeds the size limit")
        if expected_size is not None and metadata.st_size != expected_size:
            raise JudgmentBundleError(f"{label} hash mismatch")
        body = bytearray()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            body.extend(chunk)
            remaining -= len(chunk)
        if remaining:
            raise JudgmentBundleError(f"{label} changed while being read")
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise JudgmentBundleError(f"{label} changed while being read")
        return bytes(body)
    except OSError as exc:
        raise JudgmentBundleError(f"{label} could not be read: {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _parse_json(body: bytes, *, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise JudgmentBundleError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise JudgmentBundleError(f"{label} contains non-finite JSON value: {value}")

    try:
        return json.loads(
            body,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JudgmentBundleError(f"{label} is not valid JSON") from exc


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        serialized = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise JudgmentBundleError(
            f"Judgment bundle value is not canonical JSON: {exc}"
        ) from exc
    return (serialized + "\n").encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return _sha256(_canonical_json_bytes(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


def _aware_in_zone(value: datetime, zone: ZoneInfo) -> datetime:
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise JudgmentBundleError("exported_at must include a timezone")
    return resolved.astimezone(UTC)


def _safe_judgment_id(value: str) -> str:
    if re.fullmatch(IDENTIFIER_PATTERN, value) is None:
        raise JudgmentBundleError(
            "judgment_id must be a path-safe 1-64 character identifier"
        )
    return value
