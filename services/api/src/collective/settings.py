from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
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
    embedding_model_revision: str = Field(
        default="2d48f1737679ad900d5c26c5aad5410e9c70fdca", pattern=r"^[0-9a-f]{40}$"
    )
    embedding_url: AnyHttpUrl | None = None
    embedding_api_key: SecretStr | None = None
    enable_worker: bool = True
    demo_mode: bool = False
