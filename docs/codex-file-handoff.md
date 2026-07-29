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
input hashes. It then creates `data/handoffs/<run-id>/` containing:

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
On success it writes an immutable receipt. Repeating finalize is idempotent;
an expired or rejected package must be replaced by a newly prepared run.

## Scheduling and deployment

The public repository does not prescribe a machine, scheduler, model, or
execution time. Operators may invoke prepare and finalize manually or from
their own scheduler, provided the same file permissions and deterministic
checks are preserved. Deployment configuration and source-specific adapters
belong outside the public core.

## Security boundary

- Do not expose file-mode finalize as an HTTP endpoint.
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
