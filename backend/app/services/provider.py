"""Research providers for deterministic demo runs and real LangChain models."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from langchain_openai import ChatOpenAI

from ..config import Settings
from ..domain import AGENT_BY_ID, Direction, Horizon, IndexDefinition, predicted_direction
from ..schemas import AgentDraft, FrozenEvidenceSnapshot, Probabilities
from .wiki import WikiCatalog

LEGACY_CODEX_FILE_PROVIDER_NAME = "codex-file-handoff-v1"
PREVIOUS_CODEX_FILE_PROVIDER_NAME = "codex-file-handoff-v2"
CODEX_FILE_PROVIDER_NAME = "codex-file-handoff-v3"


class ResearchProvider(Protocol):
    name: str
    prompt_version: str
    trace_span_kind: str

    def research(
        self,
        *,
        agent_id: str,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        wiki: WikiCatalog,
        evidence_snapshot: FrozenEvidenceSnapshot,
    ) -> AgentDraft: ...

    def strategize(
        self,
        *,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        data_cutoff: datetime,
        volatility_20d: float,
        wiki: WikiCatalog,
        research_opinions: list[dict[str, object]],
        peer_opinions: list[dict[str, object]],
    ) -> AgentDraft: ...

    def criticize(
        self,
        *,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        wiki: WikiCatalog,
        research_opinions: list[dict[str, object]],
    ) -> AgentDraft: ...


@dataclass(slots=True)
class BlockedLiveProvider:
    """Keeps health/config endpoints available while live credentials are missing."""

    reason: str
    name: str = "blocked-live-provider"
    prompt_version: str = "blocked"
    trace_span_kind: str = "llm"

    def research(self, **_) -> AgentDraft:
        raise RuntimeError(self.reason)

    def strategize(self, **_) -> AgentDraft:
        raise RuntimeError(self.reason)

    def criticize(self, **_) -> AgentDraft:
        raise RuntimeError(self.reason)


@dataclass(slots=True)
class AwaitingCodexFileProvider(BlockedLiveProvider):
    """API-process placeholder; actual drafts enter through the file CLI."""

    reason: str = "Codex file mode only runs through scripts/codex_handoff.py."
    name: str = CODEX_FILE_PROVIDER_NAME
    prompt_version: str = CODEX_FILE_PROVIDER_NAME
    trace_span_kind: str = "external"

    def model_name_for_agent(self, agent_id: str) -> str:
        del agent_id
        return CODEX_FILE_PROVIDER_NAME


@dataclass(slots=True)
class DeterministicDemoProvider:
    """Repeatable, network-free provider for local development and CI."""

    name: str = "deterministic-binary-demo-v3"
    prompt_version: str = "deterministic-binary-demo-v3"
    trace_span_kind: str = "general"

    def research(
        self,
        *,
        agent_id: str,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        wiki: WikiCatalog,
        evidence_snapshot: FrozenEvidenceSnapshot,
    ) -> AgentDraft:
        citation = wiki.citation(
            agent_id,
            index_code=index.code,
            preferred_entry_id=index.wiki_entry_id_for(agent_id),
        )
        if agent_id == "quant_agent":
            raise RuntimeError("quant agent is unavailable until a validated data adapter exists")

        seed_material = f"{agent_id}|{index.code}|{horizon}|{as_of.date().isoformat()}"
        seed = int(hashlib.sha256(seed_material.encode()).hexdigest()[:16], 16)
        rng = random.Random(seed)
        signed_signal = rng.uniform(-0.18, 0.18)
        neutral = 0.42 + rng.uniform(-0.04, 0.08)
        directional = 1.0 - neutral
        up = directional / 2 + signed_signal
        down = directional - up
        floor = 0.08
        up = max(floor, up)
        down = max(floor, down)
        total = up + neutral + down
        probabilities = Probabilities(
            up=up / total,
            neutral=neutral / total,
            down=down / total,
        )
        direction = _direction(probabilities)
        agent = AGENT_BY_ID[agent_id]
        return AgentDraft(
            direction=direction,
            probabilities=probabilities,
            summary=(
                f"{agent.name}离线二元演示：{index.name}{horizon}在涨跌两侧中选择"
                f"{_direction_label(direction)}；该结果只验证系统链路，不是智能投研。"
            ),
            evidence=[
                f"依据 {citation.wiki_entry_id} 的 {citation.section} 框架完成结构化检查。",
                "演示提供方使用固定日期、指数和角色哈希，保证历史运行可重放。",
            ],
            counter_evidence=["离线演示未采集当日实时资讯，方向判断不可用于投资决策。"],
            invalidation_conditions=["接入实时事实流后，演示信号自动失效。"],
            wiki_entry_id=citation.wiki_entry_id,
            wiki_section=citation.section,
        )

    def strategize(
        self,
        *,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        data_cutoff: datetime,
        volatility_20d: float,
        wiki: WikiCatalog,
        research_opinions: list[dict[str, object]],
        peer_opinions: list[dict[str, object]],
    ) -> AgentDraft:
        del as_of, data_cutoff, volatility_20d
        if not research_opinions:
            raise ValueError("strategy agent requires research opinions")
        probability_rows = [
            Probabilities.model_validate(opinion["probabilities"])
            for opinion in research_opinions
        ]
        probabilities = Probabilities(
            up=sum(row.up for row in probability_rows) / len(probability_rows),
            neutral=sum(row.neutral for row in probability_rows) / len(probability_rows),
            down=sum(row.down for row in probability_rows) / len(probability_rows),
        )
        direction = _direction(probabilities)
        citation = wiki.citation(
            "strategy_agent",
            index_code=index.code,
            preferred_entry_id=index.wiki_entry_id_for("strategy_agent"),
        )
        summaries = [str(opinion["summary"]) for opinion in research_opinions]
        return AgentDraft(
            direction=direction,
            probabilities=probabilities,
            summary=(
                f"市场策略研究员综合 {len(research_opinions)} 份有效研究输入，形成"
                f"{index.name}{horizon.value} 的离线演示配置判断，并比较"
                f" {len(peer_opinions)} 份同周期跨指数输入。"
            ),
            evidence=[
                f"{opinion['agent_id']}：{opinion['summary']}" for opinion in research_opinions
            ],
            counter_evidence=[
                "基础研究可能依赖共同来源，综合结果不代表新增独立证据。",
                *[f"基础研究摘要：{summary}" for summary in summaries[:2]],
            ],
            invalidation_conditions=["任一基础研究输入未通过证据校验时，本策略判断失效。"],
            wiki_entry_id=citation.wiki_entry_id,
            wiki_section=citation.section,
        )

    def criticize(
        self,
        *,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        wiki: WikiCatalog,
        research_opinions: list[dict[str, object]],
    ) -> AgentDraft:
        citation = wiki.citation(
            "risk_critic_agent",
            index_code=index.code,
            preferred_entry_id=index.wiki_entry_id_for("risk_critic_agent"),
        )
        directional = [str(opinion["direction"]) for opinion in research_opinions]
        disagreement = len(set(directional)) > 1
        summaries = [str(opinion["summary"]) for opinion in research_opinions]
        directional_score = sum(
            float(opinion["probabilities"]["up"])
            - float(opinion["probabilities"]["down"])
            for opinion in research_opinions
            if isinstance(opinion.get("probabilities"), dict)
        )
        if abs(directional_score) < 1e-9:
            tie_seed = hashlib.sha256(
                f"critic|{index.code}|{horizon.value}|{as_of.date().isoformat()}".encode()
            ).digest()[0]
            directional_score = 1.0 if tie_seed % 2 else -1.0
        neutral = 0.62 if disagreement else 0.54
        directional_mass = 1.0 - neutral
        edge = 0.03 if disagreement else 0.06
        up = directional_mass / 2 + (edge if directional_score > 0 else -edge)
        down = directional_mass - up
        probabilities = Probabilities(up=up, neutral=neutral, down=down)
        direction = _direction(probabilities)
        return AgentDraft(
            direction=direction,
            probabilities=probabilities,
            summary=(
                f"已反证检查 {index.name}{horizon.value} 的 {len(research_opinions)} 份输入；"
                + (
                    f"有效研究存在方向分歧；反证后仍需二选一，风险调整倾向"
                    f"{_direction_label(direction)}。"
                    if disagreement
                    else f"方向较一致；防范共识拥挤后仍倾向{_direction_label(direction)}。"
                )
            ),
            evidence=[f"逐项检查研究摘要：{summary}" for summary in summaries[:3]],
            counter_evidence=[
                "离线演示缺少真实事件流，所有方向证据均可能存在遗漏。",
                "多 Agent 可能依赖同一 Wiki 框架，不能视为完全独立证据。",
            ],
            invalidation_conditions=["任何晚于数据截止时间的信息不得用于本次判断。"],
            wiki_entry_id=citation.wiki_entry_id,
            wiki_section=citation.section,
        )


class LangChainResearchProvider:
    """OpenAI-compatible LangChain provider with Pydantic structured output."""

    name = "langchain-openai-compatible"
    prompt_version = "research-agent-v5"
    trace_span_kind = "llm"

    def __init__(self, settings: Settings) -> None:
        self.model = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=0,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        ).with_structured_output(AgentDraft)

    def research(
        self,
        *,
        agent_id: str,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        wiki: WikiCatalog,
        evidence_snapshot: FrozenEvidenceSnapshot,
    ) -> AgentDraft:
        if agent_id == "quant_agent":
            raise RuntimeError("quant agent is unavailable until a validated data adapter exists")
        agent = AGENT_BY_ID[agent_id]
        entry = wiki.select_for_agent(
            agent_id,
            index_code=index.code,
            preferred_entry_id=index.wiki_entry_id_for(agent_id),
        )
        facts = [
            item.model_dump(mode="json")
            for item in evidence_snapshot.items
            if not item.entities or index.code in item.entities
        ]
        context = {
            "as_of": as_of.isoformat(),
            "instrument": {
                "code": index.code,
                "name": index.name,
                "market": index.market,
                "asset_type": index.asset_type,
                "exchange": index.exchange,
                "currency": index.currency,
                "sector": index.sector,
                "tags": list(index.tags),
            },
            "horizon": horizon.value,
            "role": index.agent_brief_for(agent_id) or agent.role,
            "wiki": entry.model_dump(mode="json", exclude={"body"}),
            "frozen_evidence": facts,
            "volatility_20d": evidence_snapshot.volatility_20d[index.code],
            "data_cutoff": evidence_snapshot.data_cutoff.isoformat(),
        }
        prompt = (
            "你是 forecast-loop 的专业研究员。严格只依据截止时间前的资料，输出 up、neutral、"
            "down 三个结果概率以及证据、反证与失效条件。direction 不允许 neutral，必须比较"
            " up 与 down 后明确选择较大的一侧；up 与 down 不得相等。neutral 只表示实际收益"
            "可能落入评价噪声带，不是可选立场。每条事实证据必须写出 frozen_evidence 的 id；"
            "概率之和必须为1，禁止编造引用。\n"
            + json.dumps(context, ensure_ascii=False)
        )
        result = self.model.invoke(prompt)
        if not isinstance(result, AgentDraft):
            result = AgentDraft.model_validate(result)
        if result.wiki_entry_id != entry.id:
            raise ValueError("model referenced an unavailable Wiki entry")
        if result.wiki_section not in {section.slug for section in entry.sections}:
            raise ValueError("model referenced an unavailable Wiki section")
        return result

    def criticize(
        self,
        *,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        wiki: WikiCatalog,
        research_opinions: list[dict[str, object]],
    ) -> AgentDraft:
        entry = wiki.select_for_agent(
            "risk_critic_agent",
            index_code=index.code,
            preferred_entry_id=index.wiki_entry_id_for("risk_critic_agent"),
        )
        context = {
            "as_of": as_of.isoformat(),
            "instrument": {
                "code": index.code,
                "name": index.name,
                "market": index.market,
                "asset_type": index.asset_type,
                "exchange": index.exchange,
                "currency": index.currency,
                "sector": index.sector,
                "tags": list(index.tags),
            },
            "horizon": horizon.value,
            "role": (
                index.agent_brief_for("risk_critic_agent")
                or AGENT_BY_ID["risk_critic_agent"].role
            ),
            "research_opinions": research_opinions,
            "wiki": entry.model_dump(mode="json", exclude={"body"}),
        }
        prompt = (
            "你是 forecast-loop 风险反证官。严格按照输入中的 role 履行该标的的反证职责，"
            "阅读其他研究员的全部观点，查找反证、共同来源导致的伪共识、数据污染和失效条件。"
            "你不参与 CIO 方向投票，但仍需给出反证后的风险倾向："
            "direction 必须为 up 或 down，并与 up/down 中较大一侧一致，两者不得相等。neutral"
            " 只表示小波动/不确定性概率，可以是最高项。禁止编造引用。\n"
            + json.dumps(context, ensure_ascii=False)
        )
        result = self.model.invoke(prompt)
        if not isinstance(result, AgentDraft):
            result = AgentDraft.model_validate(result)
        if result.wiki_entry_id != entry.id:
            raise ValueError("risk critic referenced an unavailable Wiki entry")
        if result.wiki_section not in {section.slug for section in entry.sections}:
            raise ValueError("risk critic referenced an unavailable Wiki section")
        return result

    def strategize(
        self,
        *,
        index: IndexDefinition,
        horizon: Horizon,
        as_of: datetime,
        data_cutoff: datetime,
        volatility_20d: float,
        wiki: WikiCatalog,
        research_opinions: list[dict[str, object]],
        peer_opinions: list[dict[str, object]],
    ) -> AgentDraft:
        entry = wiki.select_for_agent(
            "strategy_agent",
            index_code=index.code,
            preferred_entry_id=index.wiki_entry_id_for("strategy_agent"),
        )
        available_evidence_ids = sorted(
            {
                str(evidence_id)
                for opinion in research_opinions
                for evidence_id in (
                    opinion.get("evidence_item_ids")
                    if isinstance(opinion.get("evidence_item_ids"), list)
                    else []
                )
            }
        )
        context = {
            "as_of": as_of.isoformat(),
            "data_cutoff": data_cutoff.isoformat(),
            "instrument": {
                "code": index.code,
                "name": index.name,
                "market": index.market,
                "asset_type": index.asset_type,
                "exchange": index.exchange,
                "currency": index.currency,
                "sector": index.sector,
                "tags": list(index.tags),
            },
            "horizon": horizon.value,
            "volatility_20d": volatility_20d,
            "role": (
                index.agent_brief_for("strategy_agent")
                or AGENT_BY_ID["strategy_agent"].role
            ),
            "research_opinions": research_opinions,
            "peer_opinions": peer_opinions,
            "available_evidence_item_ids": available_evidence_ids,
            "wiki": entry.model_dump(mode="json", exclude={"body"}),
        }
        prompt = (
            "你是 forecast-loop 的策略研究员。严格按照输入中的 role 履行该标的的研究职责，"
            "只综合输入中的专业研究观点，不新增事实，判断市场状态、风格、标的相对强弱和"
            "配置优先级。区分独立证据与共同来源，"
            "不得把三位研究员的同源判断重复计票。输出 up、neutral、down 三个结果概率、最强"
            "反证和失效条件；direction 不允许 neutral，必须在 up/down 中选择概率更大的一侧，"
            "两者不得相等；neutral 仅表示评价噪声带概率。evidence_item_ids 必须是 "
            "available_evidence_item_ids 的非空子集，禁止编造引用。\n"
            + json.dumps(context, ensure_ascii=False)
        )
        result = self.model.invoke(prompt)
        if not isinstance(result, AgentDraft):
            result = AgentDraft.model_validate(result)
        if result.wiki_entry_id != entry.id:
            raise ValueError("strategy agent referenced an unavailable Wiki entry")
        if result.wiki_section not in {section.slug for section in entry.sections}:
            raise ValueError("strategy agent referenced an unavailable Wiki section")
        if not result.evidence_item_ids or not set(result.evidence_item_ids).issubset(
            available_evidence_ids
        ):
            raise ValueError("strategy agent referenced evidence outside its research inputs")
        return result


def build_provider(settings: Settings) -> ResearchProvider:
    if settings.use_demo_provider:
        return DeterministicDemoProvider()
    if settings.use_codex_file_provider:
        return AwaitingCodexFileProvider()
    if not settings.llm_api_key:
        return BlockedLiveProvider(
            "Live mode is blocked: set LLM_API_KEY or explicitly enable Demo mode."
        )
    return LangChainResearchProvider(settings)


def _direction(probabilities: Probabilities) -> Direction:
    return predicted_direction(probabilities.as_dict())


def _direction_label(direction: Direction) -> str:
    return {Direction.UP: "上涨", Direction.DOWN: "下跌"}[direction]
