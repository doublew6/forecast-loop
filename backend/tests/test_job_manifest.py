from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest
from app.jobs import (
    CronExpression,
    JobManifest,
    JobManifestLoadError,
    load_job_manifest,
    render_launchd_plist,
    render_systemd_units,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


def _payload() -> dict[str, object]:
    return {
        "schema": "vericouncil.job/v1",
        "name": "daily-forecast",
        "schedule": "15 9,15 * * 1-5",
        "timezone": "Asia/Shanghai",
        "profile": "formal",
        "prepare": {
            "command": ["forecast-loop", "forecast", "prepare", "--mode", "live"]
        },
        "draft": {
            "runner": "codex",
            "model": "example-model",
            "reasoning_effort": "high",
            "prompt": "prompts/daily-forecast-v2.md",
            "writable": ["data/handoffs/*/drafts.json"],
        },
        "finalize": {
            "command": [
                "forecast-loop",
                "forecast",
                "finalize",
                "--mode",
                "live",
                "{job_dir}",
            ]
        },
    }


def _manifest() -> JobManifest:
    return JobManifest.model_validate(_payload())


def test_manifest_validates_the_scheduler_neutral_contract() -> None:
    manifest = _manifest()

    assert manifest.schema_id == "vericouncil.job/v1"
    assert manifest.prepare.command[0] == "forecast-loop"
    assert manifest.draft.runner == "codex"
    assert manifest.draft.model == "example-model"
    assert manifest.draft.reasoning_effort == "high"
    assert manifest.finalize.command[-1] == "{job_dir}"
    assert manifest.cron.hours == (9, 15)
    assert manifest.cron.weekdays == (1, 2, 3, 4, 5)


@pytest.mark.parametrize("executable", ["signalrace", "vericouncil"])
def test_manifest_accepts_legacy_cli_commands(executable: str) -> None:
    payload = _payload()
    payload["prepare"] = {
        "command": [executable, "forecast", "prepare", "--mode", "live"]
    }
    payload["finalize"] = {
        "command": [
            executable,
            "forecast",
            "finalize",
            "--mode",
            "live",
            "{job_dir}",
        ]
    }

    manifest = JobManifest.model_validate(payload)

    assert manifest.prepare.command[0] == executable
    assert manifest.finalize.command[0] == executable


def test_current_versioned_prompt_keeps_prepare_and_finalize_outside_draft_stage() -> None:
    prompt = (ROOT / "prompts" / "daily-forecast-v2.md").read_text(encoding="utf-8")

    assert "Do not run the manifest's `prepare.command` or `finalize.command`" in prompt
    assert "Leave\n   deterministic finalize" in prompt
    assert "then run the manifest's" not in prompt
    assert "forecast-loop is a research and audit system" in prompt
    assert "VeriCouncil" not in prompt


def test_public_example_uses_the_current_versioned_prompt() -> None:
    manifest = load_job_manifest(ROOT / "jobs" / "daily-forecast.example.json")

    assert manifest.draft.prompt == "prompts/daily-forecast-v2.md"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schedule", "30 18 * *", "five cron fields"),
        ("schedule", "60 18 * * 1-5", "0..59"),
        ("schedule", "30 18 1 * 1", "day-of-month or weekday"),
        ("schedule", "30 18 * JAN 1", "unsupported syntax"),
        ("timezone", "../localtime", "IANA timezone"),
        ("timezone", "Mars/Olympus", "installed IANA timezone"),
        ("name", "Daily Forecast", "portable slug"),
        ("profile", "Formal", "portable slug"),
    ],
)
def test_manifest_rejects_nonportable_top_level_fields(
    field: str,
    value: str,
    message: str,
) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(ValidationError, match=message):
        JobManifest.model_validate(payload)


def test_cron_supports_lists_ranges_steps_and_sunday_alias() -> None:
    parsed = CronExpression.parse("*/15 9-10 * * 0,7")

    assert parsed.minutes == (0, 15, 30, 45)
    assert parsed.hours == (9, 10)
    assert parsed.weekdays == (0,)


