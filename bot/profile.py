"""Профиль пользователя (язык, часовой пояс, валюта) — общий тип для сервисов и хендлеров."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .context import settings
from .db import zone


@dataclass
class Profile:
    telegram_id: int
    lang: str
    tz_name: str
    currency: str
    first_name: str
    username: str

    @property
    def tz(self) -> ZoneInfo:
        return zone(self.tz_name, settings.app_timezone)  # type: ignore[return-value]

    @property
    def today(self) -> date:
        return datetime.now(self.tz).date()

    @property
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def tr(self, ru: str, uz: str) -> str:
        return uz if self.lang == "uz" else ru


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


__all__ = ["Profile", "h"]
