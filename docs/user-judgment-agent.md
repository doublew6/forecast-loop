# Manual Input Agent（兼容实现：User Judgment Agent）

## 定位

User Judgment Agent 是 forecast-loop 当前首个 `manual` 来源实现。它让本地操作者先形成
自己的涨跌判断，再与委员会结论比较，并在到期后积累该 Agent 的 track record。

它只是预测 Agent 的一种输入适配器，不代表 forecast-loop 只比较人类与 AI。量化程序、
AI 模型和确定性规则也可以成为 Agent；来源分类与统一接口的目标模型见
[Agent 框架模型](agent-model.md)。当前 manual 路径与 AI/Codex 路径仍是两套受限协议，
不能把本实现描述成任意 Agent 已经即插即用。

它不是第 51 个模型 draft，也不是现有投委会 DAG 的新节点：

- 不进入 `AgentOpinion`、Codex handoff 或模型提示词；
- 不改变 Strategy、Risk Critic、CIO 或 Forecast；
- 不改变 run input hash、50 槽身份矩阵或 25 条 Reflection finding roster；
- 初期权重为 0，只出现在独立判断账本和用户成绩单。

## 决策流程

```text
读取不含 CIO 方向的 Forecast target
                  |
                  v
       选择 up / down + confidence
                  |
                  v
   写核心理由 + 最强反证 + 失效条件
                  |
                  v
 声明是否尚未查看委员会结论（self-attested）
                  |
                  v
      服务端时间、截止、hash、Markdown 封签
                  |
                  v
       显示委员会方向与分歧，但不改 CIO
                  |
                  v
可信到期评价 -> 用户 shadow scorecard
```

UI 先调用 `GET /api/user-judgments/targets`。这个接口只返回 Forecast 的身份、指数、
周期、日期和截止时间，不返回委员会方向、概率或理由。封签成功后的
`UserJudgmentRead` 才返回 `committee_direction`。

这是一道产品层盲判门。用户仍可能从 Dashboard、另一个浏览器或数据库提前查看结论，
因此 `blind_attestation=true` 只是不可变的自我声明，不是密码学证明。只有同时满足
Live、截止前提交和声明盲判的记录才有 `formal_score_eligible=true`；未声明的记录仍可
保存为个人日志，但不进入成绩。

v1 的揭晓粒度是**单个 Forecast**：一条判断封签后会立即显示该条委员会方向，再判断
同一 run 的其他指数或周期时可能受到锚定。系统不会把这种产品门称作整批双盲；需要严格
比较一组独立判断时，下一阶段应增加 batch lock，在整批提交或截止后统一揭晓。

## 输入与截止

创建接口只接受：

```json
{
  "forecast_id": "…",
  "direction": "up",
  "confidence": 0.67,
  "rationale": "至少 20 字的因果解释",
  "counter_evidence": "至少 10 字的最强反方证据",
  "invalidation_condition": "至少 10 字的可观察失效条件",
  "blind_attestation": true
}
```

浏览器不能提交 actor、时间、截止、mode、hash、run 身份或结果。`actor_id` 由
`VERICOUNCIL_USER_JUDGMENT_ACTOR_ID` 在服务端配置。

Live 截止取两者较早：

```text
run.completed_at + VERICOUNCIL_USER_JUDGMENT_WINDOW_MINUTES
target_date 15:00 Asia/Shanghai
```

默认窗口为 120 分钟。run 未完成、已有可信结果或超过截止时一律拒绝；不能把到期后的
回忆包装成事前预测。Demo 允许练习封签，但永不进入正式成绩。

相同 actor × Forecast 只有一份记录。完全相同的请求是幂等重试；不同内容返回 409。
没有 PATCH 或 DELETE。运行 `make migrate` 后，数据库 trigger 也会拒绝对
`user_judgments` 和 `user_judgment_evaluations` 的 UPDATE / DELETE。

