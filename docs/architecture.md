# forecast-loop 系统架构

## 目标与边界

forecast-loop 是一个可验证的预测 Agent 框架。默认配置在每个 A 股交易日收盘后，
对沪深300、中证500、中证1000、创业板指和科创50生成下一有效交易日（D1）的涨跌
二元判断；版本化 Market Universe 也可把同一协议应用到单一市场时钟下的港股、美股、
指数或个股。升级前已封签的 D2 记录继续可读、可评价和可反省，但新运行不再写 D2。
系统同时保留上涨、小波动、下跌三种实际结果的概率，并保存当时可见的信息、Wiki 引用、
Agent 版本和后验评分。

第一期不连接券商、不执行交易、不生成仓位，也不声称概率预测等同于投资收益。
Quant Agent 通过本地 JSON 只读 bundle adapter 进入独立 shadow benchmark。公开核心
只验证来源无关的 bundle、目标矩阵和哈希，不附带训练框架、特征、模型参数或生产数据
映射。正式 Quant 权重默认为 0，不改变 Strategy 或 CIO 概率。

Wiki 是稳定研究框架，不是每日证据。任何只向模型提供 Wiki、却没有冻结行情和当日
Evidence Snapshot 的运行都必须标记为 demo，即使调用了真实 LLM API；这类运行不得
成为 latest 正式预测，也不得进入正式成绩单。

一期尚未实现自动资讯或行情采集器。live 入口消费由外部 provider 预先组装、校验并
封存的 JSON 快照；架构图中的“采集”是明确接口边界，不代表仓库已经持续抓取数据。

## Agent 框架视角

Agent 是版本化的信号生产者，不是 LLM 的别名。职责角色与注册来源分开表达：
`workflow_role=research|strategy|critic|decision|shadow` 说明它在流程中做什么，
`source_type=ai|manual|quant|deterministic` 声明默认生产适配器类型。`GET /api/agents`
已公开这两项元数据；旧 `kind` 仅保留给执行分支与 v1 哈希兼容。

注册来源不是单次 run provenance。Demo provider 可以为注册为 `ai` 的研究角色生成确定性
fixture；实际运行采用的 provider、model、prompt 和输入必须从 run/opinion 封签读取。

v0.1 的 AI/Codex、手动输入、Quant shadow 和确定性 CIO 仍使用不同执行与业务存储路径。
它们已共享内容寻址的 AgentSpec、独立参与政策、
SignalEnvelope、append-only spec/envelope 路由投影和 capability-driven evaluation
facade；统一契约不等于已经实现动态插件装载。完整边界见
[AgentSpec 与 SignalEnvelope 契约](agent-contracts.md)。

Universe 是独立于 AgentSpec 的版本化输入：它声明本次预测的市场、时区、交易日历、
收盘时间、标的元数据、Wiki 绑定和标的级 `agent_briefs`。稳定 Agent ID 不随市场更名，
但研究职责可以按标的调整并进入 universe hash。完整契约见
[版本化市场与标的 Universe](market-universes.md)。

## 总体结构

~~~mermaid
flowchart LR
    A["行情与官方资讯"] --> B["采集、去重、时间标准化"]
    B --> C["冻结 Evidence Snapshot"]
    W["版本化 Wiki"] --> C
    C --> G["LangGraph 投委会"]
    G --> V["Evidence Validator"]
    V --> P["不可变 Forecast / Opinion"]
    P --> T["Blind Target Projection<br/>不含 CIO 方向"]
    T --> U["Manual Agent<br/>User Judgment v1"]
    U --> UW["私有手动信号账本<br/>append-only"]
    P --> E["D1 到期评价 / 历史 D2 兼容评价"]
    E --> UE["Manual Signal Evaluation"]
    U --> UE
    E --> BS["可信度证据快照<br/>shadow only"]
    BS -. "版本化政策提案 + 人工审批<br/>仅影响未来 run" .-> C
    P --> API["FastAPI"]
    E --> API
    U --> API
    UE --> API
    BS --> API
    W --> API
    UW --> API
    API --> UI["React 研究台"]
~~~

系统由五层组成：

1. 数据层：行情、交易日历、事件和原始来源快照。
2. 知识层：运行者本地 `data/wiki/`（或配置路径）中的版本化 Markdown Wiki；源码只带
   `demo-only` 合成示例。
