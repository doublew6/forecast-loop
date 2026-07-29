"""Safe base renderers for user-scoped launchd and systemd scheduling.

Renderers schedule one supplied orchestration invocation. They intentionally do
not translate prepare, draft, or finalize into scheduler-native business logic.
"""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .manifest import JobManifest, validate_command_argv

_LAUNCHD_WEEKDAY = {
    0: 1,  # Sunday
    1: 2,
    2: 3,
    3: 4,
    4: 5,
    5: 6,
    6: 7,  # Saturday
}
_SYSTEMD_WEEKDAY = {
    0: "Sun",
    1: "Mon",
    2: "Tue",
    3: "Wed",
    4: "Thu",
    5: "Fri",
    6: "Sat",
}


@dataclass(frozen=True, slots=True)
class SystemdUnits:
    """A matching user service and timer unit."""

    service_name: str
    timer_name: str
    service: str
    timer: str


def render_launchd_plist(
    manifest: JobManifest,
    invocation: Sequence[str],
    *,
    host_timezone: str,
) -> bytes:
    """Render a launchd plist after proving host and manifest timezones match.

    launchd has no per-job trigger timezone. Requiring the caller to provide the
    host timezone prevents silently scheduling an Asia/Shanghai manifest using
    another host timezone.
    """

    _validate_timezone(host_timezone)
    if host_timezone != manifest.timezone:
        raise ValueError(
            "launchd host timezone must exactly match the job manifest timezone"
        )
    argv = validate_command_argv(invocation)
    calendar_intervals: list[dict[str, int]] = []
    for minute, hour, day, month, weekday in manifest.cron.combinations():
        interval: dict[str, int] = {}
        if minute is not None:
            interval["Minute"] = minute
        if hour is not None:
            interval["Hour"] = hour
        if day is not None:
            interval["Day"] = day
        if month is not None:
            interval["Month"] = month
        if weekday is not None:
            interval["Weekday"] = _LAUNCHD_WEEKDAY[weekday]
        calendar_intervals.append(interval)
    payload: dict[str, object] = {
        "Label": f"org.vericouncil.job.{manifest.name}",
        "ProgramArguments": ["/usr/bin/env", *argv],
        "EnvironmentVariables": {"TZ": manifest.timezone},
        "StartCalendarInterval": calendar_intervals,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "ThrottleInterval": 60,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def render_systemd_units(
    manifest: JobManifest,
    invocation: Sequence[str],
) -> SystemdUnits:
    """Render a systemd user service and timer around one orchestration call."""

    argv = validate_command_argv(invocation)
    stem = f"vericouncil-job-{manifest.name}"
    service_name = f"{stem}.service"
    timer_name = f"{stem}.timer"
    exec_start = " ".join(
        [":/usr/bin/env", *(_systemd_quote(argument) for argument in argv)]
    )
    service = "\n".join(
        [
            "[Unit]",
            f"Description=forecast-loop job {manifest.name}",
            "",
            "[Service]",
            "Type=oneshot",
            f"Environment={_systemd_quote(f'TZ={manifest.timezone}')}",
            f"ExecStart={exec_start}",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "",
        ]
    )
    timer_lines = [
        "[Unit]",
        f"Description=Schedule forecast-loop job {manifest.name}",
        "",
        "[Timer]",
        f"Unit={service_name}",
    ]
    timer_lines.extend(
        f"OnCalendar={calendar}" for calendar in _systemd_calendars(manifest)
    )
    timer_lines.extend(
        [
            "Persistent=true",
            "AccuracySec=1min",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return SystemdUnits(
        service_name=service_name,
        timer_name=timer_name,
        service=service,
        timer="\n".join(timer_lines),
    )


def _systemd_calendars(manifest: JobManifest) -> tuple[str, ...]:
    calendars: list[str] = []
    for minute, hour, day, month, weekday in manifest.cron.combinations():
        weekday_text = f"{_SYSTEMD_WEEKDAY[weekday]} " if weekday is not None else ""
        month_text = f"{month:02d}" if month is not None else "*"
        day_text = f"{day:02d}" if day is not None else "*"
        hour_text = f"{hour:02d}" if hour is not None else "*"
        minute_text = f"{minute:02d}" if minute is not None else "*"
        calendars.append(
            f"{weekday_text}*-{month_text}-{day_text} "
            f"{hour_text}:{minute_text}:00 {manifest.timezone}"
        )
    return tuple(calendars)


def _systemd_quote(value: str) -> str:
    # ExecStart's ':' prefix disables $ environment expansion. Percent
    # specifiers remain active in unit files, so double them before quoting.
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("host_timezone must be an installed IANA timezone") from error
