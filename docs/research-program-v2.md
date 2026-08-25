# 单主标的、多周期研究协议 v2

本文是 focused research v2 的运行、研究和审计协议。v2 是追加式系统：它使用独立的
契约、表、文件交接和评价记录，不重新解释或回填五指数 v1 历史。

v2 的核心边界是：**只把中证1000 D1 作为待激活的正式目标，同时保留一个 W1 相对
Shadow 目标和一个 D20 宏观研究状态。** 出现在数据输入中的工具，不会因此自动变成
预测目标。

## 1. 版本化契约

| 契约 | 用途 |
| --- | --- |
| `forecast-loop.research-program/v2` | 冻结工具、目标、周期和 lane |
| `forecast-loop.evidence-snapshot/v2` | 冻结交易日历、行情历史和截止前证据 |
| `forecast-loop.agent-signal/v2` | 封签 Agent 身份、周期、理由、概率或边际影响 |
| `forecast-loop.codex-handoff/v3` | 约束预测文件交接和 assignment 身份 |
| `forecast-loop.reflection/v2` | 按到期目标创建事后复盘 |
| `forecast-loop.agent-eval-suite/v2` | 冻结私有全链路回放和 baseline/candidate 身份 |
| `forecast-loop.agent-eval-report/v2` | 按目标输出发布门禁和诊断 |

公共 JSON Schema 可从 CLI 导出：

```bash
uv run forecast-loop contract schema research-program-v2
uv run forecast-loop contract schema evidence-snapshot-v2
uv run forecast-loop contract schema agent-signal-v2
uv run forecast-loop contract schema codex-handoff-v3
uv run forecast-loop contract schema reflection-v2
uv run forecast-loop contract schema agent-eval-suite-v2
uv run forecast-loop contract schema agent-eval-input-v2
uv run forecast-loop contract schema agent-eval-drafts-v2
uv run forecast-loop contract schema agent-eval-report-v2
```

`content_hash`、`program_hash`、`input_hash` 和文件 receipt 是契约的一部分。Live 输入应由
只读 adapter 生成，不要手工修补 hash 以绕过校验。

## 2. 工具、目标与 lane

Research Program 固定两个市场工具：

| 代码 | 角色 | 是否产生目标 |
| --- | --- | --- |
| `000852.SH` | 中证1000，primary | 是 |
| `000300.SH` | 沪深300，benchmark | 否，只用于 W1 相对结果和背景 |

决策目标与研究状态互相独立：

| ID | 定义 | 周期 | 配置 lane | 激活前实际 lane |
| --- | --- | --- | --- | --- |
| `csi1000-absolute-d1` | 中证1000下一交易日绝对收益 | D1 | Formal | Shadow |
| `csi1000-vs-csi300-relative-w1` | 中证1000未来5个交易日相对沪深300的超额收益 | W1 | Shadow | Shadow |
| `csi1000-absolute-d20` | 中证1000未来20个交易日宏观状态 | D20 | 研究状态，不是决策目标 | Shadow |

D1 的 `configured_lane=formal` 只表示它是唯一可申请正式发布的目标。数据库中没有通过门禁
的 append-only activation event 时，`effective_lane` 仍为 `shadow`。W1 和 D20 没有随
D1 自动激活的路径。

旧五指数中的沪深300、中证500、创业板指和科创50不再生成 v2 预测矩阵。确有需要时，
它们只能作为 cutoff 前冻结的 evidence item、市场宽度或背景衍生量进入输入，不能作为
`instruments` 或隐式 prediction target。

Manual 和 Quant 仍属于中证1000 D1 的 Shadow 对照来源；不得据此合成 W1 Quant 信号。
focused v2 文件任务也不会替缺失的 Manual/Quant 输出伪造信号。

## 3. 周期、cadence 与 Agent 输出

