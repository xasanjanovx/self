"""Утренняя и вечерняя сводки: тонкие обёртки над bot/digest.py (07.10: только то, что требует внимания, иначе — молчим)."""
from __future__ import annotations

import logging
from typing import Any

from . import digest
from .profile import Profile

logger = logging.getLogger(__name__)


async def morning_brief(profile: Profile) -> str | None:
    """Утро: деньги одной строкой + важное на сегодня (долги, платежи, лимиты, дела, календарь). Нечего сказать — None."""
    return await digest.morning(profile)


async def evening_brief(profile: Profile) -> str | None:
    """Вечер: только то, что требует внимания (срок долга, лимит, дела не закрыты, цель отстаёт, не записано, необычное). Иначе — None."""
    return await digest.evening(profile)


def parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(value or "").split(":")[:2]
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return default


__all__: list[Any] = ["morning_brief", "evening_brief", "parse_hhmm"]
