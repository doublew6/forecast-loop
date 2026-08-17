"""Deterministic owner-only Feishu brief delivery for focused v2 forecasts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import distinct, func, select

from ..config import REPOSITORY_ROOT, Settings
from ..db import Database
from ..models import AgentSignalV2Record, ForecastV2, ResearchRunV2
from ..research_v2 import CSI1000_D1_TARGET

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
DEFAULT_DELIVERY_ROOT = REPOSITORY_ROOT / "data" / "notification-delivery"
DEFAULT_BRIEF_TITLE = "forecast-loop｜中证1000预测日报"
MAX_ENV_BYTES = 64 * 1024
MAX_BRIEF_LINE_CHARS = 120

ReceiveIdType = Literal["open_id", "user_id", "union_id", "email"]


class DailyBriefV2Error(RuntimeError):
    """Raised when a trusted brief cannot be rendered or delivered."""


@dataclass(frozen=True)
class DailyBriefV2:
    forecast_id: str
    run_id: str
    anchor_date: date
    target_date: date
    direction: Literal["up", "neutral", "down"]
    effective_lane: Literal["formal", "shadow"]
    sample_number: int
    data_cutoff: datetime
    text: str
    content_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "forecast_id": self.forecast_id,
            "run_id": self.run_id,
            "anchor_date": self.anchor_date.isoformat(),
            "target_date": self.target_date.isoformat(),
            "direction": self.direction,
            "effective_lane": self.effective_lane,
            "sample_number": self.sample_number,
            "data_cutoff": self.data_cutoff.isoformat(),
            "content_hash": self.content_hash,
            "text": self.text,
        }


@dataclass(frozen=True)
class FeishuOwnerConfig:
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)
    receive_id_type: ReceiveIdType
    receive_id: str = field(repr=False)


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["sent", "already_sent"]
    forecast_id: str
    target_date: date
    marker: Path


def build_latest_daily_brief(
    database: Database,
    settings: Settings,
    *,
    run_id: str | None = None,
    title: str = DEFAULT_BRIEF_TITLE,
) -> DailyBriefV2:
    """Render the newest completed Live CSI1000 D1 forecast from trusted rows."""

    with database.session_factory() as session:
        statement = (
            select(ForecastV2, ResearchRunV2)
            .join(ResearchRunV2, ResearchRunV2.id == ForecastV2.run_id)
            .where(
                ResearchRunV2.mode == "live",
                ResearchRunV2.status == "completed",
                ForecastV2.target_id == CSI1000_D1_TARGET,
                ForecastV2.horizon == "D1",
            )
            .order_by(ForecastV2.created_at.desc(), ForecastV2.id.desc())
        )
        if run_id is not None:
            statement = statement.where(ForecastV2.run_id == run_id)
        row = session.execute(statement.limit(1)).one_or_none()
        if row is None:
            scope = f" for run {run_id}" if run_id else ""
            raise DailyBriefV2Error(f"no completed Live CSI1000 D1 forecast{scope}")
        forecast, run = row

        signals = session.scalars(
            select(AgentSignalV2Record).where(
                AgentSignalV2Record.run_id == forecast.run_id,
                AgentSignalV2Record.target_id == forecast.target_id,
                AgentSignalV2Record.agent_id.in_(("strategy_agent", "risk_critic_agent")),
            )
        ).all()
        by_agent = {signal.agent_id: signal for signal in signals}
        strategy = _draft(by_agent.get("strategy_agent"))
        critic = _draft(by_agent.get("risk_critic_agent"))

        sample_number = int(
            session.scalar(
                select(func.count(distinct(ForecastV2.target_date)))
                .join(ResearchRunV2, ResearchRunV2.id == ForecastV2.run_id)
                .where(
                    ResearchRunV2.mode == "live",
                    ResearchRunV2.status == "completed",
                    ForecastV2.target_id == CSI1000_D1_TARGET,
                    ForecastV2.horizon == "D1",
                    ForecastV2.created_at <= forecast.created_at,
                )
            )
            or 0
        )

    direction = _direction(forecast)
    data_cutoff = _localized(run.data_cutoff, settings.timezone)
    text = _render_text(
        forecast=forecast,
        direction=direction,
        sample_number=sample_number,
        data_cutoff=data_cutoff,
        shadow_target_dates=settings.reflection_shadow_target_dates,
        strategy=strategy,
        critic=critic,
        title=_title(title),
    )
    return DailyBriefV2(
        forecast_id=forecast.id,
        run_id=forecast.run_id,
        anchor_date=forecast.anchor_date,
        target_date=forecast.target_date,
        direction=direction,
        effective_lane=forecast.effective_lane,  # type: ignore[arg-type]
        sample_number=sample_number,
        data_cutoff=data_cutoff,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def load_feishu_owner_config(
    env_file: Path,
    *,
    env_prefix: str = "FORECAST_LOOP_FEISHU",
) -> FeishuOwnerConfig:
    """Load one outbound Feishu owner config from an explicit private file."""

    path = env_file.expanduser()
    if path.is_symlink():
        raise DailyBriefV2Error("Feishu env file must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as exc:
        raise DailyBriefV2Error("Feishu env file is unavailable") from exc
    if not resolved.is_file() or size > MAX_ENV_BYTES:
        raise DailyBriefV2Error("Feishu env file must be a regular file under 64 KiB")
    prefix = env_prefix.strip()
    if not prefix or not prefix.replace("_", "A").isalnum() or prefix[0].isdigit():
        raise DailyBriefV2Error("invalid Feishu env prefix")
    values = _parse_env(resolved.read_text(encoding="utf-8"))
    names = {
        "app_id": f"{prefix}_APP_ID",
        "app_secret": f"{prefix}_APP_SECRET",
        "owner_id_type": f"{prefix}_OWNER_ID_TYPE",
        "owner_id": f"{prefix}_OWNER_ID",
    }
    required = {
        name: values.get(name, "").strip()
        for name in names.values()
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise DailyBriefV2Error(
            "Feishu env file is missing: " + ", ".join(sorted(missing))
        )
    receive_id_type = required[names["owner_id_type"]]
    if receive_id_type not in {"open_id", "user_id", "union_id", "email"}:
        raise DailyBriefV2Error(f"invalid {names['owner_id_type']}")
    return FeishuOwnerConfig(
        app_id=required[names["app_id"]],
        app_secret=required[names["app_secret"]],
        receive_id_type=receive_id_type,  # type: ignore[arg-type]
        receive_id=required[names["owner_id"]],
    )


class FeishuOwnerSender:
    """Small outbound-only Feishu client; it never reads messages or group IDs."""

    def __init__(self, config: FeishuOwnerConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=10.0, follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FeishuOwnerSender:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send(self, brief: DailyBriefV2, *, message_uuid: str) -> None:
        token_response = self._post(
            TOKEN_URL,
            json_body={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
            operation="token",
        )
        token = token_response.get("tenant_access_token")
        if token_response.get("code") != 0 or not isinstance(token, str) or not token:
            raise DailyBriefV2Error(_safe_api_error("token", token_response))
        message_response = self._post(
            MESSAGE_URL,
            params={"receive_id_type": self._config.receive_id_type},
            headers={"Authorization": f"Bearer {token}"},
            json_body={
                "receive_id": self._config.receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": brief.text}, ensure_ascii=False),
                "uuid": message_uuid,
            },
            operation="message",
        )
        if message_response.get("code") != 0:
            raise DailyBriefV2Error(_safe_api_error("message", message_response))

    def _post(
        self,
        url: str,
        *,
        json_body: dict[str, object],
        operation: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                url,
                params=params,
                headers=headers,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise DailyBriefV2Error(f"Feishu {operation} request failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise DailyBriefV2Error(
                f"Feishu {operation} request failed with HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DailyBriefV2Error(f"Feishu {operation} API returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise DailyBriefV2Error(f"Feishu {operation} API returned an invalid body")
        return body


def publish_daily_brief(
    brief: DailyBriefV2,
    config: FeishuOwnerConfig,
    *,
    state_root: Path = DEFAULT_DELIVERY_ROOT,
    sender: FeishuOwnerSender | None = None,
) -> DeliveryResult:
    """Deliver once per immutable forecast, with a process-safe local marker."""

    destination_hash = hashlib.sha256(
        f"{config.receive_id_type}:{config.receive_id}".encode()
    ).hexdigest()[:16]
    marker = (
        state_root.expanduser().resolve()
        / "feishu-owner"
        / brief.target_date.isoformat()
        / f"owner-{destination_hash}.json"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    lock_path = marker.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if marker.exists():
            return DeliveryResult("already_sent", brief.forecast_id, brief.target_date, marker)
        legacy_marker = _legacy_marker_for_target(
            (marker.parents[1],),
            destination_hash=destination_hash,
            target_date=brief.target_date,
        )
        if legacy_marker is not None:
            return DeliveryResult(
                "already_sent",
                brief.forecast_id,
                brief.target_date,
                legacy_marker,
            )
        message_uuid = str(
            uuid5(
                NAMESPACE_URL,
                "forecast-loop:feishu-owner:"
                f"csi1000-absolute-d1:{brief.target_date.isoformat()}:{destination_hash}",
            )
        )
        active_sender = sender or FeishuOwnerSender(config)
        try:
            active_sender.send(brief, message_uuid=message_uuid)
        finally:
            if sender is None:
                active_sender.close()
        _write_marker(marker, brief, message_uuid=message_uuid)
        return DeliveryResult("sent", brief.forecast_id, brief.target_date, marker)


def _draft(signal: AgentSignalV2Record | None) -> dict[str, Any]:
    if signal is None or not isinstance(signal.envelope, dict):
        return {}
    draft = signal.envelope.get("draft")
    return draft if isinstance(draft, dict) else {}


def _direction(forecast: ForecastV2) -> Literal["up", "neutral", "down"]:
    values = {
        "up": forecast.probability_up,
        "neutral": forecast.probability_neutral,
        "down": forecast.probability_down,
    }
    return max(values, key=values.__getitem__)  # type: ignore[return-value]


def _render_text(
    *,
    forecast: ForecastV2,
    direction: Literal["up", "neutral", "down"],
    sample_number: int,
    data_cutoff: datetime,
    shadow_target_dates: int,
    strategy: dict[str, Any],
    critic: dict[str, Any],
    title: str,
) -> str:
    direction_label = {"up": "偏涨", "neutral": "小波动", "down": "偏跌"}[direction]
    evidence = _evidence_lines(strategy)
    risk = _risk_line(critic, forecast)
    if forecast.effective_lane == "shadow":
        runtime = f"V2 Shadow 第 {sample_number}/{shadow_target_dates} 个前瞻样本"
    else:
        runtime = "V2 Formal"
    return "\n".join(
        (
            f"【{title}】",
            f"日期：{forecast.anchor_date.isoformat()}｜"
            f"目标：下一交易日 {forecast.target_date.isoformat()}",
            "",
            f"结论：{direction_label}",
            "概率："
            f"涨 {forecast.probability_up:.1%}｜"
            f"小波动 {forecast.probability_neutral:.1%}｜"
            f"跌 {forecast.probability_down:.1%}",
            "",
            "核心依据：",
            f"1. {evidence[0]}",
            f"2. {evidence[1]}",
            "",
            f"主要风险：{risk}",
            "",
            f"运行状态：{runtime}｜数据截至 {data_cutoff:%H:%M}｜"
            f"Forecast ID: {forecast.id[:8]}",
            "说明：研究预测，不构成投资建议。",
        )
    )


def _evidence_lines(strategy: dict[str, Any]) -> tuple[str, str]:
    chain = strategy.get("transmission_chain")
    candidates = [str(item).strip() for item in chain] if isinstance(chain, list) else []
    candidates = [item for item in candidates if item]
    if len(candidates) >= 2:
        chosen = candidates[-2:]
    else:
        rationale = str(strategy.get("rationale") or "").strip()
        chosen = candidates + ([rationale] if rationale else [])
    fallbacks = (
        "冻结输入没有形成足以改变基线的第二条独立短周期证据。",
        "结论只依据截止时间前的冻结证据，不加入盘后信息。",
    )
    chosen.extend(item for item in fallbacks if len(chosen) < 2)
    return _clip(chosen[0]), _clip(chosen[1])


def _risk_line(critic: dict[str, Any], forecast: ForecastV2) -> str:
    severity = {"low": "低", "medium": "中等", "high": "高"}.get(
        str(critic.get("risk_severity") or "").lower(),
        "未分级",
    )
    counter = critic.get("counter_evidence")
    items = [str(item).strip() for item in counter] if isinstance(counter, list) else []
    items = [item for item in items if item]
    detail = items[1] if len(items) > 1 else (items[0] if items else "")
    if not detail and forecast.invalidation_conditions:
        detail = str(forecast.invalidation_conditions[0]).strip()
    if not detail:
        detail = "若冻结输入、标的身份或交易日历发生修订，结论失效。"
    return _clip(f"{severity}；{detail}")


def _clip(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= MAX_BRIEF_LINE_CHARS:
        return normalized
    return normalized[: MAX_BRIEF_LINE_CHARS - 1].rstrip("，。；;,. ") + "…"


def _title(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 80 or "\n" in value or "\r" in value:
        raise DailyBriefV2Error("brief title must contain 1-80 printable characters")
    return normalized


def _localized(value: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not name.replace("_", "a").isalnum() or name[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def _safe_api_error(operation: str, body: dict[str, Any]) -> str:
    return (
        f"Feishu {operation} API failed: "
        f"code={body.get('code', 'unknown')}, msg={body.get('msg', 'unknown error')}"
    )


def _legacy_marker_for_target(
    delivery_roots: tuple[Path, ...],
    *,
    destination_hash: str,
    target_date: date,
) -> Path | None:
    """Recognize forecast-ID markers written before target-date deduplication."""

    expected = target_date.isoformat()
    for delivery_root in delivery_roots:
        for candidate in delivery_root.glob(f"*/owner-{destination_hash}.json"):
            if candidate.parent.name == expected:
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("target_date") == expected:
                return candidate
    return None


def _write_marker(path: Path, brief: DailyBriefV2, *, message_uuid: str) -> None:
    payload = {
        "schema_version": "forecast-loop.feishu-owner-delivery/v1",
        "forecast_id": brief.forecast_id,
        "run_id": brief.run_id,
        "target_date": brief.target_date.isoformat(),
        "brief_hash": brief.content_hash,
        "message_uuid": message_uuid,
        "delivered_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix=".delivery-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
