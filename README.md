# forecast-loop

**语言 / Language：简体中文 · [English](README_EN.md)**

> Forecast. Explain. Resolve. Learn.

forecast-loop 是一个**可验证的预测 Agent 框架**。预测信号可以由人直接输入，也可以由
确定性量化程序或 AI 模型生成；无论来源如何，都要遵守同一套事前封签、证据冻结、
到期评价和审计规则。

项目受到 Ray Dalio 公开阐述的
[Idea Meritocracy / Believability-Weighted Decision Making][principles] 启发：
可信度应来自相关领域的长期记录，以及可检验、可反证的因果解释。forecast-loop 借鉴
这一治理思想，但与 Bridgewater 没有隶属或背书关系。

这里的 **Forecast** 是结果揭晓前封签的预测；**Loop** 是预测、理由、结果、反省、
Lesson 与下一次预测构成的持续闭环。项目关注的是可复验的 track record，而不是
Agent 数量、表达气势或单次盈亏。

## 核心闭环

```text
声明信号来源、身份和版本
  -> 独立形成方向、概率与理由
  -> 写出最强反证和失效条件
  -> 在结果揭晓前冻结输入、引用和输出
  -> 到期后评价方向与概率
  -> 区分 right reason / lucky correct / wrong / unresolved
  -> 经重放、人工复核和版本门禁形成未来 Lesson
```

无论信号由谁产生，系统都坚持以下边界：

- 模型输出只是待校验草案；时间、schema、哈希、引用、聚合、持久化和评分由确定性
  Python 负责。
- 预测只能使用 evidence cutoff 前已发布的证据和 Wiki 版本，历史快照与预测不可重写。
- 历史成绩不会自动改变正式权重；策略变更必须经过足量样本、重放和人工批准。
- 外部数据只能通过只读 adapter、快照或副本接入，不能写回数据所有者。
- 不连接券商、不下单，也不写入任何上游生产数据库。

## 输入方式

不同来源共享相同的身份、版本、封签和评价纪律：

| 来源 | 输入内容 | 约束 |
| --- | --- | --- |
| 人工 | 方向、理由、最强反证和失效条件 | 提交前不显示系统预测，封签后不可改写 |
| AI 模型 | 结构化方向、概率、理由和引用草案 | 只能生成草案，必须通过确定性校验 |
| 量化程序 | 带代码、参数、数据和模型哈希的信号包 | 只读接入，默认用于 shadow 评价 |
| 确定性规则 | 校验、聚合、持久化和评分结果 | 规则必须版本化并可重放 |

人工输入与其他来源使用相同的预测目标和可信市场结果，因此可以积累独立成绩；但不同
来源可能绑定不同证据，系统不会假设它们具备完全相同的信息集或评分能力。

## 研究配置

- 默认标的：沪深300、中证500、中证1000、创业板指、科创50。
- 可选市场：通过版本化 Market Universe 配置港股、美股、指数或个股。
- 预测周期：新运行只写入下一交易日（D1）预测；升级前已封签的 D2 预测继续可读、可评价。
- 事前方向：必须在上涨 / 下跌中二选一；结果按上涨 / 小波动 / 下跌评价。
- 产品形态：本地单用户研究台，不构成投资建议，不生成自动交易指令。

每次运行都会冻结目标 Universe、市场时钟、证据截止时间和研究版本。可信度证据按来源、
版本、模型、标的和预测周期分区；任何新聚合政策只影响生效后的运行，不重算历史预测。
详见[市场与标的配置](docs/market-universes.md)和
[可信度治理](docs/believability.md)。

新的 focused research v2 以 `csi1000-absolute-d1` 作为唯一可激活的正式目标，另设
中证1000相对沪深300的 W1 Shadow 目标，并把宏观、行业等自然周期判断作为独立、可审计
信号保存。它与既有五指数历史追加式并行，既不回填旧记录，也不把数据工具自动变成预测
目标。完整契约、运行步骤与激活门禁见
[单主标的、多周期研究协议 v2](docs/research-program-v2.md)。

## Architecture

