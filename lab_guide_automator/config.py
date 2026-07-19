"""
Pydantic settings — reads from .env or environment variables.

No API keys required by default — the server auto-uses your GitHub Copilot
access (via `gh auth token`) which is already set up on this machine.
"""
from __future__ import annotations
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # AI — defaults to "copilot" (GitHub Copilot via gh CLI, no key needed)
    ai_provider: str = Field("copilot", alias="AI_PROVIDER")

    # Copilot model — can be overridden at runtime via the dashboard selector
    copilot_model: str = Field("claude-sonnet-4.6", alias="COPILOT_MODEL")

    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o", alias="OPENAI_MODEL")
    openai_vision_model: str = Field("gpt-4o", alias="OPENAI_VISION_MODEL")

    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-opus-4-5", alias="ANTHROPIC_MODEL")
    anthropic_vision_model: str = Field("claude-opus-4-5", alias="ANTHROPIC_VISION_MODEL")

    ollama_base_url: str = Field("http://localhost:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field("llava", alias="OLLAMA_MODEL")

    # TTS
    tts_provider: str = Field("openai", alias="TTS_PROVIDER")
    tts_voice: str = Field("nova", alias="TTS_VOICE")

    # MkDocs / git
    mkdocs_repo_url: str = Field("", alias="MKDOCS_REPO_URL")
    mkdocs_branch: str = Field("main", alias="MKDOCS_BRANCH")
    mkdocs_site_dir: str = Field("site", alias="MKDOCS_SITE_DIR")

    # Storage
    data_dir: Path = Field(Path("./data"), alias="DATA_DIR")

    def chat_model(self) -> str:
        if self.ai_provider == "copilot":
            return self.copilot_model
        if self.ai_provider == "anthropic":
            return self.anthropic_model
        if self.ai_provider == "ollama":
            return self.ollama_model
        return self.openai_model

    def vision_model(self) -> str:
        if self.ai_provider == "copilot":
            return self.copilot_model
        if self.ai_provider == "anthropic":
            return self.anthropic_vision_model
        if self.ai_provider == "ollama":
            return self.ollama_model
        return self.openai_vision_model
