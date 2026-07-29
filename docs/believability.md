# 可信度加权决策设计

## 状态

| 能力 | 当前状态 |
| --- | --- |
| 按领域冻结历史表现证据 | 已实现，`believability-shadow/v1` |
| 冻结人工批准的 `right_reason / lucky_correct` 后验代理 | 已实现 |
| 快照 canonical hash 进入 run `input_hash` | 已实现 |
| 快照进入 `WorkflowRun.data_quality` 与可移植结果包 | 已实现 |
| 根据历史证据自动修改研究权重 | 未启用 |
| 独立的事前推理质量 rubric | 未实现 |
| 候选政策重放、人工 activation 与 Responsible Party override 事件 | 未实现 |

当前实现的目标是先回答“本次决策冻结了哪些可信度证据”，而不是在样本不足、审核机制
不完整时宣称已经知道“谁应获得多少权重”。

## 理论映射

Ray Dalio 的可信度加权决策可以概括为两个同时成立的条件：

1. **在相关领域反复做出过好判断。** 一次命中、跨领域声誉和语言自信都不构成可信
   track record。
2. **能够说明判断为什么成立。** 解释需要有证据、因果传导、反证、失效条件和可检验的
   预测，不能只是事后把结果包装成故事。

因此 forecast-loop 不把投委会实现成所有 Agent 的平票表决。可信度属于
“某个身份在某个领域和周期的可审计证据”，而不是 Agent 的永久人格分数：

```text
agent_id
+ agent_version
+ model_name
+ role_domain
+ index_code
+ horizon
+ believability_policy_version
```

新 Agent 版本或新模型身份的 exact profile 都重新进入 shadow；未来任何新 policy 也必须
由其独立 lifecycle 重新完成 forward shadow，不能继承旧 activation。`codex_file` 的
`model_name=codex-file-handoff-v1` 只是可验证的交接身份，不证明外部任务实际使用了哪个
底模；底模或系统提示发生实质变化时，受控部署必须 bump `agent_version`，不能相信草稿
自报的模型标签。

## 当前 `believability-shadow/v1`

### 冻结时点

`CommitteeWorkflow.prepare_run()` 在 Evidence Snapshot 已经确定
`data_cutoff` 后，通过确定性 Python 读取历史数据库。评价、复盘的完成时间字段以及人工
审核声明的 `reviewed_at` 只要晚于本次 `data_cutoff`，就不能进入当前快照。当前
`reviewed_at` 是受控服务写入的声明记录时间，不是外部可信时间戳；审计 seal 能发现后续
篡改，但不能证明操作者未在写入时回填时间。

完整快照保存在数据库侧：

```text
WorkflowRun.data_quality.believability_snapshot
```

模型草案所在的 frozen state 只得到 policy version、snapshot hash 和 run binding hash，
不得到历史排名或候选权重。这样既能用 `input_hash` 封存证据身份，又不会让 shadow 统计
通过提示词暗中改变本次 Strategy 判断。run bundle v2 还会把 snapshot 的 mode、as_of、
data_cutoff 和 run binding 与 `run.json` 交叉校验，防止删除 seal 或跨 run 移植。

### 历史表现轴

只读取同时满足以下条件的 `OpinionEvaluation`：

- source run 为 `mode=live`、`status=completed`；
- `EvaluationBatch.status=completed`；
- `included_in_direction_score=true`；
- 评价、batch 完成时间和原 run 时间均不晚于新 run 的 `data_cutoff`；
- Agent ID、Agent 版本、模型身份、指数和周期与当前 scope 完全一致；
- 预测方向是 up 或 down。

同一个 Opinion 如果出现多个截至当时可见的评价批次，快照按 evaluated_at、
batch completed_at、evaluation set hash、batch ID 和 OpinionEvaluation ID 的完整顺序
确定性选择最后一个，不把纠错批次重复当成新样本。当前快照保存：

- 三分类平均 Brier；
- 符号方向正确率及分母；
- 超出噪声带后的重大行情方向正确率及分母；
- 原始观察数和不同 `target_date` 数；
- 绑定评价批次、observation hash 与评价集合的 evidence hash。

