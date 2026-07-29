from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.adapters import (
    ExternalEvidenceSnapshotBuilder,
    ExternalMarketOutcomeSnapshotBuilder,
)
from app.domain import Horizon
from app.ports import (
    EvidenceSnapshotBuilder,
    MarketOutcomeSnapshotBuilder,
    NoApplicableSessionError,
    SnapshotBuilderError,
)

ZONE = ZoneInfo("Asia/Shanghai")


def _builder_executable(tmp_path: Path, *, exit_code: int = 0) -> Path:
    path = tmp_path / f"builder-{exit_code}"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import pathlib\n"
        "import sys\n"
        f"exit_code = {exit_code}\n"
        "if exit_code:\n"
        "    print('session unavailable', file=sys.stderr)\n"
        "    raise SystemExit(exit_code)\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "output.write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o700)
    return path


def test_external_evidence_builder_uses_source_neutral_contract(
    tmp_path: Path,
) -> None:
    adapter = ExternalEvidenceSnapshotBuilder(
        executable=_builder_executable(tmp_path),
        timezone="Asia/Shanghai",
        timeout_seconds=10,
    )
    assert isinstance(adapter, EvidenceSnapshotBuilder)
    output = tmp_path / "evidence.json"

    adapter.build_snapshot(
        base_session=date(2026, 7, 29),
        captured_at=datetime(2026, 7, 29, 16, 10, tzinfo=ZONE),
        output_path=output,
    )

    arguments = json.loads(output.read_text(encoding="utf-8"))
    assert arguments == [
        "--base-session",
        "2026-07-29",
        "--captured-at",
        "2026-07-29T16:10:00+08:00",
        "--output",
        str(output),
    ]


def test_external_market_builder_uses_source_neutral_contract(
    tmp_path: Path,
) -> None:
    adapter = ExternalMarketOutcomeSnapshotBuilder(
        executable=_builder_executable(tmp_path),
        timezone="Asia/Shanghai",
        timeout_seconds=10,
    )
    assert isinstance(adapter, MarketOutcomeSnapshotBuilder)
    output = tmp_path / "market.json"

    adapter.build_snapshot(
        target_date=date(2026, 7, 29),
        horizon=Horizon.D2,
        captured_at=datetime(2026, 7, 29, 16, 20, tzinfo=ZONE),
        output_path=output,
    )

    arguments = json.loads(output.read_text(encoding="utf-8"))
    assert arguments == [
        "--target-date",
        "2026-07-29",
        "--horizon",
        "D2",
        "--captured-at",
        "2026-07-29T16:20:00+08:00",
        "--output",
        str(output),
    ]


def test_external_builder_exit_three_means_no_applicable_session(
    tmp_path: Path,
) -> None:
    adapter = ExternalEvidenceSnapshotBuilder(
        executable=_builder_executable(tmp_path, exit_code=3),
        timezone="Asia/Shanghai",
        timeout_seconds=10,
    )

    with pytest.raises(NoApplicableSessionError, match="session unavailable"):
        adapter.build_snapshot(
            base_session=date(2026, 7, 29),
            captured_at=datetime(2026, 7, 29, 17, 50, tzinfo=ZONE),
            output_path=tmp_path / "unused.json",
        )


def test_external_builder_rejects_symlink_executable(tmp_path: Path) -> None:
    executable = _builder_executable(tmp_path)
    link = tmp_path / "builder-link"
    link.symlink_to(executable)
    adapter = ExternalEvidenceSnapshotBuilder(
        executable=link,
        timezone="Asia/Shanghai",
        timeout_seconds=10,
    )

    with pytest.raises(SnapshotBuilderError, match="may not be a symlink"):
        adapter.build_snapshot(
            base_session=date(2026, 7, 29),
            captured_at=datetime(2026, 7, 29, 17, 50, tzinfo=ZONE),
            output_path=tmp_path / "unused.json",
        )
