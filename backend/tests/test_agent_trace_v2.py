from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from app.config import Settings
from app.db import Database
from app.models import AgentTrace, AgentTraceArtifactLink, AgentTraceSpan
from app.services.agent_tracing import TraceRecorder, _OtelSetup
from app.services.runtime_trace_bridge import RuntimeBridgeSetup
from app.services.schema_readiness import migration_config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'trace-v2.sqlite3'}",
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        wiki_path=tmp_path / "wiki",
        demo_mode=True,
        auto_seed=False,
    )


def test_trace_recorder_creates_isolated_attempts_and_hierarchy(tmp_path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_all()
    recorder = TraceRecorder(database, settings)
    now = datetime(2026, 8, 12, 9, 0, tzinfo=ZoneInfo(settings.timezone))

    first = recorder.start_trace(
        workflow_kind="prediction",
        subject_id="run-1",
        attempt_number=1,
        target_id="csi1000-absolute-d1",
        horizon="D1",
        mode="live",
        input_hash="first-input",
        started_at=now,
        attributes={"provider": "safe", "raw_prompt": "must not persist"},
    )
    assert first is not None
    with recorder.span(
        workflow_kind="prediction",
        subject_id="run-1",
        trace_id=first,
        node_id="target:csi1000-absolute-d1",
        name="Target",
        span_kind="workflow",
    ) as parent:
        assert parent is not None
        with recorder.child_span(
            parent=parent,
            node_id="agent:macro",
            name="Macro Agent",
            span_kind="agent",
            agent_id="macro_policy_agent",
        ) as child:
            assert child is not None
    assert recorder.link_artifact(
        workflow_kind="prediction",
        subject_id="run-1",
        trace_id=first,
        span_id=child.span_id,
        artifact_kind="signal",
        artifact_id="signal-1",
        relation="output",
        content_hash="signal-body",
    )
    recorder.finish_trace(
        workflow_kind="prediction",
        subject_id="run-1",
        trace_id=first,
        status="failed",
        error="retryable",
        completed_at=now + timedelta(seconds=2),
    )

    second = recorder.start_trace(
        workflow_kind="prediction",
        subject_id="run-1",
        attempt_number=2,
        target_id="csi1000-absolute-d1",
        horizon="D1",
        mode="live",
        input_hash="first-input",
        started_at=now + timedelta(minutes=1),
    )
    assert second is not None and second != first
    recorder.finish_trace(
        workflow_kind="prediction",
        subject_id="run-1",
        trace_id=second,
        status="completed",
    )

    with database.session_factory() as session:
        traces = session.scalars(select(AgentTrace).order_by(AgentTrace.attempt_number)).all()
        assert [trace.attempt_number for trace in traces] == [1, 2]
        assert traces[0].attributes == {"provider": "safe"}
        spans = session.scalars(
            select(AgentTraceSpan)
            .where(AgentTraceSpan.trace_id == first)
            .order_by(AgentTraceSpan.started_at)
        ).all()
        assert len(spans) == 2
        assert spans[1].parent_span_id == spans[0].span_id
        link = session.scalar(
            select(AgentTraceArtifactLink).where(AgentTraceArtifactLink.trace_id == first)
        )
        assert link is not None
        assert link.content_hash is not None and len(link.content_hash) == 64
    database.dispose()


def test_otlp_setup_failure_marks_local_trace_incomplete_without_blocking(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"otlp_traces_endpoint": "https://collector.invalid/v1/traces"}
    )
    database = Database(settings.database_url)
    database.create_all()
    monkeypatch.setattr(
        "app.services.agent_tracing._build_otel_tracer",
        lambda _settings: _OtelSetup(
            tracer=None,
            telemetry_complete=False,
            note="OTLP setup failed: synthetic",
        ),
    )

    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="prediction",
        subject_id="otlp-setup-failure",
        mode="live",
        input_hash="input",
    )

    assert trace_id is not None
    assert recorder.otlp_telemetry_complete is False
    with database.session_factory() as session:
        row = session.get(AgentTrace, trace_id)
        assert row is not None
        assert row.status == "running"
        assert row.telemetry_complete is False
        assert row.attributes["telemetry_note"] == "OTLP setup failed: synthetic"
    database.dispose()


