from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.domain import INDEXES, Direction, Horizon, directional_confidence
from app.schemas import AgentDraft, Citation, Probabilities
from app.services.provider import LangChainResearchProvider
from app.services.snapshot import load_evidence_snapshot
from app.services.wiki import WikiCatalog
from app.workflow import (
    WORKFLOW_VERSION,
    CommitteeWorkflow,
    _apply_symmetric_haircut,
    _bind_evidence_citations,
    _citation_matches_evidence,
    workflow_runtime_versions,
)
from pydantic import ValidationError


class CapturingCriticProvider:
    name = "capturing-demo-model"

    def __init__(self) -> None:
        self.inputs: list[list[dict[str, object]]] = []
        self.strategy_inputs: list[list[dict[str, object]]] = []
        self.strategy_peer_inputs: list[list[dict[str, object]]] = []

    def strategize(self, **kwargs) -> AgentDraft:
        research_opinions = kwargs["research_opinions"]
        self.strategy_inputs.append(research_opinions)
        self.strategy_peer_inputs.append(kwargs["peer_opinions"])
        wiki = kwargs["wiki"]
        citation = wiki.citation("strategy_agent")
        return AgentDraft(
            direction=Direction.UP,
            probabilities=Probabilities(up=0.26, neutral=0.50, down=0.24),
            summary="综合三位有效研究员形成策略判断。",
            evidence=["检查结构化基础研究输入。"],
            counter_evidence=["基础观点可能同源。"],
            invalidation_conditions=["任一基础输入无效时失效。"],
            wiki_entry_id=citation.wiki_entry_id,
            wiki_section=citation.section,
        )

    def criticize(self, **kwargs) -> AgentDraft:
        research_opinions = kwargs["research_opinions"]
        self.inputs.append(research_opinions)
        wiki = kwargs["wiki"]
        citation = wiki.citation("risk_critic_agent")
        return AgentDraft(
            direction=Direction.DOWN,
            probabilities=Probabilities(up=0.05, neutral=0.75, down=0.20),
            summary="对三位有效研究员和策略综合做对称方向概率质量折减。",
            evidence=["检查结构化输入。"],
            counter_evidence=["存在共同来源风险。"],
            invalidation_conditions=["发现未来信息时失效。"],
            wiki_entry_id=citation.wiki_entry_id,
            wiki_section=citation.section,
        )


def _settings(tmp_path) -> Settings:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir(exist_ok=True)
    (wiki_path / "market-strategy.md").write_text(
        """---
id: VC-WIKI-MARKET-STRATEGY
title: Strategy
version: 1.0.0
status: active
tags: [strategy, allocation]
---
<!-- section:synthesis -->
# Strategy
Cross-index synthesis.
""",
        encoding="utf-8",
    )
    return Settings(
        demo_mode=True,
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        checkpoint_path=tmp_path / "checkpoint.sqlite3",
        wiki_path=wiki_path,
    )


def test_agent_draft_direction_must_match_stronger_directional_probability() -> None:
    with pytest.raises(ValidationError, match="stronger up/down probability"):
        AgentDraft(
            direction=Direction.DOWN,
            probabilities=Probabilities(up=0.3, neutral=0.6, down=0.1),
            summary="mismatch",
            evidence=["evidence"],
            wiki_entry_id="VC-WIKI-TEST",
            wiki_section="overview",
        )


