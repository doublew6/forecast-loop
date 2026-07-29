"""Build and health-check the Compose stack against synthetic temporary data."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def run_docker_smoke() -> dict[str, Any]:
    """Run a disposable migrate -> API -> web Compose startup."""

    _run(["docker", "compose", "version"], cwd=ROOT)
    project = f"forecast-loop-smoke-{uuid4().hex[:10]}"
    with tempfile.TemporaryDirectory(prefix="forecast-loop-docker-smoke-") as raw:
        work_root = Path(raw)
        data_root = work_root / "data"
        data_root.mkdir(mode=0o700)
        # The fixed non-root container UID must be able to create synthetic
        # state on both Linux and Docker Desktop bind mounts. This directory is
        # private to this process and is deleted at the end of the smoke test.
        data_root.chmod(0o777)
        env_file = work_root / "smoke.env"
        env_body = "\n".join(
            (
                "VERICOUNCIL_DATABASE_URL=sqlite:////app/data/smoke.sqlite3",
                "VERICOUNCIL_CHECKPOINT_PATH=/app/data/checkpoint.sqlite3",
                "VERICOUNCIL_HANDOFF_ROOT=/app/data/handoffs",
                "VERICOUNCIL_REFLECTION_ROOT=/app/data/reflections",
                "VERICOUNCIL_MARKET_SNAPSHOT_ROOT=/app/data/market-snapshots",
                "VERICOUNCIL_EVIDENCE_SNAPSHOT_ROOT=/app/data/evidence-snapshots",
                "VERICOUNCIL_PREDICTION_STATUS_ROOT=/app/data/prediction-status",
                "VERICOUNCIL_USER_JUDGMENT_WIKI_ROOT=/app/data/user-wiki",
                "VERICOUNCIL_WIKI_PATH=/app/wiki",
                "VERICOUNCIL_EXECUTION_PROVIDER=demo",
                "VERICOUNCIL_DEMO_MODE=true",
                "VERICOUNCIL_AUTO_SEED=false",
                "",
            )
        )
        descriptor = os.open(
            env_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(env_body)
        environment = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": project,
            "FORECAST_LOOP_API_PORT": "0",
            "FORECAST_LOOP_DATA_DIR": str(data_root),
            "FORECAST_LOOP_WEB_PORT": "0",
            "VERICOUNCIL_ENV_FILE": str(env_file),
        }
        try:
            _run(
                [
                    "docker",
                    "compose",
                    "up",
                    "--build",
                    "--detach",
                    "api",
                    "web",
                ],
                cwd=ROOT,
                env=environment,
            )
            _require_successful_migration(environment)
            api_user = _require_non_root_container("api", environment)
            web_user = _require_non_root_container("web", environment)
            health = _wait_for_json_health(
                "api",
                8000,
                "/api/health",
                environment,
            )
            web_status, web_headers = _wait_for_http(
                "web",
                8080,
                "/",
                environment,
            )
            required_headers = {
                "content-security-policy",
                "permissions-policy",
                "referrer-policy",
                "x-content-type-options",
                "x-frame-options",
            }
            missing_headers = sorted(required_headers - web_headers.keys())
            if missing_headers:
                raise RuntimeError(
                    "web response omitted required security headers: "
                    + ", ".join(missing_headers)
                )
            content_security_policy = web_headers["content-security-policy"]
            if (
                "default-src 'self'" not in content_security_policy
                or "frame-ancestors 'none'" not in content_security_policy
            ):
                raise RuntimeError("web response returned an unsafe CSP")
            if health.get("status") != "ok" or web_status != 200:
                raise RuntimeError(
                    f"unexpected Docker smoke health: {health}, web={web_status}"
                )
            return {
                "status": "passed",
                "api_mode": health.get("mode"),
                "web_status": web_status,
                "web_security_headers": sorted(required_headers),
                "migration_database": "synthetic-temporary",
                "runtime_users": {
                    "api": api_user,
                    "web": web_user,
                },
            }
        finally:
            _run(
                [
                    "docker",
                    "compose",
                    "down",
                    "--rmi",
                    "local",
                    "--volumes",
                    "--remove-orphans",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
            )


def _published_url(
    service: str,
    container_port: int,
    path: str,
    environment: dict[str, str],
) -> str:
    result = _run(
        ["docker", "compose", "port", service, str(container_port)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
    )
    address = result.stdout.strip().splitlines()[-1]
    host, separator, port = address.rpartition(":")
    if not separator or not port.isdigit():
        raise RuntimeError(f"cannot parse published port: {address}")
    if host in {"0.0.0.0", "::", "[::]"}:
        raise RuntimeError(f"smoke service was not loopback-bound: {address}")
    return f"http://127.0.0.1:{port}{path}"


def _require_successful_migration(environment: dict[str, str]) -> None:
    container = _run(
        ["docker", "compose", "ps", "--all", "--quiet", "migrate"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
    ).stdout.strip()
    if not container:
        raise RuntimeError("Compose did not create the migration container")
    exit_code = _run(
        ["docker", "inspect", "--format", "{{.State.ExitCode}}", container],
        cwd=ROOT,
        env=environment,
        capture_output=True,
    ).stdout.strip()
    if exit_code != "0":
        raise RuntimeError(f"migration container exited with code {exit_code}")


def _require_non_root_container(
    service: str,
    environment: dict[str, str],
) -> str:
    container = _run(
        ["docker", "compose", "ps", "--quiet", service],
        cwd=ROOT,
        env=environment,
        capture_output=True,
    ).stdout.strip()
    if not container:
        raise RuntimeError(f"Compose did not start the {service} container")
    configured_user = _run(
        ["docker", "inspect", "--format", "{{.Config.User}}", container],
        cwd=ROOT,
        env=environment,
        capture_output=True,
    ).stdout.strip()
    if configured_user in {"", "0", "0:0", "root", "root:root"}:
        raise RuntimeError(f"{service} container is configured to run as root")
    return configured_user


def _wait_for_json_health(
    service: str,
    container_port: int,
    path: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    response_body = _wait_for_http_body(
        service,
        container_port,
        path,
        environment,
    )
    return json.loads(response_body.decode("utf-8"))


def _wait_for_http(
    service: str,
    container_port: int,
    path: str,
    environment: dict[str, str],
) -> tuple[int, dict[str, str]]:
    _, status, headers = _wait_for_http_body(
        service,
        container_port,
        path,
        environment,
        include_response=True,
    )
    return status, headers


def _wait_for_http_body(
    service: str,
    container_port: int,
    path: str,
    environment: dict[str, str],
    *,
    include_response: bool = False,
) -> bytes | tuple[bytes, int, dict[str, str]]:
    deadline = time.monotonic() + 180
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            url = _published_url(
                service,
                container_port,
                path,
                environment,
            )
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read()
                if response.status != 200:
                    raise RuntimeError(
                        f"{service} returned HTTP {response.status}"
                    )
                if include_response:
                    headers = {
                        key.lower(): value
                        for key, value in response.headers.items()
                    }
                    return body, response.status, headers
                return body
        except (
            IndexError,
            OSError,
            RuntimeError,
            subprocess.CalledProcessError,
        ) as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(
        f"{service} did not become healthy within 180 seconds: {last_error}"
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def main() -> None:
    print(json.dumps(run_docker_smoke(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
