"""Validated configuration; importing settings does not construct network clients."""

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BILLING_", env_file=None, extra="forbid")
    mode: Literal["offline", "online"] = "offline"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    ocr_endpoint: str = "http://36.103.199.11:5002/process"
    ocr_connect_timeout: float = Field(default=10, gt=0, le=300)
    ocr_read_timeout: float = Field(default=300, gt=0, le=600)
    model_provider: str | None = None
    model_name: str | None = None
    model_base_url: str = "https://api.deepseek.com"
    model_timeout: float = Field(default=60, gt=0, le=300)
    model_max_tokens: int = Field(default=4096, ge=128, le=4096)
    ocr_connect_retries: int = Field(default=1, ge=0, le=2)
    model_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def online_requires_credentials(self):
        if self.mode == "online":
            if (
                not self.model_provider
                or not self.model_provider.strip()
                or not self.model_name
                or not self.model_name.strip()
                or self.model_api_key is None
                or not self.model_api_key.get_secret_value().strip()
            ):
                raise ValueError("online mode requires provider, model name and API key")
        return self

    def public_summary(self):
        return {
            "mode": self.mode,
            "log_level": self.log_level,
            "model_configured": bool(self.model_provider and self.model_name and self.model_api_key),
            "network_call_performed": False,
        }
