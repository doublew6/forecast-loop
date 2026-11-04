"""Read-only, allowlisted publication of finalized focused-v2 forecasts.

No research prose, source URLs, model configuration, or operator identities cross
this boundary. Publication never starts a prediction or edits source records.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from .models import ForecastV2, ResearchRunV2
from .research_v2 import (
    DEFAULT_RESEARCH_PROGRAM_V2,
    AgentSignalEnvelopeV2,
    EvidenceSnapshotV2,
    OutcomeObservationV2,
    ProbabilitiesV2,
    ResearchProgramV2,
    classify_outcome,
    content_hash,
    multiclass_brier,
    realized_outcome,
)
from .services.research_v2 import (
    ReceiptV2,
    _validate_live_outcome_sources,
    _validate_live_snapshot_sources,
)

SCHEMA = "forecast-loop.public-showcase/v1"
TARGETS = {item.target_id: item for item in DEFAULT_RESEARCH_PROGRAM_V2.decision_targets}


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PublicResult(PublicModel):
    actual_return: float = Field(allow_inf_nan=False)
    actual_label: Literal["up", "neutral", "down"]
    brier_score: float = Field(ge=0, le=2 / 3, allow_inf_nan=False)
    direction_correct: bool


class PublicForecast(PublicModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: Literal["csi1000-absolute-d1", "csi1000-vs-csi300-relative-w1"]
    lane: Literal["formal", "shadow"]
    anchor_date: date
    target_date: date
    data_cutoff: datetime
    published_at: datetime
    probabilities: ProbabilitiesV2
    threshold: float = Field(ge=0, allow_inf_nan=False)
    result: PublicResult | None = None

    @field_validator("data_cutoff", "published_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publication timestamps require a timezone")
        return value

    @model_validator(mode="after")
    def ordered(self) -> PublicForecast:
        if self.target_date <= self.anchor_date or self.data_cutoff > self.published_at:
            raise ValueError("forecast dates or cutoff are invalid")
        if self.target_id.endswith("w1") and self.lane != "shadow":
            raise ValueError("relative W1 must remain shadow")
        probabilities = self.probabilities.as_dict()
        maximum = max(probabilities.values())
        winners = [key for key, value in probabilities.items() if math.isclose(value, maximum)]
        if len(winners) != 1:
            raise ValueError("public forecast must have a unique predicted class")
        if self.result is not None:
            label = classify_outcome(self.result.actual_return, self.threshold)
            if (
                self.result.actual_label != label
                or self.result.direction_correct != (winners[0] == label)
                or not math.isclose(
                    self.result.brier_score,
                    multiclass_brier(self.probabilities, label),
                    abs_tol=1e-9,
                )
            ):
                raise ValueError("public settlement must reproduce")
        return self


class PublicSnapshot(PublicModel):
    schema_version: Literal["forecast-loop.public-showcase/v1"] = SCHEMA
    generated_at: datetime
    next_update_at: datetime
    mode: Literal["live", "demo"]
    forecasts: list[PublicForecast] = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def valid_publication(self) -> PublicSnapshot:
        for stamp in (self.generated_at, self.next_update_at):
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("publication timestamps require a timezone")
        if self.next_update_at <= max(item.published_at for item in self.forecasts):
            raise ValueError("next update must follow the latest forecast")
        if len({item.id for item in self.forecasts}) != len(self.forecasts):
            raise ValueError("duplicate public forecast")
        episodes = [(x.target_id, x.anchor_date) for x in self.forecasts]
        if len(set(episodes)) != len(episodes):
            raise ValueError("duplicate public episode")
        if any(item.published_at > self.generated_at for item in self.forecasts):
            raise ValueError("cannot publish a future forecast")
        return self


def _aware(value: datetime) -> datetime:
    # SQLite removes timezone metadata. Focused-v2 freezes Asia/Shanghai.
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def project_forecast(row: ForecastV2) -> PublicForecast:
    """Verify sealed persistence and emit only explicit public scalar fields."""
    run = row.run
    program = ResearchProgramV2.model_validate(run.program)
    snapshot = EvidenceSnapshotV2.model_validate(run.snapshot)
    if run.mode == "live":
        _validate_live_snapshot_sources(snapshot)
    receipt = ReceiptV2.model_validate(run.receipt)
    source = AgentSignalEnvelopeV2.model_validate(row.source_signal.envelope)
    if (
        run.status != "completed"
        or receipt.status != "completed"
        or run.completed_at is None
        or receipt.run_id != run.id
        or row.id not in receipt.forecast_ids
        or row.source_signal_id not in receipt.signal_ids
        or program.content_hash != DEFAULT_RESEARCH_PROGRAM_V2.content_hash
        or run.program_hash != program.content_hash
        or receipt.program_hash != program.content_hash
        or run.snapshot_hash != snapshot.content_hash
        or receipt.snapshot_hash != snapshot.content_hash
        or receipt.input_hash != run.input_hash
        or receipt.request_hash != run.request_hash
        or row.program_hash != program.content_hash
        or row.input_hash != run.input_hash
        or source.input_hash != run.input_hash
        or source.run_id != run.id
        or source.signal_id != row.source_signal_id
        or source.content_hash != row.source_signal.content_hash
        or source.signal_kind != "decision_forecast"
        or source.target_id != row.target_id
        or source.anchor_date != row.anchor_date
        or source.target_date != row.target_date
        or snapshot.base_session != row.anchor_date
        or _aware(run.data_cutoff) != snapshot.data_cutoff
        or source.evidence_cutoff != snapshot.data_cutoff
        or _aware(run.completed_at) != receipt.completed_at
        or _aware(row.created_at) > receipt.completed_at
    ):
        raise ValueError("finalized forecast binding failed")
    target = TARGETS[row.target_id]
    probabilities = ProbabilitiesV2(
        up=row.probability_up, neutral=row.probability_neutral, down=row.probability_down
    )
    if (
        source.draft.probabilities != probabilities
        or source.threshold != row.threshold
        or row.horizon != target.horizon
        or row.configured_lane != target.lane
        or row.effective_lane not in {"formal", "shadow"}
        or source.draft.rationale != row.rationale
        or row.target_date != snapshot.future_sessions[target.horizon.session_count - 1]
        or source.created_at != _aware(row.created_at)
    ):
        raise ValueError("forecast does not match its sealed signal")
    body = {
        name: getattr(row, name)
        for name in (
            "schema_version",
            "run_id",
            "source_signal_id",
            "program_hash",
            "target_id",
            "horizon",
            "configured_lane",
            "effective_lane",
            "anchor_date",
            "target_date",
            "threshold",
            "baseline_probabilities",
            "rationale",
            "counter_evidence",
            "invalidation_conditions",
            "input_hash",
        )
    }
    body.update(probabilities=probabilities.model_dump(), created_at=_aware(row.created_at))
    if content_hash(body) != row.content_hash:
        raise ValueError("forecast content hash mismatch")
    result = None
    if row.evaluation is not None:
        evaluation = row.evaluation
        signal_eval = evaluation.signal_evaluation
        observation = OutcomeObservationV2.model_validate(signal_eval.observation.observation)
        if run.mode == "live":
            _validate_live_outcome_sources(observation)
        actual = realized_outcome(observation)
        label = classify_outcome(actual, row.threshold)
        brier = multiclass_brier(probabilities, label)
        correct = source.draft.direction == label
        evaluation_body = {
            name: getattr(evaluation, name)
            for name in (
                "forecast_id",
                "signal_evaluation_id",
                "actual_value",
                "actual_label",
                "brier_score",
                "baseline_brier_score",
                "direction_correct",
            )
        }
        evaluation_body["evaluated_at"] = _aware(evaluation.evaluated_at)
        if (
            content_hash(evaluation_body) != evaluation.content_hash
            or evaluation.forecast_id != row.id
            or signal_eval.signal_id != row.source_signal_id
            or observation.content_hash != signal_eval.observation.content_hash
            or observation.mode != run.mode
            or observation.program_hash != row.program_hash
            or observation.target_id != row.target_id
            or observation.anchor_date != row.anchor_date
            or observation.target_date != row.target_date
            or observation.observed_at > _aware(evaluation.evaluated_at)
            or evaluation.actual_label != label
            or not math.isclose(evaluation.actual_value, actual, abs_tol=1e-12)
            or not math.isclose(evaluation.brier_score, brier, abs_tol=1e-12)
            or evaluation.direction_correct != correct
        ):
            raise ValueError("forecast evaluation binding failed")
        result = PublicResult(
            actual_return=actual,
            actual_label=label,
            brier_score=brier,
            direction_correct=correct,
        )
    return PublicForecast(
        id=row.content_hash,
        target_id=row.target_id,
        lane=row.effective_lane,
        anchor_date=row.anchor_date,
        target_date=row.target_date,
        data_cutoff=snapshot.data_cutoff,
        published_at=_aware(row.created_at),
        probabilities=probabilities,
        threshold=row.threshold,
        result=result,
    )


def export_snapshot(
    database: Path,
    *,
    mode: Literal["live", "demo"] = "live",
    now: datetime | None = None,
    next_update_at: datetime | None = None,
) -> PublicSnapshot:
    """Open an existing SQLite source with read-only mode, never bootstrap it."""
    if database.is_symlink():
        raise ValueError("database symlinks are not accepted")
    path = database.absolute()
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("database path must be a regular file without symlinks")
    generated = now or datetime.now(UTC)
    engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True),
    )
    try:
        with Session(engine) as session:
            rows = session.scalars(
                select(ForecastV2)
                .join(ResearchRunV2)
                .options(
                    selectinload(ForecastV2.run),
                    selectinload(ForecastV2.source_signal),
                    selectinload(ForecastV2.evaluation),
                )
                .where(
                    ResearchRunV2.mode == mode,
                    ResearchRunV2.status == "completed",
                    ForecastV2.program_hash == DEFAULT_RESEARCH_PROGRAM_V2.content_hash,
                )
                .order_by(ForecastV2.created_at.desc(), ForecastV2.id.desc())
                .limit(400)
            ).all()
            for row in rows:
                if (
                    row.run.completed_at is None
                    or _aware(row.run.completed_at) > generated
                    or (
                        row.evaluation is not None
                        and _aware(row.evaluation.evaluated_at) > generated
                    )
                ):
                    raise ValueError("future completion or settlement cannot be published")
            forecasts = [project_forecast(row) for row in rows]
    finally:
        engine.dispose()
    if not forecasts:
        raise ValueError("no completed forecasts; previous publication must be retained")
    return PublicSnapshot(
        generated_at=generated,
        next_update_at=next_update_at
        or max(x.published_at for x in forecasts) + timedelta(hours=26),
        mode=mode,
        forecasts=forecasts,
    )


def write_snapshot(snapshot: PublicSnapshot, output: Path) -> None:
    """Atomically replace one public file after all source validation passes."""
    if output.suffix != ".json":
        raise ValueError("publication output must be a JSON file")
    if output.is_symlink() or output.parent.resolve(strict=True) != output.parent.absolute():
        raise ValueError("publication output must not traverse symlinks")
    raw = snapshot.model_dump_json(indent=2).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".publication-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--demo", action="store_true", help="Explicit synthetic publication only")
    parser.add_argument("--next-update-at", type=datetime.fromisoformat)
    args = parser.parse_args()
    try:
        if args.database.resolve() == args.output.resolve():
            raise ValueError("publication cannot replace its source")
        snapshot = export_snapshot(
            args.database,
            mode="demo" if args.demo else "live",
            next_update_at=args.next_update_at,
        )
        write_snapshot(snapshot, args.output)
    except Exception:
        # Validation errors and database exceptions can contain private inputs.
        print(json.dumps({"status": "blocked", "message": "Publication validation failed"}))
        return 1
    print(json.dumps({"status": "exported", "forecasts": len(snapshot.forecasts)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
