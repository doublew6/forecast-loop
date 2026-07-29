# Snapshot adapter boundary

forecast-loop does not import a source repository. Provider mappings, licensed
fields, credentials, quality rules, and directory layouts belong to an
external read-only adapter.

Adapters may be injected as Python ports or invoked through an executable
command configured by the operator.

## Evidence builder

An executable Evidence Snapshot builder receives explicit arguments:

```text
adapter \
  --base-session YYYY-MM-DD \
  --captured-at ISO8601 \
  --output ./output/evidence-snapshot.json
```

It must create exactly the requested regular file, using the public
`FrozenEvidenceSnapshot` schema. The host validates the target set, market
clock, time ordering, trusted-source policy, completeness, and content hash
before the snapshot can enter a Live run.

## Market-outcome builder

The outcome adapter follows the same write boundary:

```text
adapter \
  --target-date YYYY-MM-DD \
  --horizon D1 \
  --captured-at ISO8601 \
  --output ./output/market-snapshot.json
```

Its output binds target identity, base and target observations, trading
calendar evidence, quality status, publication identity, and artifact hashes.
The core recomputes returns and rejects inconsistent or incomplete input.

## Quant history

Quant extensions may consume a separate source-neutral history manifest. Such
a manifest must bind the selected Market Universe and Evidence Snapshot, list
every artifact with a relative path and SHA-256 digest, and preserve the
source cutoff. The public core does not define a provider layout or training
implementation.

## Security requirements

- Keep adapters outside the public repository when they encode proprietary
  source knowledge.
- Give adapters read-only access to source data and write access only to the
  exact requested output.
- Reject symlinks, path traversal, ambiguous timestamps, partial target sets,
  stale publications, and changed files.
- Do not place private metric names, paths, provider credentials, or account
  identifiers in public IDs, warnings, fixtures, or logs.
- Treat adapter output as untrusted until schema, time, provenance,
  completeness, and hashes all pass.
- Never allow an adapter to place orders, control an account, or write to a
  source database.

Public compatibility helpers live in `app.testing.adapter_compat`; synthetic
examples are documented in [Provider and adapter compatibility](adapter-compatibility.md).
