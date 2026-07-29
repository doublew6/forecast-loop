# Public adapter examples

This directory contains the two official, keyless compatibility examples:

- `providers/public_json_signal_provider.py` implements `AgentSignalSource` and
  returns untrusted `AgentSignalDraft` records.
- `adapters/public_json_evidence_adapter.py` implements
  `EvidenceSnapshotSource` and returns a validated `FrozenEvidenceSnapshot`.

Both examples read one exact regular JSON file and write nothing. They do not
read environment variables, model keys, personal paths, application databases,
or upstream production stores. Their checked-in fixtures are synthetic and
explicitly declare `CC0-1.0` with redistribution allowed. The declaration
applies only to the synthetic fixture data. It does not grant rights to data
from an upstream website or vendor.

Run the official compatibility tests from the repository root:

```bash
uv run pytest backend/tests/test_adapter_examples_compatibility.py
```

The examples are teaching integrations, not live market providers. Copy the
boundary pattern, not the synthetic values. See
[`docs/adapter-compatibility.md`](../docs/adapter-compatibility.md) for the
contract matrix, cutoff duties, and the reusable test-kit API.
