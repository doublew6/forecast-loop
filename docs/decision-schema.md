# 决策、引用与 API Schema

## 统一枚举

### 指数

| index_code | index_name |
| --- | --- |
| 000300.SH | 沪深300 |
| 000905.SH | 中证500 |
| 000852.SH | 中证1000 |
| 399006.SZ | 创业板指 |
| 000688.SH | 科创50 |

### 周期与方向

- horizon 读取枚举：D1 或 D2；新 AgentDraft、AgentOpinion 与 Forecast 只写 D1，
  D2 仅用于升级前已封签记录的读取、评价、反省和审计。
- 新 AgentDraft、AgentOpinion 与 Forecast 的 direction：up 或 down。
- run status：queued、running、completed 或 failed。

概率对象固定为 up、neutral、down 三个 0 至 1 的数，三者之和在 1e-6 容差内为 1。
neutral 是实际收益落入评价噪声带的结果概率，不是预测立场。direction 比较 p_up 与
p_down 后取较大一侧；两者精确并列时拒绝草案。历史 neutral direction 只读保留。

## Agent 注册与通用信号契约

`GET /api/agents` 保留 `id/name/role/kind/workflow_role/source_type/version/weight/status`
兼容字段，并为每项增加完整 `spec`：

- `spec.schema_version=forecast-loop.agent-spec/v1`；
- `spec.capabilities` 明确方向、概率、理由、证据与盲判能力；
- `spec.participation` 用独立 policy ID/version 声明 formal、shadow 或 disabled；
- `spec.content_hash` 绑定上述全部字段。

单条 spec 可通过 `GET /api/agents/{agent_id}/spec` 读取。公共 JSON Schema 位于：

- `GET /api/contracts/agent-spec/schema`
- `GET /api/contracts/signal-envelope/schema`

通用 SignalEnvelope 不直接替换 AgentOpinion、Forecast 或 UserJudgment 的 v1
序列化；它作为平行接入与审计边界，完整定义见
[AgentSpec 与 SignalEnvelope 契约](agent-contracts.md)。旧对象若缺少可证明的
accepted_at 或 provenance，不做推测性回填。

## Citation

Citation 是 Agent 观点和最终预测的最小可审计单元：

~~~json
{
  "wiki_entry_id": "VC-WIKI-INDUSTRY-AI-MEMORY",
  "wiki_title": "全球 AI 算力与存储产业链",
  "wiki_version": "1.0.0",
  "section": "cross-market-timing",
  "wiki_quote": "跨市场信号必须按发布时间与 A 股截止时点对齐。",
  "content_hash": "sha256-of-frozen-wiki-entry",
  "evidence_item_id": "EVT-20260713-MU-GUIDANCE",
  "quote": "公司更新了本季度收入指引。",
  "source_url": "https://investors.micron.com/quarterly-results",
  "evidence_content_hash": "sha256-of-canonical-evidence-item",
  "source_urls": [
    "https://computeexpresslink.org/about-cxl/"
  ],
  "event_time": "2026-07-13T12:55:00+08:00",
  "published_at": "2026-07-13T13:00:00+08:00",
  "ingested_at": "2026-07-13T13:02:10+08:00"
}
~~~

约束：

- wiki_entry_id、wiki_version 与 section 必须存在于 run 开始时冻结的 Wiki 对象；验证
  和持久化都读取该对象，运行中不重新读磁盘。
- content_hash 必须匹配冻结 Wiki 条目的完整正文；wiki_quote 是对应段落的短摘录。
- evidence_item_id 必须指向同一 Evidence Snapshot 中的动态事实；source_url、quote、
  三个时间字段和 evidence_content_hash 必须与该 EvidenceItem 精确一致。
- evidence_content_hash 是 EvidenceItem 去除哈希字段后 canonical JSON 的 SHA-256，
  不是模型自行生成的摘要哈希。
- source_urls 是冻结 Wiki 条目的方法参考源；动态事实来源只使用单数 source_url。前端
  必须把二者分别标记，不能把方法参考源冒充为当日动态证据。
- 动态事实的 published_at 与 ingested_at 均不得晚于 run.data_cutoff。
- 历史记录不因 Wiki 更新或网页变化而重写。

## AgentDraft 与 AgentOpinion

LLM 只生成 AgentDraft：

~~~json
{
  "direction": "up",
  "probabilities": {
    "up": 0.28,
    "neutral": 0.47,
    "down": 0.25
  },
  "summary": "涨跌二选一时正面证据略占优，但实际收益落入小波动区间的概率最高。",
  "evidence": [
    "正式来源未出现相对既有预期的明确变化。"
  ],
  "counter_evidence": [
    "海外相关资产出现同方向价格反应，但尚无独立基本面确认。"
  ],
  "invalidation_conditions": [
    "data_cutoff 前出现可验证的新政策或公司指引。"
  ],
  "evidence_item_ids": [
    "EVT-20260713-PBOC-OMO"
  ],
  "wiki_entry_id": "VC-WIKI-MACRO-POLICY",
  "wiki_section": "daily-checklist"
}
~~~

