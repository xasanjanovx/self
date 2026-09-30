"""План дня и разбор вечером (30.09, его просьба): «примерный дневной план, не такой жёсткий, чтобы день был эффективным и с
баракой — учитывай всё: калории, траты, баланс, намаз; едет в другой город — время намаза там и мечети рядом».

Утром (после подъёма, вместе со сводкой) — мягкий план: опора — окна между намазами, а не минутки; 3 главных дела, остальное
«если успеете»; еда, деньги, уроки. Вечером — короткий разбор: что сделано, что перенести, идея на завтра.
Цифры и списки собирает код (bot/agent_tools.snapshot + намаз + привычки + уроки + место), умная модель (Gemini 3.8 Flash)
только пишет живым языком; не ответила — план собирается кодом, чтобы день не остался без плана.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

from . import ai as ai_mod
from . import daily_tasks, deeds, lessons, prayer, services, where
from . import finance as fin
from . import nutrition as nutri
from .context import ai, db
from .persona import honorific_rule
from .profile import Profile, h

logger = logging.getLogger(__name__)

_WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_WEEKDAYS_UZ = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]
_LANG_NAME = {"ru": "русском", "uz": "узбекском (латиницей, живой литературный)", "en": "английском"}


def weekday(profile: Profile, day: date) -> str:
    return (_WEEKDAYS_UZ if profile.lang == "uz" else _WEEKDAYS_RU)[day.weekday()]


# ------------------------------------------------------------------ место и намаз (в том числе в другом городе)
async def place_info(city: str | None) -> dict[str, Any] | None:
    """«Самарканд», «Москва», «Стамбул» → {"name", "lat", "lon"} (OpenStreetMap, весь мир); пусто/не нашли — None."""
    from . import media

    city = " ".join(str(city or "").split())
    if not city:
        return None
    found = await media.geocode(city, world=True)
    return {"name": found["name"], "lat": found["lat"], "lon": found["lon"]} if found else None


async def prayers(profile: Profile, day: date, place: dict[str, Any] | None = None) -> dict[str, Any]:
    """Времена намаза на день: дома (настройки подъёма) или в другом городе — по его координатам, время местное."""
    from . import wake_runner

    s, _ = await wake_runner.plan_for(profile, day)
    lat, lon = (place["lat"], place["lon"]) if place else (s.latitude, s.longitude)
    rows = await prayer.timings(day, latitude=lat, longitude=lon, method=s.calc_method)
    out: dict[str, Any] = {"times": rows, "lat": lat, "lon": lon, "place": place["name"] if place else None}
    if place:
        out["tz"] = await prayer.timezone_at(lat, lon)
    return out


def prayer_line(profile: Profile, data: dict[str, Any]) -> str:
    names = prayer.NAMES_UZ if profile.lang == "uz" else prayer.NAMES_RU
    rows = data.get("times") or {}
    if not rows:
        return ""
    line = " · ".join(f"{names[k]} {rows[k]}" for k in prayer.ORDER if k in rows)
    where_txt = data.get("place") or profile.tr("Андижан", "Andijon")
    tz = f", местное время {data['tz']}" if data.get("tz") else ""
    return f"Намаз ({where_txt}{tz}): {line}"


def mosques_block(profile: Profile, items: list[dict[str, Any]], *, place: str | None = None) -> str:
    """Готовый блок «🕌 Мечети рядом» (HTML) — строит код, а не модель: расстояния и ссылки на карту точные."""
    if not items:
        return ""

    def km(m: int) -> str:
        return f"{m} м" if m < 1000 else f"{m / 1000:.1f} км"

    rows = [f"• <a href=\"{m['map']}\">{h(m['name'])}</a> — {km(m['distance_m'])}" for m in items[:4]]
    head = profile.tr("🕌 Мечети рядом", "🕌 Yaqin masjidlar") + (f" ({h(place)})" if place else "")
    return f"{head}:\n" + "\n".join(rows)


async def mosques_for(profile: Profile, place: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Мечети у места; места нет — рядом с ним сейчас (телефон присылает при вызове JES), иначе у дома."""
    if place:
        return await prayer.mosques_near(place["lat"], place["lon"]), place["name"]
    here = where.describe(profile.telegram_id)
    if here.get("known") and here.get("fresh"):
        return await prayer.mosques_near(float(here["lat"]), float(here["lon"])), here.get("place") or here.get("address")
    from . import wake_runner

    s, _ = await wake_runner.plan_for(profile)
    return await prayer.mosques_near(s.latitude, s.longitude), None