def test_private_runtime_bridge_captures_root_io_and_nested_llm_tool_spans(
    tmp_path, monkeypatch
) -> None:
    class FakeSpan:
        def __init__(self, tracer, name, span_type, input_value, metadata, model, provider):
            self.tracer = tracer
            self.record = {
                "id": f"span-{len(tracer.spans) + 1}",
                "parent_id": None,
                "name": name,
                "type": span_type,
                "input": input_value,
                "metadata": metadata,
                "model": model,
                "provider": provider,
            }

        def __enter__(self):
            self.record["parent_id"] = self.tracer.stack[-1]["id"] if self.tracer.stack else None
            self.tracer.spans.append(self.record)
            self.tracer.stack.append(self.record)
            return self

        def set_output(self, value):
            self.record["output"] = value

        def set_usage(self, value, *, total_cost=None):
            self.record["usage"] = value
            self.record["total_cost"] = total_cost

        def __exit__(self, exc_type, _exc, _traceback):
            assert self.tracer.stack.pop() is self.record
            self.record["status"] = "error" if exc_type else "ok"

    class FakeRuntimeTracer:
        instances = []

        def __init__(self, policy_path, *, name, prompt, metadata, tags):
            self.policy_path = policy_path
            self.name = name
            self.prompt = prompt
            self.metadata = metadata
            self.tags = tags
            self.trace_id = "runtime-trace-1"
            self.output = None
            self.spans = []
            self.stack = []
            self.receipt = None
            self.instances.append(self)

        def __enter__(self):
            return self

        def span(self, name, *, type, input, metadata, model, provider):
            return FakeSpan(self, name, type, input, metadata, model, provider)

        def set_output(self, value):
            self.output = value

        def __exit__(self, _exc_type, _exc, _traceback):
            self.receipt = SimpleNamespace(
                stored=True,
                delivered=True,
                external_id=self.trace_id,
                error_code=None,
            )

    policy_path = tmp_path / "private-policy.json"
    monkeypatch.setattr(
        "app.services.agent_tracing.build_runtime_bridge",
        lambda _path: RuntimeBridgeSetup(FakeRuntimeTracer, policy_path, True),
    )
    settings = _settings(tmp_path).model_copy(
        update={"agent_runtime_trace_policy": policy_path}
    )
    database = Database(settings.database_url)
    database.create_all()
    recorder = TraceRecorder(database, settings)
    root_input = {"task": "controlled forecast", "evidence": ["fact-a"]}
    root_output = {"answer": "up", "confidence": 0.61}

    with recorder.execution(
        workflow_kind="prediction",
        subject_id="runtime-run-1",
        mode="controlled",
        input_hash="sealed-input",
        input_value=root_input,
        name="forecast-loop.prediction.run",
    ) as execution:
        with recorder.span(
            workflow_kind="prediction",
            subject_id="runtime-run-1",
            trace_id=execution.trace_id,
            node_id="agent.research",
            name="research agent",
            span_kind="agent",
        ):
            with recorder.span(
                workflow_kind="prediction",
                subject_id="runtime-run-1",
                trace_id=execution.trace_id,
                node_id="provider.research",
                name="model dispatch",
                span_kind="llm",
                model_name="model-a",
                input_value={"prompt": "model task"},
                attributes={"provider": "provider-a"},
            ) as llm:
                assert llm is not None
                llm.set_output({"draft": "up"})
                llm.set_usage({"input_tokens": 4, "output_tokens": 2})
            with recorder.span(
                workflow_kind="prediction",
                subject_id="runtime-run-1",
                trace_id=execution.trace_id,
                node_id="tool.persist",
                name="persist forecast",
                span_kind="persistence",
                input_value={"forecast": "up"},
            ) as tool:
                assert tool is not None
                tool.set_output({"stored": True})
        execution.set_output(root_output)

    runtime = FakeRuntimeTracer.instances[0]
    assert runtime.prompt == root_input
    assert runtime.output == root_output
    assert [span["type"] for span in runtime.spans] == ["general", "llm", "tool"]
    assert runtime.spans[1]["parent_id"] == runtime.spans[0]["id"]
    assert runtime.spans[2]["parent_id"] == runtime.spans[0]["id"]
    assert runtime.spans[1]["output"] == {"draft": "up"}
    assert runtime.spans[1]["usage"]["input_tokens"] == 4

    with database.session_factory() as session:
        trace = session.scalar(
            select(AgentTrace).where(AgentTrace.subject_id == "runtime-run-1")
        )
        assert trace is not None
        assert trace.status == "completed"
        assert trace.attributes["external_receipt"] is True
        assert "controlled forecast" not in str(trace.attributes)
        spans = session.scalars(
            select(AgentTraceSpan)
            .where(AgentTraceSpan.trace_id == trace.id)
            .order_by(AgentTraceSpan.started_at)
        ).all()
        assert all(span.input_digest is None or len(span.input_digest) == 64 for span in spans)
        assert all(span.output_digest is None or len(span.output_digest) == 64 for span in spans)
        assert "model task" not in str([span.attributes for span in spans])
    database.dispose()