验证层根据草案选择的 evidence_item_id 和 Wiki 段落构造 Citation，再执行精确匹配并
持久化 AgentOpinion。模型不能手写 URL、哈希或时间来绕过快照。AgentOpinion 的身份
唯一键是：

    run_id + agent_id + index_code + horizon

持久字段包括 Agent 名称、职责、版本、状态、概率、摘要、证据、反证、失效条件、
引用、对 CIO 的 contribution、weight 和原始模型响应。

当前 `weight` 是静态角色参与元数据，Live Strategy 不读取它做数值平均；三位基础
研究员均作为有效输入进入 Strategy，Strategy 则是 CIO 的唯一方向输入。该字段不是可信度
分数、全局投票权重或仓位，也不能跨上下游相加。未来只有版本化、人工激活的阶段政策才可
定义数值语义；回放必须同时读取 contribution、工作流版本和当时的 policy snapshot。
历史 `AgentOpinion.weight` 和模型自报的 confidence 不进入可信度证据计算。

正式研究 Agent 的 evidence 不能为空，且每个判断必须同时有冻结 EvidenceItem 与
版本化 Wiki 引用。Strategy Agent 只能从三位上游研究员已经声明的 evidence_item_ids
中选择证据，其概率是 CIO 的唯一方向输入；不能把 Strategy 与基础观点再次等权平均。
Strategy 的 raw_response 还保存确定性派生的 strategy_context：market_regime、style_bias、
五指数 relative_rank、rank_tied 和 `up - down` allocation_score；Meeting API 将该结构公开
用于回放，相同分数必须显式并列，不能用指数代码顺序伪造强弱。
Risk Critic、未接入的 Quant 与 CIO 的特殊权重和职责由角色定义控制，不能由模型修改。
Quant 在可信只读数据适配器接入前不生成 AgentOpinion。

## Forecast

CIO 当前为每个指数输出一条不可变 D1 Forecast：

~~~json
{
  "id": "uuid",
  "run_id": "uuid",
  "index_code": "000688.SH",
  "index_name": "科创50",
  "horizon": "D1",
  "base_trade_date": "2026-07-13",
  "target_date": "2026-07-14",
  "as_of": "2026-07-13T15:10:00+08:00",
  "data_cutoff": "2026-07-13T15:05:00+08:00",
  "direction": "up",
  "probabilities": {
    "up": 0.31,
    "neutral": 0.43,
    "down": 0.26
  },
  "threshold": 0.0062,
  "confidence": 0.54386,
  "rationale": "AI存储事件的正面映射略强于下行反证，因此二元方向选择上涨；小波动仍是最可能的实际结果。",
  "counter_evidence": [
    "同一信息已在相关资产中提前反映。"
  ],
  "invalidation_conditions": [
    "出现新的正式订单、限制措施或权重公司公告。"
  ],
  "citations": [],
  "abstain": false,
  "model_name": "gpt-5-mini",
  "model_version": "0.1.0",
  "wiki_version": "snapshot-manifest-hash",
  "input_hash": "sha256-hex",
  "evaluation": null
}
~~~

约束：

- 同一 run_id + index_code + horizon 只允许一条 Forecast。
- 新 D1 的 target_date 取快照中第一个冻结目标交易日；历史 D2 保留其原先冻结的第二个
  目标交易日。两者评价时都不能重算。
- threshold 按预测基准日 σ20 计算，详见 Wiki 的 prediction-labels。
- confidence 等于 `max(p_up, p_down) / (p_up + p_down)`，表示排除小波动结果后的涨跌条件
  置信度，仅用于展示，不替代三结果概率校准评分。
- 新 Forecast 固定 abstain=false；证据不足以形成可审计判断时应阻断正式 run，而不是以
  abstain、neutral direction 或固定涨跌补位。
- model_name 保存本次实际产生该观点的模型标识；研究 Agent 记录实际 LLM，Demo 记录
  确定性 provider，未接入的 Quant 不产生记录，CIO 记录确定性聚合器及 workflow
  版本，不写模糊的“configured-model”。
- wiki_version 表示该 run 的 Wiki 清单快照，不是单个条目的版本。
- input_hash 覆盖冻结行情/事件、Wiki 清单、实际模型、Agent 版本/weight 元数据、prompt、
  schema、聚合版本，以及 run 级可信度证据快照的 policy version、content hash 和
  `applied_to_decision=false`；重跑不得复用旧身份覆盖记录。

## WorkflowRun

WorkflowRun 是一次投委会会议的审计根：

