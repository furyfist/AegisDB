from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenMetadata
    om_host: str = "http://localhost:8585"
    om_admin_email: str = "admin@open-metadata.org"
    om_admin_password: str = "abcd1234"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_stream_name: str = "aegisdb:events"

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    webhook_secret: str = "aegisdb_secret_123"


settings = Settings()