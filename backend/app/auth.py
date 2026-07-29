"""Operator authentication for private and state-changing HTTP routes."""

from __future__ import annotations

from hmac import compare_digest
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_OPERATOR_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="OperatorBearer",
    description=(
        "Operator token required for non-Demo state changes and private User Judgment data."
    ),
)
_AUTHENTICATION_HEADERS = {
    "WWW-Authenticate": "Bearer",
    "Cache-Control": "no-store",
}


def require_operator(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(_OPERATOR_BEARER),
    ] = None,
) -> None:
    """Require the configured operator Bearer token outside Demo mode."""

    settings = request.app.state.settings
    if settings.use_demo_provider:
        return

    configured = settings.operator_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operator authentication is unavailable",
            headers={"Cache-Control": "no-store"},
        )
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not compare_digest(
            credentials.credentials.encode("utf-8"),
            configured.get_secret_value().encode("utf-8"),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator authentication required",
            headers=_AUTHENTICATION_HEADERS,
        )
