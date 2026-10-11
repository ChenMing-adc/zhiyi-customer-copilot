"""Model configuration; unrelated legacy .env keys remain compatible."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ai_provider: str = "openai"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
