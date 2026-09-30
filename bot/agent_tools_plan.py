"""«Умный помощник» (30.09): план дня, мечети рядом, намаз в другом городе, мозговой штурм.

Регистрируется в общем реестре `agent_tools.TOOLS` (импорт в конце bot/agent_tools.py) — чат, голос (bot_task) и звонки.
Умная модель (Gemini 3.8 Flash) — только для планов, идей и советов; быстрые дела остаются на быстрой.
"""
from __future__ import annotations

import logging
from typing import Any

from . import ai as ai_mod
from . import deeds, plan, prayer, services
from .agent_tools import P, ToolContext, _str, parse_day, tool
from .context import ai

logger = logging.getLogger(__name__)


def _from_voice() -> bool:
    return deeds.source.get() in {"телефон", "звонок"}


async def _to_chat(uid: int, html: str, keyboard=None) -> bool:  # noqa: ANN001
    """Длинное (план, идеи) — отдельным сообщением в чат: своя разметка и кнопки, ответ агента остаётся коротким."""
    from aiogram.types import LinkPreviewOptions

    from .context import bot_instance

    try:
        await bot_instance().send_message(uid, html, reply_markup=keyboard, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
        return True
    except Exception:
        logger.warning("plan: в чат не ушло", exc_info=True)
        return False


@tool(
    "day_plan",
    "ПРИМЕРНЫЙ ПЛАН ДНЯ — мягкий, по окнам между намазами, 3 главных дела, с учётом задач, целей, ежедневных дел, календаря, калорий, "
    "трат, баланса и намаза. «Составь план на день/на завтра», «что мне сегодня делать?», «еду завтра в Самарканд — план». "
    "city — если он будет в другом городе (намаз по времени того города и мечети рядом). Полный план приходит ему в чат; "
    "голосом — скажи одной фразой главное.",
    {"date": P("STRING", "день: YYYY-MM-DD, «сегодня», «завтра»; по умолчанию сегодня"),
     "city": P("STRING", "город или место, если в этот день он не дома («Самарканд», «Москва», «Стамбул»)")},
)
async def _day_plan(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from .context import bot_instance

    profile = ctx.profile
    day = parse_day(a.get("date"), profile.today) or profile.today
    try:
        bot = bot_instance()
    except Exception:
        return {"error": "чат сейчас недоступен"}
    await plan.send(bot, profile, "tomorrow" if day > profile.today else "morning", city=_str(a.get("city")), day=day, force=True)
    st = plan.load(ctx.uid)
    main = [it["text"] for it in st.get("items") or [] if it.get("kind") == "main"][:3]
    return {"ok": True, "sent_to_chat": True, "main": main,
            "note": "план уже отправлен отдельным сообщением в чат (закреплён, пункты с галочками); не повторяй его — назови 2–3 главных "
                    "пункта (голосом — одной-двумя фразами) и что план в чате"}


@tool(
    "edit_plan",
    "ИЗМЕНИТЬ план дня, который уже в чате: «убери прогулку из плана», «добавь в план звонок Алишеру после асра», «перенеси урок на "
    "вечер», «поставь главным оплату интернета». Сообщение с планом обновится само. Не для «сделал» — для этого plan_done.",
    {"instruction": P("STRING", "что поменять в плане — своими словами, со всеми подробностями")},
    ("instruction",),
)
async def _edit_plan(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from .context import bot_instance

    ask = _str(a.get("instruction")) or ""
    if not plan.load(ctx.uid).get("day"):
        return {"error": "плана ещё нет — сначала day_plan"}
    st = await plan.edit(bot_instance(), ctx.profile, ask)
    if st is None:
        return {"error": "не получилось поменять план"}
    return {"ok": True, "items": [("✅ " if it.get("done") else "☐ ") + it["text"] for it in st["items"]],
            "note": "план в чате обновлён; скажи одной фразой, что поменяла"}


@tool(
    "plan_done",
    "ОТМЕТИТЬ пункт плана дня сделанным (или снять отметку): «сделал, позвонил Алишеру», «урок досмотрел», «оплатил интернет». "
    "Отмечает и саму связанную задачу или ежедневное дело. done=false — снять отметку.",
    {"item": P("STRING", "что он сделал — как назвал сам"), "done": P("BOOLEAN", "true — сделано (по умолчанию), false — снять")},
    ("item",),
)
async def _plan_done(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from .context import bot_instance

    res = await plan.mark_done(bot_instance(), ctx.profile, _str(a.get("item")) or "", done=a.get("done") is not False)
    if res is None:
        return {"error": "в плане дня такого пункта нет (это могло быть обычной задачей — тогда update_task/complete_tasks)"}
    return {"ok": True, **res, "note": "отмечено в плане и в задаче; коротко подтверди, сколько пунктов осталось"}


@tool(
    "quran_text",
    "СУРА или аяты Корана с арабским текстом и ТРАНСЛИТЕРАЦИЕЙ латиницей — точно из Корана (сам не пиши текст). «Пришли суру Ихлас», "
    "«аят аль-Курси с транслитерацией», «первые 5 аятов Бакары». Приходит ему в чат отдельным сообщением.",
    {"surah": P("INTEGER", "номер суры 1–114 (Фатиха 1, Бакара 2, аят аль-Курси — 2:255, Ясин 36, Ихлас 112, Фалак 113, Нас 114)"),
     "from_ayah": P("INTEGER", "с какого аята (по умолчанию с первого)"), "to_ayah": P("INTEGER", "по какой аят (по умолчанию до конца суры)")},
    ("surah",),
)
async def _quran_text(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import quran
    from .context import bot_instance

    try:
        n = int(a.get("surah"))
    except (TypeError, ValueError):
        return {"error": "нужен номер суры 1–114"}

    def num(key: str) -> int | None:
        try:
            return int(a.get(key)) if a.get(key) is not None else None
        except (TypeError, ValueError):
            return None

    texts = await quran.messages(n, num("from_ayah"), num("to_ayah"), lang=ctx.profile.lang)
    if not texts:
        return {"error": "не удалось получить текст суры — попробуй позже"}
    bot = bot_instance()
    for t in texts:
        await bot.send_message(ctx.uid, t, parse_mode="HTML")
    return {"ok": True, "sent_to_chat": True, "messages": len(texts), "note": "текст суры с транслитерацией в чате; скажи одной фразой"}


@tool(
    "week_plan",
    "ПЛАНЁРКА НЕДЕЛИ: 3 цели на неделю по дням (из его целей, задач, платежей, итогов прошлой недели). «Поставь цели на неделю», "
    "«планёрка». Приходит в чат; утренний план дня потом опирается на эти цели.",
)
async def _week_plan(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from .context import bot_instance

    ok = await plan.send(bot_instance(), ctx.profile, "week")
    return {"ok": bool(ok), "sent_to_chat": bool(ok), "note": "планёрка недели в чате; назови цели одной-двумя фразами"}


@tool(
    "mosques_near",
    "Мечети РЯДОМ: у него сейчас (телефон присылает место, когда он зовёт JES), в городе place или у дома. Ближайшие с расстоянием "
    "и ссылкой на карту. «Где мечеть рядом?», «мечети в Самарканде», «где мне помолиться?».",
    {"place": P("STRING", "город или адрес; пусто — рядом с ним сейчас")},
)
async def _mosques_near(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    profile = ctx.profile
    place = await plan.place_info(_str(a.get("place")))
    if _str(a.get("place")) and place is None:
        return {"error": f"не нашёл место «{a.get('place')}» на карте — уточни город или адрес"}
    items, name = await plan.mosques_for(profile, place)
    if not items:
        return {"found": 0, "note": "по карте рядом мечетей нет или карта не ответила — попробуй позже"}
    return {"found": len(items), "near": name, "mosques": [{"name": m["name"], "distance_m": m["distance_m"], "map": m["map"]} for m in items[:5]]}


@tool(
    "brainstorm",
    "МОЗГОВОЙ ШТУРМ и советы умной моделью: идеи для бизнеса, подарка, проекта, экономии, учёбы, здоровья, поездки; «дай идеи», "
    "«как мне лучше…», «что посоветуешь?», «придумай». Учитывает его данные (деньги, цели, задачи, память). Возвращает конкретные "
    "идеи с шагами. Не для простых команд и не для вопросов по цифрам.",
    {"topic": P("STRING", "о чём: тема, задача, ограничения (бюджет, сроки) — со всеми подробностями"),
     "count": P("INTEGER", "сколько идей (по умолчанию 5)")},
    ("topic",),
)
async def _brainstorm(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import agent_tools
    from .handlers.agent import render_reply

    profile = ctx.profile
    topic = _str(a.get("topic")) or ""
    if not topic:
        return {"error": "topic required"}
    try:
        count = max(3, min(8, int(a.get("count") or 5)))
    except (TypeError, ValueError):
        count = 5
    persona = await services.persona(ctx.uid)
    try:
        snap = await agent_tools.snapshot(profile)
    except Exception:
        snap = ""
    from . import agent_tools_extra as extra

    try:
        memory = await extra.memory_prompt(ctx.uid)
    except Exception:
        memory = ""
    prompt = (
        f"Ты — JES, умный личный помощник {profile.first_name or 'пользователя'} (Андижан, Узбекистан; валюта — {profile.currency}). "
        f"{plan._persona_rules(profile, persona)}\n\nЗАДАЧА — мозговой штурм: {topic}\n\n"
        f"Дай {count} КОНКРЕТНЫХ идей, а не общие слова. К каждой: суть в одну строку, первые 2–3 шага на этой неделе, "
        "сколько это стоит по деньгам и времени (реалистично для Узбекистана), главный риск. Идеи должны отличаться друг от друга "
        "и учитывать его деньги, цели, задачи и то, что о нём известно. В конце — какую взять первой и почему (1–2 строки). "
        "Не выдумывай его данные. Ищешь свежие цены/факты — скажи, что их стоит проверить.\n"
        "Формат: нумерованный список, без markdown-заголовков; **жирный** для названий идей.\n\n"
        f"ЕГО ДАННЫЕ:\n{snap}\n{memory}")
    try:
        text = await ai.generate([{"text": prompt}], model=ai_mod.smart_model(), temperature=0.9, json_mode=False,
                                 thinking_budget=1024, max_tokens=6000)
    except Exception as exc:
        logger.warning("brainstorm failed", exc_info=True)
        return {"error": f"умная модель не ответила: {type(exc).__name__}"}
    if await _to_chat(ctx.uid, "💡 " + render_reply(text)):
        return {"ok": True, "sent_to_chat": True,
                "note": "идеи уже отправлены отдельным сообщением в чат; не повторяй их — назови 1–2 лучшие и какую взять первой"}
    return {"ok": True, "ideas": text, "note": "чат недоступен — перескажи лучшие идеи коротко"}


@tool(
    "prayer_times_city",
    "Времена намаза в ДРУГОМ городе на день (по времени того города) + мечети рядом: «когда там намаз», «во сколько аср в Самарканде "
    "завтра», «еду в Ташкент — намаз». Для своего города — prayer_times.",
    {"city": P("STRING", "город: «Самарканд», «Москва», «Стамбул»"), "date": P("STRING", "день: YYYY-MM-DD, «сегодня», «завтра»"),
     "mosques": P("BOOLEAN", "добавить мечети рядом (по умолчанию да)")},
    ("city",),
)
async def _prayer_city(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    profile = ctx.profile
    place = await plan.place_info(_str(a.get("city")))
    if place is None:
        return {"error": f"не нашёл город «{a.get('city')}» на карте — уточни название"}
    day = parse_day(a.get("date"), profile.today) or profile.today
    data = await plan.prayers(profile, day, place)
    if not data.get("times"):
        return {"error": "времена намаза сейчас недоступны"}
    out: dict[str, Any] = {"city": place["name"], "date": day.isoformat(), "times": data["times"],
                           "timezone": data.get("tz"), "next": None}
    if day == profile.today and data.get("tz"):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        try:
            now_there = datetime.now(ZoneInfo(str(data["tz"])))
            nxt = prayer.next_prayer(data["times"], now_there, profile.lang)
            out["local_time_there"] = now_there.strftime("%H:%M")
            out["next"] = {"name": nxt[0], "at": nxt[1], "in_minutes": nxt[2]} if nxt else None
        except Exception:
            logger.debug("prayer city: местное время", exc_info=True)
    if a.get("mosques") is not False:
        items, name = await plan.mosques_for(profile, place)
        out["mosques"] = [{"name": m["name"], "distance_m": m["distance_m"], "map": m["map"]} for m in items[:4]]
    return out
