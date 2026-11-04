"""Explicit cloud draft commands; no model tools, database writes, or finalize."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
import pydantic
from pydantic import BaseModel, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import REPOSITORY_ROOT, Settings
from ..db import Database
from ..models import ResearchRunV2
from ..research_v2 import canonical_json
from ..responses_v2 import (
    RESPONSES_EFFORT,
    RESPONSES_FALLBACK_MODEL,
    RESPONSES_MODEL,
    ResponsesDraftBundleV1,
    ResponsesDraftBundleV2,
    ResponsesDraftBundleV3,
    ResponsesDraftBundleV4,
    ResponsesDraftBundleV5,
    ResponsesHandoffRequestV1,
    ResponsesHandoffRequestV2,
    ResponsesHandoffRequestV3,
    ResponsesHandoffRequestV4,
    ResponsesHandoffRequestV5,
    ResponsesPredictionContent,
    ResponsesPredictionContentV4,
    ResponsesReasoningTaskV2,
    ResponsesReasoningTaskV3,
    ResponsesReviewBundleV1,
    ResponsesReviewBundleV2,
    ResponsesReviewContent,
    RoutedReviewContentV2,
)
from .research_v2 import (
    MAX_JSON_BYTES,
    ResearchV2Error,
    _prediction_finalize_deadline,
    _prediction_instructions,
    _validated_explicit_job_dir,
    read_research_request,
    responses_fallback_enabled,
    validate_research_draft_bundle,
    validate_responses_reasoning_task,
)

DEFAULT_OPENAI_ENV = REPOSITORY_ROOT / ".env.openai"
RESPONSES_URL = "https://api.openai.com/v1/responses"


class OpenAIResponsesError(ResearchV2Error):
    """A safe, payload-free error suitable for CLI output."""


class OpenAIAvailabilityError(OpenAIResponsesError):
    """Only confirmed quota or availability failures permit a sealed fallback."""

    def __init__(self, reason: str):
        super().__init__(f"OpenAI {reason}; no API draft published")
        self.reason = reason


class OpenAIResponsesSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_OPENAI_ENV,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )
    api_key: pydantic.SecretStr = Field(default=SecretStr(""), validation_alias="OPENAI_API_KEY")
    model: Literal["gpt-6.1-sol"] = Field(
        default=RESPONSES_MODEL,
        validation_alias="FORECAST_LOOP_OPENAI_MODEL",
    )
    reasoning_effort: Literal["high"] = Field(
        default=RESPONSES_EFFORT,
        validation_alias="FORECAST_LOOP_OPENAI_REASONING_EFFORT",
    )
    timeout_seconds: float = Field(
        default=180,
        gt=0,
        le=1800,
        validation_alias="FORECAST_LOOP_OPENAI_TIMEOUT_SECONDS",
    )
    max_output_tokens: int = Field(
        default=32768,
        ge=1024,
        le=128000,
        validation_alias="FORECAST_LOOP_OPENAI_MAX_OUTPUT_TOKENS",
    )


class _OutputContent(BaseModel):
    type: Literal["output_text", "refusal"]
    text: str | None = None


class _OutputItem(BaseModel):
    type: Literal["message", "reasoning"]
    role: Literal["assistant"] | None = None
    content: list[_OutputContent] = Field(default_factory=list)


class _Usage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _Response(BaseModel):
    # API envelopes may grow operational metadata. Domain outputs below remain strict.
    id: str = Field(pattern=r"^resp_[A-Za-z0-9_-]+$")
    status: Literal["completed"]
    model: str
    output: list[_OutputItem]
    usage: _Usage | None = None


def _strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Require nullable/defaulted fields explicitly for Responses strict JSON Schema."""
    schema = model.model_json_schema()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


def _response_chunks(response: httpx.Response, *, deadline: float):
    total = 0
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise OpenAIAvailabilityError("timeout")
        total += len(chunk)
        if total > MAX_JSON_BYTES:
            raise OpenAIResponsesError("OpenAI response exceeded the local size limit")
        yield chunk


def _availability_reason(error: Any) -> str | None:
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if not isinstance(code, str):
        return None
    if code in {"insufficient_quota", "credit_balance_exhausted"}:
        return "quota_exhausted"
    if code in {"server_error", "service_unavailable", "server_is_overloaded"}:
        return "service_unavailable"
    return None


