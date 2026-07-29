---
id: VC-WIKI-GLOBAL-EQUITY-RESEARCH
title: 全球指数与个股短周期研究框架
version: 1.0.0
updated_at: 2026-07-29
published_at: 2026-07-29T00:00:00+08:00
status: active
owners:
  - macro_policy_agent
  - market_news_agent
  - ai_storage_industry_agent
  - strategy_agent
  - risk_critic_agent
tags:
  - global-equity
  - index
  - single-stock
  - market-strategy
  - risk
source_urls:
  - https://www.sec.gov/edgar/search/
  - https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities
  - https://www.nyse.com/markets/hours-calendars
  - https://www.nasdaq.com/market-activity
---

<!-- section:scope -->
## 适用范围

本条目提供跨市场指数与个股的 D1/D2 研究骨架，不保存每日新闻、价格或事后解释。每次
预测必须另外冻结目标市场的交易日历、行情 provenance、公司公告和事件证据，并使用
Universe 中的市场、时区、币种、资产类型、行业标签和标的级 Agent 职责。

<!-- section:market-clock -->
## 市场时钟与可见性

先确认目标交易所日历、当地时区、夏令时、半日市和收盘时间，再划定 evidence cutoff。
跨市场消息以原始发布时区和运行时可见时间为准，不能把另一个市场已经收盘后的信息回填到
目标 run。停牌、公司行动或无有效收盘价时必须明确缺口，不能沿用旧价格冒充当日观察。

<!-- section:index-research -->
## 指数研究

指数判断应映射到最新可用的成份、权重、行业贡献、盈利预期、估值、利率和汇率暴露。
单只权重股或单一主题不能自动代表整个指数。跨指数比较只能在同一 Universe、同一 cutoff
和同一 horizon 内进行，并对共同来源与重复暴露去重。

<!-- section:single-stock-research -->
## 个股研究

个股判断至少区分公司特有事实、行业事实和市场共同因子。检查公告、财报窗口、指引、
产品或监管事件、竞争与供应链、估值、流动性、拥挤度和潜在跳空；长期基本面观点不得在
没有短周期催化和定价差的情况下直接压缩成 D1/D2 方向。

<!-- section:evidence -->
## 证据与预期差

优先使用交易所、监管机构和公司原始披露。事实、市场一致预期、价格反应与 Agent 推断
必须分开记录。转载同一事件不增加独立证据数量；事件发生不等于存在预期差，研究必须说明
截止时间前是否已经定价。

<!-- section:risk -->
## 反证与失效

- 交易日历、时区、币种或标的身份不一致；
- 公司行动、停牌或数据修订破坏价格可比性；
- 证据晚于 cutoff、来自未授权源或只有二手转述；
- 指数结论被单只成份股主导，或个股结论只是市场 beta 的重复表达；
- 财报、监管和盘后事件造成跳空，使连续收益假设失效；
- 多个 Agent 使用同一来源形成伪共识。

<!-- section:output -->
## 输出规则

方向只能是 up 或 down，并与 `p_up`、`p_down` 较大一侧一致；`p_neutral` 只表示实际收益
落入评价噪声带的概率。每条结论都要列出冻结证据、最强反证和可观察失效条件。关键行情、
日历或引用无法验证时阻断 run，不以固定方向或 Demo 数据补齐。

<!-- section:sources -->
## 来源入口

- [SEC EDGAR](https://www.sec.gov/edgar/search/)
- [HKEX Market Data](https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities)
- [NYSE Hours and Calendars](https://www.nyse.com/markets/hours-calendars)
- [Nasdaq Market Activity](https://www.nasdaq.com/market-activity)

这些链接是原始披露与市场日历入口，不是已冻结的每日证据，也不代表对预测有效性的背书。
