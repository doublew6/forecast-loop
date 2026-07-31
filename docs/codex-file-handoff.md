# Codex file handoff

forecast-loop supports an asynchronous file boundary for model-assisted
forecast drafts:

```text
prepare -> sealed input package -> drafts.json -> deterministic finalize
```

The model is not a synchronous dependency of the API. Python owns input
collection, timestamps, schemas, hashes, identity, citation checks,
aggregation, persistence, and receipts. The draft writer may edit only the
single `drafts.json` declared by the handoff package.

## Prepare

Create a demo package with:

```bash
make codex-prepare ARGS="--mode demo"
```

A Live package additionally requires a reviewed Evidence Snapshot:

```bash
make codex-prepare ARGS="--mode live --snapshot ./input/evidence-snapshot.json"
```

An optional Quant signal manifest can be supplied through the public sealed
bundle contract:

```bash
make codex-prepare ARGS="\
  --mode live \
  --snapshot ./input/evidence-snapshot.json \
  --quant-manifest ./input/quant-bundle/manifest.json"
```

Preparation freezes the Market Universe, target sessions, evidence cutoff,
allowed evidence IDs, Wiki citations, Agent identities, prompt version, and
input hashes. New packages use handoff protocol v3 and seal
`forecast_horizons=["D1"]`; the v1 Market Universe and Evidence Snapshot still
carry D1/D2 calendar sessions for input compatibility, but v3 creates only D1
assignments. Preparation then creates `data/handoffs/<run-id>/` containing:

- `input.json`: the immutable run package;
- `INSTRUCTIONS.md`: the draft-stage contract;
- `drafts.template.json`: the complete output matrix;
- `drafts.json`: the only file the draft writer may create or replace.

The command prints the resulting job directory. Treat that directory as
private runtime state and do not publish it or serve it over HTTP.

## Draft

The draft writer must:

1. read `INSTRUCTIONS.md`, `input.json`, and `drafts.template.json`;
2. use only evidence and Wiki references frozen before the cutoff;
3. preserve the exact assignment identities from the template;
4. write only `drafts.json`;
5. leave input files, receipts, databases, checkpoints, Wiki content, and
   external sources unchanged.

Missing or unverifiable facts stay unresolved. A draft must not add later
information, fabricate citations, or repair a sealed input.

## Finalize

Finalize the package with:

```bash
make codex-finalize ARGS="--mode demo ./data/handoffs/<run-id>"
make codex-finalize ARGS="--mode live ./data/handoffs/<run-id>"
```

Finalize reopens the package through its configured root, rejects symlinks and
path escapes, verifies raw and canonical hashes, validates every assignment,
checks evidence and Wiki references, and persists the result transactionally.
For v3, finalize first seals the exact draft hashes, attempt/checkpoint
identity, finalize time, failed-attempt history, and retry-transition chain as
`validating` in the database before claiming execution. The v3 execution token
must still match at graph claim, success, and failure publication, so an
executor from an older attempt cannot overwrite its successor. On success it
writes an immutable receipt. Repeating finalize for a completed run verifies the
frozen drafts, database output seal, counts, and receipt, then returns the same
receipt without running the graph again. If workflow completion committed
before the terminal output seal or receipt publication, finalize completes the
seal and reconstructs the exact receipt from those durable inputs. A
conflicting receipt or changed draft is rejected. Every completed recovery
path also reopens `drafts.json` without following symlinks, applies mode
`0400`, and syncs the descriptor before returning an existing or reconstructed
receipt.

## Retry a failed v3 attempt

An execution failure is different from an invalid draft. Validation errors
leave the run in `awaiting_draft`, so the writer can correct `drafts.json` and
run finalize again. If execution claimed the run and then failed, retry the
sealed v3 handoff explicitly:

```bash
make codex-retry ARGS="--mode demo ./data/handoffs/<run-id>"
make codex-retry ARGS="--mode live ./data/handoffs/<run-id>"
```

