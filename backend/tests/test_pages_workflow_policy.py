from __future__ import annotations

from pathlib import Path

import yaml

REPOSITORY = Path(__file__).resolve().parents[2]


def test_pages_deployment_requires_explicit_opt_in() -> None:
    workflow_path = REPOSITORY / ".github" / "workflows" / "pages.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    for job_name in ("build", "deploy"):
        condition = jobs[job_name]["if"]

        assert "github.event_name == 'workflow_dispatch'" in condition
        assert "vars.PAGES_ENABLED == 'true'" in condition
        assert "continue-on-error" not in jobs[job_name]

    assert jobs["deploy"]["needs"] == "build"
    assert jobs["deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }
