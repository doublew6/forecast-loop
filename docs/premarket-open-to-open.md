# 盘前动态证据与 open-to-open 预测协议

该协议为 A 股开盘前研究新增一条独立、追加式链路。它不会修改
`research-program/v2` 的 close-to-close 目标，也不会重写任何历史预测。

## 时间与目标

每天的一个预测 episode 固定为：

```text
上一交易日 15:00 ───── 09:10 ── 09:15 ── 09:24 ── 09:30 ───── 下一交易日 09:30
     动态证据开始          截止      判断      最晚封签    起点 open          终点 open
```

- 证据窗口：上一交易日收盘后至预测交易日 09:10（Asia/Shanghai）。
- 决策时间：09:15；最晚 09:24 完成确定性 finalizer。
- 目标：`csi1000-open-to-open-d1`，即预测交易日官方开盘价到下一交易日
  官方开盘价的收益。
- 相邻 episode 分别是 `D open → D+1 open` 与
  `D+1 open → D+2 open`。二者只共享一个价格端点，不重复收益区间。
- 该目标能在当天开盘前形成信号；实际执行仍可能受集合竞价、跳空、滑点和
  跟踪工具差异影响。forecast-loop 不下单，也不把指数开盘价承诺为可成交价。

新协议在积累足量独立样本并通过人工复核前固定为 `shadow`。

## 两层输入

每次 Agent 判断必须同时绑定：

1. 截止时间前已经发布的版本化 Wiki 段落，用于稳定研究框架；
2. 独立的当日动态证据快照，用于上一收盘后新增的事实和价格反应。

动态快照至少包含以下类别：

- `news`：低延迟新闻通讯或原始公告。二手新闻按 Tier 3 使用，存在原文时必须
  回到 Tier 1；
- `global_equity`：已完成的海外主要股指、波动率或行业指数变动；
- `fx_rates`：汇率和利率环境；
- 可选的商品、产业与境内盘后信息。

每条材料包含 `published_at`、`observed_at`、`ingested_at`、URL、SHA-256、
来源等级、实体、分配 Agent 和 `independence_key`。同一新闻 ID 的更新稿可以分别
冻结，但共享 `independence_key`，聚合时仍只算一项来源，不能制造虚假共识。

价格变化只证明市场反应，不能倒推出唯一新闻原因。Live adapter 必须只读，并在
任一必需类别缺失、行情不新鲜、Wiki 晚于 cutoff 或时间戳不可核验时失败关闭。

## Agent 路由

文件任务按依赖顺序包含：

1. Macro Policy：政策、流动性、汇率和利率；
2. Market News：新增资讯、预期差和同源修订；
3. Global Market：美股宽基、波动率、海外科技和跨资产风险偏好；
4. Industry：海外科技/产业变化到中证1000暴露的传导；
5. Strategy：综合四份独立草稿，不把转载或共享价格重复计权；
6. Risk Critic：列出反证和失效条件，不投方向票。

CIO 不是第七次模型调用。确定性 Python 根据 Risk Critic 的风险强度，把 Strategy
概率向均匀 baseline 收缩，再封签最终概率、方向、证据 ID、Wiki 版本和哈希。

## 文件交接

私有 adapter 先生成符合
`forecast-loop.premarket-evidence-snapshot/v1` 的 source-neutral JSON。公共核心随后运行：

```bash
make premarket-prepare ARGS="--snapshot /absolute/premarket-snapshot.json"
# Codex 读取 input.json，只写 job_dir/drafts.json
make premarket-finalize ARGS="/absolute/job/path"
make premarket-evaluate ARGS="/absolute/job/path --outcome /absolute/outcome.json"
make premarket-brief ARGS="/absolute/job/path"
make premarket-notify ARGS="/absolute/job/path --env-file /absolute/owner.env"
```

`prepare` 写入只读的 `input.json`、`drafts.template.json` 和 `INSTRUCTIONS.md`；
模型只能创建 `drafts.json`。`finalize` 验证时间、哈希、assignment、Agent 证据权限、
Wiki identity 和 09:24 deadline，在排他锁内通过目录 fd 原子写入并同步不可覆盖的
`forecast.json` 与 `receipt.json`。若进程在两次写入之间中断，09:24 前的重试只有在
冻结输入和草稿能够精确复现已有 forecast 时才补写 receipt；receipt-only、冲突或篡改
状态一律拒绝。
同一快照的 `prepare` 重试复用首次封签的 `prepared_at` 与 request hash，不覆盖已有任务
或草稿。下游读取、日报、通知、评价和成绩历史只接受带有匹配 receipt 的完整 forecast；
缺少 receipt 的半成品只能在 deadline 前由 `finalize` 恢复，不能直接进入下一阶段。
飞书发送按预测交易日和接收者幂等；不同内容不能静默覆盖已发送结果。
日报反馈只选择 `target_session < 当前 forecast_session` 的最近完整评价，因此盘前发送
时不会把当天尚未出现的开盘价当成真实结果。没有合格历史时只发送当期预测。

## 到期评价

评价只使用预测交易日与下一交易日的官方或经验证指数开盘价：

```text
realized_return = next_session_open / forecast_session_open - 1
neutral_band = 0.25 × stdev(last_20_completed_open_to_open_returns)
```

20 个历史收益只使用预测日前已经完成的 open-to-open episode。结果按
`up / neutral / down` 计算多分类 Brier 和方向诊断；结果揭晓后不得改写原快照、
Agent 草稿或 forecast。

确定性 evaluator 要求 outcome 来源和 outcome 观察时间均不早于目标交易日 09:30，且
outcome 观察时间不晚于宿主接受评价的时间。相同 forecast/outcome 的重复或并发评价
返回首次封签结果；若进程只写入 outcome 后中断，下一次调用从该 outcome 恢复，不改变
首次成功评价的 `evaluated_at` 或哈希。

成绩页从完整封签的 `forecast.json`、`outcome.json` 和 `evaluation.json` 重新验证
评价链，再按日期生成累计胜率、滚动 20 次胜率和两条复合毛收益曲线：

- 纯多头：预测上涨时持有中证1000，预测下跌或小波动时空仓；
- 多空：预测上涨时做多、预测下跌时做空，小波动时空仓。

收益曲线不包含手续费、滑点、融券和做空成本，只是对预测信号的历史诊断，不代表
可交易净收益。实际结果落在噪声带内时保留记录，但不进入方向胜率分母。
