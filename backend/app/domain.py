"""Stable domain definitions and deterministic forecast scoring functions."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from enum import StrEnum


class Direction(StrEnum):
    UP = "up"
    NEUTRAL = "neutral"
    DOWN = "down"


class Horizon(StrEnum):
    D1 = "D1"
    D2 = "D2"


class RunStatus(StrEnum):
    AWAITING_DRAFT = "awaiting_draft"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStatus(StrEnum):
    """Persistent execution states exposed by the API and worker."""

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentSourceType(StrEnum):
    """The Agent's declared default producer class, not per-run provenance."""

    AI = "ai"
    MANUAL = "manual"
    QUANT = "quant"
    DETERMINISTIC = "deterministic"


class AgentWorkflowRole(StrEnum):
    """The Agent's structured responsibility, independent of producer class."""

    RESEARCH = "research"
    STRATEGY = "strategy"
    CRITIC = "critic"
    DECISION = "decision"
    SHADOW = "shadow"


@dataclass(frozen=True, slots=True)
class IndexDefinition:
    code: str
    name: str
    market: str = "CN"
    asset_type: str = "index"
    exchange: str = ""
    currency: str = "CNY"
    timezone: str = "Asia/Shanghai"
    sector: str | None = None
    strategy_bucket: str = "balanced"
    tags: tuple[str, ...] = ()
    wiki_entry_ids: tuple[tuple[str, str], ...] = ()
    agent_briefs: tuple[tuple[str, str], ...] = ()

    def wiki_entry_id_for(self, agent_id: str) -> str | None:
        return dict(self.wiki_entry_ids).get(agent_id)

    def agent_brief_for(self, agent_id: str) -> str | None:
        return dict(self.agent_briefs).get(agent_id)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    id: str
    name: str
    role: str
    kind: str
    version: str = "0.1.0"
    weight: float = 1.0
    status: str = "active"
    _: KW_ONLY
    workflow_role: AgentWorkflowRole
    source_type: AgentSourceType


INDEXES: tuple[IndexDefinition, ...] = (
    IndexDefinition("000300.SH", "沪深300"),
    IndexDefinition("000905.SH", "中证500"),
    IndexDefinition("000852.SH", "中证1000"),
    IndexDefinition("399006.SZ", "创业板指"),
    IndexDefinition("000688.SH", "科创50"),
)


AGENTS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        "macro_policy_agent",
        "宏观政策研究员",
        "分析货币、财政、监管、汇率与海外宏观向中国宽基指数的传导。",
        "research",
        workflow_role=AgentWorkflowRole.RESEARCH,
        source_type=AgentSourceType.AI,
        version="0.2.0",
    ),
    AgentDefinition(
        "market_news_agent",
        "市场资讯研究员",
        "识别当日新增事件、跨市场变化、市场叙事与预期差。",
        "research",
        workflow_role=AgentWorkflowRole.RESEARCH,
        source_type=AgentSourceType.AI,
        version="0.2.0",
    ),
    AgentDefinition(
        "ai_storage_industry_agent",
        "AI存储行业研究员",
        "跟踪全球AI算力、存储与半导体产业链，并映射至中国宽基指数。",
        "research",
        workflow_role=AgentWorkflowRole.RESEARCH,
        source_type=AgentSourceType.AI,
        version="0.2.0",
    ),
    AgentDefinition(
        "quant_agent",
        "量化研究员（待接入）",
        "等待经过验证的只读因子、行情与模型适配器；未接入前不产生方向判断。",
        "placeholder",
        workflow_role=AgentWorkflowRole.RESEARCH,
        source_type=AgentSourceType.QUANT,
        version="0.2.0",
        weight=0.0,
        status="unavailable",
    ),
    AgentDefinition(
        "strategy_agent",
        "市场策略研究员",
        "综合专业研究观点，判断市场状态、风格、指数相对强弱与配置优先级。",
        "strategy",
        workflow_role=AgentWorkflowRole.STRATEGY,
        source_type=AgentSourceType.AI,
        version="0.2.0",
    ),
    AgentDefinition(
        "risk_critic_agent",
        "风险反证官",
        "检查反证、共识拥挤、数据污染、事件遗漏和结论失效条件。",
        "critic",
        workflow_role=AgentWorkflowRole.CRITIC,
        source_type=AgentSourceType.AI,
        version="0.3.0",
        weight=0.0,
    ),
    AgentDefinition(
        "cio_agent",
        "首席投资官",
        "综合研究观点和风险反证，形成可审计的投委会最终判断。",
        "decision",
        workflow_role=AgentWorkflowRole.DECISION,
        source_type=AgentSourceType.DETERMINISTIC,
        version="0.3.0",
        weight=0.0,
    ),
)

