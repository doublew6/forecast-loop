---
id: VC-WIKI-INDEX-CSI300
title: 沪深300指数暴露框架
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
  - csi300
  - large-cap
source_urls:
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000300
  - https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000300_Index_Methodology_cn.pdf
---

<!-- section:identity -->
## 指数身份

- 系统代码：000300.SH
- 名称：沪深300
- 官方编制机构：中证指数有限公司
- 定位：由沪深市场中规模大、流动性好的代表性证券构成，用于观察大盘 A 股整体表现。

编制方案会修订，样本也会定期调整。预测必须使用预测日有效的方案、成份和权重，
不得把本条目的概括当作永久成份表。

<!-- section:structural-exposure -->
## 结构性暴露

沪深300首先是一组大盘代表性公司，而不是单一行业指数。短周期判断应重点检查：

- 大盘盈利与经济增长预期；
- 银行、非银、消费、工业与大盘科技等实际权重分布；
- 利率、信用、汇率及境内外风险偏好；
- 权重股重大公告和指数衍生品相关资金流；
- 与其他大盘宽基的共同市场因子。

“大盘”“价值”或“核心资产”只能作为风格描述，不能替代当日行业和个股权重。

<!-- section:daily-snapshot -->
## 每日必须冻结

- 指数收盘价、成交信息和交易日；
- 最近 21 个有效收盘价及其来源；
- 当日有效成份、自由流通权重或可获得的最新官方权重；
- 前十大权重和一级行业权重；
- 当日调样、临时调整及重大停复牌；
- 权重公司在 data_cutoff 前的正式公告。

若无法取得可审计的成份快照，行业 Agent 只能描述市场风格，不得声称具体指数贡献。

<!-- section:d1-d2-drivers -->
## D1/D2研究问题

- 宏观或监管事件是否直接改变大盘公司的盈利或折现率预期？
- 事件相对市场预期是否足够新，还是已经被权重股价格反映？
- 海外市场、汇率、债券与股指期货是否给出一致或冲突信号？
- 影响集中在少数权重股，还是能跨行业传导？
- D1 与 D2 的理由是否有时间差，例如盘后公告只在下一交易日开始定价？

<!-- section:ai-memory-mapping -->
## AI存储映射

AI存储事件对沪深300的影响可能来自大盘科技成份、相关制造业公司或整体科技风险
偏好，但通常会被指数的多行业结构稀释。必须通过当日成份与业务披露证明直接暴露；
仅凭海外半导体指数上涨，不足以推出沪深300方向。

若事件主要影响中小型供应链公司，应同时检查中证500、中证1000、创业板指和科创50，
不要把主题热度机械映射到沪深300。

<!-- section:guardrails -->
## 解释护栏

- 不用当前成份解释历史日期。
- 不把指数点位涨跌归因于单一新闻，除非有充分的权重与时间证据。
- 不把 ETF 申赎、期货基差或北向相关指标单独当成基本面因果。
- 不把某个行业上涨等同于指数上涨；先计算其权重与其他行业抵消。
- 官方方案、成份和行情冲突时阻断正式预测，等待数据核对。

<!-- section:sources -->
## 来源

- [中证指数：沪深300详情](https://www.csindex.com.cn/zh-CN/indices/index-detail/000300)
- [中证指数：沪深300指数编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000300_Index_Methodology_cn.pdf)

具体成份、权重和指数表现必须引用预测日取得的官方快照或经过验证的数据提供者。