# ------------------------------------------------------------------ данные дня
async def gather(profile: Profile, *, day: date, place: dict[str, Any] | None = None) -> dict[str, Any]:
    """Всё о дне одним куском: {"text": для модели, "pray": намаз, "open": задачи на день, "habits": ежедневные дела}."""
    from . import agent_tools

    uid = profile.telegram_id
    today = profile.today
    parts: list[str] = [f"Сегодня {weekday(profile, today)}, {today:%d.%m.%Y}, сейчас {profile.now:%H:%M}. "
                        f"План на: {weekday(profile, day)} {day:%d.%m}" + (" (сегодня)" if day == today else " (завтра)" if day == today + timedelta(days=1) else "")]
    out: dict[str, Any] = {"day": day, "pray": {}, "open": [], "habits": []}
    try:
        out["pray"] = await prayers(profile, day, place)
        if line := prayer_line(profile, out["pray"]):
            parts.append(line)
    except Exception:
        logger.warning("plan: намаз не получил", exc_info=True)
    if place:
        parts.append(f"ПОЕЗДКА: в этот день он в городе «{place['name']}» — намаз по времени этого города; мечети рядом ниже "
                     "добавит код (в тексте их не перечисляй, только скажи, к какому намазу удобно зайти).")
    try:
        parts.append(await agent_tools.snapshot(profile))
    except Exception:
        logger.warning("plan: срез данных не получил", exc_info=True)
    try:
        rows = await services.tasks(uid)
        due = [r for r in rows if (not r.get("due_date")) or str(r.get("due_date"))[:10] <= day.isoformat()]
        out["open"] = due
    except Exception:
        logger.debug("plan: задачи", exc_info=True)
    habits = daily_tasks.all_items(uid)
    if habits:
        out["habits"] = [daily_tasks.describe(x, day) for x in habits if daily_tasks.is_today(x, day)]
        if out["habits"]:
            parts.append("Каждый день: " + "; ".join(
                f"{x['title']}" + (f" в {x['time']}" if x.get("time") else "") + (f" ({x['progress']})" if x.get("progress") else "")
                for x in out["habits"]))
    watching = [r for r in lessons.items(uid) if int(r.get("position") or 0) >= 60 and int(r.get("duration") or 0) >= 8 * 60
                and not lessons.finished(r) and time.time() - float(r.get("at") or 0) < 10 * 86400][:2]
    if watching:
        parts.append("Уроки в процессе: " + "; ".join(f"{r['title'][:60]} (остановился на {lessons.fmt(int(r.get('position') or 0))})" for r in watching))
    if not place and (loc := where.now_line(uid).strip()):
        parts.append(loc)
    out["text"] = "\n".join(p for p in parts if p)
    return out


def _persona_rules(profile: Profile, persona) -> str:  # noqa: ANN001
    return (f"Язык ответа — {_LANG_NAME.get(persona.lang, 'русском')}, на «вы». Голос женский — о себе в женском роде. "
            f"{honorific_rule(persona)} Слово «босс» не используй никогда.")


PLAN_PROMPT = (
    "Ты — JES, личный помощник {name}. Составь ПРИМЕРНЫЙ ПЛАН ДНЯ — мягкий и реалистичный, чтобы день был эффективным и с баракой "
    "(духом, а не давлением). {rules}\n\n"
    "КАК СОСТАВЛЯТЬ:\n"
    "• Опора дня — намазы: строй день по окнам между ними («после бомдода до восхода», «до пешина», «между пешином и асром», "
    "«после асра», «после шома»). Время в минутах указывай ТОЛЬКО у намаза, календарных встреч и задач с точным временем. "
    "Остальное — «в это окно», без «10 минут на…».\n"
    "• «Главное сегодня» — не больше 3 дел: срочные и просроченные задачи, шаг к целям, ежедневные дела. Затем «Если успеете» — 2–3 "
    "необязательных дела. Между делами оставляй воздух; день не должен быть забит.\n"
    "• Реши по данным: сколько осталось калорий и что съесть (идея на приём пищи, простые продукты Узбекистана), можно ли сегодня "
    "тратить свободно или лучше экономить (баланс, лимиты, ближайшие платежи и долги), не забыт ли урок в процессе или цель.\n"
    "• Если день уже идёт — планируй от текущего времени, прошедшее не трогай. Завтрашний план — на весь день.\n"
    "• Учитывай календарь и то, где он сейчас. Ничего не выдумывай: нет данных — не пиши про это.\n"
    "• В конце — ОДНА тёплая фраза-настрой (без нравоучений, «пусть день будет с баракой» своими словами).\n\n"
    "ФОРМАТ: коротко (до 1500 знаков), без markdown-заголовков и таблиц; можно **жирный** для названий окон и эмодзи. Пункты — «•».\n\n"
    "ДАННЫЕ:\n{data}"
)

