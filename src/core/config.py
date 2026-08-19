"""Application settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _csv(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Agent Relay"
    log_level: str = "INFO"
    database_path: Path = Path("./data/agent-relay.db")
    general_workspace: Path = Field(default_factory=lambda: Path.home() / ".agent-relay" / "general-workspace")

    default_workspace: Path = Field(default_factory=Path.cwd)
    workspace_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    max_concurrent_runs: int = Field(default=2, ge=1, le=32)
    run_timeout_seconds: float = Field(default=3600, gt=0)
    approval_timeout_seconds: float = Field(default=1800, gt=0)
    process_interrupt_grace_seconds: float = Field(default=5, ge=0)
    process_terminate_grace_seconds: float = Field(default=3, ge=0)
    subprocess_output_limit_bytes: int = Field(default=2_000_000, ge=65_536)
    subprocess_event_line_limit: int = Field(default=5_000, ge=100, le=100_000)
    max_prompt_chars: int = Field(default=20_000, ge=100, le=500_000)
    max_event_text_chars: int = Field(default=8_000, ge=500, le=100_000)
    handoff_context_chars: int = Field(default=8_000, ge=500, le=100_000)

    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_chat_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    telegram_allowed_user_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    telegram_poll_timeout_seconds: int = Field(default=30, ge=1, le=50)
    telegram_edit_interval_seconds: float = Field(default=1.5, ge=0.5)
    telegram_free_text_mode: Literal["auto", "ask", "run"] = "auto"
    telegram_max_updates_per_minute: int = Field(default=30, ge=1, le=600)
    telegram_webapp_url: str | None = None

    api_enabled: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8787, ge=1, le=65535)
    api_bearer_token: SecretStr | None = None
    api_actor_id: str = Field(default="api", min_length=1, max_length=120)

    codex_executable: str = "codex"
    codex_model: str | None = None
    codex_reasoning_effort: str = "high"
    claude_executable: str = "claude"
    claude_model: str | None = "claude-sonnet-5"
    claude_allowed_tools: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash"]
    )
    agent_env_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "HOME",
            "PATH",
            "USER",
            "LOGNAME",
            "SHELL",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
        ]
    )

    @field_validator(
        "telegram_allowed_chat_ids",
        "telegram_allowed_user_ids",
        "claude_allowed_tools",
        "agent_env_allowlist",
        mode="before",
    )
    @classmethod
    def parse_csv(cls, value: Any) -> list[str]:
        return _csv(value)

    @field_validator("workspace_roots", mode="before")
    @classmethod
    def parse_paths(cls, value: Any) -> list[Path]:
        return [Path(item).expanduser() for item in _csv(value)]

    @field_validator("codex_model", "claude_model", "telegram_webapp_url", mode="before")
    @classmethod
    def blank_to_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_runtime(self) -> Settings:
        self.default_workspace = self.default_workspace.expanduser()
        self.general_workspace = self.general_workspace.expanduser()
        if not self.workspace_roots:
            self.workspace_roots = [self.default_workspace]
        if self.telegram_enabled:
            if not self.telegram_bot_token or not self.telegram_bot_token.get_secret_value():
                raise ValueError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
            if not self.telegram_allowed_chat_ids or not self.telegram_allowed_user_ids:
                raise ValueError(
                    "TELEGRAM_ALLOWED_CHAT_IDS and TELEGRAM_ALLOWED_USER_IDS are required when Telegram is enabled"
                )
        if self.telegram_webapp_url and not self.telegram_webapp_url.startswith("https://"):
            raise ValueError("TELEGRAM_WEBAPP_URL must use HTTPS")
        api_token = self.api_token_value
        if (
            self.api_enabled
            and api_token
            and (len(api_token) < 32 or api_token.lower().startswith(("replace-", "change-", "example-")))
        ):
            raise ValueError("API_BEARER_TOKEN must be a generated secret of at least 32 characters")
        if self.api_enabled and self.api_host not in {"127.0.0.1", "::1", "localhost"} and not api_token:
            raise ValueError("API_BEARER_TOKEN is required when API_HOST is not loopback")
        return self

    @property
    def telegram_token_value(self) -> str:
        return self.telegram_bot_token.get_secret_value() if self.telegram_bot_token else ""

    @property
    def api_token_value(self) -> str:
        return self.api_bearer_token.get_secret_value() if self.api_bearer_token else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
