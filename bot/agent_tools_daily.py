"""Инструменты «каждый день» и «продолжи урок» (29.09): bot/daily_tasks.py и bot/lessons.py.

Регистрируются в общем реестре `agent_tools.TOOLS` (модуль импортируется в конце bot/agent_tools.py) — ими пользуется и чат,
и голос (bot_task на телефоне и в звонке).
"""
from __future__ import annotations

from typing import Any

from . import daily_tasks, lessons
from .agent_tools import ARR, P, ToolContext, _num, _str, tool


@tool(
    "add_daily",
    "Дело КАЖДЫЙ ДЕНЬ (или по дням недели) с напоминанием: «каждый день в 20:00 урок английского», «по будням в 7:00 зарядка», "
    "«каждый вечер читать 10 страниц». Есть ссылка на плейлист/ролик YouTube — link (кнопка в напоминании откроет урок с того "
    "места, где он остановился). Есть цель в шагах («курс из 40 уроков», «прочитать 30 глав») — total: будет прогресс в % и "
    "прогноз окончания. Не путай с разовой задачей (add_task) и напоминанием (add_reminder).",
    {"title": P("STRING", "что делать"), "time": P("STRING", "ЧЧ:ММ, когда напомнить (необязательно)"),
     "days": ARR({"type": "INTEGER"}, "дни недели 1=пн … 7=вс; нет — каждый день"),
     "link": P("STRING", "ссылка YouTube (плейлист или ролик), если есть"), "total": P("INTEGER", "сколько всего шагов в цели (необязательно)")},
    ("title",),
)
async def _add_daily(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    days = [int(d) for d in a.get("days") or [] if str(d).isdigit()]
    total = _num(a.get("total"))
    try:
        item = daily_tasks.add(ctx.profile.telegram_id, _str(a.get("title")) or "", at=_str(a.get("time")), days=days or None,
                          link=_str(a.get("link")) or "", total=int(total) if total else None)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, **daily_tasks.describe(item, ctx.profile.now.date())}


@tool("list_daily", "Его ежедневные дела: что сегодня, что сделано, серии дней и прогресс целей («что у меня каждый день?», «как мой курс?»).")
async def _list_daily(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    today = ctx.profile.now.date()
    return {"daily": [daily_tasks.describe(h, today) for h in daily_tasks.all_items(ctx.profile.telegram_id)]}


@tool(
    "daily_done",
    "Отметить ежедневное дело сделанным сегодня («посмотрел урок», «сделал зарядку», «прочитал 2 главы» — steps=2).",
    {"title": P("STRING", "какое дело (как он назвал)"), "steps": P("INTEGER", "сколько шагов цели сделал, по умолчанию 1")},
    ("title",),
)
async def _daily_done(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    uid = ctx.profile.telegram_id
    item = daily_tasks.find(uid, _str(a.get("title")))
    if item is None:
        return {"error": "такого ежедневного дела нет", "daily": [h["title"] for h in daily_tasks.all_items(uid)]}
    steps = _num(a.get("steps"))
    item = daily_tasks.done(uid, item, ctx.profile.now.date(), int(steps) if steps else 1)
    return {"ok": True, **daily_tasks.describe(item, ctx.profile.now.date())}


@tool("delete_daily", "Убрать ежедневное дело («больше не напоминай про зарядку»).", {"title": P("STRING", "какое дело")}, ("title",))
async def _delete_daily(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    item = daily_tasks.find(ctx.profile.telegram_id, _str(a.get("title")))
    if item is None:
        return {"error": "такого ежедневного дела нет"}
    daily_tasks.remove(ctx.profile.telegram_id, item)
    return {"ok": True, "deleted": item["title"]}


@tool(
    "video_resume_link",
    "Продолжить YouTube с места, где он остановился («продолжи урок», «где я остановился в уроках английского?»): вернёт ссылку "
    "с нужной секундой — дай её ему. Урок досмотрен и у ежедневного дела есть плейлист — следующий урок.",
    {"query": P("STRING", "что именно (слова из названия, канал, дело) — необязательно")},
)
async def _resume_video(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    uid = ctx.profile.telegram_id
    query = _str(a.get("query")) or ""
    habit = daily_tasks.find(uid, query) if query else None
    res = await lessons.resume(uid, query if habit is None else "", link=(habit or {}).get("link") or "")
    if res.get("error"):
        return res
    return {"ok": True, "title": res["title"], "from": res["position"], "next_lesson": res.get("next"), "url": lessons.url(res)}


# ------------------------------------------------------------------ напоминания по месту (bot/geo.py)
@tool(
    "place_reminder",
    "Напомнить, когда он ПРИДЁТ в место или УЙДЁТ из него: «когда приду домой — напомни про хлеб», «когда уйду из офиса — "
    "позвонить маме». Места: дом, работа, офис или любое сохранённое. Место не сохранено — скажи, как сохранить: «Джес, запомни, "
    "здесь мой дом» (когда он там) или адресом в чате (save_place).",
    {"place": P("STRING", "где: дом / работа / офис / название места"), "text": P("STRING", "о чём напомнить"),
     "when": P("STRING", "arrive — когда придёт (по умолчанию) | leave — когда уйдёт", enum=["arrive", "leave"])},
    ("place", "text"),
)
async def _place_reminder(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import geo

    res = geo.add_reminder(ctx.profile.telegram_id, _str(a.get("place")) or "", _str(a.get("text")) or "",
                           "leave" if _str(a.get("when")) == "leave" else "arrive")
    if res.get("error"):
        return {**res, "how": "когда будет там: «Джес, запомни, здесь мой " + str(res.get("need_place")) + "», или адрес в чате"}
    return {"ok": True, "place": res["place"], "when": "придёте" if res["when"] == "arrive" else "уйдёте", "text": res["text"],
            "note": "телефон поставит напоминание при следующей связи (обычно сразу после разговора или при включении экрана)"}


@tool(
    "save_place",
    "Сохранить место по адресу (для напоминаний по месту): «мой офис — улица Навои 15», «дом — Андижан, Бобур шох 3». "
    "Если он сейчас там — лучше голосом: «Джес, запомни, здесь мой офис».",
    {"name": P("STRING", "как назвать: дом / работа / офис / …"), "address": P("STRING", "адрес")},
    ("name", "address"),
)
async def _save_place(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import geo, media

    found = await media.geocode(_str(a.get("address")) or "")
    if not found:
        return {"error": "адрес не нашёлся на карте — уточните или сохраните голосом на месте"}
    p = geo.save_place(ctx.profile.telegram_id, _str(a.get("name")) or "", found["lat"], found["lon"], label=str(found.get("name") or ""))
    return {"ok": True, "place": p["name"], "found": p["label"]}


@tool("list_place_reminders", "Его напоминания по месту и сохранённые места («что мне напомнить дома?»).")
async def _list_place_reminders(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import geo

    uid = ctx.profile.telegram_id
    return {"places": sorted(geo.places(uid)), "reminders": [{"id": r["id"], "place": r["place"], "text": r["text"],
                                                               "when": "придёт" if r["when"] == "arrive" else "уйдёт"} for r in geo.reminders(uid)]}


@tool("delete_place_reminder", "Убрать напоминание по месту.", {"id": P("STRING", "id из list_place_reminders")}, ("id",))
async def _delete_place_reminder(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import geo

    hit = geo.remove_reminder(ctx.profile.telegram_id, _str(a.get("id")) or "")
    return {"ok": True, "deleted": hit["text"]} if hit else {"error": "нет такого напоминания"}
