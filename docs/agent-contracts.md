# AgentSpec 与 SignalEnvelope 契约

本页描述 forecast-loop v0.1 的公共 Agent 接入边界。契约解决的是“不同来源如何提交可验真
信号”，不是把所有来源强行塞进同一执行器或同一业务表。

当前 schema：

| 契约 | schema ID | 用途 |
| --- | --- | --- |
| AgentSpec | `forecast-loop.agent-spec/v1` | 冻结 Agent 身份、能力与参与政策 |
| ParticipationPolicy | `forecast-loop.participation-policy/v1` | 独立声明 formal、shadow 或 disabled 权限 |
| SignalEnvelope | `forecast-loop.signal-envelope/v1` | 封签单次信号的目标、输入、provenance 与内容 |
| QuantSignalBundle | `forecast-loop.quant-signal-bundle/v1` | 冻结只读 Quant 输出、Evidence Snapshot 绑定与五类 artifact |
| QuantInputSnapshot | `forecast-loop.quant-input-snapshot/v1` | 冻结实际模型输入与时间语义 |

可通过 API 或 CLI 读取机器可用的 JSON Schema：

```bash
curl http://127.0.0.1:8000/api/contracts/agent-spec/schema
curl http://127.0.0.1:8000/api/contracts/signal-envelope/schema

forecast-loop contract schema agent-spec
forecast-loop contract schema signal-envelope
forecast-loop contract schema quant-signal-bundle
forecast-loop contract schema quant-input-snapshot
```

## AgentSpec

AgentSpec 是内容寻址的注册记录。`source_type` 只描述该版本默认由哪类适配器生产，
不能替代单次信号的 provenance，也不能授予决策权。

```json
{
  "schema_version": "forecast-loop.agent-spec/v1",
  "agent_id": "user_judgment_agent",
  "agent_version": "0.1.0",
  "name": "用户判断 Agent",
  "role": "在查看委员会结论前独立选择涨跌，并封存理由、反证与失效条件。",
  "workflow_role": "shadow",
  "source_type": "manual",
  "capabilities": {
    "direction": true,
    "probability_mode": "confidence",
    "reasoning_mode": "structured",
    "evidence_mode": "none",
    "supports_blind_submission": true,
    "supports_input_binding": true
  },
  "participation": {
    "schema_version": "forecast-loop.participation-policy/v1",
    "policy_id": "manual-shadow",
    "policy_version": "1.0.0",
    "mode": "shadow",
    "influence": "none",
    "evaluation_metrics": ["direction", "reasoning"]
  },
  "content_hash": "<64位小写SHA-256>"
}
```

能力字段的含义：

- `probability_mode=none`：不得提交概率或 confidence；
- `probability_mode=confidence`：提交单一方向 confidence，不得伪装成三分类概率；
- `probability_mode=multiclass`：必须提交完整、归一化的 up / neutral / down 概率；
- `reasoning_mode=structured`：必须同时提交理由、最强反证和可观察失效条件；
- `evidence_mode=frozen_citations`：至少提交一条带内容哈希与观察时间的冻结引用；
- capability 表示可提交和可评价字段，不表示 formal 权力。

ParticipationPolicy 与来源正交。同样是 AI 来源，一个 Agent 可以是 formal 输入，也可以
只处于 shadow；Quant 也不能因为“是模型”就自动进入委员会。

接收 SignalEnvelope 时，宿主会把它引用的完整 AgentSpec 以 `content_hash` 存入
append-only `agent_specs`。后续注册表即使升级，历史信号仍使用当时的 spec 快照验证，
不能拿当前同名 Agent 的配置改写旧记录。归档 spec 只用于验证和幂等重放；新信号必须
匹配宿主当前已批准的注册表，adapter 不能提交一个自建 formal policy 来提升自己的
路由权限。

## SignalEnvelope

SignalEnvelope 由宿主验证并封签。`AgentSignalSource` 只返回不含宿主接收时间、
参与政策、target、input binding 和 provenance 的 `AgentSignalDraft`；确定性
`accept_signal_draft` 从当前批准的 AgentSpec 与宿主上下文注入这些字段，再计算最终
Envelope hash。模型或适配器不能通过公共字段或 source payload 自报新的 Agent 身份、
目标、接收时间、截止时间、参与政策、provenance 或内容哈希。

