from pydantic import Field, SecretStr
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
    redis_repair_stream: str = "aegisdb:repair"
    redis_escalation_stream: str = "aegisdb:escalation"
    redis_apply_stream: str = "aegisdb:apply"
    redis_consumer_group: str = "aegisdb-agents"
    redis_consumer_name: str = "diagnosis-agent-1"

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    webhook_secret: str = "aegisdb_secret_123"
    

    # LLM
    groq_api_key: SecretStr = Field(...)
    llm_model: str = "llama-3.3-70b-versatile"
    llm_max_tokens: int = 4096

    # ChromaDB
    chroma_persist_dir: str = "./data/chromadb"
    chroma_collection: str = "aegisdb_fixes"

    # Diagnosis
    confidence_threshold: float = 0.70

    # Target database
    target_db_host: str = "localhost"
    target_db_port: int = 5433
    target_db_name: str = "aegisdb"
    target_db_user: str = "aegisdb_user"
    target_db_password: str = "aegisdb_pass"

    # Sandbox
    sandbox_max_retries: int = 3
    sandbox_sample_rows: int = 500
    sandbox_timeout_seconds: int = 60

    # Apply
    dry_run: bool = True
    apply_statement_timeout_ms: int = 30000
    post_apply_verify: bool = True

settings = Settings()
