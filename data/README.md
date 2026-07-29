# Local data

This directory is intentionally excluded from version control except for this
file. Runtime databases, frozen evidence snapshots, market-data caches and
LangGraph checkpoints are local artifacts.

Portable completed-run bundles exported by the public CLI live under
`data/exports/<run-id>/`. Each directory contains normalized run, opinion and
forecast JSON plus a content-addressed `manifest.json`. Treat these bundles as
private unless their underlying evidence and market-data licenses permit
redistribution. Verify a bundle before reading or copying it:

```bash
forecast-loop run verify data/exports/<run-id>
```

The former `signalrace` and `vericouncil` executables remain available as
compatibility aliases.

Complete file-handoff audit bundles can be exported separately under
`data/audit-bundles/<run-id>/`. They bind the frozen handoff inputs, external
draft, deterministic receipt and completed result bundle. SHA-256 detects
unresealed corruption; without a separately trusted hash anchor it cannot
distinguish the original from an attacker-resealed bundle. It is not a
publisher signature and does not capture the external Codex automation or
runtime environment.

`data/job-executions/` contains private append-only orchestration state for
external Codex draft tasks. It is deliberately separate from `handoffs/`;
the runner must grant Codex write access only to the handoff's `drafts.json`
and must never grant access to the execution-state root. A terminal execution
records an internally consistent operator receipt; use an audit bundle to bind
that assertion to exported database results.

Formal forecast runs must retain immutable source hashes in the database. Demo
runs and price observations are namespaced separately and must never contribute
to or conflict with formal scorecards.

Codex file handoffs live under `data/handoffs/<run-id>/` by default. `prepare`
creates the immutable `input.json`, `INSTRUCTIONS.md` and
`drafts.template.json`; Codex may create only `drafts.json`; `finalize` writes
`receipt.json` after deterministic validation and persistence. Never reuse a
handoff directory for another run, overwrite a receipt, or manually repair a
hash. A rejected or expired package should be replaced by a newly prepared run.

The handoff directory is private runtime state, not a collaboration folder.
Do not publish it, serve it over HTTP, synchronize it through a public share, or
point `VERICOUNCIL_HANDOFF_ROOT` at any upstream production path.
Grant write access only to the local draft task that creates `drafts.json` and
the deterministic finalize process; keep the web/API services bound to
loopback.

Immutable prediction inputs live under `data/evidence-snapshots/`. A trusted
external preparation process creates them through a configured read-only
adapter and binds their hash into the handoff. They must not be edited, shared
publicly, or synchronized back into an upstream data owner.

Every scheduled prediction preparation attempt appends one immutable, hash-sealed
receipt under `data/prediction-status/<base-session>/`. Receipts contain only
sanitized status/error codes and immutable run/snapshot bindings, never local
absolute paths. The read-only API derives today's state and recent history from
these receipts; it never starts or finalizes a run. Do not edit, overwrite, or
publicly synchronize this directory.

Daily-reflection runtime packages are separate and live at
`data/reflections/<reflection-id>/`. Codex may directly create only
`source-discovery/drafts.json` and `analysis/drafts.json` there. A trusted
external collector, not Codex, supplies an optional capture bundle; this
repository currently has no general-purpose network crawler. Freezing an empty
source set is allowed only when the analysis leaves unverified causes
`unresolved`.

`data/market-snapshots/` holds private, hash-sealed market-session inputs.
`reflection-finalize` publishes the database rows and the private
`receipt.json`; it does not publish Markdown. The separate
`reflection-render` command reads a completed Live reflection and writes
immutable human-readable archives to the repository-level `reflections/` and
`lessons/` directories. Those top-level directories are not runtime handoff
roots and are never prediction-time Wiki input.
