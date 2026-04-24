from pathlib import Path
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


class SlackSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_ops_channel: str = "aegis-ops"

    # AegisDB backend
    aegisdb_base_url: str = "http://localhost:8001"

    # Redis — bot needs its own connection
    redis_host: str = "localhost"
    redis_port: int = 6379

    # LLM — read same key as backend, plain str (no SecretStr needed in bot)
    groq_api_key: str = ""

    # ChromaDB — must match backend's CHROMA_PERSIST_DIR
    chroma_persist_dir: str = "./data/chromadb"

    # Demo: table name → Slack member UID
    # Replace U00000000 with real Slack member IDs before demo
    @property
    def table_owner_map(self) -> dict[str, str]:
        return {
            "orders":    "U00000000",
            "customers": "U00000000",
        }


slack_settings = SlackSettings()