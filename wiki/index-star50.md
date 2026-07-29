---
id: VC-WIKI-INDEX-STAR50
title: 科创50指数暴露框架
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
  - star50
  - technology
source_urls:
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000688
  - https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000688_Index_Methodology_cn.pdf
---

<!-- section:identity -->
## 指数身份

- 系统代码：000688.SH
- 名称：上证科创板50成份指数，简称科创50
- 官方编制机构：上海证券交易所、中证指数有限公司
- 定位：由科创板中市值较大、流动性较好的代表性证券构成。

只有 50 个样本不意味着暴露恒定；调样、权重变化和头部公司行情都可能显著改变结构。

<!-- section:structural-exposure -->
## 结构性暴露

短周期分析重点检查：

- 半导体、硬科技及其他科创行业的实际权重和集中度；
- 头部成份公告与海外科技链映射；
- 成长估值对利率、风险溢价和产业政策的敏感性；
- 新股、限售、停复牌和样本调整对有效权重的影响；
- 指数表现与科创板整体市场广度是否分离。

“硬科技”是板块定位，不是每个成份的相同盈利因子。

<!-- section:daily-snapshot -->
## 每日必须冻结

- 指数与最近 21 个有效收盘价；
- 当日成份、自由流通权重、行业与前十大权重；
- 调样、临时调整和重大停复牌；
- 权重公司在 data_cutoff 前的公告；
- 海外半导体、设备、存储公司事件的准确发布时间。

<!-- section:d1-d2-drivers -->
## D1/D2研究问题

- 海外科技事件与科创50成份之间是否存在已披露的业务链？
- 半导体事件能否覆盖足够指数权重，还是仅是局部公司？
- 国内政策改变的是远期产业预期还是两日内可交易信息？
- 头部公司与科创板广度是否同向？
- 海外事件发生在 A 股收盘前还是之后，对 D1、D2窗口分别意味着什么？

<!-- section:ai-memory-mapping -->
## AI存储映射

五个目标指数中，科创50可能更容易形成半导体设备、材料、芯片和先进制造的直接
映射，但仍须验证：

- HBM、DRAM、NAND、先进封装和算力芯片的具体环节；
- 成份公司的公开产品、订单或客户证据；
- 该业务在公司和指数中的实际重要性；
- 海外正面事件是否同时带来供应约束、出口限制或估值反证。

不得因公司属于半导体行业就默认受益于所有 AI 存储事件。

<!-- section:guardrails -->
## 解释护栏

- 不把海外半导体指数方向机械复制给科创50。
- 不把产品送样、认证、量产和收入放量混为一谈。
- 不忽略少数权重公司对 50 成份指数的集中影响。
- 不用科创板整体新闻代替科创50成份证据。
- 数据与供应链证据不完整时阻断正式 run，不用中性、abstain 或固定方向填补缺口。

<!-- section:sources -->
## 来源

- [中证指数：科创50详情](https://www.csindex.com.cn/zh-CN/indices/index-detail/000688)
- [中证指数：科创50指数编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000688_Index_Methodology_cn.pdf)

具体成份、权重和指数表现必须引用预测日取得的官方快照或经过验证的数据提供者。