| 角色 | 自然观点 | 日常输出 | 到期与复用规则 |
| --- | --- | --- | --- |
| 市场资讯 | 每个交易日 D1 | D1 `natural_view` | 每日重新提交 |
| 行业 | 每周首个交易日 W1 相对观点 | 每日 D1 `d1_impact` | 未到期 W1 不重叠；首次允许 bootstrap |
| 宏观 | 每月首个交易日 D20 绝对观点 | 每日 D1 `d1_impact` | 未到期 D20 不重叠；首次允许 bootstrap |
| Strategy | 对当日 due 的 D1/W1 目标给完整概率 | `strategy_forecast` | D1 每日；W1 非重叠 |
| Risk Critic | 检查 Strategy 的风险与反证 | `risk_critique` | 不投方向票 |
| CIO | 确定性折扣 Strategy 概率 | `decision_forecast` | 不接受模型自报权重 |

每个 signal 还记录 `generation_reason`：市场资讯和每日边际影响为 `daily`，首次启用时的
行业/宏观自然观点为 `bootstrap`，此后按周/月到期重建为 `scheduled`，外部 Manual/Quant
对照为 `external_shadow`。因此首次状态和正常 cadence 不会在历史评价中被混为一谈。

行业或宏观自然观点仍有效时，日任务通过 signal/artifact identity 引用原观点，不把它伪装
成当天重新调用。自然观点已到期且当天又不应创建新观点时，D1 边际影响必须声明
`state_available=false`、`abstain=true`、`impact=none`、`importance=none`；禁止无限续期。
即使状态有效，行业 Agent 也可以明确弃权。

所有 signal identity 至少绑定：

```text
agent_id / agent_version / model_name / prompt_version
+ target_id / signal_kind / natural_horizon / decision_horizon
+ anchor_date / target_date / evidence_cutoff
+ program_hash / input_hash
```

五类 signal 的评分边界不同：

| `signal_kind` | 输出 | 结果评分 |
| --- | --- | --- |
| `natural_view` | 三分类概率、理由、反证、失效条件 | 自然周期到期后计算 Brier 等指标 |
| `d1_impact` | `positive/none/negative`、重要性、传导链或弃权 | 不对最终涨跌单独计算 Brier |
| `strategy_forecast` | 对决策目标的完整三分类概率 | 按对应目标评价 |
| `risk_critique` | 风险强度、反证和失效条件 | 风险/失效条件覆盖、系统错误漏报和盲审；不计算方向胜率 |
| `decision_forecast` | 确定性 CIO 概率 | 作为最终系统预测评价 |

CIO 使用冻结规则把 Strategy 概率按 Risk Critic 的风险强度向 cutoff 时的历史 baseline
收缩。它不把 Critic 当成另一张方向票，也不根据历史成绩自动调权。

## 4. Evidence Snapshot 与交易日

`EvidenceSnapshotV2` 必须满足以下条件，否则 prepare 失败关闭：

- `program_hash` 精确匹配当前 `ResearchProgramV2`；
- 同时包含 `000852.SH` 和 `000300.SH`，且不能多出隐式工具；
- 两个指数至少有20条按交易日严格递增、互相对齐的收益记录，并以 `base_session` 结束；
- 至少冻结未来20个可信交易所 session；日历 `source_hash` 必须提交 exact
  `base_session + sessions` payload，D1/W1/D20 目标日分别取第1、5、20个 session；
- 日历、行情和 evidence item 均带 source identity、SHA-256 和带时区时间戳；
- `published_at`、`observed_at`、`ingested_at` 均不得越过 `data_cutoff`；
- `data_cutoff <= as_of`，且 snapshot 创建时间不得早于 cutoff。

未来日期必须来自可信交易所日历，不能按普通工作日或“周一至周五”推算。Live Wiki 同样
按 `data_cutoff` 冻结；Live 模式不会回退到仓库中的 `demo-only` 示例。

## 5. 结果、中性带与 baseline

设中证1000单日20日波动率为 \(\sigma_{1000}\)，中证1000与沪深300的20日单日超额
收益波动率为 \(\sigma_{excess}\)。三类结果的中性带为：

| 目标 | 实现值 | 中性带 |
| --- | --- | --- |
| D1 绝对 | 中证1000一日收益 | `0.25 × sigma_1000` |
| W1 相对 | 中证1000五日收益 − 沪深300五日收益 | `0.25 × sigma_excess × sqrt(5)` |
| D20 宏观 | 中证1000二十日收益 | `0.25 × sigma_1000 × sqrt(20)` |

