# Domain Wiki Entry Template

复制本模板时，将下面代码块转换为真实 frontmatter，并删除所有占位说明。新条目在人工
检查前使用 `status: draft` 与 `version: 0.1.0`。

```markdown
---
id: VC-WIKI-DOMAIN-NAME
title: 领域标题
version: 0.1.0
updated_at: YYYY-MM-DD
published_at: YYYY-MM-DDTHH:MM:SS+08:00
status: draft
owners:
  - domain_agent_id
tags:
  - domain-tag
source_urls:
  - https://primary-source.example/
---

<!-- section:scope -->
## 职责边界

说明本领域回答什么、不回答什么，以及当前 D1 的时间尺度；如用于历史审计，另行说明
升级前 D2 的适用边界。

<!-- section:ontology -->
## 概念与实体

定义核心概念、实体、指标及彼此关系。

<!-- section:source-map -->
## 来源地图

列出优先原始来源、备选来源及不可单独用于正式结论的线索源。

<!-- section:transmission -->
## 传导与判断框架

描述可验证的中间环节，禁止从主题词直接跳到指数方向。

<!-- section:daily-checklist -->
## 每次运行检查

列出 Agent 在预测前必须完成的检查项。

<!-- section:counter-evidence -->
## 反证与矛盾

列出最强反向解释、来源冲突和需要降级置信度的情况。

<!-- section:invalidation -->
## 失效条件

说明结论在什么事实、时间或市场状态下失效。

<!-- section:maintenance -->
## 待补知识与维护问题

记录缺口、孤立概念、需要新增来源或进一步验证的问题。

<!-- section:sources -->
## 来源

- [原始来源](https://primary-source.example/)
```