## 私有 User Judgment Wiki

数据库记录是 canonical truth，同时确定性渲染一份只写一次的 Markdown：

```text
data/user-wiki/
  decisions/YYYY-MM-DD/
    YYYY-MM-DD-D1-000300-SH-<uuid>.md
```

页面包含稳定 section：

- `prediction`
- `rationale`
- `counter-evidence`
- `invalidation`
- `audit`

每份记录同时保存 canonical content SHA-256 和 Markdown artifact SHA-256。文件发布
使用受根目录约束的临时文件、`fsync` 与 exclusive hard link，拒绝 symlink、路径逃逸、
覆盖和非普通文件；默认目录权限为 `0700`，文件为 `0400`。

它与仓库内正式 `wiki/` 有意分离：

- 每日主观看法是 append-only event，不是 evergreen framework；
- `WikiCatalog` 不会把它冻结进未来预测，避免自证循环；
- 它不会自动产生 Lesson 或晋升正式 Wiki；
- 未来 Reflection 可以引用这份 seal，另行提出可复用 Lesson，但不能改写原记录。

`GET /api/user-judgments/{id}/wiki` 会在返回 Markdown 前重新验证数据库内容哈希、
文件哈希和 canonical 渲染。

## 评价与成绩

操作者不能提交 actual return。只有 completed Live evaluation batch 使用已有可信
`EvaluationResult` 生成 `UserJudgmentEvaluation`，冻结：

- actual return / label；
- 符号是否命中；
- 噪声带外重大行情是否命中；
- observation hash、batch、policy version 和评价 hash。

成绩单复用 `/api/agents/user_judgment_agent/scorecard?horizon=D1|D2`，但首版没有要求
manual Agent 提交完整 `up / neutral / down` 概率，所以明确不计算 Brier 或 calibration。
同日五指数不会让最低展示门槛从一个日期虚增为五个；仍需至少 20 个独立目标日才允许
做能力结论。

manual Agent 成绩当前不进入 `believability-shadow/v1`、Strategy 或 CIO。未来若要纳入
决策，必须按其能力声明完成适用的事前推理 rubric、独立 shadow、版本化政策重放和人工
activation event。

## API 与 CLI

```text
GET  /api/user-judgments/targets
POST /api/user-judgments
GET  /api/user-judgments
GET  /api/user-judgments/{id}
GET  /api/user-judgments/{id}/wiki
GET  /api/agents/user_judgment_agent/scorecard
```

理由建议通过文件交给 CLI，避免进入 shell history：

```bash
uv run forecast-loop judgment record \
  --forecast-id <id> \
  --direction up \
  --confidence 0.67 \
  --rationale-file ./input/rationale.md \
  --counter-evidence-file ./input/counter.md \
  --invalidation-file ./input/invalidation.md \
  --blind

uv run forecast-loop judgment verify <judgment-id>

uv run forecast-loop judgment export <judgment-id>
uv run forecast-loop judgment verify data/judgment-bundles/<judgment-id>
```

等价 Make 入口是 `make judgment-record ARGS="…"`、
`make judgment-export ARGS="<judgment-id>"` 和
`make judgment-verify ARGS="<judgment-id|bundle-path>"`。

Bundle 默认不导出 `actor_id`，但会保存理由、反证、失效条件、Forecast/input 绑定、
AgentSpec 和适用的可信评价。Demo、未声明盲判的 Live 存档、正式 shadow 使用不同的
`record_class`；正式 shadow 在评价完成前拒绝导出，离线验证也 fail closed。Bundle
只单向引用 committee run，绝不改写已发布的 run bundle。完整格式与哈希层见
[Portable User Judgment bundles](judgment-bundle.md)。

默认服务只绑定 loopback。若将写接口暴露到其他机器，必须先增加 operator
authentication、TLS 和网络访问控制；仅凭 `actor_id` 环境变量不是远程身份认证。
