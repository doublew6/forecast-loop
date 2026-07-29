---
id: VC-WIKI-INDEX-CHINEXT
title: 创业板指暴露框架
version: 1.0.0
updated_at: 2026-07-13
published_at: 2026-07-13T00:00:00+08:00
status: active
owners:
  - macro_policy_agent
  - market_news_agent
  - ai_storage_industry_agent
  - cio_agent
tags:
  - index
  - chinext
  - growth
source_urls:
  - https://www.cnindex.com.cn/docs/gz_399006.pdf
  - https://www.szse.cn/disclosure/notice/t20250430_613353.html
  - https://www.cnindex.com.cn/module/index-series.html?act_menu=1&index_type=-1
---

<!-- section:identity -->
## 指数身份

- 系统代码：399006.SZ
- 名称：创业板指
- 官方发布与管理：深圳证券交易所、深圳证券信息有限公司
- 定位：由创业板市场中规模较大、流动性较好的代表性股票构成。

指数方案曾经修订，成份和权重会调整。系统应以预测日有效的国证指数方案与快照为准。

<!-- section:structural-exposure -->
## 结构性暴露

创业板指通常被视为创新成长企业的重要标尺，但短周期判断需要拆开：

- 实际行业集中度及头部权重股影响；
- 成长估值对利率、风险溢价和盈利预期的敏感性；
- 医药、新能源、电子等权重行业之间的抵消；
- 创业板整体广度与指数头部公司表现的差异；
- 指数方案中的权重限制和样本调整影响。

“成长风格”不是上涨信号，且不代表所有成份都与 AI 或半导体有关。

<!-- section:daily-snapshot -->
## 每日必须冻结

- 指数与最近 21 个有效收盘价；
- 当日成份、权重、行业分布和前十大权重；
- 头部公司公告、业绩和异常停复牌；
- 样本调整及编制方案修订；
- 创业板综、科创50和大盘宽基的相对表现。

<!-- section:d1-d2-drivers -->
## D1/D2研究问题

- 新事件影响头部权重行业，还是只影响创业板边缘公司？
- 无风险利率或风险溢价变化是否足以改变成长估值？
- 产业事件对盈利预期的影响是否超过其他权重行业反向变化？
- 价格是否已经因主题交易提前反映？
- D1 与 D2 是否分别由盘后公告、海外映射或后续确认驱动？

<!-- section:ai-memory-mapping -->
## AI存储映射

创业板可能包含电子、半导体设备材料、服务器或数据中心相关公司，但必须由当日权重
确认其指数影响。AI 存储 Agent 应区分：

- 公司主营或已披露的直接业务；
- 经公开证据确认的供应链关系；
- 只有风格相似性的科技风险偏好代理。

如果主要 AI 存储公司位于科创板或指数之外，不能用“创业板是成长板块”替代暴露证据。

<!-- section:guardrails -->
## 解释护栏

- 不将创业板所有公司统称科技公司。
- 不忽略少数头部权重对指数与市场广度的分离。
- 不把方案中的 ESG 或权重规则直接解释为短期方向。
- 不用创业板综的成份结构代替创业板指。
- 业务与权重链条缺失时，不发布强 AI 主题判断。

<!-- section:sources -->
## 来源

- [国证指数：创业板指数编制方案](https://www.cnindex.com.cn/docs/gz_399006.pdf)
- [深交所：关于修订创业板指数编制方案的公告](https://www.szse.cn/disclosure/notice/t20250430_613353.html)
- [国证指数：指数系列查询](https://www.cnindex.com.cn/module/index-series.html?act_menu=1&index_type=-1)

具体成份、权重和指数表现必须引用预测日取得的官方快照或经过验证的数据提供者。
