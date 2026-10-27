# First auditable forecast

forecast-loop is a local research framework for comparing human, quantitative,
and AI forecasting signals. It records probabilities, reasoning, evidence
versions, and evaluation results so that a prediction can be reviewed later.

## Run the keyless example

From a source checkout, install the locked Python environment with Python 3.11+
and uv, then run:

```bash
uv sync --frozen
uv run python examples/first_forecast.py
```

Dependency installation may access package indexes. The example itself calls no
model or market-data service, needs no API key, loads no model weights, and starts
no server. It uses the deterministic demo provider and bundled synthetic Wiki
material, ignores the repository `.env`, and keeps its database, checkpoints,
and exports inside a temporary directory that is removed on exit.

The [source](../../examples/first_forecast.py) performs these steps:

1. Migrate a new isolated database.
2. Generate a completed synthetic forecast run.
3. Export `run.json`, `opinions.json`, and `forecasts.json` into a bundle.
4. Verify the bundle schemas and hashes.
5. Replace the exported forecast copy and verify that the hash mismatch is rejected.

After migration diagnostics, the last line is:

```json
{"mode": "demo", "status": "completed", "artifacts_verified": 3, "tamper_rejected": true}
```

The source database is not changed by the export-tampering check. This is an
integrity demonstration, not evidence of forecasting skill. Demo forecasts never
enter formal performance records, and hashes do not prove that underlying
sources or reasoning are true.

## Move from demo to evaluated research

Real outcome evaluation requires a Live forecast sealed before its deadline,
licensed or otherwise authorized evidence, trustworthy outcome observations,
and the matching frozen market clock. Follow the
[data-source rules](../data-sources.md),
[snapshot contract](../snapshot-adapters.md), and
[market-universe configuration](../market-universes.md).
Do not relabel this demo as Live, backfill an ex-ante record, or treat synthetic
data as market observations.

Existing regression checks exercise the public contracts:

```bash
uv run pytest backend/tests/test_run_bundle.py backend/tests/test_evaluation_integrity.py backend/tests/test_adapter_examples_compatibility.py
```

For user interfaces, follow [Quick start](../../README_EN.md#quick-start).
Node.js is needed for the React frontend; it is not needed for the Python
example above. Live model and data adapters have their own configuration and
authorization requirements. The framework does not place orders.

## Integration references

- [Read-only adapter examples](../../examples/README.md)
- [Agent framework](../agent-model.md)
- [Portable audit bundles](../audit-bundle.md)
- [Secure deployment](../secure-deployment.md)
- [Report an issue](https://github.com/doublew6/forecast-loop/issues)

Use synthetic inputs in public bug reports. Keep operator data, credentials,
private integrations, and local paths outside public reports and commits.