```json
{
  "schema_version": "forecast-loop.signal-envelope/v1",
  "signal_id": "signal-20260727-000300-d1",
  "agent_id": "user_judgment_agent",
  "agent_version": "0.1.0",
  "mode": "live",
  "target": {
    "index_code": "000300.SH",
    "horizon": "D1",
    "base_trade_date": "2026-07-27",
    "target_date": "2026-07-28",
    "as_of": "2026-07-27T15:00:00+08:00",
    "data_cutoff": "2026-07-27T14:55:00+08:00"
  },
  "submitted_at": "2026-07-27T15:02:00+08:00",
  "accepted_at": "2026-07-27T15:03:00+08:00",
  "submission_deadline": "2026-07-27T15:30:00+08:00",
  "input_binding": {
    "run_id": "run-id",
    "run_input_hash": "<64位小写SHA-256>",
    "agent_spec_hash": "<64位小写SHA-256>",
    "forecast_input_hash": null,
    "evidence_snapshot_hash": null,
    "parent_signal_hashes": []
  },
  "participation": {
    "schema_version": "forecast-loop.participation-policy/v1",
    "policy_id": "manual-shadow",
    "policy_version": "1.0.0",
    "mode": "shadow",
    "influence": "none",
    "evaluation_metrics": ["direction", "reasoning"]
  },
  "provenance": {
    "source_type": "manual",
    "producer": "local-user-interface",
    "adapter": "manual-form",
    "adapter_version": "1.0.0",
    "model_name": null,
    "model_version": null,
    "prompt_version": null,
    "prompt_hash": null,
    "code_version": null,
    "code_hash": null,
    "artifact_hashes": {}
  },
  "direction": "up",
  "probabilities": null,
  "direction_confidence": 0.67,
  "rationale": "冻结信息显示流动性与风险偏好共同支持该方向。",
  "counter_evidence": ["海外利率上行可能压制风险资产。"],
  "invalidation_conditions": ["若跌破基准日低点，则当前判断失效。"],
  "citations": [],
  "blind_attestation": true,
  "payload_schema": "forecast-loop.manual/v1",
  "source_payload": {
    "entry_format": "private-wiki"
  },
  "content_hash": "<64位小写SHA-256>"
}
```

`source_payload` 必须：

1. 有独立的 `payload_schema`；
2. 保持嵌套，不能把来源字段摊平到公共 envelope；
3. 不得重新定义 `agent_id`、target、cutoff、input binding、participation、provenance
   或 `content_hash` 等公共字段；
4. 和全部公共字段一起进入内容哈希。

公共 envelope 不导出 manual actor 的本地身份。需要保存的私有 actor 信息继续留在
User Judgment 私有账本，未来可移植 bundle 默认也应脱敏。

接收边界还会验证：

- `run_id` 必须存在，且 mode、as-of、data cutoff 与 run 一致；
- `run_input_hash` 必须等于已封签 WorkflowRun 的输入哈希；
- formal 与 shadow 信号都必须声明 submission deadline，且截止必须早于目标日；
- target、宿主接收时间、submission deadline 和 provenance 必须与宿主的可信配置逐项
  相等；adapter 自报的模型、prompt、代码或 artifact 身份不能直接成为可信事实；
- 新信号引用的 AgentSpec 必须等于宿主当前激活的 spec；只有已存在历史记录的重放才能
  按归档 hash 解析旧 spec；
- citation 的 `observed_at` 不得晚于 data cutoff；
- envelope 与 spec 的版本、能力、参与政策和 provenance 必须逐项一致。

SQLite 会按显式传入的运行时区恢复旧 WorkflowRun 的无时区投影，再转换为 UTC 比较；
因此与 PostgreSQL 一样，`+08:00` 和 `Z` 表达的同一时刻不会被误判。校验通过后，
ParticipationPolicy 会被实际路由为
`formal_input`、`formal_advisory`、`formal_decision` 或 `shadow_benchmark`，并把路由
投影写入数据库。路由只读取独立、版本化的参与政策，绝不根据 manual、AI、Quant 等
来源类型猜测决策权。`disabled` Agent 不能提交或进入任何路由。

## Provenance 门禁

每条信号都必须声明实际 `source_type`、producer、adapter 和 adapter version，且与
AgentSpec 一致。附加门禁如下：

- AI：必须有 model name、model version 和 prompt version；
- Quant：必须有 code version、code hash 和至少一个模型或参数 artifact hash；
- deterministic：必须有 code version 与 code hash；
- manual：保留界面或导入适配器身份，不把操作者姓名放进公共 envelope。

