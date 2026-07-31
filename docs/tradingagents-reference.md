# 与 TradingAgents 的关系

## 结论

forecast-loop 不 fork TradingAgents，也不以其包、状态对象或领域模型作为运行依赖。
本仓库是独立实现。

TradingAgents 是重要的公开参考：它证明了用 LangGraph 把分析师、研究辩论、交易员、
风险角色和组合经理组织成金融多 Agent 工作流的可行性。forecast-loop 借鉴组织思路，
但产品目标是“证明每个 Agent 为什么值得相信”，不是复刻单股票交易团队。

## 参考对象

- 官方仓库：[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 研究论文：[TradingAgents: Multi-Agents LLM Financial Trading Framework](https://arxiv.org/abs/2412.20138)
- 上游许可：[Apache License 2.0](https://github.com/TauricResearch/TradingAgents/blob/main/LICENSE)

本说明以 2026-07-13 可见的上游公开资料为准。上游会继续演化，评估新能力时必须
记录具体 tag 或 commit，不使用“当前最新版”作为不可复现依赖。

## 可以借鉴的模式

以下是架构思想或公开接口层面的参考，不意味着复制实现：

- 用 LangGraph 表达显式、分阶段的多 Agent 决策流程；
- 专业分析角色先独立研究，再由反方或风险角色挑战；
- 管理角色在完整上下文中形成最终结构化结果；
- 节点 checkpoint、失败恢复和持久决策记录；
- 多模型 provider 配置与结构化输出；
- 决策到期后取得结果并形成复盘。

forecast-loop 对这些通用模式使用 LangChain、LangGraph 和自己的代码重新实现。

## 核心差异

| 维度 | TradingAgents | forecast-loop |
| --- | --- | --- |
| 主要对象 | 单只股票 ticker | 五个 A 股宽基指数 |
| 输出 | 交易评级或操作决策 | 当前 D1 涨跌二元立场 + up/neutral/down 结果概率；历史 D2 可审计 |
| 角色中心 | 基本面、技术、新闻、情绪、交易与风险 | 宏观、市场事件、AI存储、市场策略、反证、CIO |
| Quant | 技术与市场分析是既有流程一部分 | 一期待接入可信只读数据，不生成观点 |
| 知识责任 | 报告、状态和决策记忆 | 版本化 Wiki 段落与原始来源双层引用 |
| 历史验证 | 已实现收益与反思 | Agent × 指数 × 周期 × 版本的评分与校准 |
| 数据市场 | 通用 ticker 和海外市场链路 | A 股交易日、指数成份、政策与全球产业映射 |
| 产品 | 研究框架与 CLI | FastAPI + React 的可审计研究台 |

因此不能把 TradingAgents 的 ticker → buy/hold/sell、AgentState、固定角色或数据层
直接当成 forecast-loop 的核心接口。

## 当前代码复用边界

初始版本：

- 没有复制 TradingAgents 源文件；
- 没有把 TradingAgents 加入依赖；
- 没有使用其 prompt、AgentState、决策日志格式或数据适配器；
- NOTICE 只做来源说明，不声称存在上游代码授权链。

“观察公开行为后独立实现”与“复制代码再改名”必须在评审中明确区分。任何来自上游
的逐行代码、测试、prompt 或文档段落都视为代码复用，不因文件很小而自动豁免。

## 将来复用代码时的流程

若未来决定移植某段 Apache-2.0 代码，合并前必须：

1. 锁定上游 commit、原始文件和具体行段。
2. 记录为什么无法用独立接口或通用 LangGraph 能力实现。
3. 保留适用的版权、许可和 NOTICE 信息，并清楚标记修改。
4. 在本文件增加“复用清单”，列出本地文件、上游文件、commit 和修改摘要。
5. 做许可证兼容性检查；法律结论不由 Agent 自行给出。
6. 添加回归测试，避免上游领域假设渗入 forecast-loop schema。

Apache-2.0 是宽松许可，但“可以复用”不等于“无需归属或记录”。本项目自己的许可
选择也不会取消随被复用代码而来的上游许可义务。

## 上游跟踪策略

TradingAgents 不作为 git remote 或 subtree。需要研究新版本时：

- 阅读官方 release、README 和对应 commit；
- 只把可泛化的改进写成独立设计议题；
- 优先使用 LangChain 或 LangGraph 官方能力；
- 通过 forecast-loop 自己的公开 schema 和测试实现；
- 不为追随上游而改变 Wiki 引用、时间快照和概率评价这些核心边界。

适合持续观察的能力包括 checkpoint 恢复、多 provider 抽象、结构化输出错误处理和
决策复盘；不适合直接引入的是单票交易领域对象、固定角色、交易执行语义和非 A 股
数据假设。