~~~json
{
  "id": "uuid",
  "as_of": "2026-07-13T15:10:00+08:00",
  "data_cutoff": "2026-07-13T15:05:00+08:00",
  "status": "completed",
  "mode": "demo",
  "started_at": "2026-07-13T15:10:00+08:00",
  "completed_at": "2026-07-13T15:10:03+08:00",
  "duration_seconds": 3.0,
  "error": null,
  "data_quality": {
    "market_data_complete": true,
    "citations_valid": true,
    "believability": {
      "policy_version": "1.0.0-shadow",
      "snapshot_hash": "64-lowercase-hex",
      "run_binding_hash": "64-lowercase-hex",
      "mode": "shadow_only",
      "applied_to_decision": false
    },
    "believability_snapshot": {
      "schema_id": "vericouncil.believability-shadow/v1",
      "phase": "demo_excluded",
      "applied_to_decision": false,
      "activation_supported": false,
      "profiles": [],
      "content_hash": "64-lowercase-hex"
    }
  },
  "workflow_steps": [
    {
      "id": "freeze_snapshot",
      "label": "冻结输入",
      "status": "completed",
      "started_at": "2026-07-13T15:10:00+08:00",
      "completed_at": "2026-07-13T15:10:01+08:00"
    }
  ],
  "input_hash": "sha256-hex"
}
~~~

mode 至少区分 demo 与 live。是否调用真实模型不决定 mode：只有冻结的行情、当日
Evidence Snapshot、Wiki 清单和时间校验均通过时才可使用 live。仅有 Wiki 的运行必须
保持 demo。failed run 保留已完成步骤和错误原因，不发布为 latest forecast。

`believability_snapshot` 的完整内容由数据库侧 prepare 生成并封存；模型草案的 state
只得到 policy version 与 snapshot hash，不得到历史排名或候选权重。执行前必须用
canonical JSON 重算 content hash 和 run binding hash，并与 frozen state 比对。当前 schema 固定
`proposed_stage_multiplier=null`、`applied_stage_multiplier=1.0`，不能表达 active policy。

## EvaluationResult

目标交易日收盘数据确认后，为 forecast_id 最多追加一个评价。请求提供两个来源绑定的
正收盘观察，不提供 actual_return：

~~~json
{
  "forecast_id": "uuid",
  "price_source": "provider-name",
  "observed_at": "2026-07-15T15:20:00+08:00",
  "start": {
    "trade_date": "2026-07-13",
    "close": 1218.31,
    "source_url": "https://www.csindex.com.cn/",
    "source_hash": "64-lowercase-hex"
  },
  "end": {
    "trade_date": "2026-07-15",
    "close": 1226.96,
    "source_url": "https://www.csindex.com.cn/",
    "source_hash": "64-lowercase-hex"
  }
}
~~~

服务端校验到期、日期、正价格、URL 与哈希后计算并追加：

~~~json
{
  "actual_return": 0.00710014,
  "label": "up",
  "correct": false,
  "brier": 0.2475,
  "evaluated_at": "2026-07-15T15:20:00+08:00",
  "price_source": "provider-name",
  "observed_at": "2026-07-15T15:20:00+08:00",
  "start_trade_date": "2026-07-13",
  "start_close": 1218.31,
  "start_source_url": "https://www.csindex.com.cn/",
  "start_source_hash": "64-lowercase-hex",
  "end_trade_date": "2026-07-15",
  "end_close": 1226.96,
  "end_source_url": "https://www.csindex.com.cn/",
  "end_source_hash": "64-lowercase-hex",
  "observation_hash": "sha256-of-canonical-evaluation-input"
}
~~~

- actual_return 只能由服务端根据 start_close 和 end_close 计算；接口拒绝调用方提交的
  收益率。
- observed_at 必须在 target_date 收盘之后，start_trade_date 必须等于冻结基准日，
  end_trade_date 必须等于冻结目标日，两个 close 都必须为正。
- live 评价的两个 URL 还必须通过与快照相同的可信域名门禁。
- label 使用该 Forecast 冻结的 threshold，不用评价日重新计算。
- correct 比较 direction 与实际 label。
- brier 使用三分类均方概率误差的平均值。
- 两个 source_hash 与 observation_hash 随评价保存，使行情和计算输入可以重放。
- 更换价格源重评不能覆盖原记录；需要先走显式审计纠错流程。

## WikiEntry

Wiki API 返回：

- id、title、version、updated_at、status、owners、tags；
- source_urls；
- sections，每项包含 slug、title 与 excerpt；
- content_hash；
- referenced_by_count；
- 详情请求可包含 body。

section 来自正文中的显式 section 注释。文件名和 Markdown 标题不是稳定引用身份。

## Scorecard

Scorecard 可按 agent_id、index_code 与 horizon 筛选，返回：

- sample_size 与 sample_sufficient；
- sign_sample_size、sign_correct、sign_accuracy；兼容字段 accuracy 等于
  sign_accuracy；
