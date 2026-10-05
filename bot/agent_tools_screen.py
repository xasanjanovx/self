"""Инструменты экранного времени (04.10.2026, bot/screentime.py): «я работаю до 18:00» — JES не напоминает про телефон;
«сколько я сегодня в телефоне?» — по данным с телефона. Голос (bot_task на телефоне и в звонке) пользуется ими же."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from . import screentime
from .agent_tools import P, ToolContext, _str, tool


def _minutes_until(ctx: ToolContext, until: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", until or "")
    if not m:
        return None
    now = ctx.profile.now
    at = now.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)), second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return max(1, int(-(-(at - now).total_seconds() // 60)))   # вверх: «до 18:00» — не 17:59


@tool("screen_busy",
      "«Я работаю / я занят» — JES не напоминает про телефон и экранное время до этого времени (его работа @ishdasiz тоже в Telegram). "
      "«я работаю до 18:00» → until; «на 2 часа» → minutes; «я освободился», «можно напоминать» → off=true. Без времени — 2 часа.",
      {"until": P("STRING", "до какого времени, ЧЧ:ММ"), "minutes": P("INTEGER", "на сколько минут"),
       "off": P("BOOLEAN", "снять «занят»")})
async def _screen_busy(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if a.get("off"):
        screentime.set_busy(ctx.uid, None)
        return {"ok": True, "busy": False}
    minutes = _minutes_until(ctx, _str(a.get("until")) or "") if a.get("until") else None
    if minutes is None:
        try:
            minutes = int(a.get("minutes") or screentime.DEFAULT_BUSY_MIN)
        except (TypeError, ValueError):
            minutes = screentime.DEFAULT_BUSY_MIN
    minutes = max(5, min(24 * 60, minutes))
    until = screentime.set_busy(ctx.uid, minutes)
    ctx.mutated = True
    return {"ok": True, "busy_until": until.astimezone(ctx.profile.tz).strftime("%H:%M") if until else None}


@tool("screen_time", "Сколько он сегодня в телефоне (с приложения JES): всего, по группам (соцсети и видео, Telegram, браузер) и топ "
      "приложений, лимиты на день. «сколько я сегодня сидел в телефоне?», «сколько в инстаграме?»", {})
async def _screen_time(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    st = screentime.load(ctx.uid)
    today = (st.get("days") or {}).get(ctx.profile.today.isoformat())
    if not today:
        return {"error": "с телефона за сегодня ещё нет данных (приложение JES присылает их раз в час, когда гаснет экран)"}
    excluded = set(st.get("excluded") or [])
    by = screentime._by_cat(today.get("apps") or [], excluded)
    top = sorted((x for x in today.get("apps") or [] if screentime.category(str(x.get("pkg"))) != "work"),
                 key=lambda x: -float(x.get("min") or 0))[:5]
    return {"minutes": {k: int(round(v)) for k, v in by.items()}, "limits": screentime.limits(st), "pickups": today.get("pickups"),
            "top": [{"app": screentime.pretty(str(x.get("pkg")), x.get("label")), "min": int(round(float(x.get("min") or 0)))} for x in top],
            "updated": today.get("at")}


__all__: list[str] = []