实现值高于正阈值为 `up`，低于负阈值为 `down`，其余为 `neutral`。Snapshot 声明的
`volatility_20d` 必须与冻结 returns 的末20个交易日样本标准差一致，否则验证失败关闭。
结果 observation 必须在目标 session 收盘后由可信、封签的结果 adapter 提供。Live 结果
还必须绑定 primary/benchmark close 来源 stamp、完整交易日 session 列表和受信 source URL；
`mode` 是结果身份的一部分，因此同一 `program/target/anchor/target_date` 的 Demo 与 Live
可以隔离并存，但同一 mode 不接受互相冲突的第二份结果。

Live baseline 只纳入 cutoff 前揭晓、且已经绑定到 completed Live signal evaluation 的
Live outcome；Demo outcome 永远不会进入 Live baseline。

Baseline 在预测 cutoff 时冻结，只读取当时已经揭晓的同目标历史类别。`up/neutral/down`
各加1个伪计数；没有历史时即 `1/3, 1/3, 1/3`。不同目标、周期和 Agent 版本绝不合并。
D1 以独立目标日统计；W1 与 D20 只允许非重叠锚点。

线上成绩单分开呈现：

1. 结果能力：三分类 Brier、相对 baseline 的改善、classwise ECE 和方向诊断；
2. 推理能力：结构规则、结果揭晓前盲审和需要时的人工复核；
3. 增量价值：私有 replay 中用明确 `no_impact` 替换单个 Agent 的 ablation。

三轴不合成总能力分或跨周期排行榜。Ablation 只作诊断，不自动修改正式聚合；线上
scorecard 在未导入对应离线诊断时可以显示空值。

成绩单中的 `brier_skill` 是相对改善率
`(baseline_mean_brier - agent_mean_brier) / baseline_mean_brier`；单个 Forecast 详情中的
`brier_improvement` 则保留为绝对 Brier 差。baseline Brier 为0时不定义相对 Skill，显示为空，
避免把两个量纲混在一起。

Risk Critic 是角色特例：成绩单显示反证覆盖率、失效条件覆盖率、风险标记率，并把
“CIO 最终预测发生错误且事前 Critic 仅声明 `none/low`”计为漏报诊断。它的 Brier 和
方向准确率始终为空，漏报指标也不会自动改变权重。

## 6. 预测 file handoff

首次运行先升级数据库：

```bash
make migrate
make database-status
```

### 6.1 Prepare

```bash
make research-v2-prepare ARGS="--mode live --snapshot /absolute/snapshot-v2.json"
```

命令返回 `status=awaiting_draft`、绝对 `job_dir` 和 `drafts_file`。目录位于：

```text
data/handoffs/v2/<run-id>/
├── input.json
├── drafts.template.json
├── INSTRUCTIONS.md
└── reasoning/
```

Python 冻结 Program、Snapshot、Wiki、历史自然观点、baseline、assignment 和所有 hash。
同一 `program/mode/anchor_date` 的首次输入一经封签，后续重试即使拿到更新后的同日
Snapshot，也只返回原 job；不会创建第二个预测身份或把晚到信息混入已经开始的前瞻任务。
如果历史数据库已经存在多个同锚点 run，prepare 会失败关闭，要求运维先审计，而不会猜测
应使用哪一个。
预测 Codex 任务固定使用 `gpt-5.6-sol / high`，只读 `input.json` 和模板，并且只能写：

```text
data/handoffs/v2/<run-id>/drafts.json
```

不要让 Codex 修改 input、模板、instruction、数据库、Wiki 或上游数据。handoff root 的
symlink、越界路径、缺失 assignment、额外证据引用、身份漂移或 Wiki hash 漂移都会失败。
外部 dispatcher 可以先对内存中的候选草稿调用与 Finalize 相同的公共校验，再以不覆盖方式
创建 `drafts.json`；Finalize 会从最终落盘字节重复全部校验。
`d1_impact` 的非弃权草稿必须提供非空 `transmission_chain`；只有显式 no-impact abstention
可以留空。生成的 `INSTRUCTIONS.md` 会复述这条 validator 合同，避免候选草稿到发布时才
因缺失传导链而失败。