def test_workflow_entry_records_real_model_dispatch_and_persistence_hierarchy(client) -> None:
    workflow = client.app.state.workflow
    workflow.provider.trace_span_kind = "llm"
    prepared = workflow.prepare_run(
        as_of=datetime.fromisoformat("2026-07-09T15:00:00+08:00")
    )

    completed = workflow.execute_prepared(prepared)

    assert completed.status == "completed"
    database = client.app.state.database
    with database.session_factory() as session:
        trace = session.scalar(
            select(AgentTrace).where(AgentTrace.subject_id == prepared.row.id)
        )
        assert trace is not None and trace.status == "completed"
        spans = session.scalars(
            select(AgentTraceSpan).where(AgentTraceSpan.trace_id == trace.id)
        ).all()
    by_id = {span.span_id: span for span in spans}
    root = next(span for span in spans if span.node_id == "workflow.execute")
    model_spans = [span for span in spans if span.node_id.startswith("provider.")]
    persistence = next(span for span in spans if span.node_id == "workflow.persist_result")
    freeze = next(span for span in spans if span.node_id == "node.freeze_snapshot")

    assert model_spans and all(span.span_kind == "llm" for span in model_spans)
    assert all(span.input_digest and span.output_digest for span in model_spans)
    assert all(by_id[span.parent_span_id].span_kind == "agent" for span in model_spans)
    assert all(by_id[span.parent_span_id].parent_span_id == root.span_id for span in model_spans)
    assert persistence.span_kind == "persistence"
    assert persistence.parent_span_id is None
    assert freeze.span_kind == "external"
    assert freeze.parent_span_id == root.span_id


def test_otlp_span_start_failure_downgrades_local_trace_and_business_continues(
    tmp_path, monkeypatch
) -> None:
    class BrokenTracer:
        def start_as_current_span(self, _name, *, attributes):
            assert attributes is not None
            raise RuntimeError("collector unavailable")

    settings = _settings(tmp_path).model_copy(
        update={"otlp_traces_endpoint": "https://collector.invalid/v1/traces"}
    )
    database = Database(settings.database_url)
    database.create_all()
    monkeypatch.setattr(
        "app.services.agent_tracing._build_otel_tracer",
        lambda _settings: _OtelSetup(
            tracer=BrokenTracer(),
            telemetry_complete=True,
        ),
    )
    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="prediction",
        subject_id="otlp-span-failure",
        mode="live",
        input_hash="input",
    )
    assert trace_id is not None

    reached_business_body = False
    with recorder.span(
        workflow_kind="prediction",
        subject_id="otlp-span-failure",
        trace_id=trace_id,
        node_id="business",
        name="business span",
        span_kind="workflow",
    ) as span:
        assert span is not None
        reached_business_body = True

    assert reached_business_body is True
    assert recorder.otlp_telemetry_complete is False
    recorder.finish_trace(
        workflow_kind="prediction",
        subject_id="otlp-span-failure",
        trace_id=trace_id,
        status="completed",
    )
    with database.session_factory() as session:
        row = session.get(AgentTrace, trace_id)
        assert row is not None
        assert row.status == "completed"
        assert row.telemetry_complete is False
        assert row.attributes["telemetry_note"] == "OTLP span start failed: RuntimeError"
        local_span = session.scalar(
            select(AgentTraceSpan).where(AgentTraceSpan.trace_id == trace_id)
        )
        assert local_span is not None and local_span.status == "completed"
    database.dispose()


