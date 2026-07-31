# Portable LLM job manifests

forecast-loop 使用兼容 schema `vericouncil.job/v1` 的 JSON 文件把任务意图保存在
仓库中，同时避免绑定某个桌面应用或系统调度器的私有状态。Manifest 是可审查的工作流
声明，不会自行授予权限、调用模型或安装定时任务。

公开示例见 [`jobs/daily-forecast.example.json`](../jobs/daily-forecast.example.json)。

## Contract

每份 Manifest 包含：

- `schema`：固定为 `vericouncil.job/v1`；
- `name`：可移植的小写任务 ID；
- `schedule`：严格五字段 cron；
- `timezone`：已安装的 IANA timezone；
- `profile`：运行方定义的配置名，例如 `formal`；
- `prepare.command`：受 allowlist 限制的 `forecast-loop forecast prepare` argv；
- `draft`：runner、模型策略、纯草案阶段 Markdown prompt 和唯一声明的写入目标
  `data/**/drafts.json`；prompt 不得要求执行 prepare 或 finalize；
- `finalize.command`：受 allowlist 限制的 `forecast-loop forecast finalize` argv。

`vericouncil.job/v1` 是兼容 schema ID。新的示例 Manifest 使用
`prompts/daily-forecast-v2.md`；运行方为自己的调度器选择独立服务 ID，公共契约不规定
机器、任务名称或执行时间。这里的 `v2` 是独立的 prompt 内容版本，不等于 handoff
protocol；当前 prepare 仍会生成 D1-only handoff v3。示例中的 cron 仅用于验证五字段
schema，不是推荐或生产日程。

两步必须显式声明 `--mode demo|live`，finalize 必须以 `{job_dir}` 结尾；它是 runner
在 prepare 成功后替换的唯一显式参数。核心 parser 会拒绝任意可执行文件、`env sh -c`、
解释器 `-c` 和未知参数，不执行通用字符串模板，也不把 Manifest 命令传给 shell。

## Validation

```bash
uv run forecast-loop jobs validate \
  jobs/daily-forecast.example.json \
  --project-root .
```

校验器会拒绝未知字段、重复 JSON key、symlink、非五字段 cron、无效时区、allowlist
之外的命令、越界 prompt，以及 `drafts.json` 之外的 Codex 写目标。

## Scheduler adapters

系统级调度器应当只唤起一个经过运行方审核的 dispatcher。业务 prepare、draft 和
finalize 仍由 dispatcher 按 Manifest 执行，不能复制进 plist 或 systemd timer。

```bash
uv run forecast-loop jobs render jobs/daily-forecast.example.json \
  --target launchd \
  --dispatcher your-forecast-loop-dispatcher \
  --host-timezone UTC \
  --output-dir build/launchd \
  --project-root .
```

```bash
uv run forecast-loop jobs render jobs/daily-forecast.example.json \
  --target systemd \
  --dispatcher your-forecast-loop-dispatcher \
  --output-dir build/systemd \
  --project-root .
```

渲染器不会覆盖已有文件。launchd 使用宿主机时区，因此要求显式确认它与 Manifest
完全一致；systemd timer 会把 IANA timezone 写入 `OnCalendar`。

Codex Desktop automation、CLI runner 或其他 Agent 平台可以读取同一 Manifest 和 prompt，
但仍需通过各自支持的界面安装、授权和调度。不要把应用内部的 automation 状态文件当作
forecast-loop 的公开 API。

## Append-only execution state

仓库提供的 `JobExecutionStore` 是一个治理状态机，不是模型 runner。它不会执行
Manifest 中的命令、启动 subprocess、调用 FastAPI 或连接模型 API。运行方仍通过受支持
界面执行确定性的 prepare/finalize 和外部 Codex 草案，只把每一步结果登记到隔离的
`data/job-executions/`：

```bash
uv run forecast-loop jobs begin jobs/daily-forecast.example.json \
  --idempotency-key 2026-07-24-evening \
  --project-root .
```

返回 `prepare_pending` 后，运行 Manifest 的 `prepare.command`，取得唯一 `job_dir`，再登记：

```bash
uv run forecast-loop jobs prepared <execution-id> <job-dir> \
  --project-root .
```

输出中的 `external_draft` 是给受支持 Codex 界面的最小授权说明：运行方应只授予 prompt、
input、instructions 和 template 的读取权限，以及该 handoff 的 `drafts.json` 创建权限。
状态机只声明并校验这个边界，实际权限仍由运行方负责实施。模型完成后：

```bash
uv run forecast-loop jobs draft-ready <execution-id> --project-root .
```

状态变为 `finalize_pending` 后，运行 Manifest 的 `finalize.command`；确定性 receipt
生成后登记终态：

```bash
uv run forecast-loop jobs finalized <execution-id> --project-root .
uv run forecast-loop jobs status <execution-id> --project-root .
```

每个 execution 由 Manifest、prompt 内容和 idempotency key 共同绑定。状态 revision
只追加不覆盖，并形成 SHA-256 hash chain；input、instructions、template、draft 与
receipt 保存 raw/canonical seal。状态机在发出 external instruction 和收稿时都会从
冻结 request 重新生成并精确检查 instructions/template。`state-root` 必须与
`handoff-root` 分离，绝不能授予 Codex 写权限。

`completed` 表示状态机看到了与冻结 input/draft 一致、满足终态字段约束的 operator
receipt；它不会自行查询数据库或重新计算最终结果，因此不是 finalize 已执行的独立证明。
需要验证输出时，继续导出结果包并运行 `forecast-loop audit export` / `audit verify`；
若要抵抗主动重封，还必须把导出时的 hash 另行签名或可信锚定。

## Trust boundary

Runner 必须保留以下顺序：

1. 执行 `prepare.command` 并取得唯一 `job_dir`；
2. 只读 handoff 的输入、说明和模板；
3. 模型只写 `<job_dir>/drafts.json`；
4. 将同一个 `job_dir` 传给 `finalize.command`；
5. 原样报告确定性校验错误，不修改可信输入绕过失败。

Manifest 不得增加交易执行、账户访问、生产数据库写入或上游数据写权限。
