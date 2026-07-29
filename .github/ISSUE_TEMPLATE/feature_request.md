---
name: Feature request
about: Propose a scoped, verifiable improvement
title: "[Feature] "
labels: enhancement
---

## Problem

What research, verification, audit, or operator problem should be solved?

Do not include secrets, personal paths, private projects, internal schemas,
machine/network details, licensed data, or private research relationships.
Security-sensitive reports must use the private vulnerability reporting channel.

## Proposed contract

Describe the user-facing behavior, inputs, outputs and failure conditions.

## Trust and compatibility

- Does this change a data, model, scheduler or storage contract?
- How does it preserve point-in-time evidence and deterministic validation?
- Does it affect a generic deployment, private extension boundary, or local data?
- What migration and tests are required?

The proposal must not add trade execution, account control, or production
upstream write paths.