def _read_response(response: httpx.Response, *, deadline: float) -> _Response:
    """Accept only a complete response, never partial text deltas from the stream."""
    if "text/event-stream" not in response.headers.get("content-type", "").lower():
        raw = b"".join(_response_chunks(response, deadline=deadline))
        return _Response.model_validate_json(raw)

    pending = b""
    data_lines: list[bytes] = []
    created_id: str | None = None
    for chunk in _response_chunks(response, deadline=deadline):
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            if line.startswith(b"data:"):
                data_lines.append(line[5:].removeprefix(b" "))
                continue
            if line or not data_lines:
                continue
            raw = b"\n".join(data_lines)
            data_lines.clear()
            if raw == b"[DONE]":
                raise OpenAIResponsesError(
                    "OpenAI stream ended before response.completed; no draft published"
                )
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise OpenAIResponsesError("OpenAI returned an invalid stream event")
            kind = event.get("type")
            if kind in {"error", "response.failed", "response.incomplete", "response.cancelled"}:
                response_body = event.get("response")
                if kind == "error":
                    # An explicit top-level code wins over a conflicting nested envelope.
                    error = event if event.get("code") is not None else event.get("error")
                else:
                    error = response_body.get("error") if isinstance(response_body, dict) else None
                reason = _availability_reason(error)
                if kind in {"error", "response.failed"} and reason is not None:
                    raise OpenAIAvailabilityError(reason)
                raise OpenAIResponsesError(
                    "OpenAI stream failed or was incomplete; no draft published"
                )
            if kind == "response.created":
                value = event.get("response")
                if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                    raise OpenAIResponsesError("OpenAI stream omitted its response identity")
                created_id = value["id"]
            elif kind == "response.completed":
                completed = _Response.model_validate(event.get("response"))
                if created_id is not None and completed.id != created_id:
                    raise OpenAIResponsesError("OpenAI stream changed its response identity")
                return completed
    raise OpenAIResponsesError("OpenAI stream ended before response.completed; no draft published")


def _generate(
    config: OpenAIResponsesSettings,
    *,
    payload: BaseModel,
    instructions: str,
    output_model: type[BaseModel],
    client: httpx.Client | None,
) -> tuple[BaseModel, dict[str, str]]:
    key = config.api_key.get_secret_value().strip()
    if not key or any(character.isspace() for character in key):
        raise OpenAIResponsesError("Fill OPENAI_API_KEY in the local .env.openai file first")
    body = {
        "model": config.model,
        "reasoning": {"effort": config.reasoning_effort},
        "store": False,
        "stream": True,
        "max_output_tokens": config.max_output_tokens,
        "instructions": instructions,
        "input": canonical_json(payload).decode("utf-8"),
        "text": {
            "format": {
                "type": "json_schema",
                "name": output_model.__name__,
                "strict": True,
                "schema": _strict_schema(output_model),
            }
        },
    }
    owns_client = client is None
    # trust_env preserves the operator's existing proxy routing; redirects stay disabled.
    http_client = client
    deadline = time.monotonic() + config.timeout_seconds
    try:
        if http_client is None:
            http_client = httpx.Client(trust_env=True, follow_redirects=False)
        with http_client.stream(
            "POST",
            RESPONSES_URL,
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(
                config.timeout_seconds,
                connect=min(15, config.timeout_seconds),
                write=min(30, config.timeout_seconds),
                pool=min(15, config.timeout_seconds),
            ),
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                if response.status_code in {408, 500, 502, 503, 504}:
                    raise OpenAIAvailabilityError(
                        "timeout" if response.status_code == 408 else "service_unavailable"
                    )
                if response.status_code in {402, 429}:
                    raw = b""
                    for chunk in response.iter_bytes():
                        raw += chunk
                        if len(raw) > 65536:
                            break
                    try:
                        error = json.loads(raw).get("error", {})
                    except (ValueError, AttributeError):
                        error = None
                    if _availability_reason(error) == "quota_exhausted":
                        raise OpenAIAvailabilityError("quota_exhausted")
                raise OpenAIResponsesError(
                    f"OpenAI Responses request failed (HTTP {response.status_code}); "
                    "no draft published"
                )
            result = _read_response(response, deadline=deadline)
        if not re.fullmatch(re.escape(config.model) + r"(?:-\d{4}-\d{2}-\d{2})?", result.model):
            raise OpenAIResponsesError("OpenAI response model does not match the sealed model")
        texts = []
        for item in result.output:
            for part in item.content:
                if item.type != "message" or part.type != "output_text" or part.text is None:
                    raise OpenAIResponsesError("OpenAI refused or returned an unexpected output")
                texts.append(part.text)
        if len(texts) != 1:
            raise OpenAIResponsesError("OpenAI must return exactly one complete JSON output")
        draft = output_model.model_validate_json(texts[0])
        provenance = {
            "surface": "openai_responses",
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "response_id": result.id,
            "response_model": result.model,
        }
        if result.usage is not None:
            provenance.update(
                {
                    "input_tokens": str(result.usage.input_tokens),
                    "output_tokens": str(result.usage.output_tokens),
                }
            )
        return draft, provenance
    except httpx.TimeoutException:
        raise OpenAIAvailabilityError("timeout") from None
    except (
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    ):
        raise OpenAIAvailabilityError("connection") from None
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ImportError):
        raise OpenAIResponsesError("OpenAI connection failed; no draft published") from None
    except (ValidationError, json.JSONDecodeError):
        raise OpenAIResponsesError(
            "OpenAI output was incomplete or failed schema validation; no draft published"
        ) from None
    finally:
        if owns_client and http_client is not None:
            http_client.close()


