# 持久预测任务队列

API provider 的 Live 运行采用数据库队列，不在 FastAPI 进程内启动后台线程。API 先冻结
证据、Wiki、可信度快照和输入哈希，再把完整 `workflow-task/v1` payload 与
`WorkflowRun` 一起持久化并返回 `202`；独立 worker 才能认领和执行任务。

```text
POST /api/runs
  -> freeze sealed input + execution manifest
  -> atomically persist WorkflowRun + WorkflowTask(queued)
  -> 202

forecast-loop worker run
  -> claim lease
  -> execute one sealed attempt
  -> atomically persist forecasts + completed run + completed task
```

## 本地运行

完成迁移后，在 API 之外启动一个 worker：

```bash
make migrate
make backend
make worker
```

也可以直接运行：

```bash
uv run forecast-loop worker run
uv run forecast-loop worker run --once --worker-id local-check
```

Docker Compose 已包含独立的 `worker` service；API、worker 共用本地 `data/` 卷，两个
HTTP 端口仍只绑定 `127.0.0.1`。worker 不监听网络端口。

## 状态与重试

持久任务状态为：

- `queued`：输入已冻结，等待 worker；
- `running`：某个 worker 持有有效租约；
- `retry_wait`：一次尝试失败或中断，等待有限重试；
- `failed`：尝试次数耗尽，正式失败；
- `completed`：结果与 run 已原子落库。

每次 claim 都增加 `attempt_count`，并写入 worker、随机 lease token、租约到期时间和本次
attempt fencing deadline。worker 周期续租，但不能把租约延长到 deadline 以后。默认最多
尝试 3 次、租约 60 秒、单次 deadline 1800 秒、重试间隔 5 秒，可用以下环境变量调整：

```dotenv
FORECAST_LOOP_TASK_MAX_ATTEMPTS=3
FORECAST_LOOP_TASK_LEASE_SECONDS=60
FORECAST_LOOP_TASK_TIMEOUT_SECONDS=1800
FORECAST_LOOP_TASK_RETRY_DELAY_SECONDS=5
```

队列使用 compare-and-swap claim；并发 worker 只有一个能获得同一任务。正式结果落库
时，lease CAS、forecasts、run 完成和 task 完成位于同一数据库事务。租约过期后，旧
worker 即使恢复运行也不能写入；heartbeat 会触发过期回收，另一个 worker 可把
`retry_wait` 任务重新认领。每次有限尝试使用独立 checkpoint stream，并从同一个已封签
payload 重新开始，因此仍在运行的旧进程不能污染新尝试的 reducer 状态。

`FORECAST_LOOP_TASK_TIMEOUT_SECONDS` 是执行资格和写入围栏的 deadline，不会强制终止
已经进入 provider/模型 SDK 的 Python 调用。若一次调用永久阻塞，需要另一个 worker
立即接手重试；单 worker 会等该调用返回后才能继续轮询。无论旧调用何时返回，过期
lease 都无法发布结果。

任务 payload 还封存 provider 类型、endpoint 哈希、模型映射、prompt、workflow/schema
版本和相关 timeout/retry 配置。worker 的当前 execution manifest 必须逐项一致，否则
任务直接 fail closed，不能用另一套配置在旧 input hash 下执行。

## 幂等与重复发布保护

Live `POST /api/runs` 支持 `Idempotency-Key`。未提供时，服务按规范化 `as_of` 生成稳定
键。同一个键和同一个时间截面会返回原 run；同一个键不能绑定另一时间截面。数据库还
保留 Live `as_of` 唯一门禁、每个 run 的 forecast identity 唯一门禁，以及任务到 run
的一对一约束。因此 API 重试、并发请求和 worker 恢复都不能发布第二份相同正式运行。

`GET /api/runs` 的每项包含可选 `task`：

```json
{
  "status": "queued",
  "task": {
    "status": "retry_wait",
    "stage": "retry_wait",
    "attempt_count": 1,
    "max_attempts": 3,
    "available_at": "2026-07-27T15:06:00+08:00",
    "last_error": "provider timeout"
  }
}
```

前端运行页保留 run 的业务状态，同时展示精确任务状态、执行阶段、尝试次数和最近错误。

## 恢复边界

- 有 `WorkflowTask` 的 queued/running run 不会被 API 重启标成失败；恢复权属于 worker。
- 升级前遗留且没有冻结 task payload 的 queued/running run 无法安全重建，启动时仍会
  fail closed。
- 本地 SQLite 适合单机自托管。多主机部署应使用支持并发事务的共享数据库，并单独完成
  备份、监控、认证和 TLS 设计。
- 队列只负责可靠执行，不把公网暴露变安全；当前 API 仍是 loopback-first。
