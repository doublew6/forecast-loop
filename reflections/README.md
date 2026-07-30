# Reflection archive layout

This directory documents the public archive format only. Completed Live
reflection Markdown defaults to the Git-ignored
`data/reflection-archives/` directory.

Rules:

- Demo does not create formal reflection archives.
- Each case binds its ReflectionRun, target session, horizon, frozen market
  snapshot, source snapshot, and immutable receipt.
- A revision creates a new file with `supersedes`; it never overwrites an old
  case.
- Finalize publishes database rows and a private receipt. The separate
  `reflection-render` command writes the human-readable archive.
- Rendering identical bytes is idempotent; conflicting existing bytes fail
  closed.
- Cases may leave causes unresolved and must not invent post-hoc causality.

Runtime handoffs live under `data/reflections/`; rendered cases live under
`data/reflection-archives/`. Neither path belongs in Git or prediction-time
Wiki input.
