from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from app.agent_contracts import (
    AgentSpecBody,
    SignalEnvelopeBody,
    SignalInputBinding,
    SignalProvenance,
    SignalTarget,
    agent_spec,
    seal_agent_spec,
    seal_signal_envelope,
)
from app.db import Database
from app.domain import AgentSourceType
from app.models import AgentSpecRecord, SignalEnvelopeRecord, WorkflowRun
from app.services.signal_contract import (
    persist_signal_envelope,
    verify_signal_envelope_record,
)
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]
ZONE = ZoneInfo("Asia/Shanghai")


def _manual_signal(
    run_id: str,
    signal_id: str = "migration-manual-signal",
):
    spec = agent_spec("user_judgment_agent")
    return seal_signal_envelope(
        SignalEnvelopeBody(
            signal_id=signal_id,
            agent_id=spec.agent_id,
            agent_version=spec.agent_version,
            mode="live",
            target=SignalTarget(
                index_code="000300.SH",
                horizon="D1",
                base_trade_date=date(2026, 7, 27),
                target_date=date(2026, 7, 28),
                as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
                data_cutoff=datetime(2026, 7, 27, 14, 55, tzinfo=ZONE),
            ),
            submitted_at=datetime(2026, 7, 27, 15, 2, tzinfo=ZONE),
            accepted_at=datetime(2026, 7, 27, 15, 3, tzinfo=ZONE),
            submission_deadline=datetime(2026, 7, 27, 15, 30, tzinfo=ZONE),
            input_binding=SignalInputBinding(
                run_id=run_id,
                run_input_hash="a" * 64,
                agent_spec_hash=spec.content_hash,
            ),
            participation=spec.participation,
            provenance=SignalProvenance(
                source_type=AgentSourceType.MANUAL,
                producer="local-user-interface",
                adapter="manual-form",
                adapter_version="1.0.0",
            ),
            direction="up",
            direction_confidence=0.62,
            rationale="流动性与风险偏好改善可能共同推动目标指数走强。",
            counter_evidence=("海外利率上行可能压制风险资产。",),
            invalidation_conditions=("若跌破基准日低点，则当前判断失效。",),
            blind_attestation=True,
            payload_schema="forecast-loop.manual/v1",
            source_payload={"entry_format": "private-wiki"},
        )
    )


def _add_run(database: Database, run_id: str = "agent-contract-run") -> None:
    with database.session_factory() as session:
        session.add(
            WorkflowRun(
                id=run_id,
                as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
                data_cutoff=datetime(2026, 7, 27, 14, 55, tzinfo=ZONE),
                status="completed",
                mode="live",
                started_at=datetime(2026, 7, 27, 15, tzinfo=ZONE),
                completed_at=datetime(2026, 7, 27, 15, 1, tzinfo=ZONE),
                duration_seconds=60.0,
                error=None,
                data_quality={},
                workflow_steps=[],
                input_hash="a" * 64,
            )
        )
        session.commit()


def _persist(session, signal, **overrides):
    arguments = {
        "signal": signal,
        "authoritative_target": signal.target,
        "authoritative_accepted_at": signal.accepted_at,
        "authoritative_submission_deadline": signal.submission_deadline,
        "authoritative_provenance": signal.provenance,
        "run_timezone": "Asia/Shanghai",
    }
    arguments.update(overrides)
    return persist_signal_envelope(session, **arguments)


