# Read-only public showcase

The public showcase is a separate static entrypoint. It reads same-origin
`forecast.json` using GET, with no login, submission, model invocation,
operational API client or operator proxy. Existing local and Demo apps remain
separate. Visitor traffic does not increase model token usage.

## Build and publish

```sh
make showcase ARGS="--database ./data/forecast.sqlite3"
make showcase ARGS="--snapshot ./data/public/forecast.json"
make showcase ARGS="--empty"
```

Choose exactly one source. The last command explicitly builds a waiting page;
it never fabricates a real prediction. Output is the Git-ignored `dist/showcase/`.
Publish only that directory to a static host such as Cloudflare Pages. Never
upload private source databases, dotenv files or runtime directories.

The database must already exist. The exporter uses SQLite read-only mode and
never migrates, seeds, starts a prediction or changes source records. It verifies
program/snapshot seals, finalized receipts, decision signals, forecast hashes and
settlement bindings. Live is the default; synthetic input requires `--demo` and
is labeled visibly even when expired. Failed export/build/scan preserves the
previous successful artifact, and must never trigger deployment.

Publication allows only hashes, known targets, lanes, dates, cutoff/seal times,
probabilities, thresholds and verified settlement scalars. Research prose, Wiki,
raw evidence, URLs, operator identity, prompts, model configuration and logs are
excluded. Review the numeric publication scope and redistribution rights before
deploying. The isolated build loads no dotenv, copies no public asset directory
and emits no source maps. Its `_headers` provides browser and cache policies on
compatible hosts; verify equivalent headers elsewhere.

This adapter supports focused-v2 D1 close-return and W1 relative-return records.
Premarket open-to-open forecasts and legacy v1 are not reinterpreted as these
targets. Those sources need separately versioned adapters before publication.

## Daily updates

Attach publication to the existing producer after finalize and after settlement:

1. Export and build using the producer's existing source database.
2. Require success and scan the exact static artifact.
3. Deploy only the artifact, then verify the public snapshot.

This repository contains no deployment credential or enabled recurring job.
Building does not enable unattended updates. The scheduler must actually reach
both the source and the publishing credential, stored outside the repository.
A cloud schedule cannot read a local database without a supported data path.

For an independent JSON export, create the output directory first:

```sh
PYTHONPATH=backend .venv/bin/python -m app.public_showcase \
  --database ./data/forecast.sqlite3 \
  --output ./data/public/forecast.json \
  --next-update-at 2026-08-14T20:30:00+08:00
```

The optional refresh deadline is explicit, not a market-calendar inference.
By default it is 26 hours after the latest forecast seal, not the export time;
re-exporting old forecasts cannot make them look fresh. A production scheduler
should derive session-aware deadlines from its already validated calendar.

The page checks for new static data every five minutes and expiry every minute.
Refresh failures retain previously loaded results with a warning; absent or
invalid input never falls back to Demo. Formal/Shadow and D1/W1 scores are
separate, pending results are excluded, neutral is included in three-class
accuracy, and Brier uses the existing three-class mean squared error (0 to 2/3).
Samples below 20 receive a warning.
Statistics cover the public window of at most 400 predictions, not lifetime
performance. Original dates and cutoff times remain visible when stale.

## Preview and checks

Port 4178 is reserved for this showcase. Inspect the listener before starting;
never stop another task's process. Dedicated Vite configuration uses strict
binding and no API proxy. For the built artifact:

```sh
.venv/bin/python -m http.server 4178 --bind 127.0.0.1 --directory dist/showcase
```

Backend tests in `backend/tests/test_public_showcase.py` cover read-only export,
seals, settlement, strict schemas, symlink rejection and preserved staleness.
Frontend tests in `frontend/src/showcase/Showcase.test.tsx` verify GET-only reads,
separate lane scores, explicit synthetic labeling and unavailable input.

## Intraday supplements

The additive `public-showcase/v2` publication keeps the original `forecasts`
collection and scoring unchanged. A separate `intraday_forecasts` collection
accepts explicitly authorized `csi1000-open-to-open-d1` updates. Each row retains
its actual intraday timestamp, cutoff, dates, probabilities and threshold, with
`publication_kind=intraday_supplement`, `performance_included=false` and
`evidence_status=partial`. The page labels these as 盘中补发 and explains that the
open-to-open interval has partly elapsed. They cannot be relabeled as premarket
forecasts, inserted into close-return records or counted in the existing scores.

A private source adapter must verify the original input seal, assignment and
citation permissions, draft provenance, deterministic probability shrinkage,
report text hash and delivery receipt before emitting these public scalars.
The public build accepts only the strict scalar contract; research prose,
private paths and source payloads remain excluded. Legacy v1 publications still
parse unchanged. The refresh deadline for a supplement is based on its actual
publication time, never the time an unchanged record is exported again.
