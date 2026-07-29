from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app import auth as auth_module
from app.config import Settings
from app.main import create_app
from app.models import (
    EvaluationResult,
    Forecast,
    PriceObservation,
    UserJudgment,
    WorkflowRun,
)
from app.schemas import UserJudgmentCreate
from app.services.user_judgment import (
    UserJudgmentNotFoundError,
    create_user_judgment,
)
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

TEST_OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"
INVALID_OPERATOR_TOKEN = "wrong-operator-token-0123456789abcdef"
OPERATOR_HEADERS = {"Authorization": f"Bearer {TEST_OPERATOR_TOKEN}"}
PROTECTED_POSTS = (
    "/api/user-judgments",
    "/api/runs",
    "/api/evaluations/run",
)
PRIVATE_USER_JUDGMENT_GETS = (
    "/api/user-judgments/targets",
    "/api/user-judgments",
    "/api/user-judgments/not-found",
    "/api/user-judgments/not-found/wiki",
)
ZONE = ZoneInfo("Asia/Shanghai")


def _judgment_payload(
    forecast_id: str,
    *,
    blind: bool = False,
    marker: str = "",
) -> dict:
    return {
        "forecast_id": forecast_id,
        "direction": "up",
        "confidence": 0.67,
        "rationale": (f"{marker}流动性改善与风险偏好回升可能共同推动目标指数在预测窗口内走强。"),
        "counter_evidence": "海外利率重新上行可能压制成长估值并削弱流动性改善效果。",
        "invalidation_condition": "若成交额显著收缩且跌破基准日低点则本判断失效。",
        "blind_attestation": blind,
    }


def _live_settings(tmp_path, *, operator_token: str | None) -> Settings:
    wiki_path = tmp_path / "wiki"
    wiki_path.mkdir(exist_ok=True)
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'operator-auth.sqlite3'}",
        checkpoint_path=tmp_path / "operator-auth-checkpoint.sqlite3",
        wiki_path=wiki_path,
        user_judgment_wiki_root=tmp_path / "user-wiki",
        execution_provider="codex_file",
        demo_mode=False,
        operator_token=operator_token,
        auto_seed=False,
    )


