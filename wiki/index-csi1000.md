---
id: VC-WIKI-INDEX-CSI1000
title: 中证1000指数暴露框架
version: 1.1.0
updated_at: 2026-07-16
published_at: 2026-07-16T00:00:00+08:00
status: active
owners:
  - macro_policy_agent
  - market_news_agent
  - ai_storage_industry_agent
  - cio_agent
tags:
  - index
  - csi1000
  - small-cap
source_urls:
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000852
  - https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000852_Index_Methodology_cn.pdf
---

<!-- section:identity -->
## 指数身份

- 系统代码：000852.SH
- 名称：中证1000
- 官方编制机构：中证指数有限公司
- 定位：选择中证800之外规模偏小且流动性较好的 1000 只证券，与沪深300和中证500
  形成互补。

“偏小”是相对其他宽基的样本定位，不代表每个成份都是同一种风格或风险。

<!-- section:structural-exposure -->
## 结构性暴露

短周期分析重点检查：

- 小盘相对大盘的流动性、风险溢价和拥挤度；
- 较分散的成份结构是否放大行业广度而降低单一权重股影响；
- 主题行情能否从少数个股扩散到足够多的指数成份；
- 成交、涨跌家数和极端个股对指数解释的差异；
- 政策对专精特新、制造业或融资环境的实际传导。

小盘高波动不等同于上涨或下跌方向；它只意味着结果概率分布和评价噪声带需要按自身数据
计算。

<!-- section:daily-snapshot -->
## 每日必须冻结

- 指数及最近 21 个有效收盘价；
- 成份、权重、行业分布和市值分位；
- 涨跌家数、成交集中度及异常停牌；
- 调样与临时调整公告；
- 与中证500、中证2000等相邻规模指数的边界检查。

<!-- section:d1-d2-drivers -->
## D1/D2研究问题

- 事件能否扩散至大量成份，还是只影响少数概念股？
- 市场流动性与风险偏好是否支持小盘相对表现？
- 公告或政策对小公司融资、订单和估值的影响能否在两日内定价？
- 极端涨停或跌停是否导致表面广度与可交易性不一致？
- D1冲击后，D2更可能延续、扩散还是均值回归？证据是什么？

<!-- section:ai-memory-mapping -->
## AI存储映射

中证1000可能覆盖较多设备、材料、零部件和主题型小市值公司，因而对 AI 存储叙事
和风险偏好更敏感。但“概念相关”与“盈利相关”必须分开：

- 只有公司正式披露才能支持直接业务关系；
- 主题联动可以作为价格证据，但只能标记为风险偏好代理；
- 产业链公司数量多并不意味着指数权重大；
- 传闻驱动且无法升级来源时，不得给出强方向。

<!-- section:guardrails -->
## 解释护栏

- 不从少数涨停股推导整个中证1000。
- 不把高换手或高波动写成方向信号。
- 不把公司互动平台的模糊回复写成确定订单。
- 不用当前小盘风格印象回填历史。
- 不能验证成份与权重时阻断正式 run，不用中性、abstain 或固定方向填补证据缺口。

<!-- section:sources -->
## 来源

- [中证指数：中证1000详情](https://www.csindex.com.cn/zh-CN/indices/index-detail/000852)
- [中证指数：中证1000指数编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000852_Index_Methodology_cn.pdf)

具体成份、权重和指数表现必须引用预测日取得的官方快照或经过验证的数据提供者。