REVIEW_PROMPT = (
    "Ты — JES, личный помощник {name}. Сейчас вечер: сделай КОРОТКИЙ РАЗБОР ДНЯ. {rules}\n\n"
    "Как: 1) что сегодня получилось — конкретно, с цифрами из данных (задачи, ежедневные дела, уроки, еда против плана, траты); "
    "хвали за реальное, без лести. 2) Что не сделано и стоит перенести — по существу, без упрёков; если день был тяжёлым — "
    "поддержи. 3) ОДНА идея на завтра (что поставить первым делом). 4) Одна тёплая фраза на ночь (перед сном: намаз хуфтон, отдых).\n"
    "Не выдумывай ничего, чего нет в данных. До 900 знаков, без markdown-заголовков; **жирный** и эмодзи можно.\n\n"
    "ДАННЫЕ:\n{data}"
)


async def _ask_model(prompt: str, *, max_tokens: int = 1600) -> str | None:
    try:
        return (await ai.generate([{"text": prompt}], model=ai_mod.smart_model(), temperature=0.6, json_mode=False,
                                  thinking_budget=1024, max_tokens=max_tokens)).strip() or None
    except Exception:
        logger.warning("plan: умная модель не ответила", exc_info=True)
        return None


def fallback_plan(profile: Profile, data: dict[str, Any]) -> str:
    """Модель недоступна — план без неё: намаз, дела на день, ежедневные дела."""
    lines = []
    if line := prayer_line(profile, data.get("pray") or {}):
        lines.append(line)
    tasks = data.get("open") or []
    if tasks:
        lines.append(profile.tr("Дела на день:", "Kun ishlari:") + " " + "; ".join(
            str(r.get("text") or "")[:50] + (f" ({r['due_time']})" if r.get("due_time") else "") for r in tasks[:6]))
    if data.get("habits"):
        lines.append(profile.tr("Каждый день:", "Har kuni:") + " " + "; ".join(x["title"] for x in data["habits"][:5]))
    return "\n".join(lines) or profile.tr("На сегодня дел пока нет — скажите, что хотите успеть, и я составлю план.",
                                         "Bugun ishlar yo'q — nima qilmoqchi ekaningizni ayting.")


async def build_plan(profile: Profile, persona, *, day: date | None = None, city: str | None = None) -> str:  # noqa: ANN001
    """HTML для Telegram: заголовок + план (+ мечети рядом, если поездка)."""
    from .handlers.agent import render_reply

    day = day or profile.today
    place = await place_info(city) if city else None
    if city and place is None:
        place_note = profile.tr(f"Город «{city}» на карте не нашёл — план по вашему городу.", f"«{city}» topilmadi — o'z shahringiz bo'yicha.")
    else:
        place_note = ""
    data = await gather(profile, day=day, place=place)
    prompt = PLAN_PROMPT.format(name=profile.first_name or "пользователя", rules=_persona_rules(profile, persona), data=data["text"])
    body = await _ask_model(prompt) or fallback_plan(profile, data)
    trip = f" · {h(place['name'])}" if place else ""
    head = f"🗓 <b>{profile.tr('План', 'Reja')}: {weekday(profile, day)}, {day:%d.%m}</b>{trip}"
    text = head + "\n\n" + render_reply(body)
    if place:
        items, name = await mosques_for(profile, place)
        if block := mosques_block(profile, items, place=name):
            text += "\n\n" + block
    if place_note:
        text += f"\n\n<i>{place_note}</i>"
    return text