@pytest.fixture
def live_client(tmp_path) -> Iterator[TestClient]:
    settings = _live_settings(
        tmp_path,
        operator_token=TEST_OPERATOR_TOKEN,
    )
    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "configured",
    (
        "short-token",
        "operator-token-with-whitespace-0123456789 abcdef",
    ),
)
def test_operator_token_rejects_misconfiguration_without_echoing_secret(
    configured: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(operator_token=configured)

    assert configured not in str(exc_info.value)


def test_operator_token_is_secret_and_blank_configuration_is_missing() -> None:
    settings = Settings(operator_token=TEST_OPERATOR_TOKEN)

    assert isinstance(settings.operator_token, SecretStr)
    assert TEST_OPERATOR_TOKEN not in repr(settings)
    assert TEST_OPERATOR_TOKEN not in settings.model_dump_json()
    assert Settings(operator_token="").operator_token is None


def test_llm_api_key_is_secret_and_blank_configuration_is_missing() -> None:
    api_key = "test-private-llm-api-key"
    settings = Settings(llm_api_key=api_key)

    assert isinstance(settings.llm_api_key, SecretStr)
    assert api_key not in repr(settings)
    assert api_key not in settings.model_dump_json()
    assert Settings(llm_api_key="").llm_api_key is None


def test_non_demo_missing_operator_configuration_fails_closed(tmp_path) -> None:
    settings = _live_settings(tmp_path, operator_token=None)

    with TestClient(create_app(settings, allow_schema_bootstrap=True)) as blocked_client:
        for path in (*PROTECTED_POSTS, *PRIVATE_USER_JUDGMENT_GETS):
            method = blocked_client.post if path in PROTECTED_POSTS else blocked_client.get
            response = method(path, json={}) if path in PROTECTED_POSTS else method(path)

            assert response.status_code == 503
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {"detail": "Operator authentication is unavailable"}

        assert blocked_client.get("/api/health").status_code == 200


@pytest.mark.parametrize("path", PROTECTED_POSTS)
def test_non_demo_post_requires_bearer_operator_token(
    live_client: TestClient,
    path: str,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    missing = live_client.post(path, json={})
    invalid = live_client.post(
        path,
        json={},
        headers={"Authorization": f"Bearer {INVALID_OPERATOR_TOKEN}"},
    )

    for response in (missing, invalid):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"detail": "Operator authentication required"}
        assert INVALID_OPERATOR_TOKEN not in response.text
    assert TEST_OPERATOR_TOKEN not in caplog.text
    assert INVALID_OPERATOR_TOKEN not in caplog.text


def test_operator_auth_ignores_query_tokens_and_rejects_other_schemes(
    live_client: TestClient,
) -> None:
    query = live_client.get(
        "/api/user-judgments",
        params={"operator_token": TEST_OPERATOR_TOKEN},
    )
    basic = live_client.get(
        "/api/user-judgments",
        headers={"Authorization": f"Basic {TEST_OPERATOR_TOKEN}"},
    )

    for response in (query, basic):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_operator_auth_uses_constant_time_comparison(
    live_client: TestClient,
    monkeypatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def compare_digest(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return False

    monkeypatch.setattr(auth_module, "compare_digest", compare_digest)

    response = live_client.get(
        "/api/user-judgments",
        headers={"Authorization": f"Bearer {INVALID_OPERATOR_TOKEN}"},
    )

    assert response.status_code == 401
    assert calls == [
        (
            INVALID_OPERATOR_TOKEN.encode(),
            TEST_OPERATOR_TOKEN.encode(),
        )
    ]


@pytest.mark.parametrize(
    ("path", "expected_status"),
    (
        ("/api/user-judgments", 422),
        ("/api/runs", 409),
        ("/api/evaluations/run", 422),
    ),
)
def test_valid_operator_token_reaches_protected_post_handler(
    live_client: TestClient,
    path: str,
    expected_status: int,
) -> None:
    response = live_client.post(
        path,
        json={},
        headers=OPERATOR_HEADERS,
    )

    assert response.status_code == expected_status
    assert response.status_code != 401


def test_private_user_judgment_gets_require_operator(
    live_client: TestClient,
) -> None:
    for path in PRIVATE_USER_JUDGMENT_GETS:
        unauthorized = live_client.get(path)
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == "Bearer"

    targets = live_client.get(
        "/api/user-judgments/targets",
        headers=OPERATOR_HEADERS,
    )
    judgments = live_client.get(
        "/api/user-judgments",
        headers=OPERATOR_HEADERS,
    )
    assert targets.status_code == 200
    assert targets.headers["cache-control"] == "no-store"
    assert judgments.status_code == 200
    assert judgments.headers["cache-control"] == "no-store"
    assert (
        live_client.get(
            "/api/user-judgments/not-found",
            headers=OPERATOR_HEADERS,
        ).status_code
        == 404
    )
    assert (
        live_client.get(
            "/api/user-judgments/not-found/wiki",
            headers=OPERATOR_HEADERS,
        ).status_code
        == 404
    )


def test_public_non_demo_reads_remain_available_without_operator_token(
    live_client: TestClient,
) -> None:
    assert live_client.get("/api/health").status_code == 200
    assert live_client.get("/api/forecasts/latest").status_code == 404
    assert live_client.get("/api/wiki").status_code == 200


def test_demo_keeps_local_routes_available_without_operator_token(
    client: TestClient,
) -> None:
    targets = client.get("/api/user-judgments/targets")
    judgments = client.get("/api/user-judgments")
    assert targets.status_code == 200
    assert targets.headers["cache-control"] == "no-store"
    assert judgments.status_code == 200
    assert judgments.headers["cache-control"] == "no-store"
    assert client.post("/api/runs", json={}).status_code == 201
    assert client.post("/api/user-judgments", json={}).status_code == 422
    assert client.post("/api/evaluations/run", json={}).status_code == 409


def test_demo_cannot_read_or_mutate_live_resources_in_a_mixed_database(
    client: TestClient,
) -> None:
    demo_run = client.post(
        "/api/runs",
        json={"as_of": "2026-07-24T15:00:00+08:00"},
    )
    live_seed = client.post(
        "/api/runs",
        json={"as_of": "2026-07-25T15:00:00+08:00"},
    )
    assert demo_run.status_code == 201
    assert live_seed.status_code == 201

    with client.app.state.database.session_factory() as session:
        demo_forecast = session.scalar(
            select(Forecast)
            .where(Forecast.run_id == demo_run.json()["id"])
            .order_by(Forecast.index_code, Forecast.horizon)
        )
        assert demo_forecast is not None
        demo_forecast_id = demo_forecast.id

    demo_created = client.post(
        "/api/user-judgments",
        json=_judgment_payload(demo_forecast_id, marker="DEMO-PRIVATE-MARKER-"),
    )
    assert demo_created.status_code == 201
    demo_judgment = demo_created.json()

    now = datetime.now(ZONE).replace(microsecond=0)
    with client.app.state.database.session_factory() as session:
        live_run = session.get(WorkflowRun, live_seed.json()["id"])
        assert live_run is not None
        live_run.mode = "live"
        live_run.completed_at = now - timedelta(minutes=5)
        live_forecasts = sorted(
            live_run.forecasts,
            key=lambda item: (item.index_code, item.horizon),
        )
        for forecast in live_forecasts:
            forecast.target_date = (now + timedelta(days=2)).date()
        session.commit()
        live_private_forecast_id = live_forecasts[0].id
        live_attack_forecast = live_forecasts[1]
        live_attack_forecast_id = live_attack_forecast.id
        live_attack_base_date = live_attack_forecast.base_trade_date
        live_attack_target_date = live_attack_forecast.target_date

        live_request = UserJudgmentCreate.model_validate(
            _judgment_payload(
                live_private_forecast_id,
                blind=True,
                marker="LIVE-PRIVATE-MARKER-",
            )
        )
        live_judgment, created = create_user_judgment(
            session,
            request=live_request,
            actor_id=client.app.state.settings.user_judgment_actor_id,
            wiki_root=client.app.state.settings.user_judgment_wiki_root,
            timezone=client.app.state.settings.timezone,
            window_minutes=client.app.state.settings.user_judgment_window_minutes,
            expected_mode="live",
            market_universe_hash=client.app.state.workflow.universe.content_hash,
            now=now,
        )
        assert created is True
        assert live_judgment.formal_score_eligible is True
        live_judgment_id = live_judgment.id
        live_private_marker = live_judgment.rationale

    targets = client.get("/api/user-judgments/targets")
    history = client.get("/api/user-judgments")
    demo_detail = client.get(f"/api/user-judgments/{demo_judgment['id']}")
    demo_wiki = client.get(demo_judgment["wiki_url"])
    live_detail = client.get(f"/api/user-judgments/{live_judgment_id}")
    live_wiki = client.get(f"/api/user-judgments/{live_judgment_id}/wiki")

    assert targets.status_code == 200
    assert targets.headers["cache-control"] == "no-store"
    assert all(item["mode"] == "demo" for item in targets.json()["items"])
    assert live_private_forecast_id not in targets.text
    assert live_attack_forecast_id not in targets.text
    assert history.status_code == 200
    assert history.headers["cache-control"] == "no-store"
    assert [item["id"] for item in history.json()["items"]] == [demo_judgment["id"]]
    assert live_judgment_id not in history.text
    assert live_private_marker not in history.text
    assert demo_detail.status_code == 200
    assert demo_detail.headers["cache-control"] == "no-store"
    assert demo_wiki.status_code == 200
    assert demo_wiki.headers["cache-control"] == "no-store"
    for hidden in (live_detail, live_wiki):
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "User Judgment not found"}
        assert live_private_marker not in hidden.text

    attempted_judgment = UserJudgmentCreate.model_validate(
        _judgment_payload(
            live_attack_forecast_id,
            blind=True,
            marker="BLOCKED-LIVE-WRITE-",
        )
    )
    with client.app.state.database.session_factory() as session:
        with pytest.raises(UserJudgmentNotFoundError):
            create_user_judgment(
                session,
                request=attempted_judgment,
                actor_id=client.app.state.settings.user_judgment_actor_id,
                wiki_root=client.app.state.settings.user_judgment_wiki_root,
                timezone=client.app.state.settings.timezone,
                window_minutes=client.app.state.settings.user_judgment_window_minutes,
                expected_mode="demo",
                market_universe_hash=client.app.state.workflow.universe.content_hash,
                now=now,
            )
        session.rollback()
        before = {
            "judgments": session.scalar(select(func.count()).select_from(UserJudgment)),
            "evaluations": session.scalar(select(func.count()).select_from(EvaluationResult)),
            "prices": session.scalar(select(func.count()).select_from(PriceObservation)),
        }
    wiki_before = {
        path.relative_to(client.app.state.settings.user_judgment_wiki_root)
        for path in client.app.state.settings.user_judgment_wiki_root.rglob("*.md")
    }

    blocked_judgment = client.post(
        "/api/user-judgments",
        json=attempted_judgment.model_dump(mode="json"),
    )
    blocked_evaluation = client.post(
        "/api/evaluations/run",
        json={
            "observations": [
                {
                    "forecast_id": live_attack_forecast_id,
                    "price_source": "test",
                    "observed_at": (f"{live_attack_target_date.isoformat()}T15:10:00+08:00"),
                    "start": {
                        "trade_date": live_attack_base_date.isoformat(),
                        "close": 100,
                        "source_url": "https://example.com/start",
                        "source_hash": hashlib.sha256(b"blocked-start").hexdigest(),
                    },
                    "end": {
                        "trade_date": live_attack_target_date.isoformat(),
                        "close": 101,
                        "source_url": "https://example.com/end",
                        "source_hash": hashlib.sha256(b"blocked-end").hexdigest(),
                    },
                }
            ]
        },
    )

    assert blocked_judgment.status_code == 404
    assert blocked_judgment.json() == {"detail": "Forecast not found"}
    assert blocked_evaluation.status_code == 409
    assert "unavailable in Demo mode" in blocked_evaluation.text

    with client.app.state.database.session_factory() as session:
        after = {
            "judgments": session.scalar(select(func.count()).select_from(UserJudgment)),
            "evaluations": session.scalar(select(func.count()).select_from(EvaluationResult)),
            "prices": session.scalar(select(func.count()).select_from(PriceObservation)),
        }
        assert (
            session.scalar(
                select(UserJudgment).where(UserJudgment.forecast_id == live_attack_forecast_id)
            )
            is None
        )
    wiki_after = {
        path.relative_to(client.app.state.settings.user_judgment_wiki_root)
        for path in client.app.state.settings.user_judgment_wiki_root.rglob("*.md")
    }
    assert after == before
    assert wiki_after == wiki_before


def test_cors_preflight_allows_only_frontend_methods_and_headers(
    live_client: TestClient,
) -> None:
    origin = "http://localhost:5173"
    allowed = live_client.options(
        "/api/user-judgments",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": ("authorization,content-type,idempotency-key"),
        },
    )
    disallowed_method = live_client.options(
        "/api/user-judgments",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "DELETE",
        },
    )
    disallowed_header = live_client.options(
        "/api/user-judgments",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-operator-token",
        },
    )
    trusted_origin_without_token = live_client.get(
        "/api/user-judgments",
        headers={"Origin": origin},
    )
    untrusted_origin = live_client.get(
        "/api/user-judgments",
        headers={"Origin": "https://attacker.example"},
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in allowed.headers
    assert allowed.headers["access-control-allow-methods"] == "GET, POST"
    allowed_headers = allowed.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "idempotency-key"):
        assert header in allowed_headers
    assert disallowed_method.status_code == 400
    assert disallowed_header.status_code == 400
    assert trusted_origin_without_token.status_code == 401
    assert trusted_origin_without_token.headers["access-control-allow-origin"] == origin
    assert untrusted_origin.status_code == 401
    assert "access-control-allow-origin" not in untrusted_origin.headers
