"""Fail closed unless an npm v2 audit report contains zero vulnerabilities."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

AUDIT_REPORT_VERSION: Final = 2
EXPECTED_ROUTER_VERSION: Final = "8.3.0"
EXPECTED_NODE_ENGINE: Final = ">=22.22"
SEVERITIES: Final = ("info", "low", "moderate", "high", "critical")


class NpmAuditPolicyError(RuntimeError):
    """The npm audit report or locked project configuration is unsafe."""


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise NpmAuditPolicyError(f"{label} must be a JSON object")
    return value


def _json_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NpmAuditPolicyError(f"cannot read {label} as JSON") from exc
    return _object(value, label)


def validate_project_configuration(repository: Path) -> None:
    """Require the patched router and its runtime baseline to stay exact."""

    root = repository.resolve(strict=True)
    package = _json_object(
        root / "frontend" / "package.json",
        "frontend/package.json",
    )
    dependencies = _object(
        package.get("dependencies"),
        "frontend/package.json dependencies",
    )
    if dependencies.get("react-router") != EXPECTED_ROUTER_VERSION:
        raise NpmAuditPolicyError(
            f"react-router must be pinned exactly to {EXPECTED_ROUTER_VERSION}"
        )
    if "react-router-dom" in dependencies:
        raise NpmAuditPolicyError(
            "react-router-dom must not reintroduce the vulnerable compatibility package"
        )
    engines = _object(package.get("engines"), "frontend/package.json engines")
    if engines.get("node") != EXPECTED_NODE_ENGINE:
        raise NpmAuditPolicyError(
            f"Node.js engine must remain {EXPECTED_NODE_ENGINE}"
        )

    lock = _json_object(
        root / "frontend" / "package-lock.json",
        "frontend/package-lock.json",
    )
    if lock.get("lockfileVersion") != 3:
        raise NpmAuditPolicyError(
            "frontend/package-lock.json must use lockfileVersion 3"
        )
    packages = _object(
        lock.get("packages"),
        "frontend/package-lock.json packages",
    )
    root_package = _object(
        packages.get(""),
        "frontend/package-lock.json root package",
    )
    root_dependencies = _object(
        root_package.get("dependencies"),
        "frontend/package-lock.json root dependencies",
    )
    if root_dependencies.get("react-router") != EXPECTED_ROUTER_VERSION:
        raise NpmAuditPolicyError(
            "package-lock root must pin the reviewed react-router version"
        )
    if "react-router-dom" in root_dependencies:
        raise NpmAuditPolicyError(
            "package-lock root must not contain react-router-dom"
        )
    router = _object(
        packages.get("node_modules/react-router"),
        "locked react-router package",
    )
    if router.get("version") != EXPECTED_ROUTER_VERSION:
        raise NpmAuditPolicyError(
            "locked react-router version differs from the reviewed version"
        )


def validate_npm_audit(
    report: Mapping[str, object],
    repository: Path,
) -> dict[str, object]:
    """Validate an npm v2 report and require every severity count to be zero."""

    if report.get("auditReportVersion") != AUDIT_REPORT_VERSION:
        raise NpmAuditPolicyError(
            "npm audit report must use auditReportVersion 2"
        )
    vulnerabilities = _object(
        report.get("vulnerabilities"),
        "vulnerabilities",
    )
    if vulnerabilities:
        raise NpmAuditPolicyError(
            "npm audit must contain zero vulnerabilities at every severity"
        )
    metadata = _object(report.get("metadata"), "metadata")
    counts = _object(
        metadata.get("vulnerabilities"),
        "metadata.vulnerabilities",
    )
    for severity in SEVERITIES:
        if counts.get(severity) != 0:
            raise NpmAuditPolicyError(
                f"metadata.vulnerabilities.{severity} must be zero"
            )
    if counts.get("total") != 0:
        raise NpmAuditPolicyError(
            "metadata.vulnerabilities.total must be zero"
        )

    validate_project_configuration(repository)
    return {
        "policy": "forecast-loop.npm-audit-zero/v1",
        "vulnerability_count": 0,
        "router_version": EXPECTED_ROUTER_VERSION,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Require a zero-vulnerability npm v2 audit report."
    )
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=Path("."))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = _json_object(arguments.audit_report, "npm audit report")
        result = validate_npm_audit(report, arguments.repository)
    except (NpmAuditPolicyError, OSError) as exc:
        print(f"npm audit policy rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