Retry verifies the failed database seal and receipt, requires zero persisted
opinions and forecasts, rejects an active replacement run, and re-verifies all
immutable Quant SignalEnvelope rows. If a process exited after committing the
top-level failure but before publishing/sealing its receipt, retry first
finishes that failed-attempt seal from the durable `validating` audit. If a
process exited after claiming a v3 run as `running`, the exclusive job lock
allows retry to convert only a zero-output, unexpired, audit-matching runner
into an explicit interrupted failure before sealing it. This recovery remains
a local CLI operation and never applies while a live finalizer still holds the
job lock. The lock is held on the job-directory inode, not on a replaceable
named lock file.

Retry then archives the failed `drafts.json` and `receipt.json` without
overwrite under:

```text
data/handoffs/<run-id>/attempts/0001/
```

The same `WorkflowRun`, `input.json`, request hash, evidence/Wiki snapshot,
Quant source records, and original finalize deadline are retained. Only the
attempt number and checkpoint thread advance. The writer must create a new
`drafts.json`, after which normal finalize applies. Retry never admits the
Quant bundle again, never extends the deadline, and never creates a replacement
run. Every `failed -> awaiting_draft` transition is append-only and hash-linked
to the preceding failed attempt, receipt, and transition. Archive files and
their directories are synced before the database is re-armed, so a crash can
be resumed from an identical working/archive copy without admitting different
bytes. A transition cannot predate either the failed execution completion or
its receipt finalization. Re-arm acquires a real SQLite write reservation (or
row lock on PostgreSQL), merges the latest unrelated `data_quality`, and
compare-and-swaps the exact failed-attempt hashes and execution token. Expired,
completed, non-v3, partially persisted, tampered, or superseded runs fail
closed.

The first v3 attempt cannot finalize before `prepared_at`. Later attempts bind
both `drafts.json.generated_at` and the sealed finalize time to the latest
retry transition, so artifacts from the preceding attempt epoch cannot be
admitted. The same chronology is rechecked when a completed receipt is
returned or reconstructed.

Both retry and finalize are local CLI/file operations. They are intentionally
not available over HTTP.

## Protocol compatibility

Finalization dispatches from the frozen protocol, Universe identity, workflow
version, and decision-schema version. It never reinterprets an older package
with current defaults.

| Handoff | Provider | Forecast horizons | Default Universe workflow/schema | Configurable Universe workflow/schema |
| --- | --- | --- | --- | --- |
| v1 | `codex-file-handoff-v1` | D1/D2 | `0.3.0` / `0.4.0` | `0.4.0` / `0.5.0` |
| v2 | `codex-file-handoff-v2` | D1/D2 | `0.3.0` / `0.4.0` | `0.4.0` / `0.5.0` |
| v3 | `codex-file-handoff-v3` | D1 only | `0.5.0` / `0.6.0` | `0.6.0` / `0.7.0` |

The writer emits v3. Existing v1/v2 jobs remain finalizable with their
original assignment matrix, run-ID checkpoint namespace, and legacy audit
shape; v3 attempt history, retry transitions, checkpoint IDs, and execution
tokens are never added to legacy jobs. Duplicate legacy finalize and missing
receipt recovery verify the completed database seal without rebuilding or
rerunning the graph. Unknown combinations,
missing v3 horizon seals, or attempts to add D2 to v3 fail before workflow
execution.

## Scheduling and deployment

The public repository does not prescribe a machine, scheduler, model, or
execution time. Operators may invoke prepare and finalize manually or from
their own scheduler, provided the same file permissions and deterministic
checks are preserved. Deployment configuration and source-specific adapters
belong outside the public core.

## Security boundary

- Do not expose file-mode finalize as an HTTP endpoint.
- Do not expose file-mode retry as an HTTP endpoint.
- Do not make `POST /api/runs` invoke an interactive model task.
- Give the draft writer write access only to its declared `drafts.json`.
- Keep adapter credentials, licensed mappings, and source paths outside the
  repository.
- Bind network services to loopback unless authentication, TLS, and access
  controls are deliberately configured.
- Remember that hashes detect mutation; they do not prove a source is truthful
  or an operator account is uncompromised.

Completed runs can be exported and verified through the public run-bundle and
audit-bundle commands described in [Audit bundles](audit-bundle.md).
