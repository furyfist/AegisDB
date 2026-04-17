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

    # LLM
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 2048

    # ChromaDB
    chroma_persist_dir: str = "./data/chromadb"
    chroma_collection: str = "aegisdb_fixes"

    # Diagnosis
    confidence_threshold: float = 0.70

settings = Settings()