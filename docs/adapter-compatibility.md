# Provider 与 data adapter 兼容性

forecast-loop 为外部贡献者提供两个不依赖密钥的最小示例，以及可从 `app.testing` 导入的
兼容性测试工具。示例数据是仓库内生成的合成夹具，不是行情、投研结论或第三方网页的
镜像。

## 官方示例

| 示例 | Port | 目标契约 | direction | probability | reasoning | citation |
| --- | --- | --- | --- | --- | --- | --- |
| `PublicJsonSignalProvider` | `AgentSignalSource` | `forecast-loop.signal-envelope/v1` 的 host-bound draft 输入 | required | multiclass | structured | frozen |
| `PublicJsonEvidenceAdapter` | `EvidenceSnapshotSource` | `forecast-loop.frozen-evidence-snapshot/v1` | N/A | N/A | N/A | frozen `EvidenceItem` provenance |
| 内置 `LocalJsonEvidenceSnapshotSource` | `EvidenceSnapshotSource` | `FrozenEvidenceSnapshot` 当前 v1 形态 | N/A | N/A | N/A | frozen `EvidenceItem` provenance |

这里的 capability 是“该 adapter 能完整提交什么”，不是决策权限。Signal provider
返回的仍是 untrusted `AgentSignalDraft`；宿主按已批准的 `AgentSpec` 注入 target、run
input hash、ParticipationPolicy、可信 provenance、deadline 和 accepted-at，验证后才
封签 `SignalEnvelope`。Data adapter 的 citation 表示带 URL、时间和内容哈希的证据来源，
不是 Agent 的观点引用。

当前已知限制：

- 两个官方示例都只读取一个本地 JSON 文件，不抓取远程 API，也没有 retry、分页或缓存；
- 合成 direction、probability 和 evidence 仅用于契约测试，不得进入 Live run；
- `LocalJsonEvidenceSnapshotSource` 读取的是已经组装好的快照，不负责取得或授权原始资料；
- 当前只承诺上述 v1 形态；新增字段或新 schema 必须作为新版本进入矩阵，不能静默改变
  已封签历史记录。

每个示例的 `AdapterManifest` 同时发布 adapter version、契约版本、四类 capability、
数据许可、只读边界、evidence cutoff 责任和已知限制。测试会检查 manifest 与实际输出
一致，不能用文档声明掩盖缺失字段。

## 只读与写入边界

`PublicJsonSignalProvider` 只允许：

1. 读取调用方明确传入的单个普通文件；
2. 验证 UTF-8 JSON、schema、来源、许可、时间和 bundle hash；
3. 在内存中返回完整 draft batch。

它不写数据库、Wiki、run、Envelope、上游文件或日志，也不能自报 AgentSpec、
ParticipationPolicy 和可信 provenance。批次中任一记录缺失或损坏时，整个调用抛出
`AgentSignalSourceError`，不得返回剩余的“看起来可用”记录。

`PublicJsonEvidenceAdapter` 只允许读取同样明确传入的单个普通文件，并在内存中返回快照。
它不采集网页、不修改快照、不回填缺口，也不更新上游数据。路径为 symlink、内容为空、
JSON/schema 损坏、许可不匹配或任一时间/哈希门禁失败时，必须抛出
`EvidenceSnapshotSourceError` 的具体子类。

真实 adapter 可以有自己的只读网络 transport，但若要写缓存或 checkpoint，必须在
manifest 中改为非只读并明确列出写入目标；这类实现不再满足“官方只读示例”门禁。无论
如何都不得写回外部数据所有者、生产数据库或交易系统。

## Evidence cutoff 责任

Signal provider 与宿主承担共同责任：

- provider 只能把在请求 `as_of` 前已观察到的冻结 citation 放进 draft；
- 宿主必须再次检查 citation `observed_at <= target.data_cutoff`，并绑定实际 run cutoff；
- provider 的 bundle `as_of` 必须与请求精确相等，不能用最近一次缓存静默代替。

Evidence adapter 承担快照内部 cutoff 责任：

- `event_time <= published_at <= ingested_at <= data_cutoff <= as_of <= created_at`；
- 交易日历与行情的 observed/ingested 时间不得晚于 cutoff；
- 所有时间必须带时区，不能把无时区本地时间当成 UTC 或 Asia/Shanghai；
- outer bundle、snapshot、EvidenceItem 和来源记录的 SHA-256 必须逐层可重算；
- 缺行情、目标交易日、来源 URL、许可或哈希时失败关闭，不得用 neutral、默认概率或旧值
  填补。

宿主仍会独立调用 Live snapshot validator。adapter 通过自己的检查，不代表可以绕过 run
级 allowlist、input hash 或审计门禁。

## 可复用 contract test kit

工具包位于 `backend/app/testing/adapter_compat.py`，安装项目后可从 `app.testing` 导入：

```python
from app.testing import (
    assert_evidence_adapter_compatible,
    assert_fails_closed,
    assert_signal_provider_compatible,
)

report = assert_signal_provider_compatible(
    source,
    as_of=as_of,
    manifest=manifest,
)

assert_fails_closed(
    lambda: malformed_source.load_signal_drafts(as_of=as_of),
    expected_error=AgentSignalSourceError,
    label="missing direction",
)
```

正向检查覆盖来源身份、时间语义、时区、必填值、许可、内容哈希和 capability。Transport
的损坏方式因实现不同，由 adapter test 构造 missing field、naive time、晚于 cutoff、
license mismatch、tampered hash、不可访问源等 probe，再统一交给 `assert_fails_closed`。

官方测试文件
`backend/tests/test_adapter_examples_compatibility.py` 演示完整写法，包括“第二条损坏时不
返回第一条”的 all-or-nothing 检查。GitHub Actions 有独立步骤运行这组 compatibility
tests；全量 backend suite 仍会再次覆盖它们。

## 数据许可

两个 JSON 夹具逐份声明：

- SPDX: `CC0-1.0`；
- origin: forecast-loop synthetic compatibility fixture；
- `redistribution_allowed=true`；
- 规范的仓库 source URL 与用途提醒。

这只许可仓库内的合成值。贡献真实 provider 时，必须记录上游数据许可、保留必要
attribution，并确认能否把原文或样本提交到公开仓库。不明确、禁止再分发或依赖付费内容的
数据不能作为官方 fixture；可以由用户在本地配置，但 CI 必须继续使用可再分发的合成夹具。
