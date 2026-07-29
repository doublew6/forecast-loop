# Read-only Quant signal adapter

forecast-loop accepts quantitative signals through a source-neutral,
content-addressed bundle. The core validates an already produced bundle; it
does not train a model, open a source database, or execute code stored in an
artifact.

## Bundle

The public `forecast-loop.quant-signal-bundle/v1` manifest binds:

- bundle identity, creation time, evidence cutoff, and content hash;
- the selected Market Universe hash;
- code, parameter, feature, model, and input-snapshot artifacts;
- raw SHA-256 digests and stable artifact versions;
- one complete signal per declared target and horizon;
- direction, probabilities, rationale, counter-evidence, and invalidation
  conditions.

Every artifact path is relative to the manifest directory. The reader rejects
absolute paths, parent traversal, symlinks, non-regular files, changed files,
duplicate identities, missing targets, invalid timestamps, mismatched hashes,
and unexpected extra targets.

Artifact code is treated as inert bytes. The adapter never imports or executes
it.

## Admission

The host, not the producer, owns trusted identity and participation policy. A
validated draft is accepted only after the host binds:

- an active `AgentSpec`;
- the current run and Evidence Snapshot;
- the exact target and Market Universe;
- artifact and manifest hashes;
- a declared read-only capability set.

The resulting `SignalEnvelope` is immutable and auditable. A producer cannot
self-assign formal decision weight.

## Shadow-only participation

The public core admits Quant signals only to shadow evaluation and always
binds their formal decision weight to zero. A bundle cannot request, derive, or
self-report decision authority. Any future formal activation mechanism would
require a separate public contract and governance review; it is not part of
this adapter.

The public core intentionally does not prescribe a training framework,
feature set, model family, calibration rule, sample window, or weighting
formula. Those choices belong to independently maintained extensions.

## Redistributable fixture

`backend/tests/fixtures/quant/read-only-v1/` contains synthetic artifacts used
to test parsing, hashing, path confinement, and fail-closed behavior. It
contains no licensed market data, account identifiers, credentials, or
production model intellectual property.

## Extension checklist

An external Quant producer should:

1. read only authorized, frozen inputs;
2. write only a new bundle under an operator-controlled output root;
3. use stable artifact versions and canonical hashes;
4. preserve event, publication, ingestion, and cutoff time semantics;
5. emit a complete target matrix or fail without partial output;
6. keep credentials, provider mappings, and proprietary research outside this
   repository;
7. pass the public compatibility and security tests before integration.
