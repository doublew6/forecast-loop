"""LangGraph investment committee workflow and persistence boundary."""

from __future__ import annotations

import hashlib
import json
import operator
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from sqlalchemy import update
from sqlalchemy.orm import Session

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:  # pragma: no cover - only used with a deliberately minimal install
    SqliteSaver = None  # type: ignore[assignment,misc]

from .config import Settings
from .db import Database
from .domain import (
    AGENT_BY_ID,
    AGENTS,
    Direction,
    Horizon,
    IndexDefinition,
    RunStatus,
    directional_confidence,
    legacy_v1_agent_hash_projection,
    neutral_threshold,
    predicted_direction,
)
from .market_universe import (
    DEFAULT_MARKET_UNIVERSE,
    MarketUniverseSpec,
    load_market_universe,
)
from .models import AgentOpinion, Forecast, WorkflowRun
from .ports import EvidenceSnapshotSource
from .schemas import AgentDraft, Citation, EvidenceItem, FrozenEvidenceSnapshot, Probabilities
from .services.believability import (
    BELIEVABILITY_POLICY_VERSION,
    BelievabilityAgentScope,
    believability_run_binding_hash,
    build_believability_snapshot,
    validate_believability_snapshot,
)
from .services.provider import ResearchProvider, build_provider
from .services.snapshot import load_evidence_snapshot
from .services.task_queue import (
    EXECUTION_MANIFEST_SCHEMA,
    ExecutionFence,
    StaleTaskLeaseError,
    fence_execution,
    finalize_execution_fence,
)
from .services.wiki import FrozenWikiCatalog, WikiCatalog


class CommitteeState(TypedDict, total=False):
    run_id: str
    as_of: str
    data_cutoff: str
    input_hash: str
    opinions: Annotated[list[dict[str, Any]], operator.add]
    forecasts: list[dict[str, Any]]
    workflow_steps: Annotated[list[dict[str, Any]], operator.add]
    data_quality: dict[str, Any]
    evidence_snapshot: dict[str, Any]
    wiki_snapshot: list[dict[str, Any]]
    market_universe: dict[str, Any]
    believability_snapshot_hash: str
    believability_snapshot_binding_hash: str
    believability_policy_version: str
    external_input_bindings: dict[str, Any]


RESEARCH_AGENT_IDS = (
    "macro_policy_agent",
    "market_news_agent",
    "ai_storage_industry_agent",
)
EFFECTIVE_RESEARCH_AGENT_IDS = (
    "macro_policy_agent",
    "market_news_agent",
    "ai_storage_industry_agent",
)
STRATEGY_AGENT_ID = "strategy_agent"
CRITIC_INPUT_AGENT_IDS = (*EFFECTIVE_RESEARCH_AGENT_IDS, STRATEGY_AGENT_ID)
DYNAMIC_EVIDENCE_AGENT_IDS = (*EFFECTIVE_RESEARCH_AGENT_IDS, STRATEGY_AGENT_ID)
WORKFLOW_VERSION = "0.3.0"
DECISION_SCHEMA_VERSION = "0.4.0"
CONFIGURABLE_UNIVERSE_WORKFLOW_VERSION = "0.4.0"
CONFIGURABLE_UNIVERSE_DECISION_SCHEMA_VERSION = "0.5.0"
DIRECTIONAL_MASS_HAIRCUT = 0.15


@dataclass(slots=True)
class PreparedRun:
    row: WorkflowRun
    initial: CommitteeState
    execution_manifest: dict[str, Any] = field(default_factory=dict)


