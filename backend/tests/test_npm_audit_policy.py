from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_npm_audit import (
    NpmAuditPolicyError,
    main,
    validate_npm_audit,
    validate_project_configuration,
)

REPOSITORY = Path(__file__).resolve().parents[2]


def _zero_report() -> dict[str, object]:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {},
        "metadata": {
            "vulnerabilities": {
                "info": 0,
                "low": 0,
                "moderate": 0,
                "high": 0,
                "critical": 0,
                "total": 0,
            }
        },
    }


def test_zero_vulnerability_report_and_patched_router_are_accepted() -> None:
    result = validate_npm_audit(_zero_report(), REPOSITORY)

    assert result == {
        "policy": "forecast-loop.npm-audit-zero/v1",
        "vulnerability_count": 0,
        "router_version": "8.3.0",
    }


def test_any_reported_vulnerability_is_rejected() -> None:
    report = _zero_report()
    report["vulnerabilities"] = {
        "example": {
            "name": "example",
            "severity": "low",
        }
    }
    report["metadata"]["vulnerabilities"]["low"] = 1  # type: ignore[index]
    report["metadata"]["vulnerabilities"]["total"] = 1  # type: ignore[index]

    with pytest.raises(NpmAuditPolicyError, match="zero vulnerabilities"):
        validate_npm_audit(report, REPOSITORY)


def test_metadata_must_match_a_zero_report() -> None:
    report = _zero_report()
    report["metadata"]["vulnerabilities"]["high"] = 1  # type: ignore[index]

    with pytest.raises(NpmAuditPolicyError, match="high must be zero"):
        validate_npm_audit(report, REPOSITORY)


def test_router_configuration_uses_patched_direct_package() -> None:
    validate_project_configuration(REPOSITORY)

    package = json.loads(
        (REPOSITORY / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    assert package["dependencies"]["react-router"] == "8.3.0"
    assert "react-router-dom" not in package["dependencies"]
    assert package["engines"]["node"] == ">=22.22"


def test_cli_accepts_a_zero_report(tmp_path: Path, capsys) -> None:
    report = tmp_path / "npm-audit.json"
    report.write_text(json.dumps(_zero_report()), encoding="utf-8")

    assert main(
        [
            "--repository",
            str(REPOSITORY),
            "--audit-report",
            str(report),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["vulnerability_count"] == 0


@pytest.mark.parametrize(
    "workflow_name",
    ["security.yml", "release.yml"],
)
def test_security_workflows_do_not_suppress_trivy_findings(
    workflow_name: str,
) -> None:
    workflow = (
        REPOSITORY / ".github" / "workflows" / workflow_name
    ).read_text(
        encoding="utf-8"
    )

    assert "trivyignores" not in workflow
    assert "scripts/check_npm_audit.py" in workflow