这些字段描述本次信号如何产生；注册表中的 `source_type` 不能代替它们。

首个 Quant adapter 采用更严格的门禁：代码、参数、feature set、模型和输入快照都必须
同时提供版本与可验证 SHA-256；raw manifest 也进入 provenance。它要求完整三分类概率，
因此具备方向、Brier 和 calibration 评价资格，但其 ParticipationPolicy 固定为
`shadow / none`，不能进入 CIO 聚合。具体 bundle、只读路径与失败关闭规则见
[只读 Quant Agent adapter](quant-adapter.md)。

## Evaluation facade

评价先读取 AgentSpec，再决定哪些指标有定义：

| 能力 | 方向指标 | Brier | calibration | reasoning review |
| --- | --- | --- | --- | --- |
| direction + confidence | 是 | 否 | 否 | 按 reasoning 能力 |
| direction + multiclass | 是 | 是 | 是 | 按 reasoning 能力 |
| critique only | 否 | 否 | 否 | 当前仅标记是否适用 |
| disabled | 否 | 否 | 否 | 否 |

如果 AgentSpec 声明 multiclass，但具体信号缺概率，验证失败；系统不能静默跳过后继续
生成不完整成绩。Reasoning 目前只标记可审核性，正式量表与双盲评分由后续版本实现。
例如 Risk Critic 当前没有三分类概率能力，其 OpinionEvaluation 的 Brier 必须保持
`null`，不能从方向倾向伪造概率成绩。

## Canonical bytes 与持久化

AgentSpec 和 SignalEnvelope 使用独立的 canonical JSON：

- UTF-8；
- `sort_keys=true`；
- 紧凑分隔符；
- `ensure_ascii=false`；
- 禁止 NaN 和 Infinity；
- 内容哈希排除且只排除顶层 `content_hash`。

这套规则不得替换历史 snapshot、workflow、handoff、User Judgment 或 run bundle 的
各自 canonical 算法。

迁移 `0008_agent_contracts` 增加 append-only `agent_specs` 与 `signal_envelopes`。
前者保存历史验证所需的完整 spec，后者保存完整 envelope JSON、run/spec 外键和公共
查询/路由投影；SQLite 在当前自动化测试中验证了数据库级 UPDATE/DELETE 拒绝，
PostgreSQL migration 也定义了等价触发器，但在加入 driver 与 CI 服务前仍视为实验性
部署路径。应用的 `create_all` 路径会安装同等门禁。历史 AgentOpinion、Forecast 和
UserJudgment 不回填：旧记录缺少
可证明的 accepted_at 或完整 provenance，伪造这些字段比不迁移更危险。旧 v1 run
bundle 也不新增 envelope 文件。

## CLI 验证

```bash
forecast-loop agent list
forecast-loop agent show user_judgment_agent
forecast-loop agent validate ./input/signal.json
forecast-loop agent validate ./input/signal.json \
  --spec ./input/archived-agent-spec.json
```

`agent validate` 只读文件、不写数据库；它限制文件大小，拒绝 symlink，并校验 schema、
时区、哈希、AgentSpec 绑定、能力、participation、provenance 和来源 payload 边界。
默认使用当前注册表；验证历史 envelope 时必须显式提供当时归档的 AgentSpec。任何未知
schema、未知 Agent、错误哈希或缺失能力字段都以非零状态失败关闭。

离线 CLI 不拥有运行数据库、宿主时钟或 adapter 配置，因此不会把 envelope 自报的
run、target、receipt、deadline 或 provenance 提升为可信事实；只有持久化接收边界把
这些字段与宿主上下文逐项比对后，信号才算 accepted。CLI 成功表示“契约内部一致”，
不表示“已被某次正式运行接收”。

## v1 兼容边界

旧 `AgentDefinition`、`AGENTS` 顺序和
`id/version/kind/status/weight/model_name` 哈希投影继续冻结。新 AgentSpec hash 不加入
现有 v1 workflow input hash；未来如果正式工作流开始消费通用 envelope，必须同时升级
workflow、decision schema 与交接协议版本。

仓库包含固定的 v1 run bundle golden，锁定 run、opinions、forecasts 三个文件的逐字节
内容、SHA-256 和 bundle hash；另一个 golden 直接调用生产 `prepare_run` 路径锁定完整
workflow input hash，确保新增契约不会改写历史制品或运行输入身份。