def test_sealed_trace_rejects_mutation_children_and_deletion(tmp_path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_all()
    recorder = TraceRecorder(database, settings)
    trace_id = recorder.start_trace(
        workflow_kind="agent_eval",
        subject_id="eval-1",
        mode="offline",
        input_hash=None,
    )
    assert trace_id is not None
    recorder.finish_trace(
        workflow_kind="agent_eval",
        subject_id="eval-1",
        trace_id=trace_id,
        status="completed",
    )

    with database.session_factory() as session:
        trace = session.get(AgentTrace, trace_id)
        assert trace is not None
        trace.error_summary = "late rewrite"
        with pytest.raises(IntegrityError, match="sealed Agent trace"):
            session.commit()
        session.rollback()
        session.add(
            AgentTraceSpan(
                id="late-span-row",
                trace_id=trace_id,
                span_id="late-span",
                parent_span_id=None,
                node_id="late",
                name="Late",
                span_kind="external",
                status="completed",
                started_at=datetime.now(ZoneInfo(settings.timezone)),
                completed_at=datetime.now(ZoneInfo(settings.timezone)),
                duration_ms=0,
                agent_id=None,
                agent_version=None,
                model_name=None,
                prompt_version=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                estimated_cost_usd=None,
                input_digest=None,
                output_digest=None,
                summary=None,
                error_code=None,
                error_summary=None,
                attributes={},
            )
        )
        with pytest.raises(IntegrityError, match="sealed Agent trace"):
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError, match="Agent traces are retained"):
            session.execute(text("DELETE FROM agent_traces WHERE id = :id"), {"id": trace_id})
    database.dispose()


