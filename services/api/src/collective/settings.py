from typing import Literal

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr | None = None
    supabase_url: AnyHttpUrl | None = None
    supabase_publishable_key: str | None = None
    supabase_secret_key: SecretStr | None = None
    supabase_jwt_issuer: str | None = None
    supabase_jwt_audience: str = "authenticated"
    model_mode: Literal["live", "record", "replay"] = "replay"
    model_provider: Literal["gemini"] = "gemini"
    model_fast_id: str = "gemini-3.8-flash"
    model_strong_id: str = "gemini-4-argon"
    gemini_api_key: SecretStr | None = None
    embedding_mode: Literal["local", "hosted", "replay"] = "replay"
    enable_worker: bool = True
    demo_mode: bool = False
