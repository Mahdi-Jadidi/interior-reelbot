from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    openai_api_key: str
    data_dir: Path
    allowed_chat_ids: frozenset[int]
    monthly_openai_limit_usd: float = 10.0
    higgsfield_monthly_credit_limit: float = 0.0
    public_base_url: str = ""
    upload_secret: str = ""
    operator_chat_id: int | None = None
    brand_profile_path: Path | None = None
    ai_base_url: str = "https://api.imarouter.com/v1"
    ai_cheap_model: str = "gpt-5.2"
    ai_creative_model: str = "gpt-5.5"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(override=False)
        ids = frozenset(
            int(item.strip())
            for item in os.getenv("ALLOWED_TELEGRAM_CHAT_IDS", "").split(",")
            if item.strip()
        )
        data_dir = Path(os.getenv("REELBOT_DATA_DIR", "./data")).resolve()
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            # MiA Router is OpenAI-compatible. Keep the legacy variable as a
            # fallback for existing installations.
            openai_api_key=os.getenv("AI_ROUTER_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            data_dir=data_dir,
            allowed_chat_ids=ids,
            monthly_openai_limit_usd=float(os.getenv("OPENAI_MONTHLY_LIMIT_USD", "10")),
            higgsfield_monthly_credit_limit=float(
                os.getenv("HIGGSFIELD_MONTHLY_CREDIT_LIMIT", "0")
            ),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
            upload_secret=os.getenv("UPLOAD_SECRET", ""),
            operator_chat_id=int(os.environ["OPERATOR_CHAT_ID"]) if os.getenv("OPERATOR_CHAT_ID") else None,
            brand_profile_path=Path(os.environ["BRAND_PROFILE_PATH"]).resolve() if os.getenv("BRAND_PROFILE_PATH") else None,
            ai_base_url=os.getenv("AI_BASE_URL", "https://api.imarouter.com/v1").rstrip("/"),
            ai_cheap_model=os.getenv("AI_CHEAP_MODEL", "gpt-5.2"),
            ai_creative_model=os.getenv("AI_CREATIVE_MODEL", "gpt-5.5"),
        )

    def validate(self) -> None:
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if self.monthly_openai_limit_usd <= 0:
            raise ValueError("OPENAI_MONTHLY_LIMIT_USD must be positive")
