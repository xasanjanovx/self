from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    supabase_url: str
    supabase_service_role_key: str
    db_table_prefix: str
    gemini_api_key: str
    gemini_model: str
    gemini_vision_model: str
    gemini_transcribe_model: str
    app_timezone: str
    default_currency: str
    default_language: str
    reminder_check_seconds: int
    weekly_report_check_seconds: int
    weekly_report_hour: int
    weekly_report_minute: int


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Environment variable `{name}` is required")
    return value


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _gemini_model(name: str, default: str = "gemini-3-flash-preview") -> str:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    lower = value.lower()
    if lower.startswith("gemini-2.5"):
        return default
    if lower == "gemini-3-flash":
        return default
    return value


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        supabase_url=_required("SUPABASE_URL"),
        supabase_service_role_key=_required("SUPABASE_SERVICE_ROLE_KEY"),
        db_table_prefix=os.getenv("DB_TABLE_PREFIX", "").strip(),
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_model=_gemini_model("GEMINI_MODEL"),
        gemini_vision_model=_gemini_model("GEMINI_VISION_MODEL"),
        gemini_transcribe_model=_gemini_model("GEMINI_TRANSCRIBE_MODEL"),
        app_timezone=os.getenv("APP_TIMEZONE", "Asia/Tashkent"),
        default_currency=os.getenv("DEFAULT_CURRENCY", "UZS"),
        default_language=os.getenv("DEFAULT_LANGUAGE", "ru"),
        reminder_check_seconds=_int("REMINDER_CHECK_SECONDS", 60),
        weekly_report_check_seconds=_int("WEEKLY_REPORT_CHECK_SECONDS", 1800),
        weekly_report_hour=_int("WEEKLY_REPORT_HOUR", 20),
        weekly_report_minute=_int("WEEKLY_REPORT_MINUTE", 0),
    )