- material_sample_size、material_correct、material_direction_accuracy，只统计
  实际收益越过噪声带的样本；
- average_brier（三分类）；
- 三个实际结果标签的 predicted、actual、true_positive、precision 和 recall；新版本的
  neutral predicted 固定为 0，前端只展示上涨与下跌立场精度；
- 涨跌条件 confidence 的 calibration、expected_calibration_error；该曲线排除实际
  neutral 样本，三结果概率质量单独由 Brier Score 评价；
- agent_version 与实际 model_name；
- note。

sample_size 是 Agent×日期×指数观察数；五个指数在同一预测日高度相关，因此只有覆盖
至少 20 个不同 target_date 的预测截面时，sample_sufficient 才为 true。统计必须先按 run
Agent 版本和实际模型分区：Demo 不产生评价且不会进入查询，不同 agent_version 或
model_name 不合并。当前接口默认统计角色清单中的当前版本与当前配置模型；历史版本
应单独展示。
缺少完整 Evidence Snapshot 的 run、未接入的 Quant 和没有到期评价的预测不进入
正式统计。

20 个独立日期只表示 Scorecard 可以展示较稳定的能力证据，不等于自动调权资格。
五指数相关性、重叠周期、prequential baseline、三分类校准、Agent/model 版本漂移和重要
子组非退化仍必须由单独的聚合政策重放处理；Scorecard 不直接驱动工作流。

## BelievabilitySnapshot

`vericouncil.believability-shadow/v1` 是 run 级审计证据，不是动态权重表。每个 profile
的稳定 scope 为：

    agent_id + agent_version + model_name + index_code + horizon + policy_version

表现组件只读取不晚于新 run cutoff 的 completed Live `OpinionEvaluation`，保存原始观察
数、不同目标日期数、平均 Brier、符号和重大行情方向表现及 source evidence hash。
解释组件只读取 completed、approved、未被 completed successor 替代的 Reflection，
保存 `right_reason / lucky_correct / wrong / unresolved` 等计数及独立 evidence hash。
全局人工门禁按 target date、horizon 和创建时间排序，只统计从最早 current Reflection
开始连续 approved 的前缀；后来的审核不能跳过更早的未审或 rejected 记录。

整份 Reflection 的人工批准只是 `human_reviewed_right_reason_proxy` 的可用性门禁，不是
逐 Opinion 的完整推理质量分数。finding confidence、历史 Opinion weight、raw response
中的自报身份和自由文本评分均不参与。完整字段、未来 reasoning rubric 与 activation
事件见 [可信度加权决策设计](believability.md)。

## HTTP API

v0.1 当前接口（首次公开发布前仍可按 changelog 显式调整）：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | /api/health | 服务状态与运行模式 |
| GET | /api/forecasts/latest | 最近一次完成会议的十条预测 |
| GET | /api/forecasts/{forecast_id} | 单条预测与评价 |
| GET | /api/meetings/{run_id} | 会议、观点、预测和步骤 |
| GET | /api/agents | 注册 Agent、结构化职责与默认来源清单 |
| GET | /api/agents/{agent_id}/scorecard | Agent 成绩单与筛选 |
| GET | /api/wiki | Wiki 元数据列表 |
| GET | /api/wiki/{entry_id} | Wiki 正文、段落、来源与引用计数 |
| GET | /api/runs | 运行列表 |
| POST | /api/runs | 新建独立 run |
| POST | /api/evaluations/run | 为到期预测追加评价 |

所有错误使用非 2xx 状态码和 detail 字段。数据缺失、引用失败或 schema 不合法不得
返回伪 completed 结果。

正式模式的 POST /api/runs 先冻结输入，持久化 `queued` run 和一对一 task，再返回
`202`；独立 `forecast-loop worker run` 使用租约、attempt fencing deadline、有限重试和 lease-token
围栏执行完整投委会。带 task 的 queued/running run 可在进程重启后由 worker 恢复；
只有升级前缺少冻结 task payload 的遗留运行会 fail closed。Demo 模式同步执行。
`Idempotency-Key`、Live `as_of` 唯一索引和 forecast identity 唯一约束共同阻止重复
发布。完整边界见[持久预测任务队列](persistent-task-queue.md)。

## 不可变性与版本

- Agent 版本、模型名称与模型版本随每条结果保存。
- Wiki 条目版本和 run 级 Wiki 清单哈希同时保存。
- 已完成 run、AgentOpinion 和 Forecast 不允许原地更新判断字段。
- 允许追加 EvaluationResult 和审计元数据，不允许改写当时输入。
- 同一 as_of 已存在 queued、running 或 completed live run 时返回 409；failed run 可创建
  新 run 重试，但不覆盖旧记录。Demo 可用独立 run_id 重放固定夹具。