3. 决策层：LangChain 结构化输出与 LangGraph 状态机。
4. 审计层：SQLite 业务数据、内容哈希和 LangGraph checkpoint。
5. 表现层：FastAPI 与 React 页面。

核心结果不得依赖 LangSmith。LangSmith 仅可作为可选 tracing。

### 开源模块边界

项目保持模块化单体，不在第一阶段拆分微服务。核心领域只接收版本化 contract：

- `ports/` 定义 `EvidenceSnapshotSource` 与 `AgentSignalSource` 等稳定只读边界；
- `adapters/` 把本地 JSON、未来对象存储或外部数据拥有者转换为核心 schema；
- `jobs/` 定义 scheduler-neutral 的任务声明与调度模板；
- `services/` 执行 prepare、校验、finalize、评价和结果包导出；
- FastAPI 与 React 只消费已通过确定性门禁的业务事实。

`EvidenceSnapshotSource` 必须返回同一 `FrozenEvidenceSnapshot`；
`AgentSignalSource` 只返回不可信 `AgentSignalDraft`，其中没有宿主接收时间、target、
input binding、参与政策或 provenance。宿主通过 `accept_signal_draft` 从当前可信注册表
和运行上下文注入这些字段并封签 SignalEnvelope；模型或 adapter 的自报身份不能直接成为
可信事实。两类 Port 都应在来源、时点、schema 或哈希无法验证时失败关闭。外部系统只能
通过 adapter 接入，不能把其私有目录、表结构或机器路径提升为核心 contract。

归档 AgentSpec 是历史验真材料，不是激活表。新 signal admission 只能使用宿主当前注册
且已批准的 spec；旧 spec hash 只允许验证或幂等重放已经存在的 envelope，不能重新获得
formal 权限。

## 投委会状态图

~~~mermaid
flowchart TD
    S["freeze_snapshot"] --> M["macro_policy_agent"]
    S --> N["market_news_agent"]
    S --> I["ai_storage_industry_agent"]
    M --> T["strategy_agent"]
    N --> T
    I --> T
    T --> K["risk_critic_agent"]
    K --> V["evidence_validator"]
    V --> C["cio_agent"]
    C --> E["LangGraph END"]
    E --> P["事务性持久化 immutable results"]
    V -. "异常" .-> F["标记 failed run"]
    P --> D1["evaluate D1 when due"]
    P --> D2["evaluate D2 when due"]
~~~

### 固定角色

| Agent | 职责 | 当前参与语义 |
| --- | --- | --- |
| macro_policy_agent | 货币、财政、监管、汇率和海外宏观传导 | 有效研究输入 |
| market_news_agent | 当日新增事件、公告、跨市场变化与叙事 | 有效研究输入 |
| ai_storage_industry_agent | 默认研究全球 AI 算力、存储、半导体与 A 股映射；自定义 Universe 可按标的覆盖研究职责 | 有效研究输入 |
| strategy_agent | 综合市场状态、风格与 Universe 内标的配置 | CIO 唯一方向输入 |
| risk_critic_agent | 反证、污染、重复证据和失效条件；输出二元风险倾向但不参与方向投票 | 0 |
| quant_agent | 读取已封签代码/参数/特征/模型/输入 artifact，生成独立 shadow signal 与测试集候选权重；不进入旧委员会 Opinion | 正式权重 0 |
| cio_agent | 回应分歧并形成最终判断 | 最终决策者 |
| user_judgment_agent | 当前手动输入实现：先盲判方向并写理由、最强反证和失效条件 | 独立 manual shadow，0 |

Quant 的旧 v1 roster 继续保持 status=unavailable、weight=0，不运行模型、不生成
AgentOpinion；新 `AgentSpec` 0.3.0 只接纳已验证的只读 bundle，并把通用
SignalEnvelope 路由到 shadow benchmark。任何候选权重都必须由宿主根据封签证据
确定性派生并写入 run input hash；在 Live shadow 门禁完成前
`decision_weight=0`。这些状态刻意分离，以保持旧 run 与正式聚合语义不变。详见
[只读 Quant Agent adapter](quant-adapter.md)。

当前 Manual Agent（兼容 ID `user_judgment_agent`）不进入 LangGraph、`AgentOpinion`、
Codex handoff、Strategy、CIO、
run input hash 或 Reflection 固定 roster。它消费去除委员会方向与概率后的 target
projection，提交后才看到委员会结果；自己的记录和成绩保存在独立 append-only 边界。

