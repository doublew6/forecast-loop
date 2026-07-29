---
id: VC-WIKI-SOURCE-TIERS
title: 数据源可信等级与引用规则
version: 1.1.0
updated_at: 2026-07-16
published_at: 2026-07-16T00:00:00+08:00
status: active
owners:
  - market_news_agent
  - risk_critic_agent
  - cio_agent
tags:
  - sources
  - provenance
  - evidence
source_urls:
  - https://www.gov.cn/
  - https://www.csrc.gov.cn/
  - https://www.sse.com.cn/
  - https://www.szse.cn/
  - https://www.sec.gov/edgar/search/
---

<!-- section:principle -->
## 基本原则

可信等级评价的是“这个来源是否有资格证明该事实”，不是网站知名度。即使来自一级
来源，管理层展望仍是观点而非已实现事实；即使来自低等级来源，也可以作为发现线索，
但不能升级成未经确认的事实。

每条证据记录：

- source_url、publisher 和 source_tier；
- event_time、published_at 与 ingested_at；
- 标题、引用摘录与内容哈希；
- 涉及实体、事件类型和原始语言；
- 与 Wiki 引用及决策的关联。

<!-- section:tier-one -->
## Tier 1：原始权威来源

包括：

- 法律法规、政府部门、央行、监管机构和交易所正式发布；
- 指数编制机构的方案、成份与调整公告；
- 上市公司监管披露、财报、正式业绩材料和公司 IR；
- 官方统计机构数据；
- 标准组织正式规范。

Tier 1 可以直接支持其职责范围内的事实，但仍需核对文件版本、统计口径、发布日期
和限定条件。

<!-- section:tier-two -->
## Tier 2：直接专业来源

包括具名行业协会、科研机构、官方会议完整记录，以及可说明采集方法的专业数据服务。
这类来源可以支持行业事实或一致口径数据，但若原始公告可得，应继续链接到 Tier 1。

无法审计方法、样本或修订记录的数据服务不得自动列为 Tier 2。

<!-- section:tier-three -->
## Tier 3：可靠二手来源

包括有编辑责任的主流媒体、通讯社和具名研究报告。它们适合发现事件、理解市场预期
或记录采访观点，但不应在存在原文时替代 Tier 1。

一条 Tier 3 报道只有在明确给出消息来源时，才可支持“某人表示”或“市场预期”
这类事实；匿名消息必须在结论中标明不确定性。

<!-- section:tier-four -->
## Tier 4：未验证线索

包括社交媒体、论坛、群聊截图、无出处转载和匿名供应链传闻。Tier 4 只能触发进一步
检索，不能单独支持正式方向判断，也不能被写入 Wiki 的稳定知识。

若在 data_cutoff 前无法升级为可核验来源，应忽略该事实；如果因此缺少关键输入则阻断
正式 run，不能用 neutral、abstain 或任意涨跌默认值掩盖数据缺口。

<!-- section:independence -->
## 来源独立性

多个网页引用同一公告、采访或匿名消息时，只算一份证据。Evidence Validator 应通过
原始 URL、内容哈希、引用关系和事件实体进行去重。

以下不是独立确认：

- 多家媒体转载同一通讯社稿件；
- 研究报告重复引用公司同一次电话会；
- 社交媒体截图来自同一个未公开传闻；
- 不同 Agent 检索到同一原始公告。

<!-- section:time-policy -->
## 时间与修订

- published_at 决定信息何时公开可见，event_time 不能替代它。
- ingested_at 晚于 data_cutoff 时，不进入当日正式 run，即使 published_at 更早；
  历史回放只有在能证明当时已被系统取得时才可使用。
- 对会修订的数据保存最初发布快照和后续修订，不覆盖旧哈希。
- 网页无明确发布时间时，降低等级并保存首次抓取时间。
- URL 失效时保留内容哈希和快照位置，并为 Wiki 做 PATCH 版本链接修复。

<!-- section:minimum-evidence -->
## 最低证据要求

- 事实判断：至少一条适格来源，优先 Tier 1。
- 市场预期：说明预期的采样来源和时间，不能由 Agent 自己假设。
- 供应链映射：公司或监管披露优先；只有 Tier 4 时不得建立确定关系。
- CIO 最终判断：至少引用一个有效研究 Agent 的 Wiki 段落和该次运行的原始来源。
- 高涨跌条件置信度、低小波动概率的判断应有相互独立的证据；否则降低置信度。

<!-- section:sources -->
## 官方入口

- [中国政府网](https://www.gov.cn/)
- [中国证券监督管理委员会](https://www.csrc.gov.cn/)
- [上海证券交易所](https://www.sse.com.cn/)
- [深圳证券交易所](https://www.szse.cn/)
- [SEC EDGAR](https://www.sec.gov/edgar/search/)

这些入口展示 Tier 1 的典型形态；具体决策仍须引用具体文件，而不是只引用网站首页。
