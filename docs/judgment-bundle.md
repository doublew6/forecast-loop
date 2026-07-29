# Portable User Judgment bundles

User Judgment bundles are immutable, offline-verifiable exports of one sealed
manual prediction. They preserve the prediction, reasoning, counter-evidence,
invalidation condition, exact Forecast binding, Agent contract, and—when the
record is eligible for formal shadow scoring—the completed trusted evaluation.

They are deliberately separate from committee run bundles:

- a judgment bundle contains a one-way reference to `run_id`,
  `run_input_hash`, `forecast_id`, and `forecast_input_hash`;
- export never opens a committee bundle for writing;
- an output path inside a recognized committee run bundle is rejected;
- a judgment can be exported after the run without changing historical
  `run.json`, `opinions.json`, `forecasts.json`, or their manifest.

## Commands

Run migrations before database-backed export:

```bash
uv run forecast-loop judgment export <judgment-id>
uv run forecast-loop judgment verify data/judgment-bundles/<judgment-id>
```

The equivalent Make targets are:

```bash
make judgment-export ARGS="<judgment-id>"
make judgment-verify ARGS="data/judgment-bundles/<judgment-id>"
```

`judgment verify <judgment-id>` remains available for verifying the private
database record and its Markdown seal. A path verifies a portable bundle and
does not require the source database or private Wiki.

By default `actor_id` is absent from `judgment.json` and the manifest says
`actor_privacy=omitted`. The prediction and its reasoning are part of the
explicit export scope, but a local operator identifier is not required for
portable verification. Source judgment/Wiki hashes that include `actor_id` are
also omitted, preventing a low-entropy identifier from being guessed by
recomputing those hashes. The default `data/judgment-bundles/` directory is
gitignored. Only use `--include-actor-id` when disclosure is intentional:

```bash
uv run forecast-loop judgment export <judgment-id> --include-actor-id
```

Export still verifies the source database row, private Markdown and any formal
evaluation before creating the privacy-minimized projection. Portable integrity
then comes from the artifact hashes, manifest hash and bundle hash.

## Fixed file set

Every v1 directory contains exactly:

```text
<judgment-id>/
  agent-spec.json
  forecast.json
  judgment.json
  evaluation.json
  manifest.json
```

- `agent-spec.json` is the exact content-addressed
  `forecast-loop.agent-spec/v1` frozen when the judgment was created. New
  judgments store that hash explicitly; an unbound legacy row is exportable
  only when its append-only historical spec lookup is unique.
- `forecast.json` is a privacy-minimized
  `forecast-loop.forecast-binding/v1`, not a copy of the committee run bundle.
  It freezes run and Forecast hashes, target identity, committee direction, and
  outcome observation status.
- `judgment.json` uses
  `forecast-loop.user-judgment-export/v1` and contains the prediction,
  confidence hex, reasoning, counter-evidence, invalidation condition,
  submission seal, and Forecast/run bindings.
- `evaluation.json` uses
  `forecast-loop.judgment-evaluation-export/v1`.
- `manifest.json` uses `forecast-loop.judgment-bundle/v1`.

Unknown fields, non-finite JSON numbers, duplicate keys, symlinks, unexpected
files, unsafe artifact paths, oversized files, and content changes while a file
is read all fail closed.

## Record classes and evaluation policy

The manifest and `judgment.json` both carry one of three mutually exclusive
classes:

| `record_class` | Meaning | `evaluation.json` |
| --- | --- | --- |
| `demo` | Practice record; never formally scored | `not_applicable`, reason `demo` |
| `non_blind_archive` | Live personal archive without blind attestation | `not_applicable`, reason `non_blind_archive` |
| `formal_shadow` | Live, deadline-safe, blind-attested shadow record | Must be `completed` |

For a formal shadow record, export is blocked until both the trusted Forecast
`EvaluationResult` and immutable `UserJudgmentEvaluation` exist. Offline
verification also requires:

- a completed outcome in `forecast.json`;
- matching judgment, Forecast, run, input, batch, and observation identities;
- a completed evaluation rather than a forged `not_applicable` marker.

Demo and non-blind records use `not_applicable`, not `pending`. This distinction
prevents a practice or archive record from being presented as a formal result
that is merely waiting for evaluation.

## Hash layers

All JSON files are encoded as UTF-8 canonical JSON with sorted keys, compact
separators, and one trailing LF.

The v1 verifier checks three layers:

1. Each artifact has an exact byte `sha256` and `size` in the manifest.
2. `manifest_hash` is canonical SHA-256 over all manifest fields except
   `manifest_hash` and `bundle_hash`.
3. `bundle_hash` is canonical SHA-256 over the schema version, manifest hash,
   and ordered artifact path/hash/size list.

These hashes detect accidental or malicious modification of a fixed bundle,
but they are not a publisher signature or trusted timestamp. A future signed
release policy can sign `bundle_hash` without changing the v1 artifact files.

## Failure examples

Verification rejects:

- a changed rationale or any other artifact byte;
- a missing, additional, symlinked, or renamed file;
- a rehashed `forecast.json` whose Forecast/run/input identity does not match
  `judgment.json`;
- a formal shadow record whose evaluation is missing, incomplete, from another
  Forecast, or bound to another observation;
- an AgentSpec whose identity, version, content hash, probability capability,
  or shadow policy does not match the judgment;
- a manifest whose privacy, class, evaluation state, or hash layers disagree
  with its artifacts.
