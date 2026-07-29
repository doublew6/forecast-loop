# Daily Reflection

Daily Reflection evaluates completed Live forecasts after their target outcome
is available. It separates deterministic outcome calculation from
model-assisted source discovery and analysis.

Demo runs never create formal Reflections, Lessons, or Wiki inputs.

## Trust boundary

- A Reflection may use only a completed Live forecast and immutable
  evaluations produced from a reviewed market-outcome snapshot.
- Outcome and source adapters are read-only and repository-external.
- Draft writers may create only `source-discovery/drafts.json` and
  `analysis/drafts.json` inside the prepared job.
- Python validates timestamps, identities, hashes, severity, findings,
  persistence, and receipts.
- A Reflection never rewrites its source forecast or promotes a Lesson
  directly into the Wiki.

If an outcome, trading calendar, publication record, or required source cannot
be verified, the run fails closed or keeps the relevant cause `unresolved`.

## Workflow

```text
reviewed outcome snapshot
  -> deterministic evaluation import
  -> reflection prepare
  -> source-discovery draft
  -> trusted source freeze
  -> analysis draft
  -> deterministic finalize
  -> deterministic Markdown render
```

### 1. Import a reviewed outcome

Use a repository-external adapter to create the public market snapshot
contract, then import it:

```bash
MARKET_OUTCOME_SNAPSHOT_BUILDER=./adapters/outcome-builder \
  make market-snapshot ARGS="\
    --target-date YYYY-MM-DD \
    --horizon D1 \
    --captured-at ISO8601 \
    --output ./input/market-snapshot.json"

make market-import ARGS="import ./input/market-snapshot.json"
```

The snapshot must bind the target identity, market clock, prices, provenance,
quality status, and content hash. The public core does not know the adapter's
provider credentials or source layout.

### 2. Prepare a Reflection

```bash
make reflection-prepare ARGS="\
  <source-run-id> \
  --horizon D1 \
  --market-snapshot ./input/market-snapshot.json"
```

Preparation freezes the source forecast, evaluation, Agent roster, allowed
finding identities, and evidence cutoff into
`data/reflections/<reflection-job-id>/`.

### 3. Discover and freeze sources

The source-discovery stage proposes HTTPS URLs for an independent collector to
review. It does not self-report page text, publication times, or hashes.

When a trusted capture bundle is available:

```bash
make reflection-freeze-sources ARGS="\
  ./data/reflections/<reflection-job-id> \
  --sources ./input/captures.json"
```

Without a trusted bundle, omit `--sources`. The deterministic stage freezes an
empty source set and analysis must leave unsupported causes unresolved.

### 4. Analyze and finalize

After writing the analysis draft allowed by the prepared instructions:

```bash
make reflection-finalize ARGS="./data/reflections/<reflection-job-id>"
make reflection-render ARGS="<reflection-id>"
```

Finalize writes the database rows and receipt. Render reads only validated
records and produces the human-readable archive. Neither command changes the
published Wiki.

## Lesson governance

A Lesson begins as a candidate. Replay, approval, revalidation, and lifecycle
hash verification remain separate deterministic operations:

```bash
make lesson-replay ARGS="./input/replay.json --submitted-by reviewer"
make lesson-approve ARGS="<lesson-id> --reviewer reviewer --notes-file ./input/review.md"
make lesson-due
make lesson-revalidate ARGS="<lesson-id> --reviewer reviewer --notes-file ./input/review.md"
make lesson-verify ARGS="<lesson-id>"
```

Sample thresholds and approval policies are versioned operator policy, not
hard-coded deployment facts. An active Lesson is not automatically a
published Wiki entry.

## API and files

Reflection and Lesson API routes are read-only views of validated state.
Prepare, source freeze, finalize, render, review, and promotion remain explicit
local commands. Runtime packages, capture bundles, receipts, and databases are
private operator data and are ignored by Git.
