---
id: VC-WIKI-MARKET-NEWS
title: 市场资讯与预期差研究框架
version: 0.1.0
updated_at: 2026-07-15
status: draft
owners:
  - market_news_agent
  - risk_critic_agent
tags:
  - market-news
  - event
  - expectations
  - disclosure
source_urls:
  - https://www.cninfo.com.cn/
  - https://www.sse.com.cn/disclosure/listedinfo/announcement/
  - https://www.szse.cn/disclosure/notice/company/index.html
  - https://www.csrc.gov.cn/csrc/c100028/common_list.shtml
---

<!-- section:scope -->
## 职责边界

市场资讯 Agent 研究 data_cutoff 前新增、可能在 D1/D2 被重新定价的事件。它负责判断
“新在哪里、相对预期差在哪里、是否已经交易”，不负责把新闻数量或标题情绪直接转换成
指数方向，也不把二手报道当成已核实事实。

<!-- section:event-ontology -->
## 事件分类骨架

- 上市公司：业绩、指引、重大合同、并购重组、风险提示与治理变化。
- 市场制度：交易规则、监管措施、指数调整、融资和减持制度。
- 跨市场：汇率、利率、商品、海外股指及加密资产的风险偏好变化。
- 突发事件：自然灾害、地缘政治、供应中断和重大技术事故。

后续维护应继续拆分事件实体、影响对象、首次发布时间、预期来源和重复转载关系。

<!-- section:source-map -->
## 来源地图

公司动态优先使用巨潮资讯、交易所公告和公司正式披露；监管动态优先使用证监会、交易所
及政府部门原文。新闻聚合和搜索只负责发现线索，正式观点必须回到可以冻结的原始来源。

<!-- section:expectation-gap -->
## 预期差判断

每个事件至少回答：此前可验证预期是什么、实际新增事实是什么、差异是否影响盈利或风险
溢价、目标指数是否有足够暴露、data_cutoff 前价格是否已经反映。无法证明预期基准时，
不得把“消息利好/利空”当成预期差。

<!-- section:index-mapping -->
## 指数映射

事件必须与预测日的指数成份、权重和风格暴露联用。单一公司事件只有在指数权重、产业链
扩散或风险偏好传导可验证时，才可以影响宽基指数判断。

<!-- section:time-policy -->
## 时间与去重

- 同时保存 event_time、published_at、ingested_at 与 data_cutoff。
- data_cutoff 后取得的内容进入下一次 run，不回填本次判断。
- 同一原始公告的转载、摘要和社交媒体转述只算一个事实源。
- 网页修订必须产生新快照和哈希，不覆盖第一次取得的版本。

<!-- section:counter-evidence -->
## 反证与常见错误

- 标题与公告正文限定条件不一致。
- 事件真实但早已进入市场预期或价格。
- 多家媒体引用同一匿名来源形成虚假共识。
- 长期产业叙事被压缩为 D1/D2 必然方向。
- 指数实际暴露不足，个股反应不能代表宽基指数。

<!-- section:maintenance -->
## 待补知识与维护问题

- 建立公开来源的稳定采集方式、许可和失败降级规则。
- 为公告、政策、跨市场和突发事件建立更细的实体与关系词表。
- 用到期评分检验哪些事件类型在 D1/D2 有可重复的增量信息。
- 补充来源冲突、网页修订和市场预期证据的实例库。

<!-- section:sources -->
## 来源

- [巨潮资讯](https://www.cninfo.com.cn/)
- [上海证券交易所上市公司公告](https://www.sse.com.cn/disclosure/listedinfo/announcement/)
- [深圳证券交易所上市公司公告](https://www.szse.cn/disclosure/notice/company/index.html)
- [中国证监会新闻发布](https://www.csrc.gov.cn/csrc/c100028/common_list.shtml)
