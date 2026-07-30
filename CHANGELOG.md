# Changelog

All notable public changes to forecast-loop are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and public releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Public-boundary checks for staged files, commit messages, complete reachable
  Git history, ref names, and release artifacts.
- Repository-external private-boundary rules that never enter the public tree or
  appear in scanner output.

### Changed

- Operator-maintained Agent Wiki pages now default to the Git-ignored
  `data/wiki/` tree. The checked-in Wiki contains only three synthetic
  `demo-only` examples, and Live never falls back to them.
- Human-readable Reflection and Lesson archives now default to
  `data/reflection-archives/` and `data/lesson-archives/`; generated Markdown
  under the legacy top-level layout is Git-ignored as a second guard.

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
