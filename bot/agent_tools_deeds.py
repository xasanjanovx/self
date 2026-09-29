"""«Что я делал вчера?», «когда я последний раз звонил Алишеру?» (29.09): история дел из bot/deeds.py.

Регистрируется в общем реестре `agent_tools.TOOLS` (импорт в конце bot/agent_tools.py) — и в чате, и голосом через bot_task.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import deeds
from .agent_tools import ARR, P, ToolContext, _str, parse_day, tool


@tool(
    "recall_deeds",
    "Его история — что JES делала для него и что происходило: звонки (кому, кто звонил ему), сообщения, записи трат и еды, "
    "напоминания, будильники, поиск, такси, видео и уроки, подъёмы. «Что я делал вчера?» — day; «когда я последний раз звонил "
    "Алишеру?» — query «Алишер» (только имя или ключевое слово, без глаголов) и variants (другие написания: мама → Onajonim, "
    "oyi); «что я просил утром?» — day сегодня. Отвечай по найденному коротко: дата и время, что было.",
    {"query": P("STRING", "имя или ключевое слово (Алишер, такси, урок); пусто — всё за период"),
     "variants": ARR({"type": "STRING"}, "другие написания того же (латиницей, родственные слова)"),
     "day": P("STRING", "день: YYYY-MM-DD, «сегодня», «вчера»; пусто — последние days дней"),
     "days": P("INTEGER", "сколько последних дней смотреть (по умолчанию 30 для поиска по имени, 7 без него)")},
)
async def _recall(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    today: date = ctx.profile.now.date()
    query = _str(a.get("query")) or ""
    variants = [str(v) for v in a.get("variants") or [] if str(v).strip()]
    day_text = _str(a.get("day"))
    if day_text:
        day = parse_day(day_text, today)
        if day is None:
            return {"error": f"не понял дату «{day_text}»"}
        start = end = day
    else:
        try:
            days = int(a.get("days") or (30 if (query or variants) else 7))
        except (TypeError, ValueError):
            days = 7
        start, end = today - timedelta(days=max(1, min(120, days)) - 1), today
    found = deeds.search(ctx.profile.telegram_id, query, variants=variants, start=start, end=end, limit=40)
    return {"period": f"{start:%d.%m}–{end:%d.%m}" if start != end else f"{start:%d.%m}", "found": len(found),
            "items": [deeds.line(r) for r in found],
            "note": "" if found else "в истории JES такого нет (она помнит только то, что делала сама и что видела на телефоне с 29.09)"}
