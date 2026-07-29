---
id: VC-WIKI-MARKET-STRATEGY
title: 市场策略与指数配置框架
version: 2.0.0
updated_at: 2026-07-16
published_at: 2026-07-16T00:00:00+08:00
status: active
owners:
  - strategy_agent
  - cio_agent
tags:
  - market-strategy
  - allocation
  - style
  - index-relative-strength
source_urls:
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000300
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000905
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000852
  - https://www.cnindex.com.cn/module/index-series.html?act_menu=1&index_type=-1
  - https://www.csindex.com.cn/zh-CN/indices/index-detail/000688
  - https://www.pbc.gov.cn/
  - https://www.csrc.gov.cn/
---

<!-- section:scope -->
## 职责边界

策略研究员不重新采集事实，也不把三位专业研究员当成四张独立选票。它只综合已经冻结并
通过结构校验的宏观、市场资讯和产业观点，形成 D1/D2 的市场状态、风格、五个指数相对
强弱与配置优先级；最终发布和风险折减仍由 Risk Critic、Evidence Validator 与 CIO 完成。

<!-- section:input-matrix -->
## 输入矩阵

每个指数与周期必须同时取得宏观政策、市场资讯和 AI 存储行业三份有效意见。记录每份意见
的方向、概率、最强证据、反证、证据 ID 和 Wiki 版本；unavailable Quant 不得进入策略综合。若输入
缺失、引用未冻结或时间晚于 data_cutoff，策略节点必须失败，不得用旧观点补齐。

<!-- section:market-regime -->
## 市场状态

先判断风险偏好、流动性、盈利预期和波动状态是否相互确认，再判断 D1/D2 方向。单一宏观
叙事、单条新闻或单个行业事件不能独立定义市场状态；状态不清、信号冲突或主要变化已经
定价时，仍须在 up/down 中按证据强弱选边，同时提高小波动概率并降低涨跌条件置信度。

<!-- section:style-allocation -->
## 风格与配置

在大盘/中小盘、价值/成长、盈利质量/主题弹性之间比较证据强度。配置结论必须能回到指数
成份、权重和行业暴露，明确属于盈利、估值、流动性还是风险偏好传导；禁止仅凭“大盘稳”
或“科技强”等标签给出指数方向。

<!-- section:index-relative-strength -->
## 五指数相对强弱

分别检查沪深300、中证500、中证1000、创业板指和科创50的权重暴露、敏感因子与事件映射。
相对排序只表达同一 data_cutoff 下的证据强弱，不等于每个指数都有绝对方向；相关指数共享
来源时必须标记共同证据，不能通过重复引用制造共识。

跨指数比较上下文必须携带上游 `evidence_item_ids` 与已绑定来源指纹，用于识别同一事件或
同一来源在多个指数上的重复出现；这些 peer 证据只用于去重和相对比较，不能扩大目标指数
策略观点允许引用的证据集合。

系统以策略概率的 `up - down` 作为可重放配置分数，在每个 horizon 内统一派生五指数名次；
分数在容差内相同则明确标记并列，不用固定代码顺序伪造证据强弱，也不接受五次独立模型
调用各自声明名次。全市场平均配置分数用于标记 risk_on / balanced / risk_off，沪深300、
中证500/1000、创业板指/科创50 三组得分差用于标记大盘、中小盘、成长或均衡风格。阈值
属于 workflow 版本的一部分。

<!-- section:synthesis -->
## 概率综合

基础概率是判断输入，不是机械投票。策略研究员应说明一致意见、关键分歧、共同来源和主导
传导链，再输出 up/neutral/down 三个结果概率；direction 只允许 up/down，按两者较大的一侧
确定且不得精确并列。neutral 仅表示评价噪声带概率。演示模式可等权平均验证链路；正式模式
不得因为多个 Agent 引用同一事实而提高置信度。策略概率是 CIO 的唯一方向输入，基础观点
不再与其二次平均。

<!-- section:counter-evidence -->
## 反证与失效

- 三位研究员实际引用同一来源，表面共识缺乏独立性。
- 宏观或产业逻辑成立，但影响周期显著长于 D1/D2。
- 指数暴露不足，个股或主题强弱无法代表宽基指数。
- 事件已被价格和估值充分反映，新增事实没有预期差。
- 市场进入高波动或流动性冲击状态，历史传导关系暂时失效。

<!-- section:run-checklist -->
## 每次运行检查

1. 三份有效研究输入是否齐全并早于 data_cutoff。
2. 证据是否独立，是否存在共同来源或同一事件的重复转载。
3. 影响周期是否与 D1/D2 匹配，是否已经定价。
4. 结论是否映射到目标指数的真实权重和风格暴露。
5. 是否给出最强反证、失效条件及降低涨跌条件置信度的理由。

<!-- section:maintenance -->
## 维护与评分

策略观点按 Agent × 指数 × D1/D2 保存，direction 必须二元选边；到期结果仍按 up/neutral/down
评价噪声带标签计算命中率、Brier Score 和概率校准。少于 20 个预测截面不做能力结论；策略
研究员自身胜率只衡量方向判断，不能替代组合收益、超额收益或对 CIO 的边际贡献评估。
框架变更必须提升 agent_version 与 Wiki 版本，旧三分类立场不得混算。

<!-- section:sources -->
## 来源

- [中证指数有限公司](https://www.csindex.com.cn/)
- [国证指数网](https://www.cnindex.com.cn/)
- [中国人民银行](https://www.pbc.gov.cn/)
- [中国证监会](https://www.csrc.gov.cn/)
