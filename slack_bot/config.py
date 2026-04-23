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

    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_ops_channel: str = "aegis-ops"
    aegisdb_base_url: str = "http://localhost:8001"

    # Redis — same values as core config, bot needs its own connection
    redis_host: str = "localhost"
    redis_port: int = 6379

    # Hardcoded table → Slack UID map for demo
    # Replace U00000000 with your actual Slack member IDs
    @property
    def table_owner_map(self) -> dict[str, str]:
        return {
            "orders":    "U00000000",
            "customers": "U00000000",
        }


slack_settings = SlackSettings()