第一期不根据少量历史自动调权。三位有效专业研究 Agent 先向 Strategy Agent 提供证据，
Strategy 形成唯一方向输入；CIO 不再把 Strategy 与其基础输入二次平均，以免同源观点重复
计权。当前 Live 图不会对 `AgentOpinion.weight` 做数值加权；该字段只是静态角色参与
元数据，为未来版本化阶段政策保留，不是历史可信度分数、全局票权或仓位。不能把上下游
字段摊平成一组票。CIO 必须公开每个 Agent 的贡献、分歧及对 Risk Critic 异议的处理。

`believability-shadow/v1` 已把当前 Agent 身份对应的历史评价证据冻结进 run，但完整
profile 只保存在数据库侧审计数据中，Strategy 与 CIO 均不读取候选权重。达到影子期门槛
只进入单独的治理评审；当前决策图不允许成绩单直接反馈修改 `weight`。

## 运行数据流

### 1. 冻结输入

一次 run 对应一个 as_of 和 data_cutoff。采集层先验证 Universe 内全部标的行情、最近 21 个有效
收盘价、交易日历和资料时间，再产生 input_hash。冻结后新增资料不得进入该 run。

live 快照必须包含：

- 恰好两个严格递增、晚于 base_session 的目标交易日，分别固定 D1 与 D2；
- 交易日历来源 URL、source_hash、observed_at、ingested_at，以及与 base/D1/D2 完全一致
  的三个 sessions；
- 全部配置标的各自的 20 日波动率、等于 base_session 的 trade_date、原始 URL、source_hash、
  observed_at 与 ingested_at；
- 至少一条带可信域名 URL、三时间字段和 canonical content_hash 的 EvidenceItem；
- 同一 as_of 日期内的新鲜 data_cutoff，以及快照整体 canonical hash。

这两个 target session 是 Evidence Snapshot v1 与 Market Universe v1 的兼容输入包络；
handoff v3 和当前 workflow 只消费第一个 D1 session，保留第二个 session 不代表会写入
新的 D2 Forecast。系统重算哈希并检查域名 allowlist、时区与截止时点。任何过期、未来、
缺项或哈希不匹配都阻断 live，不允许降级成旧快照。run 开始时还把 Wiki 条目及段落
冻结进状态；后续节点只读这份对象，避免 Wiki 在验证和持久化之间变更。

live 发布门禁要求 input_hash 同时覆盖行情/事件快照、Wiki 清单、实际模型名、Agent
版本与 weight 元数据、prompt 版本、决策 schema、聚合版本，以及本次只读可信度证据快照的
policy version、content hash 和 `applied_to_decision=false` 边界。仅有 Wiki 清单哈希，
或仍使用演示波动率时，运行只能处于 demo 路径，不能伪装成可验证 live 结果。

### 2. 结构化研究

LangChain 模型通过 Pydantic schema 返回 AgentDraft。业务层校验方向、概率、证据、
Wiki entry ID 和 section。自然语言原始响应可用于排错，但只有验证后的结构化结果
进入 AgentOpinion。

宏观、资讯和产业 Agent 并行完成后，Strategy Agent 才能运行。它只能使用上游已经声明的
evidence_item_id 子集，不得引入新事实；其概率作为 CIO 唯一方向输入。Risk Critic 同时
检查基础观点和策略综合，识别同源证据、综合失真与共识拥挤。

### 3. 引用校验

Evidence Validator 是确定性代码，不是另一个自由生成的 Agent。它检查：

- Wiki entry_id、version、section 和 content_hash；
- evidence_item_id 是否存在于冻结快照；引用 URL、摘录、时间和动态证据哈希是否与该
  EvidenceItem 精确一致；
- 动态 source_url 的可信域名与三时间字段是否早于 data_cutoff；`source_urls` 仅保留
  冻结 Wiki 条目的方法参考源；
- 概率与必填字段；
- 重复来源和 Quant 未接入时不得产生观点的规则。

因此每条持久化 Citation 同时绑定“采用哪条版本化研究规则”和“使用哪条当日冻结
事实”。只列 Wiki 首页、模型自写 URL 或未进入快照的事实均不是有效引用。

新观点的 direction 只允许 up 或 down；证据不足可以降低方向条件置信度、提高实际结果的
小波动概率，或直接阻断正式 run，但不能用 neutral、abstain 或固定方向掩盖证据缺口。
行情、时间或引用身份损坏必须使 run 失败。

