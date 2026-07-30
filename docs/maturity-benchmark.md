# 同类项目成熟度对标

## 目的与方法

这份文档回答的不是“谁的 Agent 更多”，而是 forecast-loop 作为一个开源研究产品还缺
什么。对标只采用截至 **2026-07-26** 可见的一手 GitHub 仓库说明与发布材料；功能事实
与本文推断分开描述，不用 Star 数代替工程成熟度。

持续观察的四个主项目：

- [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)：
  重点观察多 Agent 角色编排、结构化输出、决策日志、checkpoint 恢复、Docker 和
  provider 兼容。
- [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)：
  重点观察 CLI / FastAPI / Web / Docker / MCP 的产品闭环、多市场数据、回测、
  run card、Hypothesis Registry 和开源安装体验。
- [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)：
  重点观察低门槛 CLI / Web / backtester、角色表达和社区传播；其 README 明确将项目
  定位为 proof of concept，并说明当前不实际交易。
- [AI4Finance-Foundation/FinRobot](https://github.com/AI4Finance-Foundation/FinRobot)：
  重点观察金融 Agent 平台分层、确定性金融分析、报告生成和本地研究产品。

上游会变化。引用某项能力做设计决策时，Issue 或 PR 应进一步记录 tag 或 commit，不能
把“当前最新版”当成可复现依赖。

### 本次证据快照

本文核对的是各项目默认分支 README 与公开仓库材料，不等于逐文件代码审计。为避免
浮动链接掩盖版本变化，记录本次检查点：

| 项目 | 默认分支 | 2026-07-26 检查点 |
| --- | --- | --- |
| TradingAgents | `main` | [`a33fd4c0`](https://github.com/TauricResearch/TradingAgents/commit/a33fd4c0f134485a43553a2c23a63cb14adbd88f) |
| Vibe-Trading | `main` | [`8903f10d`](https://github.com/HKUDS/Vibe-Trading/commit/8903f10d0cbd3a550c26159c5b1047a89da9c1d9) |
| ai-hedge-fund | `main` | [`9557e642`](https://github.com/virattt/ai-hedge-fund/commit/9557e64273e212635a4a28cbd8128df22f166c07) |
| FinRobot | `master` | [`297a8d28`](https://github.com/AI4Finance-Foundation/FinRobot/commit/297a8d28d099be328c8a8eb658b4f782b93f3651) |

## 结论

Vibe-Trading 是当前最值得对标的**产品广度与安装体验**，TradingAgents 是最值得对标的
**Agent 编排与生态影响力**，FinRobot 是很好的**确定性金融计算与报告 provenance**
参考。基于本次公开制品检查，forecast-loop 当前差异化较强的方向不是角色数量，而是：

> 让 manual、quant、ai 与 deterministic 等不同来源的 Agent 具有稳定身份和版本，
> 在结果揭晓前封签信号，并在结果揭晓后按其能力使用可比较指标评价；任何可信度变化
> 都必须经过版本化影子验证。

这可以称为 `epistemic governance / decision audit`。它与生成研究、策略或交易实验是
互补关系，而不是用同一张功能清单竞争。当前 AI Reflection 已有后验理由归因代理；
User Judgment 的理由已封签但尚未进入正式归因 rubric，这是 P2 而不是已完成功能。

## 能力矩阵

下表中的状态标签是对 forecast-loop 相对公开制品的工程判断，不是对其他
项目的质量评级。

| 维度 | 同类成熟制品 | forecast-loop 当前 | 判断与下一步 |
| --- | --- | --- | --- |
| 多 Agent 编排 | TradingAgents 的显式分工、辩论和恢复 | LangGraph 研究 → Strategy → Risk → CIO，固定结构化 schema | **已具备**；不靠继续增加名人角色竞争 |
| 安装与体验 | Vibe-Trading 的包、CLI、Web、Docker、MCP 与文档闭环 | uv + Make + Docker + React + 静态 Demo | **仍有缺口**；需要正式 release、升级指南、Docker smoke 和更短的首次运行路径 |
| 数据与回测广度 | Vibe-Trading 的多市场 loader / backtest，AI Hedge Fund 的基础 backtester | 五个 A 股宽基指数，外部只读 snapshot adapter；不执行策略回测 | **明显缺口**，但不应以牺牲时间与来源审计为代价追求市场数量 |
| 确定性计算 | FinRobot 将数值计算与 LLM 叙述分离 | Python 负责快照、哈希、schema、聚合、评价、Lesson 生命周期 | **当前设计重点**；继续把模型限制在 untrusted draft |
| 决策可复验 | run card、decision log、checkpoint 等 | evidence cutoff、Wiki 版本、双层引用、run/audit bundle、canonical hash | **差异化较强（基于公开制品）**；仍需签名发布与外部可信时间锚 |
| 长期学习 | 决策记忆、Hypothesis Registry、Shadow Account | Live-only Reflection、人工审核、Lesson replay、可信度 shadow | **已具备治理骨架**；当前样本不足，禁止自动调权 |
| Agent 来源模型 | 多数项目主要围绕 LLM 角色或把外部输入视为操作指令 | 注册表区分 `ai / manual / quant / deterministic`；manual 有独立封签，Quant 有内容寻址只读 shadow adapter | **框架重点**；仍需真实兼容实现与跨来源 benchmark |
| 运行恢复 | TradingAgents checkpoint；成熟产品常用持久任务队列 | API 与独立 worker 之间使用数据库队列、幂等键、租约、attempt deadline、有限重试和围栏；文件交接另有 append-only 任务状态 | **已具备单机恢复骨架**；仍需多进程 soak、备份与故障演练 |
| Provider / 插件生态 | 多 provider、MCP、技能和第三方集成 | OpenAI-compatible API 或 Codex file handoff；数据 Port 初版 | **明显缺口**；先稳定 contracts，再增加 provider / adapter 示例 |
| 安全与部署 | 成熟项目的安全策略、输入限制、CI 和发布治理 | loopback 默认、无交易执行、路径/哈希校验、公开 Demo 与私有 Live 分离 | **边界清楚**；公网 Live 前仍必须增加认证、TLS、备份、SBOM 与依赖扫描 |
| 开源治理 | Release、migration、贡献指南、社区模板 | Apache-2.0、CI、CONTRIBUTING、SECURITY、CODE_OF_CONDUCT | **接近发布基线**；缺正式版本、兼容性承诺和公共 benchmark 数据集 |

## 当前已经落地的 P0

- README 首屏明确桥水 Idea Meritocracy 的启发、两维可信度和 Responsible Party。
- User Judgment Agent 是当前首个 manual shadow 实现，不进入模型 draft、Strategy、CIO、
  run input hash 或现有 Reflection roster；它不代表 forecast-loop 只比较人类与 AI。
- Agent 注册表独立暴露职责 `workflow_role` 和注册来源 `source_type`；旧 `kind` 作为兼容
  字段保留，这两项新元数据尚未写入 v1 run hash。
- 用户必须提交上涨 / 下跌、核心理由、最强反证和失效条件；提交后不可覆盖。
- 不含 CIO 方向的 `/api/user-judgments/targets` 支持产品层盲判；Live 记录还冻结
  “尚未查看委员会结论”的自我声明。未声明的记录可以存档，但不计分。
- 用户记录绑定 Forecast、run input hash、forecast input hash、服务端时间和截止窗口，
  并生成数据库内容哈希与私有 Markdown 文件哈希。
- 用户成绩只采用可信 `EvaluationResult` 和 completed evaluation batch；不接受用户提交
  actual return，也不为未提交的三分类概率伪造 Brier。
- 私有 User Judgment Wiki 与正式 `data/wiki/` 分离，不能被预测 Agent 引用或自动晋升为
  常青知识。

## P1：开源研究产品成熟度

1. 发布正式 `v0.1`：生成 changelog、升级指南、迁移 smoke、Docker smoke、签名 tag。
2. 建立固定、可再分发的 benchmark fixture：
   - manual signal；
   - AI-generated signal；
   - quant signal；
   - deterministic policy / baseline；
   - 固定委员会；
   - 等权 baseline；
   - 候选可信度委员会。
3. 按独立目标日同时报告符号命中、重大行情命中、覆盖率和失败率；三分类 Brier 与
   classwise calibration 只适用于提交完整规范化概率的参与者，禁止只展示一次性收益。
4. 为 Live HTTP 写接口增加 operator authentication；补充 SBOM、依赖扫描、secret
   scanning 与恢复演练。

已完成的 P1 基础项：版本化 `AgentSpec + SignalEnvelope + ParticipationPolicy`、
capability-driven evaluation facade、JSON Schema、离线验证 CLI、append-only
envelope 投影、v1 bundle bytes golden，冻结代码/参数/特征/模型/输入快照的首个
只读 Quant shadow adapter，默认隐去 actor 的 User Judgment bundle，公开 provider /
data adapter 示例与 compatibility test kit，以及 API 到独立 worker 的持久任务队列。
字段与兼容边界见
[AgentSpec 与 SignalEnvelope 契约](agent-contracts.md)和
[只读 Quant Agent adapter](quant-adapter.md)；任务恢复见
[持久预测任务队列](persistent-task-queue.md)。

## P2：可信度决策系统

1. 增加结果揭晓前的独立 reasoning rubric 和双盲审核，区分：
   `right_reason / lucky_correct / wrong / unresolved`。
2. 增加 User Judgment batch lock，在同一 run 的目标全部提交或截止后统一揭晓，避免
   单条即时对照锚定后续判断。
3. 对历史表现做小样本收缩、日期聚类、时间衰减和相关性惩罚；模型或 policy 版本变化
   必须重新 shadow。
4. 同时冻结静态基线、候选可信度基线、Strategy 输出和 CIO 最终结果，完成样本外重放。
5. 只有明确的人工 activation event 能让候选权重作用于未来 run；样本数或环境变量
   不能自动激活。
6. 将用户 override 也作为独立、有理由和后果的 track record，而不是让最终责任消失在
   聚合公式里。

## 不追求的对标

- 不通过接入券商或自动下单证明成熟；无交易执行是当前可信边界。
- 不以 Agent 数量、人物模仿或自然语言篇幅作为研究质量。
- 不把回测收益、Star 数或一次命中当成可信度。
- 不为了兼容更多来源而绕过 cutoff、provenance、许可或失败关闭规则。