class CommitteeWorkflow:
    """Runs and persists a complete, immutable forecast-loop meeting."""

    # Some integrity tests exercise individual deterministic graph nodes without
    # constructing infrastructure. Keep the historical default as a safe class
    # fallback; normal instances always replace these with their sealed universe.
    universe = DEFAULT_MARKET_UNIVERSE
    instruments = DEFAULT_MARKET_UNIVERSE.definitions()
    uses_configurable_universe = False
    workflow_version = WORKFLOW_VERSION
    decision_schema_version = DECISION_SCHEMA_VERSION

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        provider: ResearchProvider | None = None,
        wiki: WikiCatalog | None = None,
        evidence_source: EvidenceSnapshotSource | None = None,
        universe: MarketUniverseSpec | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.provider = provider or build_provider(settings)
        self.wiki = wiki or WikiCatalog.from_settings(settings)
        self.evidence_source = evidence_source
        self.universe = universe or load_market_universe(settings.market_universe_path)
        if self.universe.timezone != settings.timezone:
            raise ValueError(
                "Configured market universe timezone must equal VERICOUNCIL_TIMEZONE"
            )
        self.instruments = self.universe.definitions()
        self.uses_configurable_universe = (
            settings.market_universe_path is not None
            or self.universe.content_hash != DEFAULT_MARKET_UNIVERSE.content_hash
        )
        self.workflow_version = (
            CONFIGURABLE_UNIVERSE_WORKFLOW_VERSION
            if self.uses_configurable_universe
            else WORKFLOW_VERSION
        )
        self.decision_schema_version = (
            CONFIGURABLE_UNIVERSE_DECISION_SCHEMA_VERSION
            if self.uses_configurable_universe
            else DECISION_SCHEMA_VERSION
        )
        self._checkpoint_connection: sqlite3.Connection | None = None
        self.graph = self._build_graph()

    def close(self) -> None:
        if self._checkpoint_connection is not None:
            self._checkpoint_connection.close()
            self._checkpoint_connection = None

    def execution_manifest(self) -> dict[str, Any]:
        """Describe every worker-side setting that can change model execution."""

        endpoint = (
            self.settings.llm_base_url
            if self.settings.execution_mode == "api"
            else None
        )
        return {
            "schema": EXECUTION_MANIFEST_SCHEMA,
            "execution_mode": self.settings.execution_mode,
            "provider": self.provider.name,
            "provider_class": (
                f"{type(self.provider).__module__}."
                f"{type(self.provider).__qualname__}"
            ),
            "provider_endpoint_hash": (
                hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
                if endpoint is not None
                else None
            ),
            "prompt_version": getattr(
                self.provider,
                "prompt_version",
                "unspecified-provider-prompt",
            ),
            "workflow_version": self.workflow_version,
            "decision_schema_version": self.decision_schema_version,
            "timezone": self.settings.timezone,
            "market_universe_hash": self.universe.content_hash,
            "llm_timeout_seconds": self.settings.llm_timeout_seconds,
            "llm_max_retries": self.settings.llm_max_retries,
            "agent_models": {
                agent.id: self._model_name_for_agent(agent.id)
                for agent in AGENTS
            },
        }

    def run(self, *, as_of: datetime | None = None) -> WorkflowRun:
        """Prepare and execute a run synchronously (used by Demo and tests)."""

        return self.execute_prepared(self.prepare_run(as_of=as_of))

    def prepare_run(
        self,
        *,
        as_of: datetime | None = None,
        initial_status: RunStatus = RunStatus.QUEUED,
        persist: bool = True,
        external_input_bindings: dict[str, Any] | None = None,
    ) -> PreparedRun:
        """Freeze inputs, optionally persisting before any draft is consumed."""

        if initial_status not in {RunStatus.QUEUED, RunStatus.AWAITING_DRAFT}:
            raise ValueError("initial run status must be queued or awaiting_draft")

        as_of = self._normalize_as_of(as_of)
        frozen_external_inputs = _freeze_external_input_bindings(
            external_input_bindings
        )
        evidence_snapshot = load_evidence_snapshot(
            self.settings,
            as_of=as_of,
            source=self.evidence_source,
            universe=self.universe,
        )
        frozen_wiki = self.wiki.freeze(
            allow_demo_fallback=self.settings.use_demo_provider,
            cutoff=(
                None
                if self.settings.use_demo_provider
                else evidence_snapshot.data_cutoff
            ),
        )
        run_id = str(uuid4())
        started_at = datetime.now(ZoneInfo(self.settings.timezone))
        wiki_entries = frozen_wiki.list_entries()
        mode = "demo" if self.settings.use_demo_provider else "live"
        with self.database.session_factory() as session:
            believability_snapshot = build_believability_snapshot(
                session,
                mode=mode,
                as_of=as_of,
                data_cutoff=evidence_snapshot.data_cutoff,
                agent_scopes=self._believability_agent_scopes(),
                index_codes=self.universe.codes,
                horizons=tuple(horizon.value for horizon in self.universe.horizons),
                market_universe_hash=self.universe.content_hash,
                required_live_target_dates=self.settings.reflection_shadow_target_dates,
                required_approved_reflections=(
                    self.settings.reflection_required_human_reviews
                ),
            )
            believability_binding_hash = believability_run_binding_hash(
                run_id,
                believability_snapshot.content_hash,
            )
            hash_payload = {
                "as_of": as_of.isoformat(),
                "wiki": [
                    (
                        entry.id,
                        entry.version,
                        entry.content_hash,
                        (
                            None
                            if entry.published_at is None
                            else entry.published_at.isoformat()
                        ),
                    )
                    for entry in wiki_entries
                ],
                "provider": self.provider.name,
                "provider_endpoint": (
                    self.settings.llm_base_url
                    if self.settings.execution_mode == "api"
                    else None
                ),
                "prompt_version": getattr(
                    self.provider,
                    "prompt_version",
                    "unspecified-provider-prompt",
                ),
                "workflow_version": self.workflow_version,
                "decision_schema_version": self.decision_schema_version,
                "agents": legacy_v1_agent_hash_projection(
                    {
                        agent.id: self._model_name_for_agent(agent.id)
                        for agent in AGENTS
                    }
                ),
                "aggregation": {
                    "effective_research_agents": EFFECTIVE_RESEARCH_AGENT_IDS,
                    "strategy_agent": STRATEGY_AGENT_ID,
                    "cio_direction_source": STRATEGY_AGENT_ID,
                    "directional_mass_haircut": DIRECTIONAL_MASS_HAIRCUT,
                    "believability_policy_version": BELIEVABILITY_POLICY_VERSION,
                    "believability_snapshot_hash": believability_snapshot.content_hash,
                    "believability_snapshot_binding_hash": believability_binding_hash,
                    "believability_applied_to_decision": False,
                },
                "evidence_snapshot": evidence_snapshot.content_hash,
            }
            # Preserve legacy input hashes for the default universe and for runs
            # without externally bound Quant inputs; add each seal only when used.
            if self.uses_configurable_universe:
                hash_payload["market_universe"] = {
                    "schema_version": self.universe.schema_version,
                    "universe_id": self.universe.universe_id,
                    "version": self.universe.version,
                    "content_hash": self.universe.content_hash,
                }
            if frozen_external_inputs:
                hash_payload["external_input_bindings"] = frozen_external_inputs
            input_hash = hashlib.sha256(
                json.dumps(hash_payload, sort_keys=True).encode()
            ).hexdigest()
            row = WorkflowRun(
                id=run_id,
                as_of=as_of,
                data_cutoff=evidence_snapshot.data_cutoff,
                status=initial_status.value,
                mode=mode,
                started_at=started_at,
                completed_at=None,
                duration_seconds=None,
                error=None,
                data_quality={
                    "believability_snapshot": believability_snapshot.model_dump(
                        mode="json"
                    ),
                    # Persist this seal before execution so awaiting file handoffs
                    # can be selected idempotently after a process restarts with a
                    # different configured universe.
                    "market_universe": {
                        "schema_version": self.universe.schema_version,
                        "universe_id": self.universe.universe_id,
                        "version": self.universe.version,
                        "market": self.universe.market,
                        "timezone": self.universe.timezone,
                        "calendar_id": self.universe.calendar_id,
                        "session_close": self.universe.session_close,
                        "instrument_count": len(self.instruments),
                        "content_hash": self.universe.content_hash,
                    },
                },
                workflow_steps=[],
                input_hash=input_hash,
                market_universe_hash=self.universe.content_hash,
            )
            if persist:
                session.add(row)
                session.commit()
                session.refresh(row)

        initial: CommitteeState = {
            "run_id": run_id,
            "as_of": as_of.isoformat(),
            "data_cutoff": evidence_snapshot.data_cutoff.isoformat(),
            "input_hash": input_hash,
            "opinions": [],
            "forecasts": [],
            "workflow_steps": [],
            "data_quality": {},
            "evidence_snapshot": evidence_snapshot.model_dump(mode="json"),
            "wiki_snapshot": frozen_wiki.snapshot(),
            "believability_snapshot_hash": believability_snapshot.content_hash,
            "believability_snapshot_binding_hash": believability_binding_hash,
            "believability_policy_version": BELIEVABILITY_POLICY_VERSION,
        }
        if frozen_external_inputs:
            initial["external_input_bindings"] = frozen_external_inputs
        if self.uses_configurable_universe:
            initial["market_universe"] = self.universe.model_dump(mode="json")
        return PreparedRun(
            row=row,
            initial=initial,
            execution_manifest=self.execution_manifest(),
        )

    def execute_prepared(
        self,
        prepared: PreparedRun,
        *,
        raise_errors: bool = True,
        execution_fence: ExecutionFence | None = None,
        allow_recovery: bool = False,
        retryable_failure: bool = False,
        checkpoint_thread_id: str | None = None,
    ) -> WorkflowRun:
        """Execute a previously frozen run, persisting success or failure."""

        run_id = prepared.row.id
        expected_status = prepared.row.status
        initial_statuses = {
            RunStatus.QUEUED.value,
            RunStatus.AWAITING_DRAFT.value,
        }
        recovering = allow_recovery and expected_status == RunStatus.RUNNING.value
        if expected_status not in initial_statuses and not recovering:
            raise RuntimeError(f"run {run_id} cannot execute from status {expected_status}")
        started_at = datetime.now(ZoneInfo(self.settings.timezone))
        with self.database.session_factory() as session:
            persistent = session.get(WorkflowRun, run_id)
            if persistent is None:
                raise RuntimeError(f"prepared run disappeared: {run_id}")
            self._fence_task_execution(
                session,
                execution_fence,
                run_id=run_id,
                stage="executing",
            )
            if persistent.status != expected_status:
                raise RuntimeError(f"run {run_id} was already claimed or finalized")
            try:
                self._validate_market_universe_state(prepared.initial)
                if persistent.market_universe_hash != self.universe.content_hash:
                    raise RuntimeError(
                        "prepared run market universe hash no longer matches "
                        "the database seal"
                    )
                self._validate_believability_seal(
                    persistent,
                    prepared.initial,
                )
                if (
                    persistent.input_hash != prepared.initial.get("input_hash")
                    or persistent.input_hash != prepared.row.input_hash
                ):
                    raise RuntimeError(
                        "prepared run input hash no longer matches the database seal"
                    )
            except Exception as exc:
                failed_at = datetime.now(ZoneInfo(self.settings.timezone))
                failure_values = self._failure_values(
                    exc,
                    started_at=started_at,
                    completed_at=failed_at,
                    retryable=retryable_failure,
                )
                marked = session.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == run_id,
                        WorkflowRun.status == expected_status,
                    )
                    .values(**failure_values)
                )
                if marked.rowcount == 1:
                    session.commit()
                    session.refresh(persistent)
                    if not raise_errors:
                        return persistent
                else:  # pragma: no cover - requires a concurrent claimant
                    session.rollback()
                raise
            if not recovering:
                claimed = session.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == run_id,
                        WorkflowRun.status == expected_status,
                    )
                    .values(
                        status=RunStatus.RUNNING.value,
                        started_at=started_at,
                    )
                )
                if claimed.rowcount != 1:
                    session.rollback()
                    raise RuntimeError(f"run {run_id} was already claimed or finalized")
            session.commit()
        try:
            result = self.graph.invoke(
                prepared.initial,
                config={
                    "configurable": {
                        "thread_id": checkpoint_thread_id or run_id,
                    }
                },
            )
            completed_at = datetime.now(ZoneInfo(self.settings.timezone))
            with self.database.session_factory() as session:
                persistent = session.get(WorkflowRun, run_id)
                assert persistent is not None
                self._finalize_task_execution(
                    session,
                    execution_fence,
                    run_id=run_id,
                )
                if persistent.status != RunStatus.RUNNING.value:
                    raise RuntimeError(f"run {run_id} changed status during workflow execution")
                self._validate_believability_seal(
                    persistent,
                    prepared.initial,
                )
                if (
                    persistent.input_hash != prepared.initial.get("input_hash")
                    or persistent.input_hash != prepared.row.input_hash
                ):
                    raise RuntimeError("prepared run input hash changed during workflow execution")
                self._validate_result_believability_boundary(
                    persistent,
                    result,
                    prepared.initial,
                )
                self._persist_result(session, result)
                persistent.status = RunStatus.COMPLETED.value
                persistent.completed_at = completed_at
                persistent.duration_seconds = max(0.0, (completed_at - started_at).total_seconds())
                persistent.error = None
                persistent.workflow_steps = result.get("workflow_steps", [])
                persistent.data_quality = {
                    **(persistent.data_quality or {}),
                    **result.get("data_quality", {}),
                }
                session.commit()
                session.refresh(persistent)
                return persistent
        except StaleTaskLeaseError:
            raise
        except Exception as exc:
            completed_at = datetime.now(ZoneInfo(self.settings.timezone))
            with self.database.session_factory() as session:
                failed = session.get(WorkflowRun, run_id)
                assert failed is not None
                self._fence_task_execution(
                    session,
                    execution_fence,
                    run_id=run_id,
                    stage="failed_attempt",
                )
                values = self._failure_values(
                    exc,
                    started_at=started_at,
                    completed_at=completed_at,
                    retryable=retryable_failure,
                )
                for key, value in values.items():
                    setattr(failed, key, value)
                session.commit()
                session.refresh(failed)
                if not raise_errors:
                    return failed
            raise

    def _fence_task_execution(
        self,
        session: Session,
        fence: ExecutionFence | None,
        *,
        run_id: str,
        stage: str,
    ) -> None:
        if fence is None:
            return
        fence_execution(
            session,
            fence,
            run_id=run_id,
            timezone=self.settings.timezone,
            stage=stage,
        )

    def _finalize_task_execution(
        self,
        session: Session,
        fence: ExecutionFence | None,
        *,
        run_id: str,
    ) -> None:
        if fence is None:
            return
        finalize_execution_fence(
            session,
            fence,
            run_id=run_id,
            timezone=self.settings.timezone,
        )

    @staticmethod
    def _failure_values(
        error: Exception,
        *,
        started_at: datetime,
        completed_at: datetime,
        retryable: bool,
    ) -> dict[str, Any]:
        if retryable:
            return {
                "status": RunStatus.QUEUED.value,
                "started_at": started_at,
                "completed_at": None,
                "duration_seconds": None,
                "error": str(error),
            }
        return {
            "status": RunStatus.FAILED.value,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": max(
                0.0,
                (completed_at - started_at).total_seconds(),
            ),
            "error": str(error),
        }

    def _build_graph(self):
        builder = StateGraph(CommitteeState)
        builder.add_node("freeze_snapshot", self._freeze_snapshot)
        for agent_id in RESEARCH_AGENT_IDS:
            builder.add_node(agent_id, self._research_node(agent_id))
        builder.add_node(STRATEGY_AGENT_ID, self._strategy_node)
        builder.add_node("risk_critic_agent", self._critic_node)
        builder.add_node("evidence_validator", self._validate_evidence)
        builder.add_node("cio_agent", self._cio_node)

        builder.add_edge(START, "freeze_snapshot")
        for agent_id in RESEARCH_AGENT_IDS:
            builder.add_edge("freeze_snapshot", agent_id)
        builder.add_edge(list(RESEARCH_AGENT_IDS), STRATEGY_AGENT_ID)
        builder.add_edge(STRATEGY_AGENT_ID, "risk_critic_agent")
        builder.add_edge("risk_critic_agent", "evidence_validator")
        builder.add_edge("evidence_validator", "cio_agent")
        builder.add_edge("cio_agent", END)

        checkpointer = None
        if SqliteSaver is not None:
            checkpoint_path = Path(self.settings.checkpoint_path)
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
            checkpointer = SqliteSaver(self._checkpoint_connection)
            checkpointer.setup()
        return builder.compile(checkpointer=checkpointer)

    def _freeze_snapshot(self, state: CommitteeState) -> CommitteeState:
        now = _now_iso(self.settings.timezone)
        entries = _frozen_wiki(state).list_entries()
        snapshot = FrozenEvidenceSnapshot.model_validate(state["evidence_snapshot"])
        quality = {
            **state.get("data_quality", {}),
            "status": "demo" if self.settings.use_demo_provider else "ready",
            "market_data": "demo-volatility" if self.settings.use_demo_provider else "provided",
            "wiki_entries": len(entries),
            "wiki_has_sources": sum(bool(entry.source_urls) for entry in entries),
            "future_information_check": "passed",
            "evidence_items": len(snapshot.items),
            "evidence_snapshot_hash": snapshot.content_hash,
            "market_provenance_count": len(snapshot.market_data),
            "market_universe": {
                "schema_version": self.universe.schema_version,
                "universe_id": self.universe.universe_id,
                "version": self.universe.version,
                "market": self.universe.market,
                "timezone": self.universe.timezone,
                "calendar_id": self.universe.calendar_id,
                "session_close": self.universe.session_close,
                "instrument_count": len(self.instruments),
                "content_hash": self.universe.content_hash,
            },
            "trading_calendar_source": snapshot.trading_calendar.source_url,
            "trading_calendar_hash": snapshot.trading_calendar.source_hash,
            "trading_calendar_sessions": [
                session.isoformat() for session in snapshot.trading_calendar.sessions
            ],
            "believability": {
                "policy_version": state["believability_policy_version"],
                "snapshot_hash": state["believability_snapshot_hash"],
                "run_binding_hash": state["believability_snapshot_binding_hash"],
                "mode": "shadow_only",
                "applied_to_decision": False,
            },
            "warning": (
                "离线演示不包含实时行情与资讯，不可用于投资决策。"
                if self.settings.use_demo_provider
                else None
            ),
        }
        return {
            "data_quality": quality,
            "workflow_steps": [_step("freeze_snapshot", "冻结行情、资讯与 Wiki 版本", now)],
        }

    def _research_node(self, agent_id: str):
        def node(state: CommitteeState) -> CommitteeState:
            started = _now_iso(self.settings.timezone)
            as_of = datetime.fromisoformat(state["as_of"])
            agent = AGENT_BY_ID[agent_id]
            snapshot = FrozenEvidenceSnapshot.model_validate(state["evidence_snapshot"])
            wiki = _frozen_wiki(state)
            additions: list[dict[str, Any]] = []
            for index in self.instruments:
                for horizon in self.universe.horizons:
                    draft = self.provider.research(
                        agent_id=agent_id,
                        index=index,
                        horizon=horizon,
                        as_of=as_of,
                        wiki=wiki,
                        evidence_snapshot=snapshot,
                    )
                    _validate_draft_wiki(
                        draft=draft,
                        wiki=wiki,
                        agent_id=agent_id,
                        index_code=index.code,
                        preferred_entry_id=index.wiki_entry_id_for(agent_id),
                    )
                    citation = wiki.citation(
                        agent_id,
                        section=draft.wiki_section,
                        index_code=index.code,
                        preferred_entry_id=index.wiki_entry_id_for(agent_id),
                    )
                    citations = _bind_evidence_citations(
                        draft=draft,
                        wiki_citation=citation,
                        snapshot=snapshot,
                        index_code=index.code,
                        require_dynamic=(
                            not self.settings.use_demo_provider
                            and agent_id in EFFECTIVE_RESEARCH_AGENT_IDS
                        ),
                    )
                    model_name = self._model_name_for_agent(agent_id)
                    raw_response = draft.model_dump(mode="json")
                    raw_response["model_provenance"] = {
                        "provider": self.provider.name,
                        "model_name": model_name,
                    }
                    additions.append(
                        {
                            "id": str(uuid4()),
                            "run_id": state["run_id"],
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "role": index.agent_brief_for(agent.id) or agent.role,
                            "agent_version": agent.version,
                            "model_name": model_name,
                            "status": agent.status,
                            "index_code": index.code,
                            "horizon": horizon.value,
                            "target_date": _target_date(snapshot, horizon).isoformat(),
                            "direction": draft.direction.value,
                            "probabilities": draft.probabilities.model_dump(),
                            "summary": draft.summary,
                            "evidence": draft.evidence,
                            "counter_evidence": draft.counter_evidence,
                            "invalidation_conditions": draft.invalidation_conditions,
                            "citations": citations,
                            "contribution": "提供策略研究输入",
                            "weight": agent.weight,
                            "raw_response": raw_response,
                        }
                    )
            return {
                "opinions": additions,
                "workflow_steps": [_step(agent_id, agent.name, started)],
            }

        return node

    def _strategy_node(self, state: CommitteeState) -> CommitteeState:
        started = _now_iso(self.settings.timezone)
        as_of = datetime.fromisoformat(state["as_of"])
        agent = AGENT_BY_ID[STRATEGY_AGENT_ID]
        snapshot = FrozenEvidenceSnapshot.model_validate(state["evidence_snapshot"])
        wiki = _frozen_wiki(state)
        additions: list[dict[str, Any]] = []
        for index in self.instruments:
            for horizon in self.universe.horizons:
                research_opinions = [
                    opinion
                    for opinion in state.get("opinions", [])
                    if opinion["index_code"] == index.code
                    and opinion["horizon"] == horizon.value
                    and opinion["agent_id"] in EFFECTIVE_RESEARCH_AGENT_IDS
                ]
                received_research_agents = {opinion["agent_id"] for opinion in research_opinions}
                if len(research_opinions) != len(EFFECTIVE_RESEARCH_AGENT_IDS) or (
                    received_research_agents != set(EFFECTIVE_RESEARCH_AGENT_IDS)
                ):
                    raise ValueError(
                        f"strategy agent received incomplete inputs for "
                        f"{index.code}/{horizon.value}"
                    )
                safe_context = [
                    {
                        "agent_id": opinion["agent_id"],
                        "direction": opinion["direction"],
                        "probabilities": opinion["probabilities"],
                        "summary": opinion["summary"],
                        "evidence": opinion["evidence"],
                        "counter_evidence": opinion["counter_evidence"],
                        "evidence_item_ids": opinion.get("raw_response", {}).get(
                            "evidence_item_ids", []
                        ),
                        "evidence_sources": [
                            {
                                "evidence_item_id": citation.get("evidence_item_id"),
                                "source_url": citation.get("source_url"),
                                "evidence_content_hash": citation.get("evidence_content_hash"),
                            }
                            for citation in opinion.get("citations", [])
                            if citation.get("evidence_item_id")
                        ],
                    }
                    for opinion in research_opinions
                ]
                peer_opinions = [
                    opinion
                    for opinion in state.get("opinions", [])
                    if opinion["horizon"] == horizon.value
                    and opinion["index_code"] != index.code
                    and opinion["agent_id"] in EFFECTIVE_RESEARCH_AGENT_IDS
                ]
                expected_peer_identities = {
                    (peer_index.code, peer_agent_id)
                    for peer_index in self.instruments
                    if peer_index.code != index.code
                    for peer_agent_id in EFFECTIVE_RESEARCH_AGENT_IDS
                }
                received_peer_identities = {
                    (opinion["index_code"], opinion["agent_id"]) for opinion in peer_opinions
                }
                if len(peer_opinions) != len(expected_peer_identities) or (
                    received_peer_identities != expected_peer_identities
                ):
                    raise ValueError(
                        f"strategy agent received incomplete peer inputs for {horizon.value}"
                    )
                peer_context = [
                    {
                        "agent_id": opinion["agent_id"],
                        "index_code": opinion["index_code"],
                        "direction": opinion["direction"],
                        "probabilities": opinion["probabilities"],
                        "summary": opinion["summary"],
                        "evidence_item_ids": opinion.get("raw_response", {}).get(
                            "evidence_item_ids", []
                        ),
                        "evidence_sources": [
                            {
                                "evidence_item_id": citation.get("evidence_item_id"),
                                "source_url": citation.get("source_url"),
                                "evidence_content_hash": citation.get("evidence_content_hash"),
                            }
                            for citation in opinion.get("citations", [])
                            if citation.get("evidence_item_id")
                        ],
                    }
                    for opinion in peer_opinions
                ]
                draft = self.provider.strategize(
                    index=index,
                    horizon=horizon,
                    as_of=as_of,
                    data_cutoff=snapshot.data_cutoff,
                    volatility_20d=snapshot.volatility_20d[index.code],
                    wiki=wiki,
                    research_opinions=safe_context,
                    peer_opinions=peer_context,
                )
                strategy_entry = wiki.select_for_agent(
                    agent.id,
                    index_code=index.code,
                    preferred_entry_id=index.wiki_entry_id_for(agent.id),
                )
                if draft.wiki_entry_id != strategy_entry.id:
                    raise ValueError("strategy agent referenced an unavailable Wiki entry")
                if draft.wiki_section not in {section.slug for section in strategy_entry.sections}:
                    raise ValueError("strategy agent referenced an unavailable Wiki section")
                upstream_evidence_ids = {
                    str(evidence_id)
                    for opinion in safe_context
                    for evidence_id in opinion["evidence_item_ids"]
                }
                if not self.settings.use_demo_provider and not draft.evidence_item_ids:
                    raise ValueError("live strategy opinions must cite upstream evidence_item_ids")
                if not set(draft.evidence_item_ids).issubset(upstream_evidence_ids):
                    raise ValueError(
                        "strategy agent referenced evidence outside its research inputs"
                    )
                wiki_citation = wiki.citation(
                    agent.id,
                    section=draft.wiki_section,
                    index_code=index.code,
                    preferred_entry_id=index.wiki_entry_id_for(agent.id),
                )
                citations = _bind_evidence_citations(
                    draft=draft,
                    wiki_citation=wiki_citation,
                    snapshot=snapshot,
                    index_code=index.code,
                    require_dynamic=not self.settings.use_demo_provider,
                )
                model_name = self._model_name_for_agent(agent.id)
                raw_response = draft.model_dump(mode="json")
                raw_response["model_provenance"] = {
                    "provider": self.provider.name,
                    "model_name": model_name,
                }
                additions.append(
                    {
                        "id": str(uuid4()),
                        "run_id": state["run_id"],
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "role": index.agent_brief_for(agent.id) or agent.role,
                        "agent_version": agent.version,
                        "model_name": model_name,
                        "status": agent.status,
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "target_date": _target_date(snapshot, horizon).isoformat(),
                        "direction": draft.direction.value,
                        "probabilities": draft.probabilities.model_dump(),
                        "summary": draft.summary,
                        "evidence": draft.evidence,
                        "counter_evidence": draft.counter_evidence,
                        "invalidation_conditions": draft.invalidation_conditions,
                        "citations": citations,
                        "contribution": "综合基础研究，作为 CIO 的唯一方向输入",
                        "weight": agent.weight,
                        "raw_response": raw_response,
                    }
                )
        _annotate_strategy_context(additions, instruments=self.instruments)
        return {
            "opinions": additions,
            "workflow_steps": [_step(agent.id, agent.name, started)],
        }

    def _critic_node(self, state: CommitteeState) -> CommitteeState:
        started = _now_iso(self.settings.timezone)
        as_of = datetime.fromisoformat(state["as_of"])
        agent = AGENT_BY_ID["risk_critic_agent"]
        snapshot = FrozenEvidenceSnapshot.model_validate(state["evidence_snapshot"])
        wiki = _frozen_wiki(state)
        additions: list[dict[str, Any]] = []
        for index in self.instruments:
            for horizon in self.universe.horizons:
                research_opinions = [
                    opinion
                    for opinion in state.get("opinions", [])
                    if opinion["index_code"] == index.code
                    and opinion["horizon"] == horizon.value
                    and opinion["agent_id"] in CRITIC_INPUT_AGENT_IDS
                ]
                if len(research_opinions) != len(CRITIC_INPUT_AGENT_IDS):
                    raise ValueError(
                        f"critic received incomplete inputs for {index.code}/{horizon}"
                    )
                safe_context = [
                    {
                        "agent_id": opinion["agent_id"],
                        "direction": opinion["direction"],
                        "probabilities": opinion["probabilities"],
                        "summary": opinion["summary"],
                        "evidence": opinion["evidence"],
                        "counter_evidence": opinion["counter_evidence"],
                    }
                    for opinion in research_opinions
                ]
                draft = self.provider.criticize(
                    index=index,
                    horizon=horizon,
                    as_of=as_of,
                    wiki=wiki,
                    research_opinions=safe_context,
                )
                _validate_draft_wiki(
                    draft=draft,
                    wiki=wiki,
                    agent_id=agent.id,
                    index_code=index.code,
                    preferred_entry_id=index.wiki_entry_id_for(agent.id),
                )
                if draft.evidence_item_ids:
                    raise ValueError(
                        "risk critic may only challenge frozen inputs and must not "
                        "introduce new evidence_item_ids"
                    )
                citation = wiki.citation(
                    agent.id,
                    section=draft.wiki_section,
                    index_code=index.code,
                    preferred_entry_id=index.wiki_entry_id_for(agent.id),
                ).model_dump(mode="json")
                raw_response = draft.model_dump(mode="json")
                raw_response["model_provenance"] = {
                    "provider": self.provider.name,
                    "model_name": self._model_name_for_agent(agent.id),
                }
                additions.append(
                    {
                        "id": str(uuid4()),
                        "run_id": state["run_id"],
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "role": index.agent_brief_for(agent.id) or agent.role,
                        "agent_version": agent.version,
                        "model_name": self._model_name_for_agent(agent.id),
                        "status": agent.status,
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "target_date": _target_date(snapshot, horizon).isoformat(),
                        "direction": draft.direction.value,
                        "probabilities": draft.probabilities.model_dump(),
                        "summary": draft.summary,
                        "evidence": draft.evidence,
                        "counter_evidence": draft.counter_evidence,
                        "invalidation_conditions": draft.invalidation_conditions,
                        "citations": [citation],
                        "contribution": "不做方向投票；用于反证并提高小波动结果概率",
                        "weight": 0.0,
                        "raw_response": raw_response,
                    }
                )
        return {
            "opinions": additions,
            "workflow_steps": [_step(agent.id, agent.name, started)],
        }

    def _validate_evidence(self, state: CommitteeState) -> CommitteeState:
        started = _now_iso(self.settings.timezone)
        checked = 0
        dynamic_checked = 0
        snapshot = FrozenEvidenceSnapshot.model_validate(state["evidence_snapshot"])
        evidence_by_id = {item.id: item for item in snapshot.items}
        wiki = _frozen_wiki(state)
        for opinion in state.get("opinions", []):
            if not opinion["citations"]:
                raise ValueError(f"{opinion['agent_id']} produced an uncited opinion")
            bound_ids: set[str] = set()
            for raw in opinion["citations"]:
                citation = Citation.model_validate(raw)
                if not wiki.citation_is_valid(citation):
                    raise ValueError(
                        f"invalid Wiki citation {citation.wiki_entry_id}@{citation.wiki_version}"
                    )
                if citation.evidence_item_id is not None:
                    item = evidence_by_id.get(citation.evidence_item_id)
                    if item is None or not _citation_matches_evidence(citation, item):
                        raise ValueError(
                            f"invalid frozen evidence citation {citation.evidence_item_id}"
                        )
                    bound_ids.add(citation.evidence_item_id)
                    dynamic_checked += 1
                elif any(
                    value is not None
                    for value in (
                        citation.source_url,
                        citation.evidence_content_hash,
                        citation.event_time,
                        citation.published_at,
                        citation.ingested_at,
                    )
                ):
                    raise ValueError("partial dynamic citation is missing evidence_item_id")
                if citation.published_at and citation.published_at > datetime.fromisoformat(
                    state["data_cutoff"]
                ):
                    raise ValueError("citation is newer than the frozen data cutoff")
                checked += 1
            if (
                not self.settings.use_demo_provider
                and opinion["agent_id"] in DYNAMIC_EVIDENCE_AGENT_IDS
            ):
                declared_ids = set(opinion.get("raw_response", {}).get("evidence_item_ids", []))
                if not bound_ids or bound_ids != declared_ids:
                    raise ValueError(
                        f"{opinion['agent_id']} evidence IDs were not structurally bound"
                    )
        quality = {
            **state.get("data_quality", {}),
            "citations_validated": checked,
            "dynamic_citations_validated": dynamic_checked,
            "frozen_evidence_ids_available": len(evidence_by_id),
        }
        return {
            "data_quality": quality,
            "workflow_steps": [_step("evidence_validator", "证据与时间截面校验", started)],
        }

    def _cio_node(self, state: CommitteeState) -> CommitteeState:
        started = _now_iso(self.settings.timezone)
        as_of = datetime.fromisoformat(state["as_of"])
        snapshot = FrozenEvidenceSnapshot.model_validate(state["evidence_snapshot"])
        forecasts: list[dict[str, Any]] = []
        cio_opinions: list[dict[str, Any]] = []
        for index in self.instruments:
            for horizon in self.universe.horizons:
                research_inputs = [
                    opinion
                    for opinion in state.get("opinions", [])
                    if opinion["index_code"] == index.code
                    and opinion["horizon"] == horizon.value
                    and opinion["agent_id"] in EFFECTIVE_RESEARCH_AGENT_IDS
                ]
                if len(research_inputs) != len(EFFECTIVE_RESEARCH_AGENT_IDS):
                    raise ValueError(f"incomplete research set for {index.code}/{horizon.value}")
                strategy_opinion = next(
                    (
                        opinion
                        for opinion in state.get("opinions", [])
                        if opinion["agent_id"] == STRATEGY_AGENT_ID
                        and opinion["index_code"] == index.code
                        and opinion["horizon"] == horizon.value
                    ),
                    None,
                )
                if strategy_opinion is None:
                    raise ValueError(f"missing strategy opinion for {index.code}/{horizon.value}")
                # The critic has no directional vote. A fixed symmetric haircut moves
                # probability mass from both directional outcomes to the small-move
                # outcome bucket without changing the strategy agent's up/down ordering.
                risk_opinion = next(
                    opinion
                    for opinion in state["opinions"]
                    if opinion["agent_id"] == "risk_critic_agent"
                    and opinion["index_code"] == index.code
                    and opinion["horizon"] == horizon.value
                )
                parsed = _apply_symmetric_haircut(
                    Probabilities.model_validate(strategy_opinion["probabilities"]),
                    haircut=DIRECTIONAL_MASS_HAIRCUT,
                )
                direction = _direction(parsed)
                confidence = directional_confidence(parsed.as_dict())
                strategy_context = strategy_opinion.get("raw_response", {}).get(
                    "strategy_context", {}
                )
                quant_context = (
                    "Quant 待接入且权重为0"
                    if "quant_agent"
                    not in state.get("external_input_bindings", {})
                    else "Quant 已作为只读 shadow 输入，正式决策权重为0"
                )
                citations = _deduplicate_citations(
                    citation
                    for opinion in [*research_inputs, strategy_opinion]
                    for citation in opinion["citations"]
                )
                counter = _deduplicate_strings(
                    item
                    for opinion in [*research_inputs, strategy_opinion, risk_opinion]
                    for item in opinion["counter_evidence"]
                )
                invalidation = _deduplicate_strings(
                    item
                    for opinion in [*research_inputs, strategy_opinion, risk_opinion]
                    for item in opinion["invalidation_conditions"]
                )
                rationale = (
                    f"策略研究员综合三位有效研究 Agent，形成 {index.name}{horizon.value} "
                    f"的唯一方向输入（{len(self.instruments)} 个标的配置排序"
                    f"{'并列' if strategy_context.get('rank_tied') else ''}第"
                    f"{strategy_context.get('relative_rank', '未记录')}/"
                    f"{len(self.instruments)}）；"
                    f"{quant_context}，Risk Critic 不投方向票；CIO 将上下行"
                    f"结果概率各缩减 {DIRECTIONAL_MASS_HAIRCUT:.0%}，移入小波动结果桶。"
                    f"强制二元方向为"
                    f"{direction.value}（排除小波动结果后的方向置信度 {confidence:.1%}，"
                    f"小波动概率 {parsed.neutral:.1%}）。"
                )
                threshold = neutral_threshold(snapshot.volatility_20d[index.code], horizon)
                forecast = {
                    "id": str(uuid4()),
                    "run_id": state["run_id"],
                    "index_code": index.code,
                    "index_name": index.name,
                    "horizon": horizon.value,
                    "base_trade_date": snapshot.base_session.isoformat(),
                    "target_date": _target_date(snapshot, horizon).isoformat(),
                    "as_of": as_of.isoformat(),
                    "data_cutoff": state["data_cutoff"],
                    "direction": direction.value,
                    "probabilities": parsed.model_dump(),
                    "threshold": threshold,
                    "confidence": confidence,
                    "rationale": rationale,
                    "counter_evidence": counter,
                    "invalidation_conditions": invalidation,
                    "citations": citations,
                    "abstain": False,
                    "model_name": self._model_name_for_agent("cio_agent"),
                    "model_version": self.workflow_version,
                    "wiki_version": hashlib.sha256(
                        "|".join(
                            f"{item['wiki_entry_id']}@{item['wiki_version']}" for item in citations
                        ).encode()
                    ).hexdigest()[:16],
                    "input_hash": state["input_hash"],
                    "created_at": _now_iso(self.settings.timezone),
                }
                forecasts.append(forecast)
                cio = AGENT_BY_ID["cio_agent"]
                cio_opinions.append(
                    {
                        "id": str(uuid4()),
                        "run_id": state["run_id"],
                        "agent_id": cio.id,
                        "agent_name": cio.name,
                        "role": cio.role,
                        "agent_version": cio.version,
                        "model_name": self._model_name_for_agent("cio_agent"),
                        "status": cio.status,
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "target_date": _target_date(snapshot, horizon).isoformat(),
                        "direction": direction.value,
                        "probabilities": parsed.model_dump(),
                        "summary": rationale,
                        "evidence": [
                            strategy_opinion["summary"],
                            *[opinion["summary"] for opinion in research_inputs],
                        ],
                        "counter_evidence": counter,
                        "invalidation_conditions": invalidation,
                        "citations": citations,
                        "contribution": "形成最终投委会判断",
                        "weight": 0.0,
                        "raw_response": {
                            "model_provenance": {
                                "provider": "deterministic-committee-aggregation",
                                "model_name": self._model_name_for_agent("cio_agent"),
                            },
                        },
                    }
                )
        return {
            "opinions": cio_opinions,
            "forecasts": forecasts,
            "workflow_steps": [_step("cio_agent", AGENT_BY_ID["cio_agent"].name, started)],
        }

    @staticmethod
    def _persist_result(session: Session, state: CommitteeState) -> None:
        for source in state.get("opinions", []):
            opinion = dict(source)
            probabilities = Probabilities.model_validate(opinion.pop("probabilities"))
            expected_direction = predicted_direction(probabilities.as_dict()).value
            if opinion["direction"] != expected_direction:
                raise ValueError(
                    "new opinion direction must match the stronger up/down probability: "
                    f"{opinion['agent_id']}"
                )
            opinion["target_date"] = date.fromisoformat(opinion["target_date"])
            session.add(
                AgentOpinion(
                    **opinion,
                    probability_up=probabilities.up,
                    probability_neutral=probabilities.neutral,
                    probability_down=probabilities.down,
                )
            )
        for source in state.get("forecasts", []):
            forecast = dict(source)
            probabilities = Probabilities.model_validate(forecast.pop("probabilities"))
            expected_direction = predicted_direction(probabilities.as_dict()).value
            if forecast["direction"] != expected_direction:
                raise ValueError(
                    "new forecast direction must match the stronger up/down probability"
                )
            expected_confidence = directional_confidence(probabilities.as_dict())
            if abs(forecast["confidence"] - expected_confidence) > 1e-9:
                raise ValueError("new forecast confidence must use the directional definition")
            if forecast.get("abstain"):
                raise ValueError("new forecast must not abstain")
            forecast["as_of"] = datetime.fromisoformat(forecast["as_of"])
            forecast["data_cutoff"] = datetime.fromisoformat(forecast["data_cutoff"])
            forecast["created_at"] = datetime.fromisoformat(forecast["created_at"])
            forecast["base_trade_date"] = date.fromisoformat(forecast["base_trade_date"])
            forecast["target_date"] = date.fromisoformat(forecast["target_date"])
            session.add(
                Forecast(
                    **forecast,
                    probability_up=probabilities.up,
                    probability_neutral=probabilities.neutral,
                    probability_down=probabilities.down,
                )
            )

    def _model_name_for_agent(self, agent_id: str) -> str:
        if agent_id == "quant_agent":
            return "unavailable-no-quant-signal-v1"
        if agent_id == "cio_agent":
            return f"deterministic-committee-aggregation-v{self.workflow_version}"
        provider_resolver = getattr(self.provider, "model_name_for_agent", None)
        if callable(provider_resolver):
            return str(provider_resolver(agent_id))
        if self.settings.use_demo_provider:
            return self.provider.name
        return self.settings.llm_model

    def _validate_market_universe_state(self, state: CommitteeState) -> None:
        raw = state.get("market_universe")
        if raw is None:
            if self.uses_configurable_universe:
                raise RuntimeError("prepared run is missing its market universe seal")
            return
        frozen = MarketUniverseSpec.model_validate(raw)
        if frozen.model_dump(mode="json") != self.universe.model_dump(mode="json"):
            raise RuntimeError(
                "prepared run market universe no longer matches the runtime configuration"
            )

    def model_name_for_agent(self, agent_id: str) -> str:
        """Return the exact model identity used by the current agent version."""

        return self._model_name_for_agent(agent_id)

    def _believability_agent_scopes(self) -> tuple[BelievabilityAgentScope, ...]:
        scopes: list[BelievabilityAgentScope] = []
        for agent_id in (*EFFECTIVE_RESEARCH_AGENT_IDS, STRATEGY_AGENT_ID):
            agent = AGENT_BY_ID[agent_id]
            is_strategy = agent_id == STRATEGY_AGENT_ID
            scopes.append(
                BelievabilityAgentScope(
                    agent_id=agent.id,
                    agent_version=agent.version,
                    model_name=self._model_name_for_agent(agent.id),
                    role_domain="strategy" if is_strategy else "research",
                    stage=("strategy_to_cio" if is_strategy else "research_to_strategy"),
                    current_stage_weight_metadata=agent.weight,
                )
            )
        return tuple(scopes)

    @staticmethod
    def _validate_believability_seal(
        row: WorkflowRun,
        initial: CommitteeState,
    ) -> None:
        payload = (row.data_quality or {}).get("believability_snapshot")
        if not isinstance(payload, dict):
            raise RuntimeError("prepared run is missing its believability snapshot")
        try:
            snapshot = validate_believability_snapshot(payload)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        expected_hash = initial.get("believability_snapshot_hash")
        expected_binding_hash = initial.get("believability_snapshot_binding_hash")
        expected_policy = initial.get("believability_policy_version")
        if snapshot.content_hash != expected_hash:
            raise RuntimeError(
                "prepared run believability snapshot no longer matches the frozen state"
            )
        if snapshot.policy_version != expected_policy:
            raise RuntimeError(
                "prepared run believability policy no longer matches the frozen state"
            )
        if believability_run_binding_hash(row.id, snapshot.content_hash) != expected_binding_hash:
            raise RuntimeError("prepared run believability snapshot is bound to another run")
        if snapshot.mode != row.mode or snapshot.applied_to_decision:
            raise RuntimeError("prepared run believability boundary is invalid")

    @staticmethod
    def _validate_result_believability_boundary(
        row: WorkflowRun,
        result: CommitteeState,
        initial: CommitteeState,
    ) -> None:
        persisted_quality = row.data_quality or {}
        if "believability" in persisted_quality:
            raise RuntimeError(
                "prepared run believability runtime seal was written before completion"
            )
        result_quality = result.get("data_quality", {})
        if "believability_snapshot" in result_quality:
            raise RuntimeError(
                "workflow result may not replace the database believability snapshot"
            )
        expected_runtime = {
            "policy_version": initial.get("believability_policy_version"),
            "snapshot_hash": initial.get("believability_snapshot_hash"),
            "run_binding_hash": initial.get("believability_snapshot_binding_hash"),
            "mode": "shadow_only",
            "applied_to_decision": False,
        }
        if result_quality.get("believability") != expected_runtime:
            raise RuntimeError(
                "workflow result believability runtime seal does not match the frozen state"
            )

    def _normalize_as_of(self, value: datetime | None) -> datetime:
        timezone = ZoneInfo(self.settings.timezone)
        if value is None:
            now = datetime.now(timezone)
            close_hour, close_minute = (
                int(part)
                for part in self.universe.session_close.split(":", maxsplit=1)
            )
            return now.replace(
                hour=close_hour,
                minute=close_minute,
                second=0,
                microsecond=0,
            )
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone)
        return value.astimezone(timezone)


