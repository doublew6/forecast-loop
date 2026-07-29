---
id: VC-WIKI-PREDICTION-LABELS
title: D1与D2二元方向及结果标签定义
version: 2.0.2
updated_at: 2026-07-27
published_at: 2026-07-27T16:00:00+08:00
status: active
owners:
  - cio_agent
  - quant_agent
tags:
  - forecast
  - labels
  - evaluation
  - calibration
source_urls:
  - https://www.sse.com.cn/
  - https://www.szse.cn/
---

<!-- section:forecast-clock -->
## 预测时钟

预测基准日 t 是数据完整且已收盘的 A 股交易日。as_of 与 data_cutoff 使用带时区的
Asia/Shanghai 时间戳。自然日、周末和法定节假日不得作为 D1 或 D2。

- D1：t 后第一个有效交易日收盘。
- D2：t 后第二个有效交易日收盘。

同一 run 为五个指数分别生成 D1 和 D2，共十个最终预测。D2 是一期主考核周期，
D1 是辅助观察周期。

<!-- section:return-definition -->
## 实际收益定义

设预测基准日收盘价为 C0，第一个交易日收盘价为 C1，第二个交易日收盘价为 C2：

- D1 实际收益 = C1 / C0 - 1
- D2 实际收益 = C2 / C0 - 1

D2 是截至第二个交易日收盘的累计收益，不是第二天单日收益，也不是 D1 和下一日
方向的多数票。

所有价格必须来自同一口径的指数收盘序列。价格源、抓取时间和内容哈希随评价保存。

<!-- section:neutral-band -->
## 动态评价噪声带

使用预测基准日前截至当日的最近 20 个日收益率计算样本标准差 σ20。收益与阈值均
使用小数表示：

- D1 阈值 h1 = 0.25 × σ20
- D2 阈值 h2 = 0.25 × σ20 × √2

若实际收益大于正阈值，实际结果标签为 up；小于负阈值，实际结果标签为 down；落在
包含边界的区间内，实际结果标签为 neutral。这里的 neutral 只描述到期收益是否越过
噪声带，不是 Agent 可以选择的预测立场。

必须至少有 21 个连续有效收盘价以得到 20 个日收益。样本不足、包含非有限值或跨
口径拼接时，正式 run 失败，不使用固定阈值代替。

<!-- section:probability-output -->
## 概率输出

每个产生预测的 Agent Opinion 与最终 Forecast 仍输出 up、neutral、down 三个结果概率，
但 direction 必须明确选择上涨或下跌：

- 每项位于 0 至 1；
- 三项之和在 1e-6 容差内等于 1；
- direction 只比较 p_up 与 p_down，取较大的一侧；
- direction 只允许 up 或 down，不允许 neutral；
- p_up 与 p_down 精确相等属于无效输出，必须由研究 Agent 重新判断，禁止使用固定默认方向；
- p_neutral 表示实际收益落入评价噪声带的概率，可以高于两个方向概率；
- 新 Forecast 不使用 abstain 代替方向，低确信度通过概率、反证和失效条件表达。

Quant Agent 在只读因子与行情适配器完成并通过样本外验证前，status 为
unavailable，不产生 Opinion，不进入成绩单，也不影响 CIO 聚合。禁止用伪随机数或固定
方向把“尚未接入”伪装成量化判断。

<!-- section:scoring -->
## 评分

预测到期后保存：

- 二元预测方向是否命中实际三结果标签；实际标签为 neutral 时，涨跌预测均不算命中；
- 实际累计收益与实际标签；
- 多分类 Brier Score；
- 价格来源与评分时间。

概率质量仍采用三结果均方概率误差的平均值，因为系统需要记录“方向明确但实际可能只
小幅波动”的不确定性：

    Brier = ((p_up-y_up)^2 + (p_neutral-y_neutral)^2 + (p_down-y_down)^2) / 3

成绩单按 Agent、指数、周期、Agent 版本、工作流版本和模型身份过滤。2.0.0 之前允许
neutral direction 的历史预测只读保留，不得与当前二元方向版本合并比较。正式样本少于
20 时显示“样本不足”，不对能力作结论。Demo 和 unavailable Quant 不计入正式能力统计。

<!-- section:immutability -->
## 冻结与重跑

- 发布后的预测不可修改。
- 重跑生成新的 run_id、forecast_id、input_hash 和时间戳。
- 评价结果绑定原 forecast_id。
- Wiki 更新不改变历史预测的 Wiki 版本和内容哈希。
- 后验修订不允许改写“当时可见信息”，只能追加审计说明。

<!-- section:sources -->
## 来源

- [上海证券交易所](https://www.sse.com.cn/)
- [深圳证券交易所](https://www.szse.cn/)

交易日历与指数行情应从交易所或经验证的数据适配器取得。D1/D2、动态阈值和评分
公式是 forecast-loop 一期的产品定义，不是交易所规则。