def _assert_prediction_seal(
    database: Database,
    request: ResponsesHandoffRequestV1,
) -> None:
    with database.session_factory() as session:
        run = session.get(ResearchRunV2, request.run_id)
        if (
            run is None
            or run.status != "awaiting_draft"
            or (
                run.request_hash != request.request_hash
                or run.input_hash != request.input_hash
                or run.snapshot_hash != request.snapshot.content_hash
                or run.program_hash != request.program.content_hash
                or (isinstance(request, ResponsesHandoffRequestV2) and run.mode != request.mode)
            )
        ):
            raise OpenAIResponsesError("API draft requires the sealed awaiting-draft prediction")


def _assert_unused_output(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise OpenAIResponsesError("drafts.json already exists; it will not be overwritten")


def _publish(path: Path, raw: bytes) -> None:
    """Publish atomically without replacing another writer's draft or following a symlink."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".responses-draft-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise OpenAIResponsesError(
                "drafts.json appeared during generation; refusing overwrite"
            ) from None
    finally:
        os.unlink(temporary)


def draft_research_run(
    database: Database,
    settings: Settings,
    config: OpenAIResponsesSettings,
    *,
    job_dir: Path,
    client: httpx.Client | None = None,
    fallback_generator: Callable[
        [ResponsesHandoffRequestV3], ResponsesPredictionContent | ResponsesPredictionContentV4
    ]
    | None = None,
) -> Path:
    job_dir = _validated_explicit_job_dir(settings, job_dir)
    request = read_research_request(job_dir / "input.json")
    if not isinstance(request, ResponsesHandoffRequestV1):
        raise OpenAIResponsesError("Prepare with --provider openai_responses before API drafting")
    if request.run_id != job_dir.name:
        raise OpenAIResponsesError("API request run ID does not match its handoff directory")
    _assert_prediction_seal(database, request)
    output = job_dir / "drafts.json"
    _assert_unused_output(output)
    if datetime.now(ZoneInfo(settings.timezone)) >= _prediction_finalize_deadline(
        request,
        settings,
    ):
        raise OpenAIResponsesError("prediction cutoff has passed; no API request sent")
    content_type = (
        ResponsesPredictionContentV4
        if isinstance(request, ResponsesHandoffRequestV4)
        else ResponsesPredictionContent
    )
    try:
        content, provenance = _generate(
            config,
            payload=request,
            instructions=_prediction_instructions(request),
            output_model=content_type,
            client=client,
        )
    except OpenAIAvailabilityError as exc:
        if not responses_fallback_enabled(request) or fallback_generator is None:
            raise
        failed_at = datetime.now(ZoneInfo(settings.timezone))
        if failed_at >= _prediction_finalize_deadline(request, settings):
            raise OpenAIResponsesError(
                "cutoff passed before fallback; no draft published"
            ) from None
        _assert_prediction_seal(database, request)
        content = fallback_generator(request)
        content = content_type.model_validate(content)
        provenance = {
            "surface": "codex",
            "model": RESPONSES_FALLBACK_MODEL,
            "reasoning_effort": RESPONSES_EFFORT,
            "primary_surface": "openai_responses",
            "primary_model": RESPONSES_MODEL,
            "fallback_reason": exc.reason,
            "primary_failed_at": failed_at.isoformat(),
        }
    bundle_type = (
        ResponsesDraftBundleV5
        if isinstance(request, ResponsesHandoffRequestV5)
        else ResponsesDraftBundleV4
        if isinstance(request, ResponsesHandoffRequestV4)
        else ResponsesDraftBundleV3
        if isinstance(request, ResponsesHandoffRequestV3)
        else ResponsesDraftBundleV2
        if isinstance(request, ResponsesHandoffRequestV2)
        else ResponsesDraftBundleV1
    )
    bundle = bundle_type(
        run_id=request.run_id,
        request_hash=request.request_hash,
        generated_at=datetime.now(ZoneInfo(settings.timezone)),
        generated_by=provenance,
        drafts=content.drafts,
        **(
            {"status": content.status, "issues": content.issues}
            if isinstance(content, ResponsesPredictionContentV4)
            else {}
        ),
    )
    raw = canonical_json(bundle)
    # Repeat request and database checks after the cloud call, before publishing any bytes.
    _assert_prediction_seal(database, request)
    validate_research_draft_bundle(settings, job_dir=job_dir, raw_drafts=raw)
    if bundle.generated_at >= _prediction_finalize_deadline(request, settings):
        raise OpenAIResponsesError("prediction cutoff passed during generation; no draft published")
    _publish(output, raw)
    return output


def draft_reasoning_review(
    database: Database,
    settings: Settings,
    config: OpenAIResponsesSettings,
    *,
    job_dir: Path,
    client: httpx.Client | None = None,
    fallback_generator: Callable[[ResponsesReasoningTaskV2], RoutedReviewContentV2] | None = None,
) -> Path:
    job_dir = _validated_explicit_job_dir(settings, job_dir)
    task = validate_responses_reasoning_task(database, settings, job_dir=job_dir)
    output = job_dir / "reasoning" / "drafts.json"
    _assert_unused_output(output)
    with database.session_factory() as session:
        run = session.scalar(
            select(ResearchRunV2)
            .options(selectinload(ResearchRunV2.signals))
            .where(ResearchRunV2.id == task.run_id)
        )
        if run is None or any(
            signal.reasoning_reviews
            for signal in run.signals
            if signal.envelope.get("generation_reason") != "external_shadow"
        ):
            raise OpenAIResponsesError("blind review is missing or already finalized")
    instructions = (
        "Review only the frozen outcome-blind inputs. Do not use external facts. "
        "Score all five rubric dimensions 0-2, with a concise advisory. "
        "Keep every signal ID and review_input_hash. Return JSON with reviews. "
        "Declare model gpt-6.1-sol and reasoning_effort high in each rubric."
    )
    if isinstance(task, ResponsesReasoningTaskV3):
        from .research_prompts import BLIND_REVIEW_V3

        instructions = BLIND_REVIEW_V3 + "\nSelected model: gpt-6.1-sol / high."
    try:
        content, provenance = _generate(
            config,
            payload=task,
            instructions=instructions,
            output_model=ResponsesReviewContent,
            client=client,
        )
    except OpenAIAvailabilityError as exc:
        if (
            not isinstance(task, ResponsesReasoningTaskV2)
            or task.fallback_policy != "quota-or-availability/v1"
            or fallback_generator is None
        ):
            raise
        failed_at = datetime.now(ZoneInfo(settings.timezone))
        if validate_responses_reasoning_task(database, settings, job_dir=job_dir) != task:
            raise OpenAIResponsesError("blind review input changed before fallback") from None
        content = RoutedReviewContentV2.model_validate(fallback_generator(task))
        provenance = {
            "surface": "codex",
            "model": RESPONSES_FALLBACK_MODEL,
            "reasoning_effort": RESPONSES_EFFORT,
            "primary_surface": "openai_responses",
            "primary_model": RESPONSES_MODEL,
            "fallback_reason": exc.reason,
            "primary_failed_at": failed_at.isoformat(),
        }
    bundle_type = (
        ResponsesReviewBundleV2
        if isinstance(task, ResponsesReasoningTaskV2)
        else ResponsesReviewBundleV1
    )
    bundle = bundle_type(
        run_id=task.run_id,
        task_hash=task.task_hash,
        generated_at=datetime.now(ZoneInfo(settings.timezone)),
        generated_by=provenance,
        reviews=[item.model_dump() for item in content.reviews],
    )
    expected = {item.signal_id: item.review_input_hash for item in task.reviews}
    if {item.signal_id: item.review_input_hash for item in bundle.reviews} != expected:
        raise OpenAIResponsesError("review output changed its frozen signal/hash coverage")
    current = validate_responses_reasoning_task(database, settings, job_dir=job_dir)
    if current != task:
        raise OpenAIResponsesError("blind review input changed during generation")
    _publish(output, canonical_json(bundle))
    return output