def test_langchain_provider_uses_instrument_briefs_for_all_draft_roles(
    tmp_path,
) -> None:
    briefs = {
        "ai_storage_industry_agent": "研究该标的的产品周期、竞争格局与估值催化。",
        "strategy_agent": "综合该标的输入，判断相对强弱和配置优先级。",
        "risk_critic_agent": "检查该标的特有的监管、流动性和拥挤风险。",
    }
    index = replace(
        INDEXES[0],
        agent_briefs=tuple(sorted(briefs.items())),
    )
    settings = _settings(tmp_path)
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = load_evidence_snapshot(settings, as_of=as_of)
    empty_wiki_path = tmp_path / "brief-wiki"
    empty_wiki_path.mkdir()
    wiki = WikiCatalog(empty_wiki_path)

    class CapturingModel:
        def __init__(self) -> None:
            self.contexts: list[dict[str, object]] = []

        def invoke(self, prompt: str) -> AgentDraft:
            context = json.loads(prompt.rsplit("\n", maxsplit=1)[1])
            self.contexts.append(context)
            available_ids = context.get("available_evidence_item_ids", [])
            wiki_entry = context["wiki"]
            return AgentDraft(
                direction=Direction.UP,
                probabilities=Probabilities(up=0.4, neutral=0.3, down=0.3),
                summary="严格按照标的级职责完成判断。",
                evidence=["使用冻结输入。"],
                counter_evidence=["仍存在反向风险。"],
                invalidation_conditions=["冻结输入失效时结论失效。"],
                evidence_item_ids=list(available_ids[:1]),
                wiki_entry_id=wiki_entry["id"],
                wiki_section=wiki_entry["sections"][0]["slug"],
            )

    model = CapturingModel()
    provider = object.__new__(LangChainResearchProvider)
    provider.model = model
    evidence_id = snapshot.items[0].id

    provider.research(
        agent_id="ai_storage_industry_agent",
        index=index,
        horizon=Horizon.D1,
        as_of=as_of,
        wiki=wiki,
        evidence_snapshot=snapshot,
    )
    provider.strategize(
        index=index,
        horizon=Horizon.D1,
        as_of=as_of,
        data_cutoff=as_of,
        volatility_20d=snapshot.volatility_20d[index.code],
        wiki=wiki,
        research_opinions=[
            {
                "agent_id": "ai_storage_industry_agent",
                "evidence_item_ids": [evidence_id],
            }
        ],
        peer_opinions=[],
    )
    provider.criticize(
        index=index,
        horizon=Horizon.D1,
        as_of=as_of,
        wiki=wiki,
        research_opinions=[],
    )

    assert [context["role"] for context in model.contexts] == [
        briefs["ai_storage_industry_agent"],
        briefs["strategy_agent"],
        briefs["risk_critic_agent"],
    ]


def test_dynamic_citation_binds_exact_frozen_item_fields(tmp_path) -> None:
    settings = _settings(tmp_path)
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = load_evidence_snapshot(settings, as_of=as_of)
    wiki = WikiCatalog(settings.wiki_path).freeze()
    wiki_citation = wiki.citation("macro_policy_agent")
    draft = AgentDraft(
        direction=Direction.UP,
        probabilities=Probabilities(up=0.21, neutral=0.6, down=0.19),
        summary="uses frozen evidence",
        evidence=["structured binding"],
        evidence_item_ids=[snapshot.items[0].id],
        wiki_entry_id=wiki_citation.wiki_entry_id,
        wiki_section=wiki_citation.section,
    )

    raw = _bind_evidence_citations(
        draft=draft,
        wiki_citation=wiki_citation,
        snapshot=snapshot,
        index_code="000300.SH",
        require_dynamic=True,
    )[0]
    citation = Citation.model_validate(raw)

    assert _citation_matches_evidence(citation, snapshot.items[0])
    assert wiki.citation_is_valid(citation)
    assert citation.wiki_quote == wiki_citation.quote
    assert not _citation_matches_evidence(
        citation.model_copy(update={"source_url": "https://evil.example/tampered"}),
        snapshot.items[0],
    )


