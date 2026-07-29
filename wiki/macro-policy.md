---
id: VC-WIKI-MACRO-POLICY
title: 宏观政策传导框架
version: 1.1.0
updated_at: 2026-07-16
published_at: 2026-07-16T00:00:00+08:00
status: active
owners:
  - macro_policy_agent
  - risk_critic_agent
tags:
  - macro
  - monetary-policy
  - fiscal-policy
  - regulation
source_urls:
  - https://www.pbc.gov.cn/zhengcehuobisi/125207/125227/125957/index.html
  - https://data.stats.gov.cn/
  - https://sousuo.www.gov.cn/zcwjk/policyRetrieval
  - https://www.csrc.gov.cn/
  - https://www.safe.gov.cn/
  - https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
---

<!-- section:scope -->
## 职责边界

宏观政策 Agent 研究“新增信息如何通过流动性、增长预期、风险溢价和汇率影响未来
一至两个 A 股交易日”，而不是解释所有长期经济问题。它不因某项政策名称看似利好
就直接给出上涨判断，也不把政策目标当成已实现结果。

必须区分四件事：

1. 事实：正式文件、操作结果或统计发布了什么。
2. 预期：市场在发布前大致预期什么。
3. 预期差：新信息相对既有预期改变了什么。
4. 价格映射：该变化是否已在境内外价格中提前反映。

<!-- section:source-priority -->
## 信息源优先级

优先使用中国人民银行、国家统计局、国务院政策文件库、证监会、国家外汇管理局和
交易所原文。海外货币政策使用对应央行原文与正式日程。新闻摘要只能帮助发现事件，
不能替代原文确认。

口头表态必须记录讲话人、场合、完整上下文与发布时间；同一内容被多家媒体转载仍
只算一份证据。

<!-- section:transmission-chain -->
## 传导链

对每个事件依次检查：

- 政策工具：利率、准备金、公开市场操作、财政支出、税费、行业监管或资本市场制度。
- 直接变量：资金价格、信用供给、政府需求、企业成本、汇率或可交易资产供给。
- 盈利与估值：影响现金流预期，还是影响折现率与风险偏好。
- 指数暴露：受影响行业和市值风格在目标指数中的实际权重。
- 时间尺度：信息能否在 D1 或 D2 内被市场定价。

若链条中任一关键环节只有猜测，必须降低置信度并写入失效条件。

<!-- section:event-classification -->
## 每日事件分类

| 类型 | 先验证的问题 | 常见误判 |
| --- | --- | --- |
| 货币操作 | 数量、价格、期限和到期量共同意味着什么 | 只看投放量，不看净投放与价格 |
| 利率或准备金 | 是否超预期，传导对象是谁 | 把方向正确等同于短期指数必涨 |
| 财政政策 | 是否已有预算、执行主体与时间表 | 把目标、部署和实际支出混为一谈 |
| 监管政策 | 约束对象、实施日期和过渡安排 | 忽略利好一方可能对应另一方成本 |
| 宏观数据 | 同比、环比、季调、修订和基数 | 只看标题数字，不看预期差 |
| 汇率与跨境资金 | 境内外时点、美元因素和政策信号 | 把相关性写成单向因果 |
| 海外央行 | 决议、指引与市场定价差 | 忽略事件发生在 A 股收盘之后 |

<!-- section:index-mapping -->
## 对五个指数的映射

映射必须与五个指数 Wiki 及当日成份权重快照联用：

- 沪深300：更关注大盘盈利、金融条件和外资风险偏好的广泛传导。
- 中证500与中证1000：除增长预期外，更检查流动性和小中盘风险溢价。
- 创业板指与科创50：更检查长久期成长估值、科技产业政策和利率敏感性。

以上只是研究起点，不是固定方向。若当日成份结构、拥挤度或已有价格反应与框架
冲突，以冻结的当日证据为准。

<!-- section:daily-checklist -->
## 每日检查

- 记录 event_time、published_at、ingested_at 和 data_cutoff。
- 查阅正式发布日程，区分“未发布”和“抓取失败”。
- 对比前值、修订值、市场预期和实际值，注明预期来源。
- 检查股、债、汇及相关海外资产是否已先行定价。
- 为每条结论列出至少一个反向解释。
- 不存在可验证预期差时仍须按现有证据在 up/down 中选边，但必须降低涨跌条件置信度、
  提高小波动概率并写明数据缺口；关键输入不可信时阻断正式 run。

<!-- section:invalidation -->
## 失效条件

出现以下任一情况时，原结论需要降级或失效：

- 正式原文与摘要含义不一致；
- 关键数据在发布后被重大修订；
- 政策没有明确实施安排，或执行被延后；
- 市场在 data_cutoff 前已经完成与判断同方向的大幅定价；
- 指数实际成份暴露与假设相反；
- 主要传导来自 D1/D2 之外的长期机制。

<!-- section:sources -->
## 来源

- [中国人民银行：中国货币政策执行报告](https://www.pbc.gov.cn/zhengcehuobisi/125207/125227/125957/index.html)
- [国家统计局：国家数据](https://data.stats.gov.cn/)
- [国务院政策文件库](https://sousuo.www.gov.cn/zcwjk/policyRetrieval)
- [中国证券监督管理委员会](https://www.csrc.gov.cn/)
- [国家外汇管理局](https://www.safe.gov.cn/)
- [Federal Reserve：FOMC 日程与材料](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
