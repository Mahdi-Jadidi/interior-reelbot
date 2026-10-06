from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    ai_router_api_key: str
    data_dir: Path
    allowed_chat_ids: frozenset[int]
    monthly_ai_limit_usd: float = 10.0
    higgsfield_monthly_credit_limit: float = 0.0
    public_base_url: str = ""
    upload_secret: str = ""
    operator_chat_id: int | None = None
    brand_profile_path: Path | None = None
    ai_base_url: str = "https://miarouter.online/v1"
    ai_cheap_model: str = "matilda-cerulean-i"
    ai_creative_model: str = "claude-sonnet-5"
    ai_plan_request_cost_usd: float = 0.25
    ai_provider: str = "miarouter"
    chatgpt_plan_model: str = "gpt-6.1-sol"

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
            ai_router_api_key=os.getenv("AI_ROUTER_API_KEY", ""),
            data_dir=data_dir,
            allowed_chat_ids=ids,
            monthly_ai_limit_usd=float(os.getenv("AI_MONTHLY_LIMIT_USD", "10")),
            higgsfield_monthly_credit_limit=float(
                os.getenv("HIGGSFIELD_MONTHLY_CREDIT_LIMIT", "0")
            ),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
            upload_secret=os.getenv("UPLOAD_SECRET", ""),
            operator_chat_id=int(os.environ["OPERATOR_CHAT_ID"]) if os.getenv("OPERATOR_CHAT_ID") else None,
            brand_profile_path=Path(os.environ["BRAND_PROFILE_PATH"]).resolve() if os.getenv("BRAND_PROFILE_PATH") else None,
            ai_base_url=os.getenv("AI_BASE_URL", "https://miarouter.online/v1").rstrip("/"),
            ai_cheap_model=os.getenv("AI_CHEAP_MODEL", "matilda-cerulean-i"),
            ai_creative_model=os.getenv("AI_CREATIVE_MODEL", "claude-sonnet-5"),
            ai_plan_request_cost_usd=float(os.getenv("AI_PLAN_REQUEST_COST_USD", "0.25")),
            ai_provider=os.getenv("AI_PROVIDER", "miarouter").strip().lower(),
            chatgpt_plan_model=os.getenv("CHATGPT_PLAN_MODEL", "gpt-6.1-sol").strip(),
        )

    def validate(self) -> None:
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if self.ai_provider not in {"miarouter", "chatgpt_plan"}:
            raise ValueError("AI_PROVIDER must be miarouter or chatgpt_plan")
        if self.ai_provider == "miarouter" and not self.ai_router_api_key:
            raise ValueError("AI_ROUTER_API_KEY is required when AI_PROVIDER=miarouter")
        if self.ai_provider == "chatgpt_plan" and not self.chatgpt_plan_model:
            raise ValueError("CHATGPT_PLAN_MODEL is required when AI_PROVIDER=chatgpt_plan")
        if self.ai_provider == "miarouter" and self.monthly_ai_limit_usd <= 0:
            raise ValueError("AI_MONTHLY_LIMIT_USD must be positive")
        if self.ai_provider == "miarouter" and self.ai_plan_request_cost_usd <= 0:
            raise ValueError("AI_PLAN_REQUEST_COST_USD must be positive")