def test_0008_preserves_legacy_rows_and_installs_append_only_guard(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "migration.sqlite3"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("VERICOUNCIL_DATABASE_URL", database_url)
    configuration = Config(str(ROOT / "backend" / "alembic.ini"))

    command.upgrade(configuration, "0007_user_judgment_agent")
    database = Database(database_url)
    try:
        with database.session_factory() as session:
            session.add(
                WorkflowRun(
                    id="legacy-run-before-agent-contract",
                    as_of=datetime(2026, 7, 27, 15, tzinfo=ZONE),
                    data_cutoff=datetime(2026, 7, 27, 14, 55, tzinfo=ZONE),
                    status="completed",
                    mode="live",
                    started_at=datetime(2026, 7, 27, 15, tzinfo=ZONE),
                    completed_at=datetime(2026, 7, 27, 15, 1, tzinfo=ZONE),
                    duration_seconds=60.0,
                    error=None,
                    data_quality={},
                    workflow_steps=[],
                    input_hash="a" * 64,
                )
            )
            session.commit()
        # Revision 0001 creates current metadata on a fresh database. Dropping
        # the new tables reproduces a real database that stopped at 0007 before
        # the current models existed.
        with database.engine.begin() as connection:
            connection.execute(text("DROP TABLE signal_envelopes"))
            connection.execute(text("DROP TABLE agent_specs"))
    finally:
        database.dispose()

    command.upgrade(configuration, "head")
    database = Database(database_url)
    try:
        assert {"agent_specs", "signal_envelopes"}.issubset(
            inspect(database.engine).get_table_names()
        )
        with database.session_factory() as session:
            legacy = session.get(WorkflowRun, "legacy-run-before-agent-contract")
            assert legacy is not None
            assert legacy.input_hash == "a" * 64

            signal = _manual_signal(legacy.id)
            row, created = _persist(
                session,
                signal,
                source_record_type="user_judgment",
                source_record_id="judgment-fixture",
            )
            assert created is True
            assert row.routing_lane == "shadow_benchmark"
            assert row.formal_aggregation is False
            assert row.shadow_benchmark is True
            session.commit()

        with database.session_factory() as session:
            stored_spec = session.get(
                AgentSpecRecord,
                signal.input_binding.agent_spec_hash,
            )
            assert stored_spec is not None
            row = session.get(SignalEnvelopeRecord, "migration-manual-signal")
            assert row is not None
            verified = verify_signal_envelope_record(row)
            assert verified.content_hash == signal.content_hash

            with pytest.raises(IntegrityError, match="immutable Agent contract"):
                row.content_hash = "f" * 64
                session.commit()
            session.rollback()

            row = session.get(SignalEnvelopeRecord, "migration-manual-signal")
            with pytest.raises(IntegrityError, match="immutable Agent contract"):
                session.delete(row)
                session.commit()
    finally:
        database.dispose()


def test_0010_seeds_legacy_judgment_spec_and_restores_sqlite_fk(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "judgment-spec-migration.sqlite3"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("VERICOUNCIL_DATABASE_URL", database_url)
    configuration = Config(str(ROOT / "backend" / "alembic.ini"))

    command.upgrade(configuration, "head")
    command.downgrade(configuration, "0009_persistent_workflow_tasks")
    database = Database(database_url)
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER IF EXISTS trg_agent_specs_reject_delete")
            )
            connection.execute(
                text(
                    "DELETE FROM agent_specs "
                    "WHERE agent_id = 'user_judgment_agent'"
                )
            )
        assert "agent_spec_hash" not in {
            column["name"]
            for column in inspect(database.engine).get_columns(
                "user_judgments"
            )
        }
    finally:
        database.dispose()

    command.upgrade(configuration, "head")
    database = Database(database_url)
    try:
        inspector = inspect(database.engine)
        assert "agent_spec_hash" in {
            column["name"]
            for column in inspector.get_columns("user_judgments")
        }
        assert any(
            foreign_key["constrained_columns"] == ["agent_spec_hash"]
            and foreign_key["referred_table"] == "agent_specs"
            and foreign_key["referred_columns"] == ["content_hash"]
            for foreign_key in inspector.get_foreign_keys("user_judgments")
        )
        with database.session_factory() as session:
            rows = session.scalars(
                select(AgentSpecRecord).where(
                    AgentSpecRecord.agent_id == "user_judgment_agent",
                    AgentSpecRecord.agent_version == "0.1.0",
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].content_hash == (
                "b8268786bb1db8a9eebd55da4710ac9961114f0e865b4dc11dcf360c1084b40c"
            )
        with database.engine.connect() as connection:
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND name LIKE 'trg_user_judgments_%'"
                    )
                )
            }
        assert triggers == {
            "trg_user_judgments_reject_delete",
            "trg_user_judgments_reject_update",
        }
    finally:
        database.dispose()


