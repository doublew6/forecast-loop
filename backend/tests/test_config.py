from __future__ import annotations

from app.config import Settings


def test_pytest_does_not_load_repository_dotenv_by_default() -> None:
    assert Settings.model_config["env_file"] is None