### 6.2 Finalize

```bash
make research-v2-finalize ARGS="/absolute/job/path/from-prepare"
```

Finalize 校验外部草稿，确定性生成 CIO signal，原子持久化 signal/forecast/receipt，并创建
盲审任务。HTTP 没有 prediction prepare/finalize 写入口；必须保留 CLI/文件边界。

### 6.3 Manual/Quant D1 Shadow 导入

Manual 和 Quant 都只能在一个已完成的 v2 run 上追加标准
`forecast-loop.agent-signal/v2` 的 D1 `natural_view`。它们不会进入 assignment、Strategy、
CIO 或 `ForecastV2`，也不会改变已经封签的正式预测。Scorecard 会按完整
`agent/version/model/prompt/target/kind/horizon` 身份单独分组，并明确标记为 Shadow-only；
到期后由同一个 `research-v2 evaluate` 路径计算结果指标。

Manual 输入是严格的 `forecast-loop.manual-shadow-input/v2` JSON，必须完整绑定
`run_id/mode/program_hash/snapshot_hash/run_input_hash`、D1 anchor/target、data cutoff，提交
完整三分类概率、反证、失效条件和 `blind_attestation=true`。不得把旧版 confidence 推断成
概率。文件可省略 `content_hash` 由本地 CLI 确定性封签；若提供 seal，则不匹配时失败关闭：

```bash
make research-v2-shadow-manual \
  ARGS="/absolute/manual-shadow-input-v2.json --database-url sqlite:////absolute/db.sqlite3"
```

Quant 只读取受信 root 下经过 adapter 校验、且包含唯一精确 CSI1000 D1 target 的现有 bundle：

```bash
make research-v2-shadow-quant \
  ARGS="<run-id> --root /trusted/quant/root --manifest relative/manifest.json"
```

Quant bundle 必须绑定同一 Evidence Snapshot，生成时间不得晚于 run prepare。没有 bundle 时
不要调用导入命令；系统保持 Quant 缺失，不生成空值信号，更不会回退或伪造 W1 Quant。
两种输入都必须晚于 run completion、早于配置的窗口截止和目标日零点；Demo/Live 不能串用。
同一外部 seal 的重试幂等，不同内容不能覆盖已有 append-only signal。外部输入文件或 Quant
bundle 仍是审计源材料；数据库 signal 的 `input_hash` 保持为冻结的 run input hash。

每个实际接纳的 Manual/Quant signal 还会创建独立、结果不可见的 Shadow 推理审核任务：

```text
data/handoffs/v2/<run-id>/shadow-reasoning/<signal-id>/
```

Codex 只能写该任务内的 `drafts.json`；随后由本地确定性边界执行：

```bash
make research-v2-shadow-reasoning-finalize \
  ARGS="/absolute/shadow-reasoning/job/path"
```

审核结果以标准 `ReasoningReviewV2` 追加保存并建立 signal/review Trace artifact 链接，但不进入
正式聚合。任务创建或 telemetry 失败不会回滚已经接纳的 Shadow signal；未完成审核则在成绩单中
保持推理轴缺失，不伪造评分。

### 6.4 结果揭晓前盲审

Finalize 后，独立 Codex 任务只读：

```text
<job-dir>/reasoning/input.json
<job-dir>/reasoning/drafts.template.json
```

其中明确标记 `outcomes_included=false`。任务固定使用 `gpt-5.6-sol / high`，只写：

```text
<job-dir>/reasoning/drafts.json
```

然后执行：

```bash
make research-v2-reasoning-finalize ARGS="<job-dir>"
```

五项 rubric 每项0–2分：证据相关性、因果链、标的与周期映射、反证与失效条件、概率与
不确定性一致性。LLM 分数始终 advisory。以下样本必须进入人工队列：

- `review_input_hash mod 10 == 0` 的可复算约10%抽样；
- 任一结构规则失败；
- 总分低于7；
- 任一维度为0。

人工决定是独立、不可变事件：