### 4. 冻结决策

CIO 当前无条件将 Strategy 的上下行结果概率各对称缩减 15%，把这部分概率移入小波动
结果桶；Risk Critic 的反证进入 counter evidence 与 invalidation conditions，但不决定
是否执行或改变折扣幅度。该操作提高结果不确定性，但不改变排除小波动后的涨跌比。
随后为 Universe 内每个标的生成一个 D1 Forecast。升级前的 D2 Forecast 保持不可变并
继续通过历史读取和评价路径访问。Forecast 必须比较 p_up 与 p_down 后明确选择较大的一侧；两者精确
并列时 schema 校验失败。p_neutral 只描述到期收益处于评价噪声带的可能性，不是可选
立场。已完成记录不允许覆盖；同一 as_of 的
queued、running 或 completed live run 会被唯一门禁拒绝，failed run 才允许以新 run_id
重试。历史引用继续指向当时 Wiki 版本和内容哈希。

### 5. 到期评价

当前 D1 与历史 D2 都只在各自冻结 target_date 收盘后评价。请求提供基准日和目标日两条
正收盘观察，每条都带交易日、原始 URL 和 source_hash；服务端校验日期与成熟时间后计算
累计收益，不接受调用方提交的 actual_return。评价只追加到原 forecast_id，并保存两条
价格、观察时间、输入哈希、标签、是否正确和 Brier Score。

Demo 完全不生成正式评价；迁移会清理旧版生成的合成 Demo 评价。成绩单只读取
completed Live 评价批次中的 `OpinionEvaluation`，再按 agent_version 与 model_name
分区；不同角色版本或实际模型永不合并。CIO 使用确定性聚合器及 workflow 版本作为
模型身份，不随研究 LLM 名称变化。成绩同时保留符号命中、超出噪声带后的重大行情
命中和三分类 Brier。sample_size 记录 Agent×日期×指数观察数，但至少覆盖 20 个不同
target_date 的预测截面后才允许比较角色能力。未接入的 Quant 与 Risk Critic 不计算
方向胜率。

### 6. 可信度证据影子快照

prepare 只读取不晚于新 run `data_cutoff` 的 completed Live 历史。表现证据严格按
Agent ID、Agent version、model name、index 和 horizon 分区；解释证据只采用 completed
Reflection 中已被 immutable human review 批准、且没有被 completed successor 取代的
`right_reason / lucky_correct` 后验归因。模型自报 confidence、历史 Opinion weight 和
自由文本评分均不参与。

完整 canonical payload 写入
`WorkflowRun.data_quality.believability_snapshot`，其 SHA-256 写入 frozen state 与
`input_hash`，并用 run ID 生成独立 binding hash。执行前 Python 重新计算两个 hash；
数据库侧 payload、跨 run 移植或交接 seal 被修改时 run 失败。图节点只看到 policy
version 和 hash，不读取 profile，因此 19、20 或更多历史日期下的正式 Forecast 算法
完全相同。

当前 snapshot 固定 `proposed_stage_multiplier=null`、`applied_stage_multiplier=1.0`、
`activation_supported=false`。20 个独立 Live reflection 目标日、当前 exact identity
profile 的相同历史门槛，以及最早连续 10 份 current Reflection 的 approved review
只是最低观察门槛，不是自动调权资格。完整治理与未来 activation 设计见
[可信度加权决策设计](believability.md)。

## 持久化模型

业务 SQLite 保存：

- WorkflowRun：运行时间、状态、数据质量、可信度证据快照、步骤和 input_hash。
- AgentOpinion：每个 Agent × 指数 × 周期的结构化观点。
- Forecast：CIO 最终概率、阈值、引用和版本。
- EvaluationResult：到期后的实际收益与评分。
- PriceObservation：按 demo/live 隔离、带原始 URL 和 source_hash 的可追溯指数收盘观察。
- UserJudgment：用户事前方向、理由、反证、失效条件、截止、输入绑定及私有 Wiki seal。
- UserJudgmentEvaluation：复用可信评价批次生成的符号 / 重大行情结果，不接受用户提交收益。

LangGraph SQLite checkpoint 与业务表分离。checkpoint 保存节点状态，业务表保存产品
事实与审计；v0.1 尚未提供跨进程自动恢复入口。run_id 同时作为工作流 thread 的稳定
关联键；只有完成后的业务记录才可被前端当作正式结果展示。

