---
id: VC-WIKI-DEMO-RISK-CHECKLIST
title: "Example: risk preflight"
version: 0.1.0
updated_at: 2026-07-30
status: demo-only
owners:
  - risk_critic_agent
tags:
  - demo
  - risk
source_urls:
  - https://example.com/synthetic-risk
---

<!-- section:preflight -->
## Preflight

This is synthetic example content. Check for future-information leakage,
post-cutoff sources, duplicated evidence, missing market clocks, and
probabilities that cannot be recomputed.

<!-- section:invalidation -->
## Invalidation

An invalidation condition must name an observable event and time boundary.
Phrases such as "if the market changes" are not testable conditions.