```bash
make research-v2-reasoning-review \
  ARGS="<review-id> --decision approved --reviewer <name>"
```

需要备注时追加 `--notes-file /absolute/review-notes.md`，避免把正文放进 shell history。
重大行情的事后解释应进入 Reflection，不能冒充 outcome-blind review。

## 7. 到期评价与 Reflection

可信结果 adapter 生成并封签一份 `forecast-loop.outcome-observation/v2` JSON 后执行：

```bash
make research-v2-evaluate ARGS="/absolute/outcome-observation-v2.json"
```

Live JSON 至少包含 `mode=live`、primary source stamp、calendar source stamp，以及与 stamp
哈希完全一致的 `source_hashes`；W1 相对结果还必须包含沪深300 close 与其 source stamp。

评价只匹配完全一致的 Program、target、anchor 和 target date。`d1_impact` 与
`risk_critique` 不会被伪装成可计算 Brier 的市场预测。

每个已到期 forecast 独立创建 Reflection 草稿，例如：

```json
{
  "schema_version": "forecast-loop.reflection/v2",
  "forecast_id": "<forecast-id>",
  "forecast_hash": "<forecast-sha256>",
  "evaluation_id": "<forecast-evaluation-id>",
  "evaluation_hash": "<evaluation-sha256>",
  "target_id": "csi1000-absolute-d1",
  "anchor_date": "2026-08-12",
  "target_date": "2026-08-13",
  "actual_label": "up",
  "verdict": "right_reason",
  "findings": [],
  "content_hash": "<sealed-draft-sha256>"
}
```

```bash
make research-v2-reflection-create ARGS="/absolute/reflection-v2.json"
make research-v2-reflection-review \
  ARGS="<reflection-id> --decision approved --reviewer <name>"
```

可选 `verdict` 为 `right_reason`、`lucky_correct`、`wrong`、`noise` 或 `unresolved`。
Reflection v2 必须绑定不可变 Forecast 和 Evaluation 的 ID/hash、目标、anchor/target date 与
实际类别；`content_hash` 不匹配或同一 forecast 的第二份内容不同会失败关闭。它只覆盖自己
的到期目标，不要求五指数或固定25项矩阵；审批同样是不可变事件。

## 8. Trace v2 运维

Trace 是 best-effort 遥测账本，不是预测事实源。固定层级为：

```text
运行根节点 -> 预测目标 -> Agent 调用 -> 校验 -> 聚合 -> 持久化
```

同一 workflow subject 的每次真实 retry 使用独立 `attempt_number`。复用自然观点时使用
artifact link 的 `reused` 关系；prepare、外部草稿回执和 finalize 分开记录。没有可信
本地计时的外部阶段标记为 external receipt，不推测耗时。

Trace 只保留 allowlist 元数据、摘要、引用身份、token/cost（可用时）及输入输出 digest；
禁止保存完整 prompt、原始模型响应、敏感工具参数或业务正文。artifact link 可关联
signal、forecast、evaluation、reasoning review、Reflection 和 bad case，但不复制正文。

终态 `completed/failed/degraded` 会封签 trace；之后不能修改 trace/span/link，也没有删除
API或自动清理任务。OTLP 只是私有可选镜像，本地或导出失败不得阻断预测，只令
`telemetry_complete=false`。

常用只读查询：

```text
GET /api/agent-observability/summary?hours=24
GET /api/agent-traces?workflow_kind=prediction&target_id=csi1000-absolute-d1&horizon=D1&status=completed&limit=50
GET /api/agent-traces/<trace-id>
```

列表以不透明 cursor 翻页，还支持 `agent_id`、`started_from` 和 `started_to`。详情返回
artifact/evaluation 链接。容量告警由
`FORECAST_LOOP_AGENT_TRACE_STORAGE_WARNING_BYTES` 控制，默认1 GiB；告警只提示运维人员，
不会删除历史或阻断运行。完整策略见
[Agent 评测、Benchmark、Bad Case 与 Trace](agent-evaluation-observability.md)。

## 9. 发布前 Agent Eval v2