五指数同一目标日可以增加观察信息，但不会让独立日期数增加五倍。Demo、failed run、
Risk Critic 和尚未接入的 Quant 不进入方向可信度。

### 因果解释轴的当前代理

当前数据库还没有独立的事前推理审核表。因此 v1 不生成“推理质量总分”，只冻结一个
明确标注的：

```text
human_reviewed_right_reason_proxy
```

它只读取：

- completed Live Reflection；
- 不晚于 cutoff 的 immutable `ReflectionHumanReview(decision=approved)`；
- source run 中同一 Agent/version/model/index/horizon 的原始 Opinion；
- 没有被截至当时已完成的 successor Reflection 取代的 finding。

快照记录 `right_reason + verified/supported`、`lucky_correct`、`wrong`、
`reasoning_or_weighting_failure` 和 unresolved 数量。`ReflectionFinding.confidence`、
历史 `AgentOpinion.weight`、模型自报可信度和自由文本措辞均不参与 hash 或计数。

这仍只是后验归因代理，存在 hindsight rationalization 风险。整份 Reflection 的人工批准
只能证明该批后验材料可用，不能替代对每条原始 Opinion 的独立推理审核。

### 影子门禁

snapshot phase 只有：

```text
demo_excluded
shadow
governance_review_required
```

首批至少 20 个独立 Live reflection 目标日保持 `shadow`，并要求至少 10 份不可变人工
批准记录。审核门禁不是“任意凑够 10 份”：Python 对 current Reflection head 确定性
排序，只统计从最早记录开始连续 approved 的前缀；completed successor 会替换被纠正的
旧记录。服务拒绝对同一个 predecessor 创建多个有效 successor；若历史数据或并发竞争
仍形成分叉或平行 current head，门禁会 fail-closed，不统计任何 approved 前缀。每个当前
Agent/model/index/horizon exact profile 也必须分别达到历史与代理样本门槛，否则顶层仍
保持 shadow。所有门槛达到后也只变成
`governance_review_required`：

- `applied_to_decision=false`
- `activation_supported=false`
- `proposed_stage_multiplier=null`
- `applied_stage_multiplier=1.0`

当前 schema 根本没有 activation 路径，因此改环境变量、修改历史 Opinion 的 `weight`
或让模型在文本中声明自己更可靠，都不能改变正式 Forecast。

## 角色和阶段边界

### Research → Strategy

宏观、资讯和产业研究员属于同一阶段。当前 `AgentOpinion.weight` 只是静态角色参与
元数据，Live Strategy 不做数值加权。未来只有这个阶段内的 active policy 才能定义和
调整相对影响力；不能把研究员、Strategy、Risk Critic 和 CIO 的数字摊平成一组全局票。

### Strategy → CIO

Strategy 是 CIO 的唯一方向输入。未来即使 Strategy 有可信度证据，也应表现为对其概率
向冻结 baseline 的受控收缩，而不是把 Strategy 与三份基础研究再次平均。

### Risk Critic

Risk Critic 没有方向票。其未来 track record 应评价事前风险覆盖率、失效条件命中、
漏报和误报，不能用方向 Brier 生成权重，也不能暗中改变当前固定 15% 对称不确定性
haircut。

### Quant

Quant 的旧委员会 roster 不产生 Opinion、权重为零；首个内容寻址只读 adapter 使用独立
`AgentSpec` 0.3.0 从 shadow 开始。它不能继承其他角色或旧 placeholder 的成绩，也不能
因为提供了完整概率就自动获得 formal 权力。公开核心只接纳封签 bundle 并进行独立
shadow 评价，`decision_weight` 恒为 0；它不派生候选权重，也不提供正式 activation
路径。模型自报指标和历史留出测试都不能改变这一边界。

### CIO / Responsible Party

CIO 是对最终判断负责的人或角色，不是普通投票 Agent。可信度加权只能提供一个可审计的
参考基线，不能把责任转交给公式。

## 正式激活前必须补齐的能力

以下内容是候选政策设计，不属于当前运行中的已实现公式。

### 1. 事前推理 rubric

每条原始 Opinion 由独立评审者按五项各 0–2 分：

1. 证据相关性与覆盖；
2. 因果传导链是否明确；
3. 到具体指数和 D1/D2 的映射是否合理；
4. 是否真正处理反证与失效条件；
5. 概率、置信度与不确定性是否一致。

