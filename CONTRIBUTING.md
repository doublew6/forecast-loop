# Contributing to forecast-loop

感谢你考虑为 forecast-loop 贡献代码、文档、测试或研究方法。forecast-loop 的目标是建立
可验证、可复现、可审计的 AI 预测与复盘流程，而不是执行交易或包装不可验证的模型观点。

## 开始之前

- 一般问题、功能建议和缺陷请使用 GitHub Issues。
- 安全漏洞不要提交公开 Issue；请按照 [SECURITY.md](SECURITY.md) 私下报告。
- 较大的架构、数据契约或治理变更，请先创建 Issue 说明问题、边界和迁移方案，避免双方
  在目标尚未对齐时投入大量实现工作。
- 提交真实市场数据前，请确认其许可允许再分发。优先使用最小、脱敏且可公开复现的
  fixture。

## 项目边界

贡献必须保留以下边界：

- forecast-loop 是研究、证据和审计系统，不得增加自动下单、账户操作或生产交易数据库写入。
- 外部专有或私有上游数据源只能通过只读适配器接入；核心领域逻辑不得依赖某个私有
  目录、用户名或机器。
- 模型输出是不可信的结构化草案。时间、来源、schema、哈希、聚合、持久化和评分必须由
  确定性代码完成。
- 保留 `prepare -> drafts.json -> finalize` 信任边界。HTTP 接口不得直接触发 Codex
  文件模式的 finalize，也不得让模型修改输入包、receipt、Wiki、checkpoint 或上游数据。
- 正式预测只能使用证据截止时间之前发布的冻结证据和 Wiki 版本；后验学习不得改写历史
  预测。
- Web 和 API 默认保持本机回环绑定。公开部署示例必须同时考虑认证、TLS、访问控制和
  私密运行产物。

## 本地开发

需要 Python 3.11+、`uv`、Node.js 20.19+ 和 `make`。按照仓库
[README.md](README.md) 的 Quick start 安装依赖：

```bash
cp .env.example .env
make install
make migrate
```

分别运行后端、持久任务 worker 和前端：

```bash
make backend
make worker
make frontend
```

默认 Demo provider 不需要模型密钥。不要把 `.env`、数据库、checkpoint、handoff、
真实快照、日志或其他运行产物提交到仓库。

仓库中的 branch 名、commit message、Issue、Pull Request、评论和附件都会成为公开内容。
不要在公开渠道粘贴疑似密钥、个人路径、私有项目名称、内部 schema、机器/网络信息或
未确认可再分发的数据；安全问题使用 `SECURITY.md` 所述私密渠道。

## 提交变更

1. 从最新默认分支创建范围单一的分支。
2. 保持提交小而清晰；不要把格式化、生成文件和无关重构混入功能变更。
3. 为行为变更增加测试，为用户可见变化更新文档和 `CHANGELOG.md` 的
   `Unreleased` 部分。
4. 数据库 schema 变更必须附带 Alembic migration，并验证已有数据可升级。
5. 新适配器应有明确契约、失败关闭行为和不依赖外部密钥的测试 fixture。
6. 在 Pull Request 中说明问题、方案、验证步骤、兼容性影响和安全/数据边界。

Provider 或 data adapter 贡献请从
[官方公开示例与 compatibility test kit](docs/adapter-compatibility.md) 开始，并把
direction、probability、reasoning、citation 能力、数据许可、只读/写入边界和 evidence
cutoff 责任写入 `AdapterManifest`。CI fixture 必须可再分发，不得依赖个人绝对路径或
secret。

提交 Pull Request 前运行：

```bash
git add <explicit-files>
make public-preflight-staged
make lint
make test
make build
make public-preflight-range
```

如果某项检查因环境限制无法执行，请在 Pull Request 中明确说明，而不是省略。
维护者还必须使用位于仓库外的私有边界规则安装 hooks：

```bash
make install-hooks PRIVATE_BOUNDARY_FILE=/absolute/private-patterns
```

该文件不得加入 Git、日志或 CI artifact。涉及潜在私有上下文的变更必须先在私有 staging
环境审查，再以中性术语和合成 fixture 导出；公开 PR 创建后才失败已经无法撤销首次披露。

## 代码与文档约定

- Python 和 TypeScript 代码、标识符、文件名及代码注释使用英文。
- 面向用户的中文界面和中文文档可以使用中文。
- 不记录模型生成的自报身份作为可信事实；模型、prompt、证据、策略和 Wiki 版本应由
  可验证的输入与配置绑定。
- 新增时间字段时需明确时区与语义，区分事件时间、来源发布时间和系统可见时间。
- 不用 hash 代替来源真实性、完整性或授权校验；hash 只证明冻结后的内容未改变。

## 许可

除非你明确标注为 “Not a Contribution”，你有意提交并被项目接受的贡献将按照
[Apache License 2.0](LICENSE) 授权，不附加额外条款。提交贡献前，请确保你有权以该
许可提供相关内容，并保留适用的第三方版权和归因信息。

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
