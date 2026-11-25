from functools import lru_cache
from typing import Annotated, Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DEFAULT_JWT_SECRET = "dev-jwt-secret-change-in-production-min-32-bytes"
_DEFAULT_QR_HMAC_SECRET = "dev-qr-hmac-secret-change-in-production-32b"
_DEFAULT_RAZORPAY_WEBHOOK_SECRET = "rzp_test_mock_webhook_secret_placeholder"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "postgresql+asyncpg://galley:galley@localhost:5432/galley"
    REDIS_URL: str = "redis://localhost:6379/0"
    JWT_SECRET: str = _DEFAULT_JWT_SECRET
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480
    QR_HMAC_SECRET: str = _DEFAULT_QR_HMAC_SECRET
    QR_EXPIRY_SECONDS: int = 300
    QR_REDIS_TTL_BUFFER_SECONDS: int = 600
    RAZORPAY_KEY_ID: str = "rzp_test_mock_key_id"
    RAZORPAY_KEY_SECRET: str = "rzp_test_mock_key_secret_placeholder"
    RAZORPAY_WEBHOOK_SECRET: str = _DEFAULT_RAZORPAY_WEBHOOK_SECRET
    RAZORPAY_MOCK_MODE: bool = True
    CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
    ]
    SURGE_START_HOUR: int = 12
    SURGE_END_HOUR: int = 13
    TIMEZONE: str = "Asia/Kolkata"
    BCRYPT_ROUNDS: int = 12
    PIN_LOCKOUT_THRESHOLD: int = 5
    PIN_LOCKOUT_MINUTES: int = 15
    ORDER_PAYMENT_TIMEOUT_MINUTES: int = 10
    ESCALATION_SLA_MINUTES: int = 15
    KITCHEN_PARALLELISM: int = 2
    DEMO_MODE_ENABLED: bool = False
    OFFLINE_SYNC_MAX_AGE_MINUTES: int = 240
    PAISE_PER_GOLD_COIN: int = 100
    POINTS_PER_GOLD_COIN: int = 10
    PAISE_PER_LOYALTY_POINT: int = 1000

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            return [str(origin).strip() for origin in value if str(origin).strip()]
        raise ValueError("CORS_ORIGINS must be a list of origins or a comma-separated string")

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.ENVIRONMENT.lower() == "production":
            secrets_to_check = [
                ("JWT_SECRET", self.JWT_SECRET, _DEFAULT_JWT_SECRET),
                ("QR_HMAC_SECRET", self.QR_HMAC_SECRET, _DEFAULT_QR_HMAC_SECRET),
                (
                    "RAZORPAY_WEBHOOK_SECRET",
                    self.RAZORPAY_WEBHOOK_SECRET,
                    _DEFAULT_RAZORPAY_WEBHOOK_SECRET,
                ),
            ]
            for name, value, default_value in secrets_to_check:
                if value == default_value or len(value.encode("utf-8")) < 32:
                    raise ValueError(
                        f"{name} must be non-default and at least 32 bytes when ENVIRONMENT=production"
                    )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
