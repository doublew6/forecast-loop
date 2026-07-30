# Upgrading to v0.1.0

本文面向从 `v0.1.0` 之前的开发快照升级的本地运行者。升级不会自动公开本地数据，也
不会把 Demo、旧预测或用户判断重新计分。

## 1. Freeze and back up

停止 API、worker 和定时任务，记录当前 commit，并为以下内容创建独立备份：

- 主 SQLite 数据库与 LangGraph checkpoint；
- `data/handoffs`、reflection、market snapshot 与 evidence snapshot；
- `data/wiki/` 中的正式 Agent Wiki 和私有 User Judgment Wiki；
- 本地环境配置，但不要把密钥复制到仓库或 Issue。

如果已有 recovery bundle，先运行：

```bash
make recovery-verify ARGS="./backups/backup-bundle"
```

完整恢复边界见 [recovery.md](recovery.md)。

## 2. Install the locked release

检出经过验证的 `v0.1.0` annotated tag，并核对发布页的 `SHA256SUMS` 与 provenance
attestation。源码安装使用锁文件：

```bash
uv sync --frozen --all-groups
uv sync --frozen --reinstall-package forecast-loop
cd frontend
npm ci
cd ..
```

主命令现在是 `forecast-loop`。`signalrace` 和 `vericouncil` 仍是 `v0.1.x` 的兼容别名；
现有 `VERICOUNCIL_*` 环境变量、历史 schema ID 和 hash domain 不需要批量重命名。

## 3. Upgrade and verify the database

始终先对备份副本或 staging 副本演练，再升级唯一运行副本：

```bash
make database-status
make migrate
make database-status
make migration-smoke
```

`database-status --deep` 必须显示单一当前 head，且完整性检查通过。不要在 schema 落后、
出现多个 head 或数据根目录不可读时继续启动正式预测。

## 4. Verify historical artifacts

抽样验证升级前的结果包、审计包和 User Judgment bundle：

```bash
uv run forecast-loop run verify ./exports/run-bundle
uv run forecast-loop audit verify ./exports/audit-bundle
uv run forecast-loop judgment verify ./exports/judgment-bundle
```

兼容性变化：

- 新 handoff/reflection 默认写 protocol `2.0.0`，旧 `1.0.0` 仍可读取和终检；
- 新 User Judgment 写入 v2 schema/policy，旧 v1 Markdown 与 hash 验证器保持冻结；
- `AgentSpec`、`ParticipationPolicy` 与 `SignalEnvelope` 使用各自 v1 公共 schema；
- 未知 protocol、schema/policy 不匹配和被篡改内容继续失败关闭。

详见 [compatibility-policy.md](compatibility-policy.md)。

## 5. Validate the application

```bash
make test
make lint
make build
make docker-config
```

有 Docker 环境时再运行隔离的 `make docker-smoke`。启动后确认：

- API 与 Web 仍只绑定预期网络接口；
- Demo 被明确标注，Live 不会回退到 Demo 假装成功；
- 旧预测、评价、Wiki 快照和用户封签数量未变化；
- worker、handoff 和 finalize 使用同一 release 版本。

## Rollback

不要移动 `v0.1.0` tag、修改已发布 release asset，或让旧代码写入已经升级的唯一数据库。
回退步骤是停止服务、保留失败现场、把升级前备份恢复到新的隔离目录，再使用之前记录的
commit 启动验证。若升级已经产生新的正式记录，应先导出并保留它们，不能通过删除记录
制造“从未发生”的历史。
