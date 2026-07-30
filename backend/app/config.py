"""Application configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings loaded from ``.env`` and environment variables."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    app_name: str = "forecast-loop API"
    app_version: str = "0.1.0"
    api_prefix: str = "/api"
    database_url: str = Field(
        default=f"sqlite:///{REPOSITORY_ROOT / 'data' / 'vericouncil.sqlite3'}",
        validation_alias="VERICOUNCIL_DATABASE_URL",
    )
    checkpoint_path: Path = Field(
        default=REPOSITORY_ROOT / "data" / "checkpoints" / "langgraph.sqlite3",
        validation_alias="VERICOUNCIL_CHECKPOINT_PATH",
    )
    wiki_path: Path = Field(
        default=REPOSITORY_ROOT / "data" / "wiki",
        validation_alias="VERICOUNCIL_WIKI_PATH",
    )
    bundled_wiki_path: Path = Field(
        default=REPOSITORY_ROOT / "wiki",
        validation_alias="FORECAST_LOOP_BUNDLED_WIKI_PATH",
    )
    market_universe_path: Path | None = Field(
        default=None,
        validation_alias="FORECAST_LOOP_MARKET_UNIVERSE_PATH",
    )
    handoff_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "handoffs",
        validation_alias="VERICOUNCIL_HANDOFF_ROOT",
    )
    reflection_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "reflections",
        validation_alias="VERICOUNCIL_REFLECTION_ROOT",
    )
    reflection_archive_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "reflection-archives",
        validation_alias="FORECAST_LOOP_REFLECTION_ARCHIVE_ROOT",
    )
    lesson_archive_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "lesson-archives",
        validation_alias="FORECAST_LOOP_LESSON_ARCHIVE_ROOT",
    )
    market_snapshot_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "market-snapshots",
        validation_alias="VERICOUNCIL_MARKET_SNAPSHOT_ROOT",
    )
    evidence_snapshot_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "evidence-snapshots",
        validation_alias="VERICOUNCIL_EVIDENCE_SNAPSHOT_ROOT",
    )
    prediction_status_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "prediction-status",
        validation_alias="VERICOUNCIL_PREDICTION_STATUS_ROOT",
    )
    user_judgment_wiki_root: Path = Field(
        default=REPOSITORY_ROOT / "data" / "user-wiki",
        validation_alias="VERICOUNCIL_USER_JUDGMENT_WIKI_ROOT",
    )
    user_judgment_actor_id: str = Field(
        default="local-operator",
        min_length=1,
        max_length=120,
        validation_alias="VERICOUNCIL_USER_JUDGMENT_ACTOR_ID",
    )
    operator_token: SecretStr | None = Field(
        default=None,
        validation_alias="FORECAST_LOOP_OPERATOR_TOKEN",
    )
    user_judgment_window_minutes: int = Field(
        default=120,
        ge=5,
        le=24 * 60,
        validation_alias="VERICOUNCIL_USER_JUDGMENT_WINDOW_MINUTES",
    )
    evidence_snapshot_builder: Path | None = Field(
        default=None,
        validation_alias="FORECAST_LOOP_EVIDENCE_SNAPSHOT_BUILDER",
    )
    market_outcome_snapshot_builder: Path | None = Field(
        default=None,
        validation_alias="FORECAST_LOOP_MARKET_OUTCOME_SNAPSHOT_BUILDER",
    )
    snapshot_builder_timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=30 * 60,
        validation_alias="FORECAST_LOOP_SNAPSHOT_BUILDER_TIMEOUT_SECONDS",
    )
    reflection_lesson_proposals_enabled: bool = Field(
        default=False,
        validation_alias="VERICOUNCIL_REFLECTION_LESSON_PROPOSALS_ENABLED",
    )
    reflection_required_human_reviews: int = Field(
        default=10,
        ge=10,
        validation_alias="VERICOUNCIL_REFLECTION_REQUIRED_HUMAN_REVIEWS",
    )
    reflection_shadow_target_dates: int = Field(
        default=20,
        ge=20,
        validation_alias="VERICOUNCIL_REFLECTION_SHADOW_TARGET_DATES",
    )
    evidence_snapshot_path: Path | None = Field(
        default=None,
        validation_alias="VERICOUNCIL_EVIDENCE_SNAPSHOT_PATH",
    )
    timezone: str = Field(default="Asia/Shanghai", validation_alias="VERICOUNCIL_TIMEZONE")
    demo_mode: bool = Field(default=True, validation_alias="VERICOUNCIL_DEMO_MODE")
    execution_provider: Literal["demo", "api", "codex_file"] | None = Field(
        default=None,
        validation_alias="VERICOUNCIL_EXECUTION_PROVIDER",
    )
    auto_seed: bool = Field(default=True, validation_alias="VERICOUNCIL_AUTO_SEED")

    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="LLM_API_KEY",
    )
    llm_base_url: str = Field(default="https://api.openai.com/v1", validation_alias="LLM_BASE_URL")
    llm_model: str = Field(default="gpt-5-mini", validation_alias="LLM_MODEL")
    llm_timeout_seconds: float = Field(
        default=45.0,
        gt=0,
        validation_alias="LLM_TIMEOUT_SECONDS",
    )
    llm_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias="LLM_MAX_RETRIES",
    )
    task_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="FORECAST_LOOP_TASK_MAX_ATTEMPTS",
    )
    task_lease_seconds: int = Field(
        default=60,
        ge=5,
        le=15 * 60,
        validation_alias="FORECAST_LOOP_TASK_LEASE_SECONDS",
    )
    task_timeout_seconds: int = Field(
        default=30 * 60,
        ge=30,
        le=6 * 60 * 60,
        validation_alias="FORECAST_LOOP_TASK_TIMEOUT_SECONDS",
    )
    task_retry_delay_seconds: int = Field(
        default=5,
        ge=0,
        le=15 * 60,
        validation_alias="FORECAST_LOOP_TASK_RETRY_DELAY_SECONDS",
    )

    @field_validator(
        "evidence_snapshot_path",
        "evidence_snapshot_builder",
        "market_outcome_snapshot_builder",
        "market_universe_path",
        mode="before",
    )
    @classmethod
    def blank_optional_path_is_none(cls, value: object) -> object:
        """An empty dotenv assignment must not resolve to the current directory."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("user_judgment_actor_id", mode="before")
    @classmethod
    def strip_user_judgment_actor_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("operator_token")
    @classmethod
    def validate_operator_token(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        if value is None:
            return None
        token = value.get_secret_value()
        if not token:
            return None
        if len(token) < 32:
            raise ValueError("FORECAST_LOOP_OPERATOR_TOKEN must contain at least 32 characters")
        if any(character.isspace() for character in token):
            raise ValueError("FORECAST_LOOP_OPERATOR_TOKEN must not contain whitespace")
        return value

    @field_validator("llm_api_key")
    @classmethod
    def blank_llm_api_key_is_none(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        return value

    @field_validator("task_timeout_seconds")
    @classmethod
    def task_timeout_covers_lease(cls, value: int, info) -> int:
        lease_seconds = info.data.get("task_lease_seconds", 60)
        if value < lease_seconds:
            raise ValueError(
                "FORECAST_LOOP_TASK_TIMEOUT_SECONDS must be at least "
                "FORECAST_LOOP_TASK_LEASE_SECONDS"
            )
        return value

    @property
    def use_demo_provider(self) -> bool:
        return self.execution_mode == "demo"

    @property
    def use_codex_file_provider(self) -> bool:
        return self.execution_mode == "codex_file"

    @property
    def execution_mode(self) -> Literal["demo", "api", "codex_file"]:
        if self.execution_provider is not None:
            return self.execution_provider
        return "demo" if self.demo_mode else "api"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
