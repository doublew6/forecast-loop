# Changelog

All notable public changes to forecast-loop are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and public releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A separate 09:15 pre-market protocol for CSI 1000 open-to-open forecasts,
  with frozen overnight news, global-market and FX/rates evidence, Agent-level
  routing, Wiki bindings, deterministic risk discounting, sealed evaluation,
  and idempotent owner briefs.
- Audited pre-market history with lag-safe brief feedback, cumulative and
  rolling direction accuracy, plus gross compounded long-only and long-short
  strategy curves in the scorecard UI.
- Deterministic, owner-only Feishu delivery for concise CSI 1000 D1 briefs,
  with target-date idempotency, dry-run rendering, and safe retry markers.
- Focused research v2 contracts and append-only workflows for one CSI 1000 D1
  activation target, a W1 relative shadow target, and D20 natural-horizon state.
- Outcome-blind Agent Eval v2 file replay with per-target release gates,
  advisory reasoning reviews, diagnostic ablations, and bad-case feedback.
- Attempt-level sanitized traces with artifact links, immutable terminal seals,
  cursor pagination, filters, and storage monitoring.
- An opt-in private runtime-trace bridge that records the real task input and
  final forecast at the root, nests model and tool calls, and keeps all raw
  content outside the public trace store.
- Read-only v2 research, forecast, scorecard, reasoning-review, evaluation, and
  trace views in the API and frontend.
- Public-boundary checks for staged files, commit messages, complete reachable
  Git history, ref names, and release artifacts.
- Repository-external private-boundary rules that never enter the public tree or
  appear in scanner output.

### Changed

- Runtime tracing deployment guidance now distinguishes same-node loopback
  delivery from explicitly authorized private-network HTTPS delivery, without
  publishing operator endpoints or policy paths.
- The primary dashboard and scorecard views now separate formal D1 decisions,
  W1 shadow research, natural-horizon views, reasoning quality, and incremental
  contribution instead of presenting a cross-horizon overall ranking.
- New forecast writes and the current Dashboard use D1 only. Handoff protocol
  v3 seals that contract, previously prepared v2 D1/D2 jobs remain finalizable,
  and historical D2 forecasts remain readable and evaluable.
- Operator-maintained Agent Wiki pages now default to the Git-ignored
  `data/wiki/` tree. The checked-in Wiki contains only three synthetic
  `demo-only` examples, and Live never falls back to them.
- Human-readable Reflection and Lesson archives now default to
  `data/reflection-archives/` and `data/lesson-archives/`; generated Markdown
  under the legacy top-level layout is Git-ignored as a second guard.
- The loopback Vite dev/preview proxy can authenticate Live operator routes
  with the server-side root `.env` token. It strips any browser-supplied
  `Authorization` header before injecting the configured credential.
- New Live User Judgments use the target trade date's configured market-open
  time as their deadline. Existing v1/v2 seals remain verifiable, while new
  records use the immutable `user-judgment/v3` policy.

### Fixed

- The focused Dashboard runtime card now reads the current v2 CSI 1000 D1 seal,
  shows its frozen SSE anchor-to-target session pair, and fails closed instead
  of presenting legacy five-index preparation receipts as current activity.
- Interrupted pre-market finalization now recovers a missing receipt only from
  the verified forecast and frozen drafts before the exclusive 09:24 deadline,
  while receipt-only, conflicting, and tampered artifact states fail closed.
- Focused-v2 draft instructions now state that every non-abstaining D1 impact
  requires a non-empty `transmission_chain`, matching the fail-closed schema
  validator before an external dispatcher publishes `drafts.json`.
- Focused-v2 prepare now reuses the first frozen run for the same program,
  mode, and anchor date, while external dispatchers can run the exact public
  draft validation before publishing `drafts.json` without overwriting it.
- Audit-bundle and job-execution readers now fail closed when handoff receipt
  protocol, provider, or retry metadata is version-inconsistent.
- Source archive creation now audits the selected Git revision with the
  built-in public-boundary policy before writing any artifact.
- Demo Wiki fallback now activates when a local catalog has no runtime-valid
  entry, including draft-only catalogs. Live list, detail, and freeze paths
  never return bundled or inline demo material.
- Lesson Markdown links now resolve from the configured Lesson archive to the
  configured Reflection archive instead of assuming legacy directory names.
- The current static Demo run reports the same five D1 forecasts in its batch,
  meeting, and run-history metadata.

## [0.1.0] - 2026-07-29

### Added

- Deterministic forecasting, evidence freezing, evaluation, reflection, and
  governed lesson lifecycle.
- Human, model, quantitative, and deterministic signal contracts.
- Versioned Market Universe configuration and source-neutral read-only
  provider/adapter interfaces.
- Portable job manifests, immutable run and audit bundles, migration and
  recovery tooling, and a local demo mode.
- FastAPI backend, React frontend, Docker configuration, synthetic compatibility
  fixtures, and a reproducible release pipeline.
- Secret, PII, dependency, filesystem, container, history, and release-artifact
  security gates.

### Security

- Public data fixtures are synthetic or explicitly redistributable.
- Local databases, handoffs, checkpoints, logs, generated output, personal
  paths, and private integration details are excluded and rejected by automated
  checks.