def test_create_all_path_enforces_run_binding_foreign_keys_and_immutability(
    tmp_path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'create-all.sqlite3'}")
    database.create_all()
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        _add_run(database)
        signal = _manual_signal("agent-contract-run")
        with database.session_factory() as session:
            row, created = _persist(session, signal)
            assert created is True
            session.commit()

        with database.session_factory() as session:
            with pytest.raises(ValueError, match="host deadline"):
                _persist(
                    session,
                    signal,
                    authoritative_submission_deadline=(
                        signal.submission_deadline + timedelta(minutes=1)
                    ),
                )

        with database.session_factory() as session:
            with pytest.raises(ValueError, match="host receipt time"):
                _persist(
                    session,
                    signal,
                    authoritative_accepted_at=(
                        signal.accepted_at + timedelta(seconds=1)
                    ),
                )
            with pytest.raises(ValueError, match="host target"):
                _persist(
                    session,
                    signal,
                    authoritative_target=signal.target.model_copy(
                        update={"index_code": "HOST.INDEX"}
                    ),
                )
            with pytest.raises(ValueError, match="host-bound provenance"):
                _persist(
                    session,
                    signal,
                    authoritative_provenance=signal.provenance.model_copy(
                        update={"producer": "trusted-host-adapter"}
                    ),
                )

        equivalent_body = _manual_signal("agent-contract-run").model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        equivalent_body["signal_id"] = "equivalent-utc-run-binding"
        equivalent_body["target"]["as_of"] = datetime(
            2026,
            7,
            27,
            7,
            tzinfo=UTC,
        ).isoformat()
        equivalent_body["target"]["data_cutoff"] = datetime(
            2026,
            7,
            27,
            6,
            55,
            tzinfo=UTC,
        ).isoformat()
        equivalent = seal_signal_envelope(
            SignalEnvelopeBody.model_validate(equivalent_body)
        )
        with database.session_factory() as session:
            host_target = _manual_signal("agent-contract-run").target
            _, created = _persist(
                session,
                equivalent,
                authoritative_target=host_target,
            )
            assert created is True
            session.commit()

        late_deadline_body = equivalent.model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        late_deadline_body["signal_id"] = "utc-date-hides-late-deadline"
        late_deadline_body["submission_deadline"] = datetime(
            2026,
            7,
            27,
            16,
            30,
            tzinfo=UTC,
        ).isoformat()
        late_deadline = seal_signal_envelope(
            SignalEnvelopeBody.model_validate(late_deadline_body)
        )
        with database.session_factory() as session:
            with pytest.raises(
                ValueError,
                match="host submission deadline must precede",
            ):
                _persist(
                    session,
                    late_deadline,
                    authoritative_target=host_target,
                )

        with database.session_factory() as session:
            row = session.get(SignalEnvelopeRecord, signal.signal_id)
            row.envelope = {"tampered": True}
            with pytest.raises(IntegrityError, match="immutable Agent contract"):
                session.commit()

        missing_run = _manual_signal(
            "missing-run",
            signal_id="missing-run-signal",
        )
        with database.session_factory() as session:
            with pytest.raises(ValueError, match="run_id does not exist"):
                _persist(
                    session,
                    missing_run,
                )

        wrong_hash_body = _manual_signal("agent-contract-run").model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        wrong_hash_body["signal_id"] = "wrong-run-hash-signal"
        wrong_hash_body["input_binding"]["run_input_hash"] = "f" * 64
        wrong_hash = seal_signal_envelope(
            SignalEnvelopeBody.model_validate(wrong_hash_body)
        )
        with database.session_factory() as session:
            with pytest.raises(ValueError, match="run_input_hash"):
                _persist(
                    session,
                    wrong_hash,
                )

        wrong_mode_body = _manual_signal("agent-contract-run").model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        wrong_mode_body["signal_id"] = "wrong-run-mode-signal"
        wrong_mode_body["mode"] = "demo"
        wrong_mode = seal_signal_envelope(
            SignalEnvelopeBody.model_validate(wrong_mode_body)
        )
        with database.session_factory() as session:
            with pytest.raises(ValueError, match="mode does not match"):
                _persist(
                    session,
                    wrong_mode,
                )

        current_spec = agent_spec("user_judgment_agent")
        promoted_spec_body = current_spec.model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        promoted_spec_body["participation"].update(
            {
                "policy_id": "adapter-self-promote",
                "policy_version": "1.0.0",
                "mode": "formal",
                "influence": "input",
            }
        )
        promoted_spec = seal_agent_spec(
            AgentSpecBody.model_validate(promoted_spec_body)
        )
        promoted_body = _manual_signal("agent-contract-run").model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        promoted_body["signal_id"] = "adapter-self-promoted-formal-signal"
        promoted_body["input_binding"]["agent_spec_hash"] = (
            promoted_spec.content_hash
        )
        promoted_body["participation"] = promoted_spec.participation.model_dump(
            mode="json"
        )
        promoted_signal = seal_signal_envelope(
            SignalEnvelopeBody.model_validate(promoted_body)
        )
        with database.session_factory() as session:
            with pytest.raises(ValueError, match="AgentSpec hash"):
                _persist(session, promoted_signal)
    finally:
        database.dispose()


