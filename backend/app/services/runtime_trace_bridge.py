"""Optional bridge to a private runtime-tracing implementation.

The public application does not depend on, configure, or persist a private
telemetry backend.  Operators may explicitly supply a policy path and install
an implementation of the small ``evalmesh.RuntimeTracer`` contract in the
runtime environment.  Real prompt and output content only crosses this module
while that opt-in execution is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class RuntimeBridgeSetup:
    tracer_type: type[Any] | None
    policy_path: Path | None
    telemetry_complete: bool
    note: str | None = None

    @property
    def enabled(self) -> bool:
        return self.tracer_type is not None and self.policy_path is not None


@dataclass(frozen=True, slots=True)
class RuntimeDelivery:
    trace_id: str | None
    external_id: str | None
    stored: bool
    delivered: bool
    error_code: str | None


def build_runtime_bridge(policy_path: Path | None) -> RuntimeBridgeSetup:
    if policy_path is None:
        return RuntimeBridgeSetup(None, None, True)
    try:
        module = import_module("evalmesh")
        tracer_type = module.RuntimeTracer
        if not isinstance(tracer_type, type):
            raise TypeError("RuntimeTracer is unavailable")
    except Exception as exc:
        return RuntimeBridgeSetup(
            None,
            policy_path,
            False,
            f"runtime trace setup failed: {type(exc).__name__}",
        )
    return RuntimeBridgeSetup(tracer_type, policy_path, True)


class RuntimeExecution:
    """Best-effort owner for one private root Trace."""

    def __init__(
        self,
        setup: RuntimeBridgeSetup,
        *,
        name: str,
        prompt: Any,
        metadata: dict[str, Any],
        tags: list[str],
    ) -> None:
        self._setup = setup
        self._tracer = None
        self._entered = False
        self._name = name
        self._prompt = prompt
        self._metadata = metadata
        self._tags = tags
        self.error_note: str | None = setup.note
        self.delivery = RuntimeDelivery(None, None, False, False, None)

    @property
    def active(self) -> bool:
        return self._entered and self._tracer is not None

    def enter(self) -> None:
        if not self._setup.enabled:
            return
        try:
            assert self._setup.tracer_type is not None
            assert self._setup.policy_path is not None
            tracer = self._setup.tracer_type(
                self._setup.policy_path,
                name=self._name,
                prompt=self._prompt,
                metadata=self._metadata,
                tags=self._tags,
            )
            tracer.__enter__()
            self._tracer = tracer
            self._entered = True
            self.delivery = RuntimeDelivery(
                trace_id=_string_or_none(getattr(tracer, "trace_id", None)),
                external_id=None,
                stored=False,
                delivered=False,
                error_code=None,
            )
        except Exception as exc:
            self.error_note = f"runtime trace start failed: {type(exc).__name__}"
            self._tracer = None
            self._entered = False

    def span(
        self,
        name: str,
        *,
        span_type: Literal["general", "tool", "llm", "guardrail"],
        input_value: Any,
        metadata: dict[str, Any],
        model: str | None,
        provider: str | None,
    ) -> RuntimeSpan | None:
        if not self.active:
            return None
        try:
            context = self._tracer.span(
                name,
                type=span_type,
                input=input_value,
                metadata=metadata,
                model=model,
                provider=provider,
            )
            context.__enter__()
            return RuntimeSpan(context)
        except Exception as exc:
            self.error_note = f"runtime span start failed: {type(exc).__name__}"
            return None

    def set_output(self, value: Any) -> None:
        if not self.active:
            return
        try:
            self._tracer.set_output(value)
        except Exception as exc:
            self.error_note = f"runtime trace output failed: {type(exc).__name__}"

    def close(self, exc: BaseException | None = None) -> RuntimeDelivery:
        if not self.active:
            return self.delivery
        tracer = self._tracer
        assert tracer is not None
        try:
            tracer.__exit__(
                type(exc) if exc is not None else None,
                exc,
                exc.__traceback__ if exc is not None else None,
            )
        except Exception as close_error:
            self.error_note = f"runtime trace delivery failed: {type(close_error).__name__}"
        receipt = getattr(tracer, "receipt", None)
        self.delivery = RuntimeDelivery(
            trace_id=_string_or_none(getattr(tracer, "trace_id", None)),
            external_id=_string_or_none(getattr(receipt, "external_id", None)),
            stored=bool(getattr(receipt, "stored", False)),
            delivered=bool(getattr(receipt, "delivered", False)),
            error_code=_string_or_none(getattr(receipt, "error_code", None)),
        )
        if not self.delivery.delivered and self.error_note is None:
            code = self.delivery.error_code or "not_delivered"
            self.error_note = f"runtime trace delivery failed: {code}"
        self._entered = False
        self._tracer = None
        return self.delivery


class RuntimeSpan:
    """Best-effort child span whose values never enter the public trace store."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self.error_note: str | None = None

    def set_output(self, value: Any) -> None:
        try:
            self._context.set_output(value)
        except Exception as exc:
            self.error_note = f"runtime span output failed: {type(exc).__name__}"

    def set_usage(self, value: dict[str, Any], *, total_cost: float | None = None) -> None:
        try:
            self._context.set_usage(value, total_cost=total_cost)
        except Exception as exc:
            self.error_note = f"runtime span usage failed: {type(exc).__name__}"

    def close(self, exc: BaseException | None = None) -> None:
        try:
            self._context.__exit__(
                type(exc) if exc is not None else None,
                exc,
                exc.__traceback__ if exc is not None else None,
            )
        except Exception as close_error:
            self.error_note = f"runtime span close failed: {type(close_error).__name__}"


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
