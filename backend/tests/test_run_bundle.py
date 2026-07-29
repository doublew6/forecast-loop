from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from app.services.run_bundle import (
    MAX_ARTIFACT_BYTES,
    RunBundleError,
    export_run_bundle,
    verify_run_bundle,
)


def _completed_run(client) -> str:
    response = client.post(
        "/api/runs",
        json={"as_of": "2026-07-13T15:30:00+08:00"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_export_and_verify_completed_run_bundle(client, tmp_path) -> None:
    run_id = _completed_run(client)

    bundle = export_run_bundle(
        client.app.state.database,
        run_id=run_id,
        output_root=tmp_path / "exports",
        exported_at=datetime(2026, 7, 24, 12, tzinfo=UTC),
    )
    manifest = verify_run_bundle(bundle)

    assert bundle.name == run_id
    assert manifest.run_id == run_id
    assert manifest.status == "completed"
    assert [item.path for item in manifest.artifacts] == [
        "run.json",
        "opinions.json",
        "forecasts.json",
    ]
    assert all(len(item.sha256) == 64 for item in manifest.artifacts)
    assert len(manifest.bundle_hash) == 64

    run_payload = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    forecast_payload = json.loads(
        (bundle / "forecasts.json").read_text(encoding="utf-8")
    )
    assert run_payload["id"] == run_id
    assert run_payload["forecasts_count"] == len(forecast_payload)


def test_verify_rejects_tampered_artifact(client, tmp_path) -> None:
    run_id = _completed_run(client)
    bundle = export_run_bundle(
        client.app.state.database,
        run_id=run_id,
        output_root=tmp_path / "exports",
    )
    (bundle / "forecasts.json").write_text("[]\n", encoding="utf-8")

    with pytest.raises(RunBundleError, match="hash mismatch"):
        verify_run_bundle(bundle)


def test_export_never_overwrites_existing_bundle(client, tmp_path) -> None:
    run_id = _completed_run(client)
    output_root = tmp_path / "exports"
    export_run_bundle(
        client.app.state.database,
        run_id=run_id,
        output_root=output_root,
    )

    with pytest.raises(RunBundleError, match="already exists"):
        export_run_bundle(
            client.app.state.database,
            run_id=run_id,
            output_root=output_root,
        )


def test_verify_rejects_unexpected_member(client, tmp_path) -> None:
    run_id = _completed_run(client)
    bundle = export_run_bundle(
        client.app.state.database,
        run_id=run_id,
        output_root=tmp_path / "exports",
    )
    (bundle / "unexpected.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RunBundleError, match="unexpected files"):
        verify_run_bundle(bundle)


def test_verify_rejects_oversized_artifact_before_reading(client, tmp_path) -> None:
    run_id = _completed_run(client)
    bundle = export_run_bundle(
        client.app.state.database,
        run_id=run_id,
        output_root=tmp_path / "exports",
    )
    with (bundle / "forecasts.json").open("wb") as stream:
        stream.truncate(MAX_ARTIFACT_BYTES + 1)

    with pytest.raises(RunBundleError, match="exceeds the size limit"):
        verify_run_bundle(bundle)