```text
read-only sources + versioned Wiki
               |
               v
      frozen evidence snapshot
               |
               v
 human / model / quant signals
               |
               v
 deterministic validation + aggregation
               |
               v
      immutable forecasts
               |
               v
 evaluation -> reflection -> gated lessons
```

模型与工作流迭代使用独立的 Agent Eval 闭环：冻结 baseline/candidate 输入，在结果揭示前
分别生成预测、推理审核和 ablation 草稿，再由确定性程序计算按目标划分的发布门禁。预测、
Reflection 和评测也会生成裁剪后的本地 trace；遥测故障不改变正式结果。详见
[Agent 评测与可观测性](docs/agent-evaluation-observability.md)。

后端使用 FastAPI、LangChain 和 LangGraph；前端使用 React、TypeScript 和 Vite；本地状态、
预测和评价保存在 SQLite。运行者自己的 Agent Wiki 默认保存在被 Git 忽略的
`data/wiki/`；源码中的 `wiki/` 只包含少量 `demo-only` 合成示例。核心结果不依赖
LangSmith。

公开扩展边界采用 Ports/Adapters：外部来源返回带时间与内容哈希的冻结快照，推理过程
只能生成受限草案，确定性程序负责校验和持久化。生产数据的许可字段、凭据、私有路径和
provider 映射留在仓库外，forecast-loop 只接收 source-neutral 的只读封签产物。

## Quick start

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 0.9.8 和 Node.js 20.19+。
Docker 使用 3.12.13，CI 使用可由 `uv` 管理安装的 3.12.12；两者均使用 Node.js 22，
以减少环境偏差。

```bash
cp .env.example .env
make install
make migrate
make backend
```

另开两个终端：

```bash
make worker
make frontend
```

打开 `http://localhost:5173`。默认
`VERICOUNCIL_EXECUTION_PROVIDER=demo`，无需模型密钥。Demo 使用确定性离线生成器验证
流程和界面，不是智能研究，也不会计入正式成绩。若本地 Wiki 尚无条目，Demo 会展示
源码附带的三个合成示例；Live 永远不会加载这些示例。

个人 Agent、adapter、历史表现和研究 Wiki 都属于本地运行者。Agent 实现应放在独立扩展
或仓库外可执行 adapter 中，运行数据及 Wiki 留在本地数据库和 `data/wiki/`，不应提交
到公共项目。

### 切换市场或标的

选择版本化 Universe，并设置匹配的市场时区：

```bash
export FORECAST_LOOP_MARKET_UNIVERSE_PATH=./examples/market-universes/us-index-and-equities.json
export VERICOUNCIL_TIMEZONE=America/New_York
```

重启后，研究台和人工判断入口会按新标的显示。Live 模式还必须接入目标市场的只读
Evidence Snapshot；配置文件本身不会下载或授权任何行情。

### 每日量化研究

量化程序通过通用只读端口提交封签 bundle。Bundle 必须绑定代码、参数、模型、输入和
Market Universe 哈希，并与本次 Evidence Snapshot 精确匹配。公开仓库不附带特定训练
框架、生产数据适配器或研究参数；这些实现位于独立扩展中。

Quant 默认只进入 shadow 评价，不能写回外部数据源或绕过人工激活门禁。通用合同、示例
fixture 与兼容性测试见[Quant 数据协议](docs/quant-adapter.md)。

### Docker

```bash
cp .env.example .env
make docker-config
make docker-up
```

前端位于 `http://127.0.0.1:4173`，API 位于 `http://127.0.0.1:8000/api`。Compose
默认只绑定 loopback；不要在没有认证、TLS 和网络访问控制时公开端口。

GitHub Pages 静态 Demo 默认关闭。启用时需将 Pages Source 设为 GitHub Actions，并设置
repository variable `PAGES_ENABLED=true`。

## 核心工作流

### 1. 先写自己的判断

运行迁移后打开 `http://localhost:5173/judgments`。页面先隐藏系统预测，要求用户选择
上涨或下跌，并填写理由、最强反证和可观察失效条件；封签后才显示对照结果。
非 Demo 部署需要在仓库根目录的私有 `.env` 配置
`FORECAST_LOOP_OPERATOR_TOKEN`，并通过 loopback Vite dev/preview 或其他可信
同源代理访问。服务端代理会替换认证头，token 不进入浏览器。