USER_JUDGMENT_AGENT = AgentDefinition(
    "user_judgment_agent",
    "用户判断 Agent",
    "在查看委员会结论前独立选择涨跌，并封存理由、反证与失效条件。",
    "human",
    workflow_role=AgentWorkflowRole.SHADOW,
    source_type=AgentSourceType.MANUAL,
    version="0.1.0",
    weight=0.0,
    status="shadow",
)

# ``AGENTS`` is the immutable committee roster used by the workflow, handoff
# matrix and run input hash. Displaying the manual shadow participant
# must never add it to those model draft slots.
DISPLAY_AGENTS: tuple[AgentDefinition, ...] = (*AGENTS, USER_JUDGMENT_AGENT)

INDEX_BY_CODE = {item.code: item for item in INDEXES}
AGENT_BY_ID = {item.id: item for item in DISPLAY_AGENTS}


def legacy_v1_agent_hash_projection(
    model_names: Mapping[str, str],
) -> list[dict[str, str | float]]:
    """Return the frozen Agent fragment used by the v1 workflow input hash.

    New AgentSpec capabilities and participation policy hashes belong only in a
    future explicitly versioned workflow protocol. Keep this field set and
    ``AGENTS`` order byte-compatible with already published runs.
    """

    return [
        {
            "id": agent.id,
            "version": agent.version,
            "kind": agent.kind,
            "status": agent.status,
            "weight": agent.weight,
            "model_name": model_names[agent.id],
        }
        for agent in AGENTS
    ]


def neutral_threshold(volatility_20d: float, horizon: Horizon | str) -> float:
    """Return the symmetric neutral band for a forecast horizon.

    ``volatility_20d`` is the standard deviation of daily returns expressed as a
    decimal (for example 0.012 means 1.2%).
    """

    if not math.isfinite(volatility_20d) or volatility_20d < 0:
        raise ValueError("volatility_20d must be a finite non-negative number")
    horizon_value = Horizon(horizon)
    scale = 1.0 if horizon_value is Horizon.D1 else math.sqrt(2.0)
    return 0.25 * volatility_20d * scale


def realized_return(start_close: float, end_close: float) -> float:
    if start_close <= 0 or end_close <= 0:
        raise ValueError("close prices must be positive")
    return end_close / start_close - 1.0


def classify_return(actual_return: float, threshold: float) -> Direction:
    if not math.isfinite(actual_return):
        raise ValueError("actual_return must be finite")
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be a finite non-negative number")
    if actual_return > threshold:
        return Direction.UP
    if actual_return < -threshold:
        return Direction.DOWN
    return Direction.NEUTRAL


def validate_probabilities(probabilities: Mapping[str | Direction, float]) -> dict[str, float]:
    normalized = {
        direction.value: float(
            probabilities.get(direction, probabilities.get(direction.value, 0.0))
        )
        for direction in Direction
    }
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in normalized.values()):
        raise ValueError("probabilities must be finite values between zero and one")
    if not math.isclose(sum(normalized.values()), 1.0, abs_tol=1e-6):
        raise ValueError("probabilities must sum to one")
    return normalized


def predicted_direction(probabilities: Mapping[str | Direction, float]) -> Direction:
    """Return the mandatory up/down choice encoded by a probability vector.

    ``neutral`` remains an uncertainty/outcome bucket for historical
    compatibility, but it is never a permitted prediction direction.  Exact
    directional ties are rejected so the system cannot hide an arbitrary
    bullish or bearish bias behind a tie-break convention.
    """

    values = validate_probabilities(probabilities)
    if math.isclose(values[Direction.UP.value], values[Direction.DOWN.value], abs_tol=1e-9):
        raise ValueError("up and down probabilities must not be tied")
    return (
        Direction.UP
        if values[Direction.UP.value] > values[Direction.DOWN.value]
        else Direction.DOWN
    )


def directional_confidence(probabilities: Mapping[str | Direction, float]) -> float:
    """Return confidence conditional on the outcome not being neutral/noise."""

    values = validate_probabilities(probabilities)
    directional_total = values[Direction.UP.value] + values[Direction.DOWN.value]
    if directional_total <= 0:
        raise ValueError("at least one directional probability must be positive")
    return max(values[Direction.UP.value], values[Direction.DOWN.value]) / directional_total


def multiclass_brier_score(
    probabilities: Mapping[str | Direction, float], actual_label: Direction | str
) -> float:
    """Mean squared probability error across the three mutually exclusive labels."""

    values = validate_probabilities(probabilities)
    actual = Direction(actual_label)
    return sum(
        (values[direction.value] - float(direction is actual)) ** 2 for direction in Direction
    ) / len(Direction)


def sample_volatility(returns: Sequence[float]) -> float:
    """Sample standard deviation used by data providers when deriving sigma20."""

    if len(returns) < 2:
        raise ValueError("at least two returns are required")
    if any(not math.isfinite(value) for value in returns):
        raise ValueError("returns must all be finite")
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance)