公开 v1 fixture 继续作为结构回归。发布候选还必须使用 outcome-blind 的私有全链路 v2
replay；带真实 outcome 的 suite 放在仅 host finalize 可读、且不得挂载给外部草稿任务的
`FORECAST_LOOP_AGENT_EVAL_OUTCOME_ROOT`（默认 `data/eval-outcomes`），真实 outcome 不会进入
handoff 的 `input.json`。`FORECAST_LOOP_AGENT_EVAL_PRIVATE_ROOT` 只承载交接任务与脱敏报告。

```bash
make agent-eval-prepare ARGS="--suite <suite-id> --source private \
  --baseline <baseline-arm> --candidate <candidate-arm>"

# baseline/candidate 两个独立任务只写各自文件：
# <job-dir>/<baseline-arm>/drafts.json
# <job-dir>/<candidate-arm>/drafts.json
# 完成后再由两个不同 producer 的结果盲化任务写：
# <job-dir>/reviewer/drafts.json
# <job-dir>/ablation/drafts.json

make agent-eval-status ARGS="<job-dir>"
make agent-eval-finalize ARGS="<job-dir>"
```

`status` 依次为 `awaiting_draft`、`ready_to_finalize`、`completed`。只有 baseline、
candidate、reviewer 和 ablation 四份草稿全部存在，且 reviewer 逐条绑定完整 arm output
hash、ablation 逐条绑定 candidate full output hash 与冻结 `no_impact` override，才进入
`ready_to_finalize`。Finalize 随后重新加载受信 suite 揭示 outcome，生成不可变
`report.json` 和 `receipt.json`；receipt 同时封签四份草稿。此 finalize 没有 HTTP 入口。
重复 finalize、状态读取和 activation 都会从 host outcome root 重新计算完整报告并逐字节
比对，不能仅凭一组自洽的 report/receipt hash 放行。

D1 activation 追加成功后，系统会在所有 v1 新建边界停止生成五指数 v1 run，包括 demo
同步 workflow、`POST /api/runs`、持久队列 enqueue 和 Codex file prepare。队列会在
prepare 与 enqueue 两处重复检查，避免两者之间发生 activation 的竞态。该守卫只拒绝
新的 `workflow_runs` 行；已经存在或排队的 v1 run 仍可完成恢复，既有 run、forecast、
Reflection、bundle、查询和审计记录永久保留且不会回填或改写。若后续以 append-only
`retired` event 明确撤回 v2 D1 activation，最新事件才会重新开放 v1 新建。

每个 target 独立应用默认门禁：

| 门禁 | 默认要求 |
| --- | ---: |
| schema、cutoff、citation、trajectory、must-pass bad case | 100% |
| 独立 paired episode | 至少20；不足为 `insufficient_sample` |
| candidate mean Brier − baseline mean Brier | 不超过0.01 |
| baseline 方向准确率 − candidate 方向准确率 | 不超过2个百分点 |
| candidate/baseline P95 latency | 不超过1.20倍 |
| candidate/baseline mean token | 不超过1.15倍 |

只有 `release_gate=true` 的 target 影响整体 release decision。W1 等 Shadow target 仍输出
完整诊断，但不单独阻断 D1 发布。Reasoning summary 只能 advisory；人工确认的严重问题
必须转成 must-pass bad case。失败 Eval、线上异常、Trace 诊断和人工审核发现的问题都应
进入同一个 append-only bad-case 状态机：

```text
detected -> triaged -> confirmed -> materialized -> resolved
                    \-> rejected
```

Ablation 把某个 Agent 替换为明确 `no_impact`，比较最终 Brier；它只解释增量价值，不自动
调权或激活 Agent。

## 10. D1 激活与发布顺序

正式激活必须按以下顺序执行：

1. 先部署 v2 表、契约、Trace 和 Eval；v1 继续正式运行；
2. 公开结构门禁通过后，v2 与 v1 并行 Shadow；
3. 累积至少20个独立、已到期评价的 **Live D1 前瞻目标日**；
4. 人工批准最早10份 D1 v2 Reflection；
5. 私有全链路 Agent Eval 的 D1 release target 得到 `pass`，且 report/receipt hash 相符；
6. 由明确 operator 追加 activation event；
7. 之后才停止生成新的五指数 v1 run，历史查询和审计永久保留。

