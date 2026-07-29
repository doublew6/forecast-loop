---
id: VC-WIKI-RISK-CHECKLIST
title: 风险与反证检查表
version: 2.0.2
updated_at: 2026-07-27
published_at: 2026-07-27T16:00:00+08:00
status: active
owners:
  - risk_critic_agent
  - cio_agent
tags:
  - risk
  - evidence
  - leakage
  - validation
source_urls:
  - https://www.nist.gov/itl/ai-risk-management-framework
  - https://docs.langchain.com/oss/python/langgraph/persistence
  - https://www.csrc.gov.cn/
---

<!-- section:role -->
## 角色边界

Risk Critic 的任务是寻找结论可能错误的原因，检查证据和时间污染，并提出失效条件。
它需要给出反证后的 up/down 风险倾向，但不参与 CIO 方向投票、权重为零；CIO 必须明确
回应其关键异议。

<!-- section:pre-run -->
## 运行前检查

- 预测日是有效 A 股交易日，D1 与 D2 均按交易日历计算。
- 五个指数当前收盘价、此前至少 21 个有效收盘价和数据时间戳完整。
- 行情源没有静默返回旧交易日数据。
- 所有资讯均包含 event_time、published_at、ingested_at、source_url 和内容哈希。
- data_cutoff 已冻结，晚于截止时间的内容不进入上下文。
- Wiki 条目 ID、版本、段落与内容哈希可以解析。

<!-- section:evidence-review -->
## 证据检查

- 每个关键事实是否来自原始来源，还是二手转述？
- 引用段落是否真的支持结论，还是只与主题相关？
- 同一原始事件的转载是否被误算成多份独立证据？
- 事实、管理层观点、市场预期和 Agent 推断是否清楚分开？
- 是否存在原文限定词、时间范围或口径被摘要遗漏？
- 是否列出与主要方向相冲突的可靠证据？
- 是否使用后来修订的数据回填历史判断？

<!-- section:reasoning-review -->
## 推理检查

- 因果链是否跳过了必要环节？
- 行业事件是否有目标指数的当日成份和权重支持？
- 相关性是否被写成因果关系？
- “长期利好”是否被不当压缩成 D1/D2 方向？
- 市场是否已在 data_cutoff 前完成定价？
- 多个 Agent 是否因共享同一来源而形成虚假共识？
- 是否存在宏观、行业和市场事件的相互抵消？

<!-- section:output-review -->
## 输出检查

- up、neutral、down 均位于 0 至 1，且总和为 1。
- direction 只允许 up 或 down，并与 p_up、p_down 中较大的一侧一致。
- p_up 与 p_down 精确相等必须拒绝；不得默认看多或看空。
- p_neutral 只表示实际收益落入评价噪声带的概率，不是预测立场。
- D1、D2 分开输出，不得用一套理由机械复制。
- 评价噪声带引用预测日计算值和对应波动率样本。
- evidence、counter_evidence、invalidation_conditions 均为具体可核查陈述。
- 正式研究 Agent 至少有一个有效 Wiki 引用和原始来源。
- Quant 数据适配器未通过验证时 status=unavailable，不产生 Opinion，weight=0。
- 缺证据时降低涨跌条件置信度并明确数据缺口；关键输入不可信时阻断 run，不得补造引用。

<!-- section:blocking-rules -->
## 必须阻断发布的情况

出现以下任一项，Evidence Validator 应使 run 失败，而不是降级后悄悄发布：

- 关键行情缺失、交易日错误或 data_cutoff 不可信；
- 引用的 Wiki ID、版本、段落或内容哈希不存在；
- 原始来源发布时间晚于 data_cutoff；
- 概率 schema 不合法；
- LLM 输出无法在允许重试次数内解析；
- input_hash 无法生成或冻结快照失败；
- unavailable Quant 产生方向 Opinion 或被赋予非零聚合权重；
- Strategy 概率与其三份基础研究输入被再次平均，造成同源观点重复计权。
- 把不同工作流阶段的 weight 摊平相加，误读为所有 Agent 直接向 CIO 投票。

资料足够但观点分歧不属于运行失败，应保留分歧并由 CIO 明确选择涨跌，同时给出较低的
涨跌条件置信度、较高的小波动概率和可证伪条件。

<!-- section:post-run -->
## 运行后检查

- 已完成预测不可覆盖；重跑必须创建新的 run_id。
- D1 与 D2 只能在目标交易日收盘数据确认后评分。
- Demo、旧版 placeholder 和当前 unavailable 状态按成绩单规则分别处理。
- 评价保留价格来源、实际收益、标签、Brier Score 和 evaluated_at。
- 发现数据修订时追加纠错记录，不修改当时真实可见的信息快照。

<!-- section:sources -->
## 来源

- [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [中国证券监督管理委员会](https://www.csrc.gov.cn/)

本检查表是 forecast-loop 的工程与研究治理规则；外部来源提供风险管理、持久化和
监管信息入口，不代表对系统预测有效性的背书。
