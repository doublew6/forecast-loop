"""Generate a synthetic forecast bundle and reject a tampered export."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import Database
from app.services.run_bundle import (
    RunBundleError,
    export_run_bundle,
    verify_run_bundle,
)
from app.services.schema_readiness import upgrade_database
from app.services.wiki import WikiCatalog
from app.workflow import CommitteeWorkflow


def main() -> None:
    with TemporaryDirectory(prefix="forecast-loop-example-") as directory:
        root = Path(directory)
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite:///{root / 'demo.sqlite3'}",
            checkpoint_path=root / "checkpoints.sqlite3",
            wiki_path=root / "wiki",
            bundled_wiki_path=Path(__file__).resolve().parents[1] / "wiki",
            market_universe_path=None,
            timezone="Asia/Shanghai",
            prediction_status_root=root / "prediction-status",
            user_judgment_wiki_root=root / "user-wiki",
            execution_provider="demo",
            demo_mode=True,
            auto_seed=False,
            agent_trace_enabled=False,
        )
        upgrade_database(settings.database_url)
        database = Database(settings.database_url)
        workflow = CommitteeWorkflow(
            settings=settings,
            database=database,
            wiki=WikiCatalog.from_settings(settings),
        )
        try:
            run = workflow.run(as_of=datetime(2026, 8, 3, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
            bundle = export_run_bundle(database, run_id=run.id, output_root=root / "exports")
            manifest = verify_run_bundle(bundle)
            (bundle / "forecasts.json").write_text("[]\n", encoding="utf-8")
            tamper_rejected = False
            try:
                verify_run_bundle(bundle)
            except RunBundleError as error:
                if "hash mismatch" not in str(error):
                    raise
                tamper_rejected = True
            if not tamper_rejected:
                raise RuntimeError("Tampered export was unexpectedly accepted")
            print(
                json.dumps(
                    {
                        "mode": "demo",
                        "status": manifest.status,
                        "artifacts_verified": len(manifest.artifacts),
                        "tamper_rejected": tamper_rejected,
                    }
                )
            )
        finally:
            workflow.close()
            database.dispose()


if __name__ == "__main__":
    main()