def _frozen_wiki(state: CommitteeState) -> FrozenWikiCatalog:
    return FrozenWikiCatalog(state["wiki_snapshot"])


def _freeze_external_input_bindings(
    bindings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Deep-copy optional external bindings into strict canonical JSON values."""

    if not bindings:
        return {}
    if any(not isinstance(key, str) or not key.strip() for key in bindings):
        raise ValueError("external input binding names must be non-blank strings")
    try:
        encoded = json.dumps(
            bindings,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("external input bindings must be finite JSON values") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - input annotation guards this.
        raise ValueError("external input bindings must be a JSON object")
    return decoded


def _validate_draft_wiki(
    *,
    draft: AgentDraft,
    wiki: FrozenWikiCatalog,
    agent_id: str,
    index_code: str,
    preferred_entry_id: str | None = None,
) -> None:
    """Apply provider-independent Wiki identity checks to every draft source."""

    entry = wiki.select_for_agent(
        agent_id,
        index_code=index_code,
        preferred_entry_id=preferred_entry_id,
    )
    if draft.wiki_entry_id != entry.id:
        raise ValueError(f"{agent_id} referenced an unavailable Wiki entry")
    if draft.wiki_section not in {section.slug for section in entry.sections}:
        raise ValueError(f"{agent_id} referenced an unavailable Wiki section")


def _bind_evidence_citations(
    *,
    draft: AgentDraft,
    wiki_citation: Citation,
    snapshot: FrozenEvidenceSnapshot,
    index_code: str,
    require_dynamic: bool,
) -> list[dict[str, Any]]:
    if require_dynamic and not draft.evidence_item_ids:
        raise ValueError("live research opinions must cite frozen evidence_item_ids")
    if not draft.evidence_item_ids:
        return [wiki_citation.model_dump(mode="json")]

    evidence_by_id = {item.id: item for item in snapshot.items}
    citations: list[dict[str, Any]] = []
    for evidence_id in draft.evidence_item_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            raise ValueError(f"model referenced unavailable frozen evidence {evidence_id}")
        if item.entities and index_code not in item.entities:
            raise ValueError(f"frozen evidence {evidence_id} is not scoped to index {index_code}")
        bound = wiki_citation.model_copy(
            deep=True,
            update={
                "quote": item.quote,
                "evidence_item_id": item.id,
                "source_url": item.source_url,
                "evidence_content_hash": item.content_hash,
                "event_time": item.event_time,
                "published_at": item.published_at,
                "ingested_at": item.ingested_at,
            },
        )
        citations.append(bound.model_dump(mode="json"))
    return citations


def _citation_matches_evidence(citation: Citation, item: EvidenceItem) -> bool:
    return (
        citation.evidence_item_id == item.id
        and citation.source_url == item.source_url
        and citation.evidence_content_hash == item.content_hash
        and citation.event_time == item.event_time
        and citation.published_at == item.published_at
        and citation.ingested_at == item.ingested_at
        and citation.quote == item.quote
    )


def _target_date(snapshot: FrozenEvidenceSnapshot, horizon: Horizon) -> date:
    if len(snapshot.target_sessions) != 2:
        raise ValueError("evidence snapshot does not contain D1/D2 target sessions")
    return snapshot.target_sessions[0 if horizon is Horizon.D1 else 1]


def _annotate_strategy_context(
    opinions: list[dict[str, Any]],
    *,
    instruments: tuple[IndexDefinition, ...],
) -> None:
    """Derive one coherent cross-instrument allocation view for each horizon.

    The target count and style buckets come from the frozen market universe,
    rather than a hard-coded A-share symbol list.
    """

    index_position = {
        instrument.code: position for position, instrument in enumerate(instruments)
    }
    bucket_by_code = {
        instrument.code: instrument.strategy_bucket for instrument in instruments
    }
    scope_label = (
        "五指数"
        if tuple(instrument.code for instrument in instruments)
        == DEFAULT_MARKET_UNIVERSE.codes
        else f"{len(instruments)} 个标的"
    )
    for horizon in Horizon:
        rows = [item for item in opinions if item["horizon"] == horizon.value]
        if len(rows) != len(instruments):
            raise ValueError(
                f"strategy context requires {len(instruments)} instruments "
                f"for {horizon.value}"
            )
        by_index = {item["index_code"]: item for item in rows}
        if set(by_index) != set(index_position):
            raise ValueError(
                f"strategy context has duplicate or missing instruments for {horizon.value}"
            )

        scores = {
            code: float(item["probabilities"]["up"]) - float(item["probabilities"]["down"])
            for code, item in by_index.items()
        }
        ranked_codes = sorted(
            scores,
            key=lambda code: (-scores[code], index_position[code]),
        )
        ranks: dict[str, int] = {}
        group_score: float | None = None
        current_rank = 1
        for position, code in enumerate(ranked_codes, start=1):
            if group_score is None or abs(scores[code] - group_score) > 1e-6:
                current_rank = position
                group_score = scores[code]
            ranks[code] = current_rank
        mean_score = sum(scores.values()) / len(scores)
        market_regime = (
            "risk_on" if mean_score > 0.05 else "risk_off" if mean_score < -0.05 else "balanced"
        )
        bucket_values: dict[str, list[float]] = {}
        for code, score in scores.items():
            bucket = bucket_by_code[code]
            if bucket != "balanced":
                bucket_values.setdefault(bucket, []).append(score)
        style_scores = {
            bucket: sum(values) / len(values)
            for bucket, values in bucket_values.items()
        }
        ordered_styles = sorted(
            style_scores,
            key=lambda bucket: (-style_scores[bucket], bucket),
        )
        if len(ordered_styles) == 1:
            style_bias = ordered_styles[0]
        elif (
            len(ordered_styles) >= 2
            and style_scores[ordered_styles[0]] - style_scores[ordered_styles[1]] >= 0.03
        ):
            style_bias = ordered_styles[0]
        else:
            style_bias = "balanced"

        for code in ranked_codes:
            item = by_index[code]
            rank = ranks[code]
            rank_tied = sum(value == rank for value in ranks.values()) > 1
            context = {
                "market_regime": market_regime,
                "style_bias": style_bias,
                "relative_rank": rank,
                "rank_tied": rank_tied,
                "allocation_score": round(scores[code], 6),
            }
            item["raw_response"]["strategy_context"] = context
            item["summary"] = (
                f"{item['summary']} {scope_label}相对配置排序"
                f"{'并列' if rank_tied else ''}第 {rank}/{len(instruments)}，"
                f"配置分数为 {scores[code]:+.3f}。"
            )


def _apply_symmetric_haircut(probabilities: Probabilities, *, haircut: float) -> Probabilities:
    if not 0 <= haircut <= 1:
        raise ValueError("haircut must be between zero and one")
    up = (1.0 - haircut) * probabilities.up
    down = (1.0 - haircut) * probabilities.down
    return Probabilities(up=up, neutral=1.0 - up - down, down=down)


def _direction(probabilities: Probabilities) -> Direction:
    return predicted_direction(probabilities.as_dict())


def _now_iso(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat()


def _step(step_id: str, label: str, started_at: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "status": "completed",
        "started_at": started_at,
        "completed_at": datetime.now().astimezone().isoformat(),
    }


def _deduplicate_citations(citations) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for citation in citations:
        key = (
            citation["wiki_entry_id"],
            citation["wiki_version"],
            citation["section"],
            citation.get("evidence_item_id"),
        )
        unique[key] = citation
    return list(unique.values())


def _deduplicate_strings(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
