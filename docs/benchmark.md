# 跨来源 benchmark

`benchmarks/cross-source-v1` 是一个完全合成、可再分发的回归数据集，用于证明
forecast-loop 能在同一口径下比较 manual、AI、quant 与 deterministic Agent，并比较
固定委员会、等权 baseline 和候选 believability 委员会。它用于工程回归，不是投资表现
证据，也不能据此启用生产权重。

## 冻结与复验

fixture 使用 CC0-1.0。`manifest.json` 冻结 `benchmark.json`、`agent-specs.json` 和
`LICENSE.txt` 的字节数与 SHA-256；fixture、manifest、AgentSpec archive、每个正式
`AgentSpec`、候选权重 policy body 和 golden 文件另有 canonical JSON hash。loader 会
核对 participant 的 id、version、source type、probability capability 与 evaluation
policy 投影，并拒绝绝对路径、`..`、symlink、未知文件、重复 JSON key、NaN 和
Infinity。候选 believability 权重在评价窗口前已冻结：

- `weights_trained_through < weights_effective_at <` 首个 target date；
- `weights_source_hash` 必须可由内嵌 policy body 重算；
- `fitted_on_fixture_outcomes` 永远为 `false`。

运行报告或复验 golden：

```bash
forecast-loop benchmark run benchmarks/cross-source-v1
forecast-loop benchmark verify benchmarks/cross-source-v1
make benchmark-verify
```

CI 使用独立的 `benchmark verify` 步骤。任何 fixture、聚合口径或报告字段变化都会改变
golden report hash，必须作为显式版本变更审阅。

## 公平比较口径

`independent_period_count` 只统计唯一 `target_date`，不会因为同一天增加指数或 horizon
而虚增独立样本。报告另列：

- target opportunity 数；
- Agent×date×index×horizon opportunity / observation 数；
- committee opportunity / observation 数；
- failed 与 missing 数。

所有比率先在单个 target date 内求均值，再跨 target date 做 macro-average。因此，同一
天更多指数不会获得更大权重。coverage 与 failure（failed + missing）相加为 1。

方向命中按 `actual_return > 0` 或 `< 0` 评价，零收益为 N/A。重大行情仅包含
`abs(actual_return) > neutral_threshold`，等于阈值不算重大；三分类 outcome 则使用同一
正负阈值生成 `up / neutral / down`。

三分类 Brier 固定为三个类别平方误差的均值，范围为 0 到 2/3。classwise calibration
分别计算 up、neutral、down 的固定 bins。每个 observation 的权重是
`1 / (独立日期数 × 当日 observation 数)`；先形成跨日期的 bin probability 与 outcome
均值，再计算 `Σ bin_mass × abs(p̄ - ȳ)`，因此报告 ECE 可直接由展示 bins 重算。这两类
指标只对完整、归一化的 multiclass probability vector 有定义。manual fixture 是
confidence-only，其 Brier 与 calibration 必须为 `null`；即使 payload 尝试夹带
probabilities 也会被拒绝。

三个委员会使用完全相同的 multiclass roster 和 opportunity，只允许固定权重不同。任一
required member 失败或缺失时，该 committee opportunity 直接失败，不能对剩余成员做
availability renormalization。manual Agent 独立参赛，不进入委员会 roster。