def test_historical_agent_spec_is_resolved_by_content_hash(
    monkeypatch,
    tmp_path,
) -> None:
    import app.agent_contracts as contract_registry

    database = Database(f"sqlite:///{tmp_path / 'historical-spec.sqlite3'}")
    database.create_all()
    try:
        _add_run(database)
        archived = agent_spec("user_judgment_agent")
        upgraded = seal_agent_spec(
            AgentSpecBody(
                **archived.model_dump(
                    exclude={"content_hash", "role"},
                ),
                role=archived.role + " 新版本。",
            )
        )
        body = _manual_signal("agent-contract-run").model_dump(
            mode="json",
            exclude={"content_hash"},
        )
        body["signal_id"] = "historical-spec-signal"
        body["input_binding"]["agent_spec_hash"] = archived.content_hash
        signal = seal_signal_envelope(
            SignalEnvelopeBody.model_validate(body)
        )
        with database.session_factory() as session:
            _persist(session, signal)
            session.commit()

        monkeypatch.setitem(
            contract_registry.AGENT_SPEC_BY_ID,
            archived.agent_id,
            upgraded,
        )
        assert archived.content_hash != upgraded.content_hash
        with database.session_factory() as session:
            replayed, created = _persist(session, signal)
            assert created is False
            assert replayed.agent_spec_hash == archived.content_hash

            row = session.get(SignalEnvelopeRecord, signal.signal_id)
            verified = verify_signal_envelope_record(row)
            assert verified.content_hash == signal.content_hash
            assert row.spec_record.content_hash == archived.content_hash
            assert row.spec_record.spec["role"] == archived.role

        new_body = signal.model_dump(mode="json", exclude={"content_hash"})
        new_body["signal_id"] = "self-promoted-old-policy"
        new_signal = seal_signal_envelope(
            SignalEnvelopeBody.model_validate(new_body)
        )
        with database.session_factory() as session:
            with pytest.raises(ValueError, match="AgentSpec hash"):
                _persist(session, new_signal)
    finally:
        database.dispose()