必须保存逐项分数、评审者、时间、Opinion ID、证据 hash，以及评审者是否在结果揭晓前
盲评。LLM 可以起草审核意见，但不能给自己的推理打分。

### 2. 历史表现的 prequential baseline

不能直接把命中率映射为权重。候选历史轴应以三分类 Brier 相对“预测发生前已经冻结”的
baseline 改善为主，按目标日聚类，并向父级 scope 或零技能先验收缩：

```text
delta_d = mean(Brier_reference - Brier_agent) within target date d
```

baseline 可以使用截止当时 expanding-window 的类别频率；没有历史时使用
`1/3, 1/3, 1/3`。禁止用当前评价窗口的真实标签回头拟合。符号和重大行情命中只作为
诊断，避免与 Brier 双重计分；三分类 classwise ECE 作为校准门禁。

不确定性估计必须以 `target_date` 为 cluster，不能把同日五指数和重叠 horizon 当成独立
样本。至少 20 个不同目标日、足够的三分类观察数和固定 policy 参数后，才能形成候选
历史表现下界。

### 3. 两个轴是“且”关系

历史表现与推理质量不能完全互相补偿。未来若形成两个经过门禁的 0–1 轴
`H` 与 `E`，候选 policy 可用几何平均：

```text
B = sqrt(H * E)
```

缺少任一轴时只能展示 provisional evidence，不能把缺失项默认为 1，也不能产生正式
stage multiplier。

### 4. 有界候选权重和影子对照

初始候选 multiplier 应限制在当前阶段权重的 `0.75–1.25`，并在同阶段 peer 内归一化。
每次 run 同时冻结四条互不覆盖的结果：

1. 当前静态研究 baseline；
2. 可信度候选 weighted baseline；
3. Strategy 的实际输出及其相对 weighted baseline 的 delta；
4. CIO 的正式 Forecast。

到期后分别比较 Brier、classwise ECE、重大行情和重要子组。至少完成新的 forward
shadow，不退化且重放 hash 可验证后，才能提出 activation。

### 5. 独立 activation event

正式政策需要新的 append-only 治理对象，至少冻结：

- policy ID、version、完整参数和代码版本；
- 训练/观察窗口与全部 source evaluation hashes；
- 适用 Agent/version/model/index/horizon；
- 权重上下限、回退链、replay 与子组结果；
- 审批者、生效时间、前一事件 hash 与 event hash。

`eligible` 不能自动变成 `active`。任何公式、baseline、rubric、scope hierarchy 或参数
变化都必须 bump policy version、重新 shadow，并且只作用于生效后的新 run。现有 Lesson
的概率校准生命周期不能被借用来偷偷激活 Agent 权重。

### 6. Responsible Party override

未来允许 CIO 覆盖候选加权基线，但必须保留两套概率和显式事件：

```text
weighted_probabilities
final_probabilities
override_applied
override_reason_code
override_rationale_hash
actor / authority_role
decision_cutoff
credibility_snapshot_hash
event_hash
```

新证据必须重新冻结 input，不能写进自由文本绕过 cutoff。到期后单独计算 override 相对
weighted baseline 的增量表现；该结果只属于 Responsible Party 的 override track record，
不能回写上游 Agent 的可信度。

## 防止自证循环

- 只评分各 Agent 的原始独立 Opinion，不用最终委员会结果反推上游 Agent“正确”。
- 只使用预测发生前可见的 policy、baseline 和历史评价。
- 所有符合预先规则的样本自动纳入；不能看过结果后选择样本。
- 新版本和新模型不继承旧权重。
- 不允许同一生成链路评价自己的推理。
- unresolved 不当作失败，但必须展示缺失率，防止选择性“不评价”。
- 始终保留静态 counterfactual baseline。
- 任何 active policy 都持续接受 forward 评价，并可由新的 append-only 事件禁用。

核心原则不是“让最近最准的 Agent 自动说了算”，而是：

> 只有在相关作用域内，长期概率表现经不确定性处理后仍可靠，并且因果解释经过独立、
> 可追溯的审核，才允许它获得有限、可撤销、人工激活的更高阶段内影响力。
