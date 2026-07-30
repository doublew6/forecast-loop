# Bundled Wiki examples

This directory is part of the public source tree and therefore contains only
synthetic examples, an empty proposal layout, and a reusable template. It is
not the operator's research Wiki.

Operator-maintained Wiki pages default to the Git-ignored `data/wiki/`
directory. `VERICOUNCIL_WIKI_PATH` may instead point to another local,
access-controlled directory.

Runtime behavior:

- Demo reads the local Wiki first. If it contains no parseable entries, Demo
  falls back to the bundled `demo-only` examples in this directory.
- Live reads only the configured local Wiki. It fails closed when no eligible
  `active` entry is available and never treats these examples as live research.
- Each run freezes the selected IDs, versions, sections, publication times, and
  content hashes, so later Wiki edits cannot rewrite historical forecasts.
- Files under `proposals/` and `templates/` never enter the runtime catalog.

To start a local Wiki, copy [the entry template](templates/domain-entry.md) to
`data/wiki/<topic>.md`. Keep unreviewed work as `status: draft`; a Live-eligible
entry needs `status: active`, inspectable sources, and a timezone-aware
`published_at` no later than the run's evidence cutoff.

Bundled examples:

- [Evidence discipline](example-evidence-method.md)
- [Strategy synthesis](example-market-strategy.md)
- [Risk preflight](example-risk-checklist.md)
