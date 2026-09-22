from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


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
    # «Джарвис»: модель агента (по умолчанию = GEMINI_MODEL; можно gemini-2.5-pro) и бюджет «размышлений»
    # (0 = быстрее, но тупее на неоднозначных фразах; 512 — +1–2 с на сложных).
    agent_model: str
    agent_thinking_budget: int
    app_timezone: str
    default_currency: str
    default_language: str
    weekly_report_check_seconds: int
    weekly_report_hour: int
    weekly_report_minute: int
    # Personal bot: only these Telegram IDs may use it. Empty = everyone.
    allowed_telegram_ids: frozenset[int] = field(default_factory=frozenset)
    # Optional channel (e.g. "@ishdasiz" or "-100123...") for one-tap publishing.
    vacancy_channel: str = ""
    # Optional: URL (или @username) для кнопки-футера в посте вакансии.
    vacancy_footer_url: str = "https://t.me/ishdasiz"

    def is_allowed(self, telegram_id: int | None) -> bool:
        if not self.allowed_telegram_ids:
            return True
        return telegram_id is not None and int(telegram_id) in self.allowed_telegram_ids


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


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "") or ""
    result: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.add(int(chunk))
        except ValueError:
            continue
    return frozenset(result)


def _gemini_model(name: str, default: str = "gemini-2.5-flash") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _candidate_env_files() -> list[Path]:
    """`.env` рядом с пакетом, в корне репо и в cwd — чтобы бот находил
    креды независимо от того, откуда запущен."""
    here = Path(__file__).resolve()
    candidate_dirs = [here.parent.parent, here.parent.parent.parent, Path.cwd()]

    seen: set[Path] = set()
    files: list[Path] = []
    for directory in candidate_dirs:
        env_path = (directory / ".env").resolve()
        if env_path in seen:
            continue
        seen.add(env_path)
        if env_path.exists():
            files.append(env_path)
    return files


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    # Реальные переменные окружения (docker env_file) всегда важнее .env-файлов.
    for env_path in _candidate_env_files():
        load_dotenv(dotenv_path=env_path, override=False)
        for key, value in dotenv_values(env_path, encoding="utf-8-sig").items():
            if not key or value is None:
                continue
            os.environ.setdefault(key, value)
    load_dotenv(override=False)
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        supabase_url=_required("SUPABASE_URL"),
        supabase_service_role_key=_required("SUPABASE_SERVICE_ROLE_KEY"),
        db_table_prefix=os.getenv("DB_TABLE_PREFIX", "").strip(),
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_model=_gemini_model("GEMINI_MODEL"),
        gemini_vision_model=_gemini_model("GEMINI_VISION_MODEL"),
        gemini_transcribe_model=_gemini_model("GEMINI_TRANSCRIBE_MODEL"),
        agent_model=_gemini_model("AGENT_MODEL", _gemini_model("GEMINI_MODEL")),
        agent_thinking_budget=_int("AGENT_THINKING_BUDGET", 512),
        app_timezone=os.getenv("APP_TIMEZONE", "Asia/Tashkent"),
        default_currency=os.getenv("DEFAULT_CURRENCY", "UZS"),
        default_language=os.getenv("DEFAULT_LANGUAGE", "ru"),
        weekly_report_check_seconds=_int("WEEKLY_REPORT_CHECK_SECONDS", 1800),
        weekly_report_hour=_int("WEEKLY_REPORT_HOUR", 20),
        weekly_report_minute=_int("WEEKLY_REPORT_MINUTE", 0),
        allowed_telegram_ids=_ids("ALLOWED_TELEGRAM_IDS"),
        vacancy_channel=os.getenv("VACANCY_CHANNEL", "").strip(),
        vacancy_footer_url=os.getenv("VACANCY_FOOTER_URL", "https://t.me/ishdasiz").strip() or "https://t.me/ishdasiz",
    )
