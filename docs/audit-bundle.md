# Portable audit bundles

`vericouncil.audit-bundle/v1` 把一次已完成的文件交接与对应结果包绑定到一个不可覆盖的
本地目录。它用于迁移、复核和发现未重封的损坏，不是数字签名，也不能证明发布者身份。

## Export

先从本地 SQLite 导出完成的结果：

```bash
uv run forecast-loop run export <run-id>
```

再把同一 run 的 handoff 与结果绑定：

```bash
uv run forecast-loop audit export <job-dir-or-run-id> \
  --handoff-root data/handoffs \
  --run-bundle data/exports/<run-id> \
  --output-root data/audit-bundles
```

导出器只接受 `handoff-root` 的直属 UUID 目录和已经通过
`vericouncil.run-bundle/v2` 验证的结果包，不覆盖已有目标，也拒绝 group/world
writable 的输出目录。

## Contents

```text
data/audit-bundles/<run-id>/
├── manifest.json
├── handoff/
│   ├── input.json
│   ├── evidence_snapshot.json
│   ├── INSTRUCTIONS.md
│   ├── drafts.template.json
│   ├── drafts.json
│   └── receipt.json
└── results/
    ├── manifest.json
    ├── run.json
    ├── opinions.json
    └── forecasts.json
```

验证器会检查：

- 文件集合、大小、regular-file 与 symlink 边界；
- frozen evidence 的内容哈希和 Live 历史时间约束；
- input、draft、receipt 的 raw/canonical seal；
- run ID、mode、input hash 与结果计数；
- 按终检时排序规则重新计算的 `receipt.output_hash`；
- 每个 artifact 和总 manifest 的 SHA-256。

复制或接收 bundle 后运行：

```bash
uv run forecast-loop audit verify data/audit-bundles/<run-id>
```

## Honest limits

Manifest 固定声明：

- `publisher_authentication: none`
- `external_orchestration_captured: false`
- `runtime_environment_captured: false`

因此 SHA-256 只能发现未重新计算 manifest 的损坏。只有把导出时的 `bundle_hash`
另行锚定到可信渠道，才能判断之后是否发生变化；独立 bundle 无法区分原件与攻击者
重封后的版本。它不包含外部 Codex Desktop automation 的私有任务状态，也不封存 Python
环境、依赖 lock 的实际安装结果、操作系统或可信时间戳。若要公开发布，应另行使用
发布者签名、透明日志或可信对象存储版本锁定，并先确认行情、资讯与引用内容的再分发许可。
