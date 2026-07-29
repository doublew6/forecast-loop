"""Strict, portable declarations for scheduled forecast-loop workflows.

The manifest describes the workflow boundary; it does not implement a scheduler
or execute any step. Commands are argv arrays on purpose so an executor never
needs to invoke a shell.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

JOB_MANIFEST_SCHEMA = "vericouncil.job/v1"
PUBLIC_CLI_EXECUTABLES = frozenset({"forecast-loop", "signalrace", "vericouncil"})
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SCHEDULE_EXPANSIONS = 512

_PORTABLE_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TIMEZONE = re.compile(
    r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$"
)
_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "pwsh",
    "sh",
    "zsh",
}


class JobManifestLoadError(ValueError):
    """Raised when a manifest file cannot be read as a trusted JSON document."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def validate_command_argv(value: Sequence[str]) -> tuple[str, ...]:
    """Validate a shell-free, PATH-resolved command invocation."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("command must be an argv array")
    argv = tuple(value)
    if not argv:
        raise ValueError("command must contain an executable")
    if len(argv) > 64:
        raise ValueError("command may contain at most 64 argv entries")
    if any(not isinstance(item, str) for item in argv):
        raise ValueError("every command argv entry must be a string")
    for item in argv:
        if not item or item != item.strip():
            raise ValueError("command argv entries must be non-empty and unpadded")
        if len(item) > 4096:
            raise ValueError("command argv entries may contain at most 4096 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in item):
            raise ValueError("command argv entries may not contain control characters")
    executable = argv[0]
    if not _PORTABLE_EXECUTABLE.fullmatch(executable):
        raise ValueError(
            "command executable must be a portable PATH name without directory separators"
        )
    if executable.casefold() in _SHELL_EXECUTABLES:
        raise ValueError("shell executables are not allowed; declare an argv-native command")
    return argv


class CommandStep(StrictModel):
    """One deterministic, argv-native workflow step."""

    command: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("command", mode="before")
    @classmethod
    def command_is_shell_free(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("command must be an argv array")
        return validate_command_argv(value)


def _validate_forecast_command(
    command: tuple[str, ...],
    *,
    action: Literal["prepare", "finalize"],
) -> None:
    """Restrict deterministic steps to the public forecast CLI contract."""

    expected_suffix = ("forecast", action)
    if command[0] not in PUBLIC_CLI_EXECUTABLES or command[1:3] != expected_suffix:
        raise ValueError(
            f"{action}.command must start with: forecast-loop {' '.join(expected_suffix)} "
            "(legacy signalrace and vericouncil are also accepted)"
        )

    arguments = command[3:]
    if action == "finalize":
        if not arguments or arguments[-1] != "{job_dir}":
            raise ValueError(
                "finalize.command must end with the dispatcher-provided {job_dir}"
            )
        arguments = arguments[:-1]
    elif "{job_dir}" in arguments:
        raise ValueError("prepare.command may not contain {job_dir}")

    allowed_options = {
        "prepare": {"--mode", "--as-of", "--snapshot", "--output-root"},
        "finalize": {"--mode", "--snapshot", "--output-root"},
    }[action]
    if len(arguments) % 2:
        raise ValueError(f"{action}.command options must be flag/value pairs")

    seen: set[str] = set()
    for position in range(0, len(arguments), 2):
        option, value = arguments[position : position + 2]
        if option not in allowed_options:
            raise ValueError(f"{action}.command option is not allowed: {option}")
        if option in seen:
            raise ValueError(f"{action}.command option may appear only once: {option}")
        if value.startswith("-") or "{" in value or "}" in value:
            raise ValueError(f"{action}.command has an unsafe value for {option}")
        if option == "--mode" and value not in {"demo", "live"}:
            raise ValueError(f"{action}.command --mode must be demo or live")
        seen.add(option)
    if "--mode" not in seen:
        raise ValueError(f"{action}.command must declare --mode explicitly")


def _safe_relative_parts(
    value: str,
    *,
    field_name: str,
    allow_wildcard_parts: bool,
) -> tuple[str, ...]:
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty, unpadded relative path")
    if len(value) > 1024:
        raise ValueError(f"{field_name} may contain at most 1024 characters")
    if "\\" in value or value.startswith("/") or value.endswith("/"):
        raise ValueError(f"{field_name} must be a portable relative POSIX path")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} may not contain empty, dot, or parent segments")
    for part in parts:
        if allow_wildcard_parts and part == "*":
            continue
        if "*" in part or "?" in part or "[" in part or "]" in part:
            raise ValueError(
                f"{field_name} wildcards, when allowed, must occupy a complete path segment"
            )
        if not _PATH_PART.fullmatch(part):
            raise ValueError(f"{field_name} contains an unsupported path segment")
    return parts


def _validate_prompt_path(value: str) -> str:
    parts = _safe_relative_parts(
        value,
        field_name="draft.prompt",
        allow_wildcard_parts=False,
    )
    if len(parts) < 2 or parts[0] != "prompts":
        raise ValueError("draft.prompt must be a Markdown file below prompts/")
    if not parts[-1].endswith(".md"):
        raise ValueError("draft.prompt must reference a .md file")
    return value


def _validate_writable_path(value: str) -> str:
    parts = _safe_relative_parts(
        value,
        field_name="draft.writable",
        allow_wildcard_parts=True,
    )
    if len(parts) < 3 or parts[0] != "data":
        raise ValueError("draft.writable entries must be below data/")
    if parts[-1] != "drafts.json":
        raise ValueError("Codex draft access may target drafts.json files only")
    return value


class DraftStep(StrictModel):
    """Untrusted structured-draft step delegated to a named runner adapter."""

    runner: str
    model: str | None = None
    reasoning_effort: (
        Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None
    ) = None
    prompt: str
    writable: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("runner")
    @classmethod
    def runner_is_a_portable_slug(cls, value: str) -> str:
        if not _SLUG.fullmatch(value):
            raise ValueError("draft.runner must be a lowercase portable slug")
        return value

    @field_validator("model")
    @classmethod
    def model_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value != value.strip() or len(value) > 128:
            raise ValueError("draft.model must be a non-empty model identifier")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("draft.model may not contain control characters")
        return value

    @field_validator("prompt")
    @classmethod
    def prompt_is_scoped(cls, value: str) -> str:
        return _validate_prompt_path(value)

    @field_validator("writable", mode="before")
    @classmethod
    def writable_paths_are_scoped(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("draft.writable must be an array of paths")
        paths = tuple(value)
        if not paths:
            raise ValueError("draft.writable must contain at least one path")
        if len(paths) > 16:
            raise ValueError("draft.writable may contain at most 16 paths")
        if any(not isinstance(path, str) for path in paths):
            raise ValueError("every draft.writable entry must be a string")
        validated = tuple(_validate_writable_path(path) for path in paths)
        if len(set(validated)) != len(validated):
            raise ValueError("draft.writable entries must be unique")
        return validated


@dataclass(frozen=True, slots=True)
class CronExpression:
    """Parsed five-field cron expression with portable scheduler semantics."""

    minutes: tuple[int, ...] | None
    hours: tuple[int, ...] | None
    days: tuple[int, ...] | None
    months: tuple[int, ...] | None
    weekdays: tuple[int, ...] | None

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        if not isinstance(expression, str):
            raise ValueError("schedule must be a five-field cron string")
        if expression != expression.strip() or len(expression) > 128:
            raise ValueError("schedule must be unpadded and at most 128 characters")
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("schedule must contain exactly five cron fields")
        minutes = _parse_cron_field(fields[0], minimum=0, maximum=59, label="minute")
        hours = _parse_cron_field(fields[1], minimum=0, maximum=23, label="hour")
        days = _parse_cron_field(fields[2], minimum=1, maximum=31, label="day")
        months = _parse_cron_field(fields[3], minimum=1, maximum=12, label="month")
        weekdays = _parse_cron_field(
            fields[4],
            minimum=0,
            maximum=7,
            label="weekday",
            normalize_weekday=True,
        )
        if days is not None and weekdays is not None:
            raise ValueError(
                "schedule may restrict day-of-month or weekday, but not both"
            )
        parsed = cls(
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            weekdays=weekdays,
        )
        if parsed.expansion_count > MAX_SCHEDULE_EXPANSIONS:
            raise ValueError(
                f"schedule expands to more than {MAX_SCHEDULE_EXPANSIONS} calendar entries"
            )
        return parsed

    @property
    def expansion_count(self) -> int:
        fields = (self.minutes, self.hours, self.days, self.months, self.weekdays)
        count = 1
        for field in fields:
            count *= len(field) if field is not None else 1
        return count

    def combinations(
        self,
    ) -> tuple[
        tuple[int | None, int | None, int | None, int | None, int | None],
        ...,
    ]:
        values = tuple(field if field is not None else (None,) for field in (
            self.minutes,
            self.hours,
            self.days,
            self.months,
            self.weekdays,
        ))
        return tuple(product(*values))  # type: ignore[arg-type,return-value]


def _parse_cron_field(
    token: str,
    *,
    minimum: int,
    maximum: int,
    label: str,
    normalize_weekday: bool = False,
) -> tuple[int, ...] | None:
    if token == "*":
        return None
    if not token or any(character not in "0123456789,-*/" for character in token):
        raise ValueError(f"schedule {label} field contains unsupported syntax")
    values: set[int] = set()
    for item in token.split(","):
        if not item:
            raise ValueError(f"schedule {label} field contains an empty list item")
        base, step = _split_cron_step(item, label=label)
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2 or not all(bound.isdigit() for bound in bounds):
                raise ValueError(f"schedule {label} range is invalid")
            start, end = (int(bound) for bound in bounds)
        elif base.isdigit() and step == 1:
            start = end = int(base)
        else:
            raise ValueError(
                f"schedule {label} steps require '*' or an explicit numeric range"
            )
        if start < minimum or end > maximum or start > end:
            raise ValueError(
                f"schedule {label} values must be within {minimum}..{maximum}"
            )
        values.update(range(start, end + 1, step))
    if normalize_weekday:
        values = {0 if value == 7 else value for value in values}
        full_range = set(range(0, 7))
    else:
        full_range = set(range(minimum, maximum + 1))
    if values == full_range:
        return None
    return tuple(sorted(values))


def _split_cron_step(item: str, *, label: str) -> tuple[str, int]:
    parts = item.split("/")
    if len(parts) > 2 or not parts[0]:
        raise ValueError(f"schedule {label} step is invalid")
    if len(parts) == 1:
        return parts[0], 1
    if not parts[1].isdigit() or int(parts[1]) < 1:
        raise ValueError(f"schedule {label} step must be a positive integer")
    return parts[0], int(parts[1])


class JobManifest(StrictModel):
    """Versioned, scheduler-neutral workflow declaration."""

    schema_id: Literal["vericouncil.job/v1"] = Field(alias="schema")
    name: str
    schedule: str
    timezone: str
    profile: str
    prepare: CommandStep
    draft: DraftStep
    finalize: CommandStep

    @field_validator("name", "profile")
    @classmethod
    def slug_fields_are_portable(cls, value: str) -> str:
        if not _SLUG.fullmatch(value):
            raise ValueError("name and profile must be lowercase portable slugs")
        return value

    @field_validator("schedule")
    @classmethod
    def schedule_is_portable_cron(cls, value: str) -> str:
        CronExpression.parse(value)
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_is_an_iana_name(cls, value: str) -> str:
        if not _TIMEZONE.fullmatch(value) or ".." in value:
            raise ValueError("timezone must be a portable IANA timezone name")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an installed IANA timezone") from error
        return value

    @field_validator("prepare")
    @classmethod
    def prepare_uses_the_public_cli(cls, value: CommandStep) -> CommandStep:
        _validate_forecast_command(value.command, action="prepare")
        return value

    @field_validator("finalize")
    @classmethod
    def finalize_uses_the_public_cli(cls, value: CommandStep) -> CommandStep:
        _validate_forecast_command(value.command, action="finalize")
        return value

    @property
    def cron(self) -> CronExpression:
        return CronExpression.parse(self.schedule)


def load_job_manifest(path: str | Path) -> JobManifest:
    """Load a bounded, regular, non-symlink JSON manifest."""

    manifest_path = Path(path)
    if manifest_path.suffix.casefold() != ".json":
        raise JobManifestLoadError("job manifests must use the .json extension")
    if manifest_path.is_symlink():
        raise JobManifestLoadError("job manifest may not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(manifest_path, flags)
    except OSError as error:
        raise JobManifestLoadError(f"could not open job manifest: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise JobManifestLoadError("job manifest must be a regular file")
        if metadata.st_size > MAX_MANIFEST_BYTES:
            raise JobManifestLoadError(
                f"job manifest exceeds the {MAX_MANIFEST_BYTES}-byte limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise JobManifestLoadError(
            f"job manifest exceeds the {MAX_MANIFEST_BYTES}-byte limit"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise JobManifestLoadError("job manifest must be UTF-8 JSON") from error
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, JobManifestLoadError) as error:
        if isinstance(error, JobManifestLoadError):
            raise
        raise JobManifestLoadError(f"job manifest is not valid JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise JobManifestLoadError("job manifest JSON root must be an object")
    return JobManifest.model_validate(payload)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise JobManifestLoadError(f"job manifest contains duplicate key: {key}")
        payload[key] = value
    return payload
