# VeriCouncil Wiki Log

本文件只追加，不回写历史记录。条目标题固定使用
`## [YYYY-MM-DD] operation | subject`，便于 Codex 和命令行检索。

## [2026-07-15] bootstrap | Karpathy LLM Wiki scaffold

- 采用“不可变来源、AI 维护 Markdown Wiki、Schema 约束”的三层模式。
- 增加内容索引、演进日志、提案区和领域条目模板。
- 新建市场资讯领域骨架草案。
- 维护阶段仍为本地 AI bootstrap；尚未启用自动晋升。

## [2026-07-16] promote | market strategy agent

- 新增 `VC-WIKI-MARKET-STRATEGY`，用于市场状态、风格与五指数配置综合。
- 工作流改为“基础研究 → 策略研究员 → 风险反证 → 校验 → CIO”。
- 策略概率作为 CIO 唯一方向输入，避免和基础观点重复计权。
- 五指数配置分数、并列排名、市场状态与风格标签由同一完整截面确定性派生并保存。
- 策略观点沿用 AgentOpinion 与到期 Scorecard，按独立版本记录历史表现。

## [2026-07-16] promote | mandatory binary prediction direction

- `VC-WIKI-PREDICTION-LABELS` 升级至 2.0.0：所有新预测立场只允许 up 或 down。
- neutral 仅保留为实际收益落入评价噪声带的结果概率，不再是可选预测立场。
- p_up 与 p_down 精确相等时拒绝输出，不使用固定多头或空头默认值。
- Quant 在可信只读数据适配器接入前不产生 Opinion，避免用固定中性或伪随机方向冒充模型。
- 旧三分类预测按原版本只读保留，禁止与新版本成绩混算。

## [2026-07-27] promote | public project rename to SignalRace

- 对外项目名称由 VeriCouncil 更新为 SignalRace（人机信号赛马）。
- `VC-WIKI-MANIFEST`、`VC-WIKI-PREDICTION-LABELS` 和
  `VC-WIKI-RISK-CHECKLIST` 分别升级至 1.3.1、2.0.1 和 2.0.1。
- 历史 Wiki 快照、协议 schema、哈希域和本日志既有字节不回写。

## [2026-07-27] document | source-agnostic Agent framework

- “人机信号赛马”不再作为中文项目名；对外品牌保持 SignalRace，中文仅描述为“可验证的预测 Agent 框架”。
- Agent 来源区分为 `ai`、`manual`、`quant` 和 `deterministic`；User Judgment Agent 是首个
  `manual` 实现，不代表项目只比较人类与 AI。
- 本次增加结构化职责 `workflow_role` 与注册来源 `source_type`，不回写 v1 run hash、
  历史 schema 或既有 Wiki 日志条目。

## [2026-07-27] promote | public project rename to forecast-loop

- 对外项目名称由 SignalRace 更新为 `forecast-loop`；Forecast 表示事前封签预测，
  Loop 表示预测、解释、结算、反省、Lesson 与下一次预测形成的持续闭环。
- `VC-WIKI-MANIFEST`、`VC-WIKI-PREDICTION-LABELS` 和
  `VC-WIKI-RISK-CHECKLIST` 分别升级至 1.3.2、2.0.2 和 2.0.2。
- 三份 active 条目的 `published_at` 固定为 `2026-07-27T16:00:00+08:00`；早于该
  时刻的 Live cutoff 不得引用本次晋升版本。
- 历史 Wiki 快照、`vericouncil.*` schema、哈希域、兼容 CLI 和本日志既有字节不回写。

## [2026-07-29] promote | configurable global equity universe

- 新增 active 条目 `VC-WIKI-GLOBAL-EQUITY-RESEARCH` 1.0.0，为港股、美股、指数和个股
  提供市场时钟、证据、预期差、个股基本面与风险反证框架。
- 版本化 Market Universe 可为每个标的固定 Wiki entry，并通过 `agent_briefs` 调整固定
  Agent ID 在新市场或个股上的研究职责。
- 每次预测仍冻结具体 Wiki 版本与 evidence cutoff；每日新闻不会直接成为长期 Wiki 真理。
- Daily Reflection v1 继续保持五个 A 股指数边界，自定义 Universe 不会绕过其数据与人工门禁。