也可以通过 CLI 提交。理由使用文件传入，避免进入 shell history：

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
```

Live 判断默认在目标交易日 `09:30`（以该运行冻结的市场时区为准）截止。只有截止前
提交、声明尚未查看系统预测的 Live 记录才进入独立 shadow 成绩。

### 2. Daily Reflection

反省只处理已完成的 Live 预测；新运行写入 D1，升级前的 D2 历史记录仍可读取、评价和
反省。Demo 不生成正式 Reflection、Lesson 或 Wiki 输入。

```bash
make migrate
make market-snapshot
make market-import
make reflection-prepare
```

来源发现草案写入 `source-discovery/drafts.json`。确定性程序冻结可信 capture bundle 后，
分析草案写入 `analysis/drafts.json`，再完成发布与渲染：

```bash
make reflection-freeze-sources ARGS="./data/reflections/<job-id> --sources ./input/captures.json"
make reflection-finalize ARGS="./data/reflections/<job-id>"
make reflection-render ARGS="<reflection-id>"
```

项目没有内置通用网络爬虫；没有可信 capture bundle 时，未验证原因必须保持
`unresolved`。Lesson 在满足样本和人工复核门禁前只能保持 shadow。

### 3. 快照与可移植审计

从经过核验的 draft 生成不可变证据快照：

```bash
make snapshot DRAFT=path/to/draft.json OUTPUT=data/snapshots/2026-07-13.json

uv run forecast-loop snapshot validate \
  data/snapshots/2026-07-13.json \
  --root data/snapshots \
  --as-of 2026-07-13T16:00:00+08:00
```

SHA-256 能发现封装后内容是否变化，但不能证明来源真实、事实正确或信息完整。正式预测
仍须核验原文、事件时间和可见时间；修订必须生成新快照和新 run。

完成的 run 可以导出和离线验真：

```bash
uv run forecast-loop run export <run-id>
uv run forecast-loop run verify data/exports/<run-id>

uv run forecast-loop audit export <job-dir> \
  --handoff-root data/handoffs \
  --run-bundle data/exports/<run-id>
uv run forecast-loop audit verify data/audit-bundles/<run-id>
```

## Validation

```bash
make test
make lint
make build
```

## Documentation

- [单主标的、多周期研究协议 v2](docs/research-program-v2.md)
- [Agent 评测与可观测性](docs/agent-evaluation-observability.md)
- [系统架构](docs/architecture.md)
- [Agent 框架模型](docs/agent-model.md)
- [市场与标的配置](docs/market-universes.md)
- [数据源与时间规则](docs/data-sources.md)
- [Snapshot adapter 边界](docs/snapshot-adapters.md)
- [Provider / adapter 兼容性](docs/adapter-compatibility.md)
- [Quant 数据协议](docs/quant-adapter.md)
- [决策与引用结构](docs/decision-schema.md)
- [持久任务队列](docs/persistent-task-queue.md)
- [文件化 LLM 定时任务](docs/job-manifest.md)
- [Daily Reflection](docs/daily-reflection.md)
- [可信度治理](docs/believability.md)
- [可移植审计包](docs/audit-bundle.md)
- [本地 Wiki 与公共示例](wiki/README.md)
- [部署与安全](docs/secure-deployment.md)
- [发布与兼容性](docs/releasing.md)

## Important boundary

forecast-loop 是研究、证据和审计工具，不构成投资建议。它不自动交易，也不把 Demo
结果计入正式成绩；关键输入不完整时必须失败并显示原因，不能静默复用旧数据。

当前版本是 **v0.1.0 early release**。pre-1.0 公共接口仍可能按
[兼容性政策](docs/compatibility-policy.md)演进。

项目采用 [Apache License 2.0](LICENSE)。贡献方式、安全报告和社区行为规范分别见
[CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 与
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

[principles]: https://www.principles.com/principles/633d5d13-8610-425f-ad62-cd62347d9165
