# Compatibility policy

forecast-loop 使用语义化版本；`0.x` 属于 early release，pre-1.0 公共契约会尽量保持向后可读，
只有明确记录的版本边界才能改变写入格式或默认行为。本文描述 `v0.1.0` 的最低兼容承诺，
不把内部 Python 函数、数据库实现细节或未记录字段误当成稳定 API。

## Supported release line

| Release line | Status | Security fixes | Compatibility |
| --- | --- | --- | --- |
| `v0.1.x` | Supported early release | Latest patch | 本文所列公共契约 |
| Default branch | Development | Best effort | 可能包含下个版本变更 |
| Earlier snapshots | Unsupported | No | 只保留明确列出的历史读取器 |

`v0.1.0` 的运行基线是 Python 3.11+、Node.js 20.19+ 和 SQLite。CI 与发布流程使用锁定
版本构建，但库使用者仍应以 `pyproject.toml`、`uv.lock` 和
`frontend/package-lock.json` 为安装事实。

## Stable in v0.1.x

以下接口在 `v0.1.x` 内不会无说明地删除或改变含义：

- `forecast-loop` CLI 的文档化命令、退出状态以及离线验证行为；
- `forecast-loop.agent-spec/v1`、`forecast-loop.participation-policy/v1` 和
  `forecast-loop.signal-envelope/v1`；
- 已发布的 run bundle、audit bundle、User Judgment bundle 和 benchmark fixture；
- 数据库迁移的前向升级路径；
- `prepare -> drafts.json -> finalize` 文件交接边界；
- 结果封签、内容哈希、证据截止时间和失败关闭语义。

新增可选字段必须有安全默认值。读取器可以接受新字段，但不能跳过版本、来源、时间或哈希
校验。写入格式的语义变化必须使用新的 schema、protocol 或 policy 版本。

## Historical compatibility

`v0.1.0` 默认写入当前格式，同时保留以下历史读取能力：

- handoff protocol `1.0.0` 和 reflection protocol `1.0.0` 可继续终检与审计；
- 新 handoff/reflection 包默认使用 protocol `2.0.0`；
- `vericouncil.user-judgment/v1` 与 `user-judgment/v1` 保持字节级验证；
- 新 User Judgment 使用 `forecast-loop.user-judgment/v2` 与
  `user-judgment/v2`；
- `vericouncil.*` schema ID、`VERICOUNCIL_*` 环境变量，以及
  `signalrace`、`vericouncil` CLI 名称是历史兼容接口。

兼容别名在整个 `v0.1.x` 系列中保留。未来移除必须进入明确的破坏性版本说明，并在至少
一个发布周期前标记弃用。历史 artifact、receipt 或 hash domain 不会被原地重写。

## Database and local data

升级前必须备份 SQLite、checkpoint、Wiki 和本地 handoff 根目录。数据库迁移是
forward-only 操作；不要用旧二进制直接打开已升级的唯一生产副本。需要回退时，应恢复
升级前备份到隔离目录，而不是修改已封签记录或手工删除 migration revision。

Demo、测试 fixture 和公开 benchmark 不构成对真实数据 adapter 的兼容保证。外部
adapter 必须通过文档化 compatibility test kit，并自行声明来源、许可、时间语义和缺失值
策略。

## Breaking-change process

任何破坏性变化都必须同时完成：

1. 在 `CHANGELOG.md` 标记 `Breaking`；
2. 更新本文件和相应 JSON Schema；
3. 提供升级说明、旧 fixture 与回归测试；
4. 证明旧数据是继续只读、显式迁移，或被清楚拒绝；
5. 不通过静默回退、忽略未知版本或重算历史结果来“兼容”。

发现未记录的兼容回归时，请附上最小脱敏 artifact、版本和验证命令提交 Issue；安全问题
按照 [SECURITY.md](../SECURITY.md) 私密报告。