def test_trace_api_supports_cursor_filters_links_and_storage_metrics(client) -> None:
    recorder: TraceRecorder = client.app.state.trace_recorder
    base = datetime(2026, 8, 12, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    for number, target, horizon, agent in (
        (1, "csi1000-absolute-d1", "D1", "macro_policy_agent"),
        (2, "csi1000-vs-csi300-relative-w1", "W1", "industry_agent"),
        (3, "csi1000-absolute-d1", "D1", "market_news_agent"),
    ):
        trace_id = recorder.start_trace(
            workflow_kind="prediction",
            subject_id=f"trace-filter-{number}",
            target_id=target,
            horizon=horizon,
            mode="shadow",
            input_hash=f"input-{number}",
            started_at=base + timedelta(minutes=number),
        )
        assert trace_id is not None
        with recorder.span(
            workflow_kind="prediction",
            subject_id=f"trace-filter-{number}",
            trace_id=trace_id,
            node_id=f"agent-{number}",
            name="Agent",
            span_kind="agent",
            agent_id=agent,
        ) as span:
            assert span is not None
        assert recorder.link_artifact(
            workflow_kind="prediction",
            subject_id=f"trace-filter-{number}",
            trace_id=trace_id,
            artifact_kind="forecast",
            artifact_id=f"forecast-{number}",
            relation="output",
        )
        recorder.finish_trace(
            workflow_kind="prediction",
            subject_id=f"trace-filter-{number}",
            trace_id=trace_id,
            status="completed",
        )

    page_one = client.get(
        "/api/agent-traces",
        params={"target_id": "csi1000-absolute-d1", "horizon": "D1", "limit": 1},
    )
    assert page_one.status_code == 200, page_one.text
    assert len(page_one.json()["items"]) == 1
    assert page_one.json()["next_cursor"]
    page_two = client.get(
        "/api/agent-traces",
        params={
            "target_id": "csi1000-absolute-d1",
            "horizon": "D1",
            "limit": 1,
            "cursor": page_one.json()["next_cursor"],
        },
    )
    assert page_two.status_code == 200, page_two.text
    assert page_two.json()["items"][0]["id"] != page_one.json()["items"][0]["id"]

    filtered = client.get(
        "/api/agent-traces",
        params={
            "agent_id": "industry_agent",
            "started_from": base.isoformat(),
            "started_to": (base + timedelta(hours=1)).isoformat(),
            "status": "completed",
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["subject_id"] for item in filtered.json()["items"]] == ["trace-filter-2"]
    detail = client.get(f"/api/agent-traces/{filtered.json()['items'][0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["attempt_number"] == 1
    assert detail.json()["artifact_links"][0]["artifact_id"] == "forecast-2"

    invalid = client.get("/api/agent-traces", params={"cursor": "not-a-cursor"})
    assert invalid.status_code == 422
    summary = client.get("/api/agent-observability/summary")
    assert summary.status_code == 200
    assert summary.json()["database_size_bytes"] > 0
    assert summary.json()["stored_span_count"] >= 3
    assert summary.json()["stored_artifact_link_count"] >= 3
    assert isinstance(summary.json()["storage_warning"], bool)


def test_migration_preserves_legacy_trace_and_allows_new_attempt(tmp_path) -> None:
    settings = _settings(tmp_path)
    configuration = migration_config(settings.database_url)
    command.upgrade(configuration, "0013_agent_eval_observability")
    now = datetime(2026, 8, 12, 9, 0, tzinfo=ZoneInfo(settings.timezone))
    database = Database(settings.database_url)
    with database.engine.begin() as connection:
        # Revision 0001 creates current metadata on a fresh database. Rebuild
        # the two affected tables to reproduce the actual 0013 shape.
        connection.execute(text("DROP TABLE agent_trace_artifact_links"))
        connection.execute(text("DROP INDEX ix_agent_traces_horizon"))
        connection.execute(text("DROP INDEX ix_agent_traces_target_id"))
        connection.execute(
            text(
                "CREATE TABLE agent_traces_0013 ("
                "id VARCHAR(32) PRIMARY KEY, workflow_kind VARCHAR(24) NOT NULL, "
                "subject_id VARCHAR(64) NOT NULL, mode VARCHAR(24) NOT NULL, "
                "status VARCHAR(24) NOT NULL, started_at DATETIME NOT NULL, "
                "completed_at DATETIME, input_hash VARCHAR(64), "
                "trace_policy_version VARCHAR(32) NOT NULL, telemetry_complete BOOLEAN NOT NULL, "
                "error_code VARCHAR(120), error_summary TEXT, attributes JSON NOT NULL, "
                "CONSTRAINT uq_agent_trace_subject UNIQUE (workflow_kind, subject_id))"
            )
        )
        connection.execute(text("DROP TABLE agent_trace_spans"))
        connection.execute(text("DROP TABLE agent_traces"))
        connection.execute(text("ALTER TABLE agent_traces_0013 RENAME TO agent_traces"))
        connection.execute(
            text(
                "CREATE TABLE agent_trace_spans ("
                "id VARCHAR(36) PRIMARY KEY, trace_id VARCHAR(32) NOT NULL REFERENCES "
                "agent_traces(id) ON DELETE CASCADE, span_id VARCHAR(16) NOT NULL, "
                "parent_span_id VARCHAR(16), node_id VARCHAR(120) NOT NULL, "
                "name VARCHAR(200) NOT NULL, span_kind VARCHAR(24) NOT NULL, "
                "status VARCHAR(24) NOT NULL, started_at DATETIME NOT NULL, "
                "completed_at DATETIME, duration_ms FLOAT, agent_id VARCHAR(64), "
                "agent_version VARCHAR(32), model_name VARCHAR(160), prompt_version VARCHAR(80), "
                "input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, "
                "estimated_cost_usd FLOAT, input_digest VARCHAR(64), output_digest VARCHAR(64), "
                "summary TEXT, error_code VARCHAR(120), error_summary TEXT, "
                "attributes JSON NOT NULL, "
                "CONSTRAINT uq_agent_trace_span_identity UNIQUE (trace_id, span_id))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_traces "
                "(id, workflow_kind, subject_id, mode, status, started_at, completed_at, "
                "input_hash, trace_policy_version, telemetry_complete, error_code, "
                "error_summary, attributes) VALUES "
                "(:id, 'prediction', 'legacy-run', 'live', 'completed', :now, :now, "
                "NULL, '1.1.0', 1, NULL, NULL, '{}')"
            ),
            {"id": "legacytrace000000000000000000000", "now": now},
        )
    command.upgrade(configuration, "0014_trace_attempts")

    with database.engine.begin() as connection:
        legacy = connection.execute(
            text(
                "SELECT attempt_number, target_id, horizon, status FROM agent_traces "
                "WHERE subject_id = 'legacy-run'"
            )
        ).one()
        assert tuple(legacy) == (1, None, None, "completed")
        connection.execute(
            text(
                "INSERT INTO agent_traces "
                "(id, workflow_kind, subject_id, attempt_number, target_id, horizon, mode, "
                "status, started_at, completed_at, input_hash, trace_policy_version, "
                "telemetry_complete, error_code, error_summary, attributes) VALUES "
                "(:id, 'prediction', 'legacy-run', 2, 'csi1000-absolute-d1', 'D1', "
                "'live', 'running', :now, NULL, NULL, '2.0.0', 1, NULL, NULL, '{}')"
            ),
            {"id": "retrytrace0000000000000000000000", "now": now},
        )
    database.dispose()