完成的 run 还可以导出为本地不可覆盖的 `vericouncil.run-bundle/v2`。SQLite 继续承担
查询索引，bundle 中的 `run.json`、`opinions.json`、`forecasts.json` 和
`manifest.json` 用于离线迁移与完整性校验。bundle hash 可以发现导出后的内容变化，但
它不是数字签名，也没有外部可信时间锚。当前格式是结果包，不包含 frozen evidence、
handoff input 或 receipt，因此不能单独重建模型当时可见的全部输入，也不替代底层来源
授权与真实性审查。v2 强制验证可信度 snapshot 的内部 hash、run binding、mode、as_of
和 data_cutoff；显式旧版 v1 manifest 仅保留只读兼容。
当前导出器只生成 v2，并在创建目标目录前验证 seal；缺少 seal 的历史数据库记录会被
明确拒绝，不能被包装成一个随后必然校验失败的 v2 bundle。

需要复核完整文件交接时，`vericouncil.audit-bundle/v1` 会把 frozen evidence、
`input.json`、`INSTRUCTIONS.md`、draft template、实际 drafts、receipt 与结果包封存在
同一目录，并重新计算终检 `output_hash`。它仍明确标注没有发布者认证、未捕获外部
Codex automation 和运行环境。任务编排状态则独立保存在 append-only
`vericouncil.job-execution/v1` hash chain 中，绝不授予模型写权限。

## API 与前端

FastAPI 暴露 latest forecast、meeting detail、agents、scorecards、wiki、runs、
evaluation 和 user-judgments 接口。前端只消费 API，不直接读取 SQLite 或 Markdown。
手动输入的 target 接口有意删除 CIO 方向、概率与理由；封签完成后，detail 接口才返回
对照结果。私有 Markdown 每次读取前重新验证数据库内容哈希、文件哈希与 canonical 渲染。

正式模式的 POST /api/runs 会先冻结全部输入，原子持久化 `queued` run 与
`workflow-task/v1` payload，然后返回 `202`。独立 worker 通过 compare-and-swap
租约认领并执行 LangGraph；前端轮询 run 和精确 task 状态。worker 周期续租，但不能越过
单次 attempt fencing deadline；中断后由新 worker 进入有限重试，并在每次数据库写入前使用 lease token
围栏旧进程。API 重启只 fail closed 那些升级前没有持久 task payload 的遗留运行。
Demo 模式仍同步执行。完整协议见[持久预测任务队列](persistent-task-queue.md)。

页面职责：

- Dashboard：当前五个指数的 D1 概率、阈值和数据状态。
- Meeting Detail：Agent 观点、证据、反证、引用和执行轨迹；显式打开旧运行时可切换其
  已封签的 D2。
- Scorecards：展示当前 Agent/模型版本按指数与周期统计的准确率、Brier Score 和样本口径；
  旧版本记录保留在数据库中，历史版本切片查询仍待补充；Scorecard 是后验证据展示，
  不会自动改写工作流。
- Wiki：条目版本、段落、来源和历史引用次数。
- Runs：运行步骤、排队 / 执行 / 重试状态、尝试次数、失败原因与新建重跑。
- 我的判断：盲判表单、封签回执、委员会对照、私有 Wiki 和个人 shadow 成绩。

## 可靠性约束

- 所有时间使用带时区时间戳，交易逻辑统一为 Asia/Shanghai。
- 节点必须可重试且幂等；外部抓取与持久化分离。
- LLM 只能返回草案，不能直接写数据库或修改 Wiki。
- 关键输入缺失时显式失败，不静默使用旧行情。
- 密钥只从环境变量读取，不写入 checkpoint、日志、Wiki 或前端。
- 新增行业 Agent 必须提供唯一 ID、Pydantic 输出、Wiki owner、引用要求、零副作用
  节点和测试夹具，再接入并行研究阶段。
- 是否调用真实模型不决定运行是否正式；只有行情、资讯、时间截面和引用快照全部通过
  确定性门禁，mode=live 的结果才可发布和评分。

## 技术依据

- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangChain structured output](https://docs.langchain.com/oss/python/langchain/structured-output)

上述链接说明所采用的状态图、checkpoint 和结构化输出能力；forecast-loop 的角色、
预测和审计模型是本项目自身设计。
