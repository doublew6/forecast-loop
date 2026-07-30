from __future__ import annotations

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
