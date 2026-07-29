---
id: VC-WIKI-INDEX-CSI500
title: 中证500指数暴露框架
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
  - csi500
  - mid-cap
source_urls:
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000905
  - https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000905_Index_Methodology_cn.pdf
---

<!-- section:identity -->
## 指数身份

- 系统代码：000905.SH
- 名称：中证500
- 官方编制机构：中证指数有限公司
- 定位：在沪深市场中选择沪深300之外、具有一定流动性和规模代表性的 500 只证券，
  与沪深300形成规模层次互补。

中证500常被用作中盘风格观察工具，但具体行业和市值暴露会随样本调整变化。

<!-- section:structural-exposure -->
## 结构性暴露

短周期分析重点检查：

- 中盘相对大盘的风险偏好与流动性；
- 行业分布是否让某项制造业、科技或周期事件更集中；
- 成份公司业绩预告、订单与监管公告的扩散程度；
- 与沪深300、中证1000之间的相对强弱是否来自规模风格还是行业差异；
- 股指期货与指数产品资金变化是否只是结果，而非事件原因。

“中盘成长”不是固定属性，任何风格标签都须由当日权重数据验证。

<!-- section:daily-snapshot -->
## 每日必须冻结

- 指数收盘价与最近 21 个有效收盘价；
- 当日有效成份、权重、行业与市值分布；
- 调样公告、临时剔除和长期停牌情况；
- 主要权重公司在 data_cutoff 前的新公告；
- 与沪深300和中证1000的重叠检查及相对收益。

<!-- section:d1-d2-drivers -->
## D1/D2研究问题

- 新事件影响的是中盘公司盈利，还是只是全市场风险偏好？
- 流动性变化对中盘估值的边际作用是否强于大盘？
- 相关产业链公司在指数中是少数个股，还是形成可观行业权重？
- 事件是否已在主题交易中提前反映，存在利好兑现风险？
- D2 是否需要等待海外交易时段或后续公司确认？

<!-- section:ai-memory-mapping -->
## AI存储映射

中证500可能包含半导体设备、材料、电子制造和数据中心供应链中的中盘公司，因此
AI存储事件有机会形成比沪深300更直接的产业链映射。但必须逐日验证：

- 公司确有公开披露的相关业务，而非名称或概念联想；
- 相关业务对公司和指数的权重足够重要；
- 海外事件能通过客户、产品、价格或资本开支链条传导；
- 同期其他大权重行业没有明显反向冲击。

<!-- section:guardrails -->
## 解释护栏

- 不把“中小盘活跃”当作所有中证500成份同步受益。
- 不用未经证实的供应链名单建立指数暴露。
- 不把中证500与中证1000混称小盘指数。
- 不因指数外公司事件直接推导指数方向。
- 成份权重不可得时降低置信度，不补造行业占比。

<!-- section:sources -->
## 来源

- [中证指数：中证500详情](https://www.csindex.com.cn/zh-CN/indices/index-detail/000905)
- [中证指数：中证500指数编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000905_Index_Methodology_cn.pdf)

具体成份、权重和指数表现必须引用预测日取得的官方快照或经过验证的数据提供者。
