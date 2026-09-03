from __future__ import annotations

from datetime import time

from app.config import REPOSITORY_ROOT, Settings


def test_pytest_does_not_load_repository_dotenv_by_default() -> None:
    assert Settings.model_config["env_file"] is None


def test_local_wiki_and_archives_default_to_gitignored_data() -> None:
    settings = Settings()

    assert settings.wiki_path == REPOSITORY_ROOT / "data" / "wiki"
    assert settings.bundled_wiki_path == REPOSITORY_ROOT / "wiki"
    assert (
        settings.reflection_archive_root
        == REPOSITORY_ROOT / "data" / "reflection-archives"
    )
    assert settings.lesson_archive_root == REPOSITORY_ROOT / "data" / "lesson-archives"
    assert settings.user_judgment_market_open == time(9, 30)


def test_user_judgment_market_open_loads_from_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VERICOUNCIL_USER_JUDGMENT_MARKET_OPEN", "08:45")

    assert Settings().user_judgment_market_open == time(8, 45)


def test_blank_private_runtime_trace_policy_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("FORECAST_LOOP_AGENT_RUNTIME_TRACE_POLICY", "  ")

    assert Settings().agent_runtime_trace_policy is None