激活命令只接受私有 eval handoff 目录中已 finalize 的 `report.json`：

```bash
make research-v2-activate \
  ARGS="--agent-eval-report /absolute/data/evals/handoffs/<job-id>/report.json \
  --actor <operator-name>"
```

激活前还必须把当前 candidate arm manifest 的 exact SHA-256 配置为
`FORECAST_LOOP_AGENT_EVAL_RELEASE_CANDIDATE_HASH`；验证器要求 private、非 synthetic suite，
要求 candidate 的 Research Program hash 等于当前 v2 Program，并拒绝比默认门禁更宽松的
policy。D1 episode 按唯一 target date 计数，W1/D20 episode 的 anchor-target 窗口不得重叠。

命令必须重新验证 receipt、D1 target gate、至少20个独立 episode、20个 Live 前瞻目标日和
10份最早 Reflection 审批。激活只改变未来 D1 forecast 的 `effective_lane`；不重写历史，
也不激活 W1、D20、Manual/Quant 权重或任何历史可信度权重。

## 11. 只读 API 与成绩单

focused v2 提供以下只读接口：

| 接口 | 内容 |
| --- | --- |
| `GET /api/v2/research-program` | 当前 Program 与 content hash |
| `GET /api/v2/forecasts/latest` | D1 主卡片与独立 W1 Shadow 卡片 |
| `GET /api/v2/agent-scorecards` | 最终系统、自然周期、D1影响、推理、增量价值五栏 |
| `GET /api/v2/reasoning-reviews` | cursor 分页的盲审及人工队列状态 |
| `GET /api/agent-evals/jobs-v2` | 私有文件任务的脱敏状态、按目标门禁与报告摘要 |

`GET /api/agent-evals/suites` 会同时列出保留的公开 v1 fixture 和 v2 replay suite；v2
条目分别返回真实预测 `target_ids` 与可比较的 `arm_ids`。Web 端对
`runner_kind=codex_file_replay` 只展示状态，不提供启动或 finalize 按钮，始终保留四份
独立草稿的 CLI/文件边界。

界面和 API 不提供跨目标、跨周期或跨 Agent version 的“最佳角色”总排名。沪深300只作为
比较基准展示，不能被误读成第二个正式预测。

## 12. v1 兼容与禁止事项

- 旧 `AgentOpinion`、`Forecast`、五指数 Reflection 和 v1 Trace 保持只读；不做推测性回填；
- v1 Trace 迁移后作为 `attempt_number=1` 保持可读；
- v1 fixture、历史 run hash、bundle 和审计接口继续验证原始语义；
- v2 的 signal、forecast、outcome、evaluation、reasoning review、Reflection 和 activation
  使用独立追加式记录；
- v2 激活只停止未来 v1 run 的生成，不删除、不重算、不改写旧数据；
- 不得因某个指数出现在 evidence、Universe 或前端比较图中就自动生成 Agent 矩阵；
- 不得让 Trace、LLM rubric、ablation、样本数或环境变量单独绕过人工 activation；
- 不得下单、写入任何外部生产数据库或改变只读数据同步方向。

## 13. 每日运行检查清单

1. `make database-status` 显示 schema 已到当前 Alembic head；
2. Snapshot 的 Program hash、双指数历史、未来20个 session 和 cutoff 校验通过；
3. Prepare 返回唯一 `run-id`，Codex 只写被授权的 `drafts.json`；
4. Finalize receipt 与数据库 signal/forecast 数量、hash 相符；
5. D1 与 W1 卡片的 target date 来自 snapshot session，且 W1 没有重叠；
6. 盲审 input 明确不含 outcome，所有 required review 已进入人工队列；
7. 到期后再导入可信 outcome、评价并分别创建 Reflection；
8. 检查 Trace 的 `telemetry_complete`、失败/degraded attempt 和容量告警；
9. Agent Eval 未 `pass` 或激活样本不足时，D1 必须继续显示 Shadow；
10. 任何异常只追加新事件、attempt 或 bad case，不修改已封签历史。