async def build_review(profile: Profile, persona) -> str:  # noqa: ANN001
    """Вечерний разбор дня (HTML)."""
    from .handlers.agent import render_reply

    uid, today = profile.telegram_id, profile.today
    lines: list[str] = [f"Сегодня {weekday(profile, today)}, {today:%d.%m.%Y}, сейчас {profile.now:%H:%M}."]
    try:
        rows = await db.list_tasks(uid, include_done=True) if db.available("tasks") else []
        done = [r for r in rows if r.get("done") and str(r.get("done_at") or "")[:10] == today.isoformat()]
        left = [r for r in rows if not r.get("done") and (r.get("due_date") and str(r["due_date"])[:10] <= today.isoformat())]
        lines.append(f"Задачи: сделано {len(done)}" + (f" ({'; '.join(str(r.get('text'))[:40] for r in done[:5])})" if done else "")
                     + f"; на сегодня и просрочено осталось {len(left)}" + (f" ({'; '.join(str(r.get('text'))[:40] for r in left[:5])})" if left else ""))
    except Exception:
        logger.debug("review: задачи", exc_info=True)
    habits = [daily_tasks.describe(x, today) for x in daily_tasks.all_items(uid) if daily_tasks.is_today(x, today)]
    if habits:
        lines.append("Каждый день: " + "; ".join(f"{x['title']} — {'сделано' if x['done_today'] else 'не сделано'}"
                                                   + (f" ({x['progress']})" if x.get("progress") else "") for x in habits))
    try:
        snap = await services.finance_snapshot(profile)
        plan = await services.nutrition_profile(uid)
        tot = nutri.totals(await services.today_calorie_logs(profile))
        lines.append(f"Траты сегодня: {fin.fmt_money(snap.today_expense)}, доход {fin.fmt_money(snap.today_income)}; баланс: карта "
                     f"{fin.fmt_money(snap.balances['card'])}, наличные {fin.fmt_money(snap.balances['cash'])}.")
        lines.append(f"Съедено {int(tot['calories'])} ккал" + (f" из {plan.get('daily_calories')}" if plan and plan.get("daily_calories") else "")
                     + f" (Б{int(tot['protein'])}/Ж{int(tot['fat'])}/У{int(tot['carbs'])}).")
    except Exception:
        logger.debug("review: деньги и еда", exc_info=True)
    watched = [r for r in lessons.items(uid) if time.strftime("%Y-%m-%d", time.localtime(float(r.get("at") or 0))) == today.isoformat()
               and int(r.get("position") or 0) >= 60 and int(r.get("duration") or 0) >= 8 * 60]
    if watched:
        lines.append("Уроки/длинные видео: " + "; ".join(f"{r['title'][:50]} ({lessons.fmt(int(r['position']))})" for r in watched[:3]))
    todays = deeds.rows(uid, start=today, end=today)
    if todays:
        lines.append("Что делала JES сегодня: " + "; ".join(str(r.get("text"))[:50] for r in todays[-8:]))
    data = "\n".join(lines)
    body = await _ask_model(REVIEW_PROMPT.format(name=profile.first_name or "пользователя", rules=_persona_rules(profile, persona), data=data),
                            max_tokens=1000)
    return f"🌙 <b>{profile.tr('Разбор дня', 'Kun tahlili')}</b>\n\n" + render_reply(body or data)


# ------------------------------------------------------------------ перенос и отправка
async def move_open_to_tomorrow(profile: Profile) -> int:
    """Невыполненные задачи на сегодня и просроченные → на завтра (время задачи сохраняется)."""
    if not db.available("tasks"):
        return 0
    uid, tomorrow = profile.telegram_id, (profile.today + timedelta(days=1)).isoformat()
    moved = 0
    for r in await db.list_tasks(uid):
        if r.get("due_date") and str(r["due_date"])[:10] <= profile.today.isoformat():
            await db.update_task(uid, r["id"], {"due_date": tomorrow, "notified_key": None})
            moved += 1
    services.invalidate(uid, "tasks")
    return moved


def keyboard(profile: Profile, kind: str):  # noqa: ANN201
    from aiogram.types import InlineKeyboardMarkup

    from .keyboards import _btn

    if kind == "review":
        rows = [[_btn("➡️ " + profile.tr("Перенести на завтра", "Ertaga ga ko'chirish"), "plan:move")],
                [_btn("🗓 " + profile.tr("План на завтра", "Ertangi reja"), "plan:tomorrow")]]
    else:
        rows = [[_btn("🔄 " + profile.tr("Пересоставить", "Qayta tuzish"), "plan:today")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send(bot, profile: Profile, kind: str = "morning", *, city: str | None = None, day: date | None = None) -> bool:  # noqa: ANN001
    """kind: morning | tomorrow | review. True — отправлено."""
    persona = await services.persona(profile.telegram_id)
    if kind == "review":
        text = await build_review(profile, persona)
    else:
        text = await build_plan(profile, persona, day=day or (profile.today + timedelta(days=1) if kind == "tomorrow" else None), city=city)
    from aiogram.types import LinkPreviewOptions

    await bot.send_message(profile.telegram_id, text, reply_markup=keyboard(profile, "review" if kind == "review" else "plan"),
                           link_preview_options=LinkPreviewOptions(is_disabled=True))
    logger.info("plan: %s отправлен %s", kind, profile.telegram_id)
    return True


__all__ = ["build_plan", "build_review", "send", "place_info", "prayers", "mosques_for", "mosques_block", "move_open_to_tomorrow", "gather"]
