---
id: VC-WIKI-MANIFEST
title: forecast-loop 版本化 Wiki 约定
version: 1.3.2
updated_at: 2026-07-27
published_at: 2026-07-27T16:00:00+08:00
status: active
owners:
  - cio_agent
tags:
  - governance
  - citation
source_urls:
  - https://docs.langchain.com/oss/python/langchain/structured-output
  - https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
---

<!-- section:purpose -->
## 用途

本目录是 forecast-loop 的可验证知识基线。Agent 可以基于 Wiki 形成判断，但不能把
Wiki 当成实时事实流：指数成份、公司业绩、政策决定和市场价格等动态信息仍须引用
当次运行冻结的原始资料。

一个可计入正式成绩的观点必须同时具备：

- Wiki 条目的稳定 ID、版本和段落 slug；
- 生成时的条目内容哈希；
- Wiki 条目列出的原始公开来源；
- 不晚于 data_cutoff 的证据时间；
- 可以独立检查的证据、反证与失效条件。

本目录参考 Karpathy 的 LLM Wiki 模式：原始资料保持不可变，AI 持续维护可读、互链的
Markdown Wiki，维护规则由 `AGENTS.md` 与本文件共同约束。与一般个人 Wiki 不同，
forecast-loop 还必须冻结预测时间截面，不能让后验资料改写历史判断。

<!-- section:layers -->
## 三层结构

1. 原始资料层：Evidence Snapshot、行情快照和原始公开来源。内容不可原地修改。
2. Wiki 层：本目录内由本地 AI 搭骨架、后续由 Codex 维护的 Markdown 研究知识。
3. Schema 层：`AGENTS.md` 与本文件定义目录、字段、版本、引用和维护流程。

`index.md` 是面向内容的导航目录，每次正式知识变更后更新；`log.md` 是只追加的演进记录。
待审核修改放入 `proposals/`，模板放入 `templates/`；两者均从运行时 Wiki Catalog 排除，
不得被正式预测引用。

<!-- section:frontmatter -->
## Frontmatter 规范

每个条目都使用以下可机读字段：

- id：永久不变的大写稳定 ID。重命名文件不得改变 ID。
- title：面向人的中文标题。
- version：语义化版本号。
- updated_at：最近一次知识修改日期，格式为 YYYY-MM-DD。
- published_at：条目进入正式可用 Wiki 的带时区时间。Live prepare 会用
  Evidence Snapshot 的 `data_cutoff` 严格拒绝缺失该字段或发布时间晚于 cutoff 的 active
  条目，并把该时间冻结进 input/hash；Demo fallback 与这条 Live 门禁隔离。
- status：active、draft 或 deprecated；只有 active 条目可被 Agent 选入并冻结到正式预测。
- owners：负责使用和维护该知识的 Agent ID。
- tags：检索标签。
- source_urls：支持本条目知识框架的原始公开 URL 数组。

文件名只是存储位置，不是引用身份。历史决策只认 id、version、section 和
content_hash。

<!-- section:anchors -->
## 稳定段落锚点

每个可引用标题前放置显式注释：

    <!-- section:daily-checklist -->
    ## 每日检查

section slug 在同一条目内必须唯一。修改标题不改变 slug；删除已被历史决策引用的
段落时，必须先将条目标记 deprecated，并保留兼容说明。

<!-- section:versioning -->
## 版本规则

- PATCH：错别字、链接修复，不改变判断含义。
- MINOR：增加指标、检查项或来源，保持旧引用含义。
- MAJOR：改变预测定义、因果框架或证据等级。
- 历史运行始终保留当时版本和内容哈希；更新 Wiki 不得回写旧决策。
- 模型可以提出修改草案，但同一次预测运行不得自动修改并立即引用新内容。

<!-- section:operations -->
## 维护操作

- Ingest：读取一份新的冻结来源，生成相关主题页、交叉链接和 `index.md` 的修改提案。
- Query：先读 `index.md`，再读取相关完整段落；值得长期保留的新综合结论可以形成提案。
- Lint：检查冲突、过时结论、孤立页面、缺失链接、无来源断言和待补研究问题。
- Promote：只有通过来源、时间、格式、版本和引用校验的提案才能进入正式 Wiki。

前期由本地 AI 建立领域骨架并人工检查。后期自动维护默认只生成提案；自动发布权限应按
变更风险逐步开放，因果框架、来源等级和预测定义的变化不得无审查发布。
draft 条目仍可通过 Wiki Catalog/API 查看和审查，但只有 Promote 后的 active 条目才会进入
正式运行的冻结快照、输入哈希和 Agent 上下文。

<!-- section:catalog -->
## 条目目录

| 稳定 ID | 条目 | 主要使用者 |
| --- | --- | --- |
| VC-WIKI-MACRO-POLICY | [宏观政策传导](macro-policy.md) | macro_policy_agent |
| VC-WIKI-MARKET-NEWS | [市场资讯与预期差](market-news.md) | market_news_agent |
| VC-WIKI-MARKET-STRATEGY | [市场策略与指数配置](market-strategy.md) | strategy_agent、cio_agent |
| VC-WIKI-INDEX-CSI300 | [沪深300暴露](index-csi300.md) | 全体研究 Agent |
| VC-WIKI-INDEX-CSI500 | [中证500暴露](index-csi500.md) | 全体研究 Agent |
| VC-WIKI-INDEX-CSI1000 | [中证1000暴露](index-csi1000.md) | 全体研究 Agent |
| VC-WIKI-INDEX-CHINEXT | [创业板指暴露](index-chinext.md) | 全体研究 Agent |
| VC-WIKI-INDEX-STAR50 | [科创50暴露](index-star50.md) | 全体研究 Agent |
| VC-WIKI-INDUSTRY-AI-MEMORY | [AI存储产业链](industry-ai-memory.md) | ai_storage_industry_agent |
| VC-WIKI-RISK-CHECKLIST | [风险与反证检查表](risk-checklist.md) | risk_critic_agent |
| VC-WIKI-PREDICTION-LABELS | [预测标签定义](prediction-labels.md) | 全体 Agent |
| VC-WIKI-SOURCE-TIERS | [数据源可信等级](source-tiers.md) | evidence_validator |

<!-- section:source-notes -->
## 来源

- [LangChain 结构化输出文档](https://docs.langchain.com/oss/python/langchain/structured-output)
- [Karpathy：LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

这些链接分别支持“结构化结果应由 schema 校验”和“AI 维护持久 Markdown Wiki”的工程
模式；本目录的时间冻结、证据哈希、发布门禁和历史不可变规则是 forecast-loop 自身治理规则。