def test_cron_caps_scheduler_expansion() -> None:
    with pytest.raises(ValueError, match="more than 512"):
        CronExpression.parse("0-58 0-22 1 * *")


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("vericouncil forecast prepare", "argv array"),
        (["/usr/local/bin/vericouncil"], "portable PATH name"),
        (["bash", "-c", "vericouncil forecast prepare"], "shell executables"),
        (["vericouncil", "bad\nargument"], "control characters"),
        (["vericouncil", ""], "non-empty"),
    ],
)
def test_manifest_rejects_unsafe_command_shapes(
    command: object,
    message: str,
) -> None:
    payload = _payload()
    payload["prepare"] = {"command": command}

    with pytest.raises(ValidationError, match=message):
        JobManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "command", "message"),
    [
        (
            "prepare",
            ["env", "sh", "-c", "vericouncil forecast prepare --mode live"],
            "must start with",
        ),
        (
            "prepare",
            ["python", "-c", "print('prepare')"],
            "must start with",
        ),
        (
            "prepare",
            ["vericouncil", "forecast", "prepare", "--profile", "formal"],
            "option is not allowed",
        ),
        (
            "prepare",
            ["vericouncil", "forecast", "prepare"],
            "declare --mode explicitly",
        ),
        (
            "finalize",
            ["vericouncil", "forecast", "finalize", "--mode", "live"],
            r"end with.*\{job_dir\}",
        ),
    ],
)
def test_manifest_allowlists_deterministic_cli_commands(
    field: str,
    command: list[str],
    message: str,
) -> None:
    payload = _payload()
    payload[field] = {"command": command}

    with pytest.raises(ValidationError, match=message):
        JobManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("prompt", "message"),
    [
        ("/tmp/prompt.md", "relative POSIX path"),
        ("prompts/../secret.md", "parent segments"),
        ("docs/prompt.md", "below prompts"),
        ("prompts/*.md", "wildcards"),
        ("prompts/daily.txt", r"\.md file"),
    ],
)
def test_manifest_scopes_prompt_files(prompt: str, message: str) -> None:
    payload = _payload()
    payload["draft"] = {
        "runner": "codex",
        "prompt": prompt,
        "writable": ["data/handoffs/*/drafts.json"],
    }

    with pytest.raises(ValidationError, match=message):
        JobManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("writable", "message"),
    [
        (["/tmp/drafts.json"], "relative POSIX path"),
        (["data/../secrets/drafts.json"], "parent segments"),
        (["wiki/proposal/drafts.json"], "below data"),
        (["data/handoffs/**/drafts.json"], "complete path segment"),
        (["data/handoffs/*/receipt.json"], "drafts.json files only"),
        (
            ["data/handoffs/*/drafts.json", "data/handoffs/*/drafts.json"],
            "unique",
        ),
    ],
)
def test_manifest_scopes_codex_writes(
    writable: list[str],
    message: str,
) -> None:
    payload = _payload()
    payload["draft"] = {
        "runner": "codex",
        "prompt": "prompts/daily-forecast-v2.md",
        "writable": writable,
    }

    with pytest.raises(ValidationError, match=message):
        JobManifest.model_validate(payload)


def test_manifest_forbids_unknown_fields() -> None:
    payload = _payload()
    payload["business_logic_in_scheduler"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        JobManifest.model_validate(payload)


def test_json_loader_rejects_duplicate_keys_and_symlinks(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"vericouncil.job/v1","schema":"other"}')
    with pytest.raises(JobManifestLoadError, match="duplicate key"):
        load_job_manifest(duplicate)

    target = tmp_path / "target.json"
    target.write_text(json.dumps(_payload()), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(JobManifestLoadError, match="symlink"):
        load_job_manifest(link)


def test_json_loader_returns_a_validated_manifest(tmp_path: Path) -> None:
    path = tmp_path / "daily-forecast.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")

    assert load_job_manifest(path) == _manifest()


def test_launchd_renderer_maps_cron_without_embedding_business_steps() -> None:
    rendered = render_launchd_plist(
        _manifest(),
        ["forecast-loop", "jobs", "run", "jobs/daily-forecast.json"],
        host_timezone="Asia/Shanghai",
    )
    payload = plistlib.loads(rendered)

    assert payload["Label"] == "org.vericouncil.job.daily-forecast"
    assert payload["ProgramArguments"] == [
        "/usr/bin/env",
        "forecast-loop",
        "jobs",
        "run",
        "jobs/daily-forecast.json",
    ]
    assert payload["EnvironmentVariables"] == {"TZ": "Asia/Shanghai"}
    assert len(payload["StartCalendarInterval"]) == 10
    assert payload["StartCalendarInterval"][0] == {
        "Minute": 15,
        "Hour": 9,
        "Weekday": 2,
    }
    assert b"/Users/" not in rendered
    assert b"forecast prepare" not in rendered


def test_launchd_renderer_fails_closed_on_timezone_mismatch() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        render_launchd_plist(
            _manifest(),
            ["forecast-loop", "jobs", "run", "jobs/daily-forecast.json"],
            host_timezone="UTC",
        )


def test_systemd_renderer_emits_user_service_and_timezone_aware_timer() -> None:
    units = render_systemd_units(
        _manifest(),
        [
            "forecast-loop",
            "jobs",
            "run",
            "jobs/daily forecast %n $HOME.json",
        ],
    )

    assert units.service_name == "vericouncil-job-daily-forecast.service"
    assert units.timer_name == "vericouncil-job-daily-forecast.timer"
    assert (
        'ExecStart=:/usr/bin/env "forecast-loop" "jobs" "run" '
        '"jobs/daily forecast %%n $HOME.json"'
    ) in units.service
    assert "NoNewPrivileges=true" in units.service
    assert "OnCalendar=Mon *-*-* 09:15:00 Asia/Shanghai" in units.timer
    assert "OnCalendar=Fri *-*-* 15:15:00 Asia/Shanghai" in units.timer
    assert units.timer.count("OnCalendar=") == 10
    assert "Persistent=true" in units.timer
    assert "/Users/" not in units.service