def test_strategy_receives_only_three_effective_research_inputs(tmp_path) -> None:
    settings = _settings(tmp_path)
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = load_evidence_snapshot(settings, as_of=as_of)
    frozen_wiki = WikiCatalog(settings.wiki_path).freeze()
    provider = CapturingCriticProvider()
    workflow = object.__new__(CommitteeWorkflow)
    workflow.settings = settings
    workflow.provider = provider
    opinions = []
    for index in INDEXES:
        for horizon in Horizon:
            for agent_id in (
                "macro_policy_agent",
                "market_news_agent",
                "ai_storage_industry_agent",
                "quant_agent",
            ):
                opinions.append(
                    {
                        "agent_id": agent_id,
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "direction": Direction.UP.value,
                        "probabilities": {"up": 0.21, "neutral": 0.6, "down": 0.19},
                        "summary": agent_id,
                        "evidence": ["evidence"],
                        "counter_evidence": [],
                        "raw_response": {"evidence_item_ids": []},
                    }
                )
    state = {
        "run_id": "run-strategy-test",
        "as_of": as_of.isoformat(),
        "evidence_snapshot": snapshot.model_dump(mode="json"),
        "wiki_snapshot": frozen_wiki.snapshot(),
        "opinions": opinions,
    }

    result = workflow._strategy_node(state)

    assert len(result["opinions"]) == 10
    assert all(opinion["agent_id"] == "strategy_agent" for opinion in result["opinions"])
    assert all(
        {item["agent_id"] for item in research}
        == {
            "macro_policy_agent",
            "market_news_agent",
            "ai_storage_industry_agent",
        }
        for research in provider.strategy_inputs
    )
    assert all(
        all("evidence_sources" in item for item in research)
        for research in provider.strategy_inputs
    )
    assert all(len(peers) == (len(INDEXES) - 1) * 3 for peers in provider.strategy_peer_inputs)
    assert all(
        len({item["index_code"] for item in peers}) == len(INDEXES) - 1
        for peers in provider.strategy_peer_inputs
    )
    assert all(
        all("evidence_item_ids" in item and "evidence_sources" in item for item in peers)
        for peers in provider.strategy_peer_inputs
    )
    for position, peers in enumerate(provider.strategy_peer_inputs):
        target_index = INDEXES[position // len(Horizon)].code
        assert target_index not in {item["index_code"] for item in peers}
    for horizon in Horizon:
        horizon_opinions = [
            opinion for opinion in result["opinions"] if opinion["horizon"] == horizon.value
        ]
        contexts = [opinion["raw_response"]["strategy_context"] for opinion in horizon_opinions]
        assert {context["relative_rank"] for context in contexts} == {1}
        assert all(context["rank_tied"] for context in contexts)
        assert len({context["market_regime"] for context in contexts}) == 1
        assert len({context["style_bias"] for context in contexts}) == 1
        assert all("五指数相对配置排序并列第" in opinion["summary"] for opinion in horizon_opinions)

    first_index = INDEXES[0].code
    corrupted_opinions = [
        opinion
        for opinion in opinions
        if not (
            opinion["index_code"] == first_index
            and opinion["horizon"] == Horizon.D1.value
            and opinion["agent_id"] == "market_news_agent"
        )
    ]
    duplicate = next(
        opinion
        for opinion in opinions
        if opinion["index_code"] == first_index
        and opinion["horizon"] == Horizon.D1.value
        and opinion["agent_id"] == "macro_policy_agent"
    )
    corrupted_opinions.append(dict(duplicate))
    with pytest.raises(ValueError, match="incomplete inputs"):
        workflow._strategy_node({**state, "opinions": corrupted_opinions})


def test_live_strategy_cannot_cite_evidence_outside_upstream_inputs(tmp_path) -> None:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir()
    (wiki_path / "strategy.md").write_text(
        """---
id: VC-WIKI-MARKET-STRATEGY
title: Strategy
version: 1.0.0
status: active
tags: [strategy]
---
<!-- section:scope -->
# Scope
Strategy framework.
""",
        encoding="utf-8",
    )
    wiki = WikiCatalog(wiki_path)
    entry = wiki.select_for_agent("strategy_agent")

    class StaticModel:
        def invoke(self, _prompt: str) -> AgentDraft:
            return AgentDraft(
                direction=Direction.UP,
                probabilities=Probabilities(up=0.21, neutral=0.6, down=0.19),
                summary="strategy",
                evidence=["evidence"],
                evidence_item_ids=["EVT-NOT-IN-UPSTREAM"],
                wiki_entry_id=entry.id,
                wiki_section="scope",
            )

    provider = object.__new__(LangChainResearchProvider)
    provider.model = StaticModel()
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(ValueError, match="outside its research inputs"):
        provider.strategize(
            index=INDEXES[0],
            horizon=Horizon.D1,
            as_of=as_of,
            data_cutoff=as_of,
            volatility_20d=0.01,
            wiki=wiki,
            research_opinions=[
                {
                    "agent_id": "macro_policy_agent",
                    "direction": "up",
                    "probabilities": {"up": 0.21, "neutral": 0.6, "down": 0.19},
                    "summary": "macro",
                    "evidence_item_ids": ["EVT-UPSTREAM"],
                }
            ],
            peer_opinions=[],
        )


def test_workflow_rejects_strategy_evidence_not_present_in_upstream_opinions(tmp_path) -> None:
    demo_settings = _settings(tmp_path)
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = load_evidence_snapshot(demo_settings, as_of=as_of)
    frozen_wiki = WikiCatalog(demo_settings.wiki_path).freeze()
    live_settings = demo_settings.model_copy(update={"demo_mode": False})

    class BypassingProvider(CapturingCriticProvider):
        def strategize(self, **kwargs) -> AgentDraft:
            citation = kwargs["wiki"].citation("strategy_agent")
            return AgentDraft(
                direction=Direction.UP,
                probabilities=Probabilities(up=0.21, neutral=0.6, down=0.19),
                summary="tries to bypass the provider-level evidence guard",
                evidence=["untrusted evidence"],
                evidence_item_ids=[snapshot.items[0].id],
                wiki_entry_id=citation.wiki_entry_id,
                wiki_section=citation.section,
            )

    workflow = object.__new__(CommitteeWorkflow)
    workflow.settings = live_settings
    workflow.provider = BypassingProvider()
    opinions = []
    for index in INDEXES:
        for horizon in Horizon:
            for agent_id in (
                "macro_policy_agent",
                "market_news_agent",
                "ai_storage_industry_agent",
                "quant_agent",
            ):
                opinions.append(
                    {
                        "agent_id": agent_id,
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "direction": Direction.UP.value,
                        "probabilities": {"up": 0.21, "neutral": 0.6, "down": 0.19},
                        "summary": agent_id,
                        "evidence": ["evidence"],
                        "counter_evidence": [],
                        "raw_response": {"evidence_item_ids": []},
                    }
                )
    state = {
        "run_id": "run-strategy-boundary-test",
        "as_of": as_of.isoformat(),
        "evidence_snapshot": snapshot.model_dump(mode="json"),
        "wiki_snapshot": frozen_wiki.snapshot(),
        "opinions": opinions,
    }

    with pytest.raises(ValueError, match="outside its research inputs"):
        workflow._strategy_node(state)


def test_critic_never_receives_quant_and_persists_binary_risk_tilt(tmp_path) -> None:
    settings = _settings(tmp_path)
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = load_evidence_snapshot(settings, as_of=as_of)
    frozen_wiki = WikiCatalog(settings.wiki_path).freeze()
    provider = CapturingCriticProvider()
    workflow = object.__new__(CommitteeWorkflow)
    workflow.settings = settings
    workflow.provider = provider
    opinions = []
    for index in INDEXES:
        for horizon in Horizon:
            for agent_id in (
                "macro_policy_agent",
                "market_news_agent",
                "ai_storage_industry_agent",
                "quant_agent",
                "strategy_agent",
            ):
                opinions.append(
                    {
                        "agent_id": agent_id,
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "direction": Direction.UP.value,
                        "probabilities": {"up": 0.21, "neutral": 0.6, "down": 0.19},
                        "summary": agent_id,
                        "evidence": ["evidence"],
                        "counter_evidence": [],
                    }
                )
    state = {
        "run_id": "run-test",
        "as_of": as_of.isoformat(),
        "evidence_snapshot": snapshot.model_dump(mode="json"),
        "wiki_snapshot": frozen_wiki.snapshot(),
        "opinions": opinions,
    }

    result = workflow._critic_node(state)

    assert len(result["opinions"]) == 10
    assert all(
        {item["agent_id"] for item in research}
        == {
            "macro_policy_agent",
            "market_news_agent",
            "ai_storage_industry_agent",
            "strategy_agent",
        }
        for research in provider.inputs
    )
    assert {opinion["direction"] for opinion in result["opinions"]} == {"down"}
    assert all(
        opinion["probabilities"] == {"up": 0.05, "neutral": 0.75, "down": 0.2}
        for opinion in result["opinions"]
    )


def test_committee_haircut_is_directionally_symmetric() -> None:
    base = Probabilities(up=0.6, neutral=0.1, down=0.3)
    adjusted = _apply_symmetric_haircut(base, haircut=0.15)

    assert adjusted.up / base.up == pytest.approx(0.85)
    assert adjusted.down / base.down == pytest.approx(0.85)
    assert adjusted.neutral > base.neutral
    assert directional_confidence(adjusted.as_dict()) == pytest.approx(
        directional_confidence(base.as_dict())
    )


def test_cio_uses_strategy_as_single_direction_source(tmp_path) -> None:
    settings = _settings(tmp_path)
    as_of = datetime(2026, 7, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = load_evidence_snapshot(settings, as_of=as_of)
    frozen_wiki = WikiCatalog(settings.wiki_path).freeze()
    citation = frozen_wiki.citation("strategy_agent").model_dump(mode="json")
    workflow = object.__new__(CommitteeWorkflow)
    workflow.settings = settings
    workflow.provider = CapturingCriticProvider()

    def state_with_base(probabilities: dict[str, float]) -> dict:
        opinions = []
        for index in INDEXES:
            for horizon in Horizon:
                for agent_id in (
                    "macro_policy_agent",
                    "market_news_agent",
                    "ai_storage_industry_agent",
                ):
                    opinions.append(
                        {
                            "agent_id": agent_id,
                            "index_code": index.code,
                            "horizon": horizon.value,
                            "probabilities": probabilities,
                            "summary": agent_id,
                            "citations": [citation],
                            "counter_evidence": [],
                            "invalidation_conditions": [],
                        }
                    )
                opinions.append(
                    {
                        "agent_id": "strategy_agent",
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "probabilities": {"up": 0.6, "neutral": 0.2, "down": 0.2},
                        "summary": "fixed strategy",
                        "citations": [citation],
                        "counter_evidence": [],
                        "invalidation_conditions": [],
                    }
                )
                opinions.append(
                    {
                        "agent_id": "risk_critic_agent",
                        "index_code": index.code,
                        "horizon": horizon.value,
                        "probabilities": {"up": 0.15, "neutral": 0.7, "down": 0.15},
                        "summary": "risk",
                        "citations": [citation],
                        "counter_evidence": [],
                        "invalidation_conditions": [],
                    }
                )
        return {
            "run_id": "run-cio-test",
            "as_of": as_of.isoformat(),
            "data_cutoff": snapshot.data_cutoff.isoformat(),
            "input_hash": "a" * 64,
            "evidence_snapshot": snapshot.model_dump(mode="json"),
            "opinions": opinions,
        }

    neutral_base = workflow._cio_node(
        state_with_base({"up": 0.1, "neutral": 0.8, "down": 0.1})
    )
    bullish_base = workflow._cio_node(
        state_with_base({"up": 0.8, "neutral": 0.1, "down": 0.1})
    )

    assert [item["probabilities"] for item in neutral_base["forecasts"]] == [
        item["probabilities"] for item in bullish_base["forecasts"]
    ]
    assert neutral_base["forecasts"][0]["probabilities"] == pytest.approx(
        {"up": 0.51, "neutral": 0.32, "down": 0.17}
    )
    assert {item["direction"] for item in neutral_base["forecasts"]} == {"up"}
    assert all(not item["abstain"] for item in neutral_base["forecasts"])


def test_model_identity_distinguishes_llm_research_from_deterministic_cio(tmp_path) -> None:
    workflow = object.__new__(CommitteeWorkflow)
    workflow.settings = Settings(
        demo_mode=False,
        llm_api_key="test-key",
        llm_model="actual-llm-model",
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
    )
    workflow.provider = CapturingCriticProvider()

    assert workflow._model_name_for_agent("macro_policy_agent") == "actual-llm-model"
    assert (
        workflow._model_name_for_agent("cio_agent")
        == f"deterministic-committee-aggregation-v{WORKFLOW_VERSION}"
    )
    assert workflow._model_name_for_agent("quant_agent") == "unavailable-no-quant-signal-v1"


def test_supported_workflow_runtime_profiles_have_unique_version_pairs() -> None:
    profiles = {
        workflow_runtime_versions(
            uses_configurable_universe=uses_configurable_universe,
            runtime_mode=runtime_mode,
        )
        for uses_configurable_universe in (False, True)
        for runtime_mode in ("current", "legacy_dual_horizon")
    }

    assert len(profiles) == 4
