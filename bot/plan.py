"""План дня и разбор вечером (30.09, его просьба): мягкий план по окнам между намазами, красиво — цитатами; пункты с галочками,
которые он отмечает; менять план можно ответом на него (текстом или голосом); план закреплён в чате, пока не придёт следующий.

Как устроено:
  • данные собирает код (bot/agent_tools.snapshot + намаз + привычки + уроки + место + цели недели);
  • умная модель (Gemini 3.8 Flash) возвращает ЧИСТУЮ структуру (JSON): пункты с окном и ссылкой на его задачу/цель/ежедневное дело,
    еда, деньги, настрой, суры для заучивания — ничего не оформляет;
  • оформляет код (`render`): заголовок, окна с точным временем намаза, цитаты <blockquote>, ☐/✅, прогресс;
  • состояние плана — в DATA_DIR/plan_<uid>.json: по нему работают кнопки-галочки, правка ответом, «сделал X» и разбор вечером;
  • новые «Главное» без задачи становятся задачами сами (его выбор), ✅ в плане отмечает и саму задачу;
  • модель не ответила — план собирается кодом, чтобы день не остался без плана.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
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
_MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_MONTHS_UZ = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]
_LANG_NAME = {"ru": "русском", "uz": "узбекском (латиницей, живой литературный)", "en": "английском"}
WINDOW_KEYS = ("fajr", "morning", "dhuhr", "asr", "maghrib", "isha")
# 02.10 его решение: план КОРОТКИЙ — до 5 дел списком, без окон между намазами, цитат, сур, мечетей, еды/денег/настроя и без закрепа;
# сообщение исчезает при следующем действии в боте, как остальное (а не висит закреплённым). Вечернего разбора и воскресной планёрки нет.
MAX_ITEMS = 5
MAX_MAIN = 2
PLAN_TTL_S = 18 * 3600   # не открывал бота — уберётся само к вечеру
HTML_LIMIT = 3900
REPLY_HINT_RU = "Нажмите пункт, когда сделаете. Ответьте — поменяю."
REPLY_HINT_UZ = "Bajarganingizda punktni bosing. Javob yozing — o'zgartiraman."


def weekday(profile: Profile, day: date) -> str:
    return (_WEEKDAYS_UZ if profile.lang == "uz" else _WEEKDAYS_RU)[day.weekday()]


def date_text(profile: Profile, day: date) -> str:
    return f"{weekday(profile, day)}, {day.day} {(_MONTHS_UZ if profile.lang == 'uz' else _MONTHS_RU)[day.month - 1]}"


# ------------------------------------------------------------------ состояние
def _sfile(uid: int, name: str = "plan") -> Path | None:
    folder = os.getenv("DATA_DIR")
    return Path(folder) / f"{name}_{int(uid)}.json" if folder else None


def load(uid: int, name: str = "plan") -> dict[str, Any]:
    path = _sfile(uid, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(uid: int, data: dict[str, Any], name: str = "plan") -> None:
    path = _sfile(uid, name)
    if path is None:
        return
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("plan: состояние не сохранил", exc_info=True)


def is_plan_message(uid: int, message_id: int) -> bool:
    st = load(uid)
    return bool(st.get("msg_id")) and int(st["msg_id"]) == int(message_id)


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


def windows(profile: Profile, times: dict[str, str]) -> list[dict[str, str]]:
    """Окна дня между намазами с точным временем: [{"key", "emoji", "title", "range"}]. Нет времён — пусто."""
    if not times or not all(k in times for k in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha")):
        return []
    uz = profile.lang == "uz"
    t = times
    spec = [("fajr", "🌅", "После бомдода" if not uz else "Bomdoddan keyin", t["Fajr"], t["Sunrise"]),
            ("morning", "☀️", "До пешина" if not uz else "Peshingacha", t["Sunrise"], t["Dhuhr"]),
            ("dhuhr", "🕛", "Между пешином и асром" if not uz else "Peshin va asr orasida", t["Dhuhr"], t["Asr"]),
            ("asr", "🌇", "Между асром и шомом" if not uz else "Asr va shom orasida", t["Asr"], t["Maghrib"]),
            ("maghrib", "🌆", "Между шомом и хуфтоном" if not uz else "Shom va xufton orasida", t["Maghrib"], t["Isha"]),
            ("isha", "🌙", "После хуфтона" if not uz else "Xuftondan keyin", t["Isha"], "")]
    return [{"key": k, "emoji": e, "title": ti, "range": f"{a}–{b}" if b else f"{a} →"} for k, e, ti, a, b in spec]


def mosques_block(profile: Profile, items: list[dict[str, Any]], *, place: str | None = None) -> str:
    """Готовый блок «🕌 Мечети рядом» (HTML) — строит код: расстояния и ссылки на карту точные."""
    if not items:
        return ""

    def km(m: int) -> str:
        return f"{m} м" if m < 1000 else f"{m / 1000:.1f} км"

    rows = [f"• <a href=\"{m['map']}\">{h(m['name'])}</a> — {km(m['distance_m'])}" for m in items[:4]]
    head = profile.tr("🕌 Мечети рядом", "🕌 Yaqin masjidlar") + (f" ({h(place)})" if place else "")
    return f"<b>{head}</b>\n<blockquote>" + "\n".join(rows) + "</blockquote>"


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
def _week_line(uid: int, today: date) -> str:
    wk = load(uid, "week")
    iso = today.isocalendar()
    if wk.get("week") == f"{int(iso[0]):04d}-W{int(iso[1]):02d}" and wk.get("goals"):
        return "ЦЕЛИ НЕДЕЛИ (он поставил их на планёрке): " + "; ".join(str(g.get("text")) for g in wk["goals"][:4])
    return ""


async def gather(profile: Profile, *, day: date, place: dict[str, Any] | None = None) -> dict[str, Any]:
    """Всё о дне: {"text": для модели, "pray", "wins", "open": задачи, "habits", "goals"} — ссылки на его записи с id."""
    from . import agent_tools
    from . import carry

    uid = profile.telegram_id
    today = profile.today
    parts: list[str] = [f"Сегодня {weekday(profile, today)}, {today:%d.%m.%Y}, сейчас {profile.now:%H:%M}. "
                        f"План на: {weekday(profile, day)} {day:%d.%m}" + (" (сегодня)" if day == today else " (завтра)" if day == today + timedelta(days=1) else "")]
    out: dict[str, Any] = {"day": day, "pray": {}, "wins": [], "open": [], "habits": [], "goals": []}
    try:
        parts.append(await agent_tools.snapshot(profile))
    except Exception:
        logger.warning("plan: срез данных не получил", exc_info=True)
    try:
        rows = await services.tasks(uid)
        out["open"] = [r for r in rows if (not r.get("due_date")) or str(r.get("due_date"))[:10] <= day.isoformat()]
        marks = []
        for r in out["open"][:12]:
            n = carry.count(uid, r.get("id"))
            if n:
                marks.append(f"«{str(r.get('text'))[:40]}» переносилась {n} раз")
        if marks:
            parts.append("Долго не делаются: " + "; ".join(marks))
    except Exception:
        logger.debug("plan: задачи", exc_info=True)
    try:
        out["goals"] = await services.goals(uid)
    except Exception:
        logger.debug("plan: цели", exc_info=True)
    habits = daily_tasks.all_items(uid)
    if habits:
        out["habits"] = [daily_tasks.describe(x, day) for x in habits if daily_tasks.is_today(x, day)]
        if out["habits"]:
            parts.append("Каждый день (ref type=daily, id в квадратных скобках): " + "; ".join(
                f"[{x['id']}] {x['title']}" + (f" в {x['time']}" if x.get("time") else "") + (f" ({x['progress']})" if x.get("progress") else "")
                for x in out["habits"]))
    out["text"] = "\n".join(p for p in parts if p)
    return out


def _persona_rules(profile: Profile, persona) -> str:  # noqa: ANN001
    return (f"Язык ответа — {_LANG_NAME.get(persona.lang, 'русском')}, на «вы». Голос женский — о себе в женском роде. "
            f"{honorific_rule(persona)} Слово «босс» не используй никогда.")


SCHEMA = (
    '{"items": [{"text": "коротко, с глагола, до 70 знаков", "kind": "main|task", '
    '"ref": {"type": "task|goal|daily", "id": "id из данных"} или null}]}'
)

PLAN_PROMPT = (
    "Ты — JES, личный помощник {name}. Составь КОРОТКИЙ ПЛАН ДНЯ: только то, что на самом деле нужно сделать. {rules}\n\n"
    "Верни ТОЛЬКО JSON по схеме (оформлением займётся код, ничего не форматируй, без markdown и эмодзи):\n{schema}\n\n"
    "ПРАВИЛА:\n"
    "• Не больше {max_items} пунктов — самое важное, день не должен быть забит. kind: main — не больше {max_main} главных "
    "(срочные и просроченные задачи, платежи и долги к сроку, шаг к цели); остальное task.\n"
    "• Цели: «ШАГ СЕГОДНЯ» у цели в данных — возьми как пункт (главный, если срочно) с ref type=goal и id цели; формулировку сократи. "
    "Отметка такого пункта сама двигает цель.\n"
    "• ref: если пункт — это его существующая задача (в данных «[id] текст»), цель или ежедневное дело — укажи её id ИЗ ДАННЫХ (type "
    "task|goal|daily). Нет такой записи — ref null (главные такие пункты станут задачами сами). Не выдумывай id.\n"
    "• Пункты — только ДЕЛА: задачи, шаги к целям, ежедневные дела. НЕ пиши: еду и калории, деньги и советы, отдых, сон, прогулку, намаз, "
    "суры, видео и музыку, приветствия, настрой и пожелания.\n"
    "• Дела, которые давно переносятся, — сократи до маленького шага.\n"
    "• Если день уже идёт — планируй от текущего времени, прошедшее не трогай. Дел нет — items [].\n"
    "• Ничего не выдумывай: нет данных — не пиши про это.\n\n"
    "ДАННЫЕ:\n{data}"
)

EDIT_PROMPT = (
    "Ты — JES, личный помощник {name}. Вот его ТЕКУЩИЙ план дня (JSON) и его просьба, что поменять. {rules}\n"
    "Верни ПОЛНЫЙ обновлённый план ТОЙ ЖЕ схемы (только JSON): у пунктов, которые остались, сохрани поле id и done; новые пункты — без id "
    f"(kind main — не больше {MAX_MAIN}; всего не больше {MAX_ITEMS}); «убери X» — удали; «добавь X» — добавь; «сделал X» — done true. "
    "Остальное не трогай.\n"
    "СХЕМА: {schema}\nК каждому пункту добавлены id и done.\n\n"
    "ТЕКУЩИЙ ПЛАН:\n{state}\n\nЕГО ПРОСЬБА: {ask}\n\nДАННЫЕ (для справки):\n{data}"
)


def _clean_ref(ref: Any, open_ids: set[str], daily_ids: set[str], goal_ids: set[str]) -> dict[str, str] | None:
    if not isinstance(ref, dict):
        return None
    kind, rid = str(ref.get("type") or ""), str(ref.get("id") or "").strip()
    ok = {"task": open_ids, "daily": daily_ids, "goal": goal_ids}.get(kind)
    return {"type": kind, "id": rid} if ok is not None and rid in ok else None


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", str(text or "").lower().replace("ё", "е")).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def clean_items(raw: Any, data: dict[str, Any], *, previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Пункты модели → чистый список: окна и kind по словарю, ссылки только на существующие записи, без повторов; done и auto
    сохраняются у прежних пунктов (по id или совпадению текста)."""
    open_ids = {str(r.get("id")) for r in data.get("open") or []}
    daily_ids = {str(x.get("id")) for x in data.get("habits") or []}
    goal_ids = {str(g.get("id")) for g in data.get("goals") or []}
    old = previous or []
    for p in old:  # ссылки прежнего плана остаются допустимыми, даже если задача уже не «открытая на сегодня»
        ref = p.get("ref") or {}
        if ref.get("type") == "task":
            open_ids.add(str(ref.get("id")))
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    main = 0
    for n, it in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(it, dict):
            continue
        text = " ".join(str(it.get("text") or "").split())[:90]
        if not text or _norm(text) in seen:
            continue
        seen.add(_norm(text))
        kind = "main" if str(it.get("kind") or "task") == "main" else "task"
        if kind == "main":
            main += 1
            kind = "main" if main <= MAX_MAIN else "task"
        window = "any"   # окон между намазами в плане больше нет
        prev = next((p for p in old if it.get("id") and p.get("id") == it.get("id")), None) or \
            next((p for p in old if _similar(p.get("text", ""), text) >= 0.8), None)
        items.append({"id": (prev or {}).get("id") or f"i{int(time.time() * 1000) % 10**8}{n}", "text": text, "kind": kind, "window": window,
                      "ref": _clean_ref(it.get("ref"), open_ids, daily_ids, goal_ids) or (prev or {}).get("ref"),
                      "done": bool((prev or {}).get("done")) or bool(it.get("done") and prev), "auto": bool((prev or {}).get("auto")),
                      "effect": (prev or {}).get("effect")})
        if len(items) >= MAX_ITEMS:
            break
    return items


async def _link_and_add(profile: Profile, items: list[dict[str, Any]], data: dict[str, Any], day: date) -> None:
    """«Главное» без своей записи: похожая открытая задача — привязываем; иначе создаём задачу на этот день (его выбор)."""
    uid = profile.telegram_id
    open_rows = data.get("open") or []
    for it in items:
        if it["ref"] or it["kind"] != "main":
            continue
        match = max(open_rows, key=lambda r: _similar(str(r.get("text") or ""), it["text"]), default=None)
        if match is not None and _similar(str(match.get("text") or ""), it["text"]) >= 0.72:
            it["ref"] = {"type": "task", "id": str(match.get("id"))}
            continue
        if not db.available("tasks"):
            continue
        try:
            row = await db.add_task(uid, text=it["text"], due_date=day.isoformat(), due_time=None)
            if row.get("id") is not None:
                it["ref"], it["auto"] = {"type": "task", "id": str(row["id"])}, True
        except Exception:
            logger.warning("plan: задачу не создал", exc_info=True)
    services.invalidate(uid, "tasks")


def _parse_json(text: str) -> dict[str, Any] | None:
    from .ai import extract_json

    try:
        data = extract_json(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _ask_json(prompt: str, *, max_tokens: int = 4500) -> dict[str, Any] | None:
    # размышления модели входят в лимит ответа: при 1600 на пробе 30.09 план обрезался — лимит с запасом, размышления «low»
    try:
        raw = await ai.generate([{"text": prompt}], model=ai_mod.smart_model(), temperature=0.6, json_mode=True,
                                thinking_budget=512, max_tokens=max_tokens)
    except Exception:
        logger.warning("plan: умная модель не ответила", exc_info=True)
        return None
    return _parse_json(raw)


def fallback_state(profile: Profile, data: dict[str, Any]) -> dict[str, Any]:
    """Модель недоступна — план без неё: дела на день (первые три — главные) и ежедневные дела."""
    items = []
    for n, r in enumerate((data.get("open") or [])[:MAX_ITEMS]):
        items.append({"text": str(r.get("text") or "")[:90], "kind": "main" if n < MAX_MAIN else "task", "window": "any",
                      "ref": {"type": "task", "id": str(r.get("id"))}})
    for x in (data.get("habits") or [])[:max(0, MAX_ITEMS - len(items))]:
        items.append({"text": x["title"][:90], "kind": "task", "window": "any", "ref": {"type": "daily", "id": str(x["id"])}})
    return {"items": items}


# ------------------------------------------------------------------ оформление
def _mark(it: dict[str, Any]) -> str:
    return "✅" if it.get("done") else "☐"


def _line(it: dict[str, Any], notes: dict[str, str]) -> str:
    text = h(it["text"])
    body = f"<s>{text}</s>" if it.get("done") else text
    star = "⭐ " if it.get("kind") == "main" and not it.get("done") else ""
    return f"{_mark(it)} {star}{body}{notes.get(it['id'], '')}"


def render(profile: Profile, st: dict[str, Any], *, carry_notes: dict[str, str] | None = None) -> str:
    """Оформление плана (HTML Telegram): заголовок и список ☐/✅ — главные первыми, без окон, цитат и прочего."""
    notes = carry_notes or {}
    day = date.fromisoformat(st["day"])
    items = sorted(st.get("items") or [], key=lambda it: 0 if it.get("kind") == "main" else 1)   # сортировка устойчивая — порядок модели сохраняется
    done = sum(1 for it in items if it.get("done"))
    head = f"🗓 <b>{profile.tr('План', 'Reja')} · {date_text(profile, day)}</b>" + (f" · {done}/{len(items)}" if items and done else "")
    rows = [_line(it, notes) for it in items] or [profile.tr("Дел на этот день нет — отдыхайте 🙂", "Bu kunga ish yo'q — dam oling 🙂")]
    hint = "<i>" + profile.tr(REPLY_HINT_RU, REPLY_HINT_UZ) + "</i>"
    return "\n".join([head, *rows, "", hint])[:4090]


def keyboard(profile: Profile, st: dict[str, Any]):  # noqa: ANN201
    from aiogram.types import InlineKeyboardMarkup

    from .keyboards import _btn

    items = st.get("items") or []
    order = sorted(range(len(items)), key=lambda i: (0 if items[i].get("kind") == "main" else 1 if items[i].get("kind") == "task" else 2, i))
    buttons = []
    for i in order[:8]:
        label = f"{_mark(items[i])} {items[i]['text']}"
        buttons.append(_btn(label if len(label) <= 30 else label[:29] + "…", f"pl:d:{i}"))
    rows = [[b] for b in buttons] if len(buttons) <= 5 else [buttons[j:j + 2] for j in range(0, len(buttons), 2)]
    rows.append([_btn("✏️ " + profile.tr("Изменить", "O'zgartirish"), "pl:e"), _btn("🔄 " + profile.tr("Заново", "Qayta"), "pl:r"),
                 _btn("🗓 " + profile.tr("На завтра", "Ertaga"), "pl:t")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------------ состояние плана: сборка, отметки, правка
async def _carry_notes(profile: Profile, st: dict[str, Any]) -> dict[str, str]:
    from . import carry

    out = {}
    for it in st.get("items") or []:
        ref = it.get("ref") or {}
        if ref.get("type") == "task" and (n := carry.count(profile.telegram_id, ref.get("id"))) and not it.get("done"):
            out[it["id"]] = f" <i>({profile.tr(f'переносилось {n} раз', f'{n} marta ko`chirilgan')})</i>"
    return out


async def sync_done(profile: Profile, st: dict[str, Any]) -> bool:
    """Задача выполнена не из плана (голосом, в списке) — в плане тоже ✅. True — что-то изменилось."""
    tasks = [it for it in st.get("items") or [] if (it.get("ref") or {}).get("type") == "task" and not it.get("done")]
    if not tasks or not db.available("tasks"):
        return False
    try:
        rows = {str(r.get("id")): r for r in await db.list_tasks(profile.telegram_id, include_done=True)}
    except Exception:
        return False
    changed = False
    for it in tasks:
        row = rows.get(str(it["ref"]["id"]))
        if row is not None and row.get("done"):
            it["done"], changed = True, True
    return changed


async def build_state(profile: Profile, persona, *, day: date, city: str | None = None, previous: dict[str, Any] | None = None,
                      ask: str | None = None) -> dict[str, Any]:
    """Новый план (или правка прежнего по просьбе ask) → состояние с окнами, пунктами, задачами."""
    data = await gather(profile, day=day)   # city больше не нужен: ни намаза по городу, ни мечетей в плане нет
    args = dict(name=profile.first_name or "пользователя", rules=_persona_rules(profile, persona), schema=SCHEMA, data=data["text"])
    if ask and previous:
        shown = {"items": [{k: it.get(k) for k in ("id", "text", "kind", "ref", "done")} for it in previous.get("items") or []]}
        prompt = EDIT_PROMPT.format(state=json.dumps(shown, ensure_ascii=False), ask=ask, **args)
    else:
        prompt = PLAN_PROMPT.format(max_items=MAX_ITEMS, max_main=MAX_MAIN, **args)
    raw = await _ask_json(prompt) or fallback_state(profile, data)
    st: dict[str, Any] = {
        "day": day.isoformat(), "place": None, "wins": [], "created_at": (previous or {}).get("created_at") or time.time(),
        "items": clean_items(raw.get("items"), data, previous=(previous or {}).get("items")), "surahs": [],
        "msg_id": (previous or {}).get("msg_id"), "quran_sent": None}
    await _link_and_add(profile, st["items"], data, day)
    return st


def _clean_surahs(raw: Any) -> list[dict[str, int]]:
    out = []
    for s in raw if isinstance(raw, list) else []:
        try:
            n = int(s.get("surah"))
            a, b = int(s.get("from") or 1), int(s.get("to") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= 114 and a >= 1:
            out.append({"surah": n, "from": a, "to": b if b >= a else 0})
    return out[:3]


async def _remove_dropped_auto_tasks(profile: Profile, before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    """Убрали пункт из плана (правка или «Заново») — созданная им задача тоже удаляется, если она не сделана. Свои прежние задачи
    и задачи, которые остались в плане, не трогаем."""
    kept = {str((it.get("ref") or {}).get("id")) for it in after if (it.get("ref") or {}).get("type") == "task"}
    ids = [it["ref"]["id"] for it in before if it.get("auto") and not it.get("done") and (it.get("ref") or {}).get("type") == "task"
           and str(it["ref"]["id"]) not in kept]
    if ids and db.available("tasks"):
        try:
            await db.delete_tasks(profile.telegram_id, ids)
            services.invalidate(profile.telegram_id, "tasks")
        except Exception:
            logger.warning("plan: задачи не удалил", exc_info=True)


async def _publish(bot, profile: Profile, st: dict[str, Any], *, edit: bool, pin: bool = False) -> dict[str, Any]:  # noqa: ANN001
    """Отправить или обновить сообщение плана. Без закрепа (pin оставлен для совместимости и не используется): сообщение — временное,
    как остальное в боте, — исчезает при следующем действии; прежний план при отправке нового удаляется."""
    from aiogram.types import LinkPreviewOptions

    from . import screen as screen_mod

    uid = profile.telegram_id
    text = render(profile, st, carry_notes=await _carry_notes(profile, st))
    kb = keyboard(profile, st)
    opts = LinkPreviewOptions(is_disabled=True)
    if edit and st.get("msg_id"):
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=int(st["msg_id"]), reply_markup=kb, parse_mode="HTML", link_preview_options=opts)
            return st
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return st
            logger.info("plan: сообщение не обновилось (%s) — отправляю заново", str(exc)[:80])
    old = load(uid)
    msg = await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML", link_preview_options=opts)
    st["msg_id"] = msg.message_id
    screen_mod.track_ephemeral(uid, msg.message_id, PLAN_TTL_S)
    if old.get("msg_id") and int(old["msg_id"]) != msg.message_id:
        try:
            await bot.delete_message(uid, int(old["msg_id"]))   # прежний план — не копим в чате
        except Exception:
            logger.debug("plan: прежний план не удалился", exc_info=True)
    return st


async def send_quran(bot, profile: Profile, st: dict[str, Any]) -> int:  # noqa: ANN001
    """Суры из плана — текст и транслитерация латиницей отдельными сообщениями (bot/quran.py). Возвращает, сколько сообщений."""
    from . import quran

    sent = 0
    for s in st.get("surahs") or []:
        for text in await quran.messages(s["surah"], s.get("from"), s.get("to") or None, lang=profile.lang):
            await bot.send_message(profile.telegram_id, text, parse_mode="HTML")
            sent += 1
    if sent:
        st["quran_sent"] = st.get("day")
    return sent


async def send(bot, profile: Profile, kind: str = "morning", *, city: str | None = None, day: date | None = None,
               force: bool = False) -> bool:  # noqa: ANN001
    """kind: morning | tomorrow | review | week. True — отправлено. force — явная просьба («Заново», «составь план»): всегда
    пересобрать, а не просто закрепить старый утренний план."""
    persona = await services.persona(profile.telegram_id)
    if kind == "review":
        return await send_review(bot, profile, persona)
    if kind == "week":
        return await send_week(bot, profile, persona)
    from . import carry

    today = profile.today
    day = day or (today + timedelta(days=1) if kind == "tomorrow" else today)
    old = load(profile.telegram_id)
    if kind == "morning":
        try:
            await carry.rollover(bot, profile)          # просроченное переезжает на сегодня (или JES спрашивает, что с ним)
        except Exception:
            logger.warning("plan: перенос просроченного не удался", exc_info=True)
    st = await build_state(profile, persona, day=day, city=city)
    st["updated_at"] = time.time()
    replace = bool(old.get("day") == day.isoformat() and old.get("msg_id"))
    if replace:                        # план на этот день уже есть — обновляем то же сообщение (исчезло — придёт новое)
        st["msg_id"] = old["msg_id"]
        await _remove_dropped_auto_tasks(profile, old.get("items") or [], st["items"])
    st = await _publish(bot, profile, st, edit=replace)
    save(profile.telegram_id, st)
    logger.info("plan: %s отправлен %s (%d пунктов)", kind, profile.telegram_id, len(st["items"]))
    return True


async def edit(bot, profile: Profile, ask: str) -> dict[str, Any] | None:  # noqa: ANN001
    """Изменить план по просьбе («убери прогулку», «добавь звонок в 15:00», «перенеси урок на после асра»)."""
    uid = profile.telegram_id
    old = load(uid)
    if not old.get("day"):
        return None
    persona = await services.persona(uid)
    st = await build_state(profile, persona, day=date.fromisoformat(old["day"]), previous=old, ask=ask)
    await _remove_dropped_auto_tasks(profile, old.get("items") or [], st["items"])
    st["updated_at"] = time.time()
    st = await _publish(bot, profile, st, edit=True)
    save(uid, st)
    return st


async def _apply_done(profile: Profile, it: dict[str, Any]) -> None:
    """Отметка пункта → его запись: задача (done / не done), ежедневное дело (сделано сегодня)."""
    uid, ref = profile.telegram_id, it.get("ref") or {}
    try:
        if ref.get("type") == "task" and db.available("tasks"):
            await db.update_task(uid, ref["id"], {"done": bool(it["done"]), "done_at": services.utc_now().isoformat() if it["done"] else None})
            services.invalidate(uid, "tasks")
        elif ref.get("type") == "daily" and it["done"]:
            habit = daily_tasks.find(uid, ref["id"])
            if habit is not None:
                daily_tasks.done(uid, habit, profile.today)
        elif ref.get("type") == "goal":
            # 30.09 «прогресс без ручного ввода»: ✅ у шага цели двигает саму цель (отметка привычки, сумма в накопления, этап)
            from . import goal_steps

            it["effect"] = await goal_steps.apply(profile, ref["id"], bool(it["done"]), it.get("effect"))
    except Exception:
        logger.warning("plan: отметка не дошла до записи", exc_info=True)


async def toggle(bot, profile: Profile, idx: int) -> str | None:  # noqa: ANN001
    """Кнопка-галочка: пункт сделан / не сделан; связанная задача или ежедневное дело — тоже. Возвращает текст пункта."""
    uid = profile.telegram_id
    st = load(uid)
    items = st.get("items") or []
    if not 0 <= idx < len(items):
        return None
    it = items[idx]
    it["done"] = not it.get("done")
    await _apply_done(profile, it)
    st["updated_at"] = time.time()
    await _publish(bot, profile, st, edit=True, pin=False)
    save(uid, st)
    return it["text"]


async def mark_done(bot, profile: Profile, query: str, done: bool = True) -> dict[str, Any] | None:  # noqa: ANN001
    """«Сделал, позвонил Алишеру» — находит самый похожий пункт плана и отмечает. None — пункта нет."""
    uid = profile.telegram_id
    st = load(uid)
    items = st.get("items") or []
    q = _norm(query)

    def score(it: dict[str, Any]) -> float:
        t = _norm(it["text"])
        return _similar(it["text"], query) + (0.3 if q and (q in t or t in q) else 0)

    best = max(items, key=score, default=None)
    if best is None or score(best) < 0.4:
        return None
    best["done"] = bool(done)
    await _apply_done(profile, best)
    st["updated_at"] = time.time()
    await _publish(bot, profile, st, edit=True, pin=False)
    save(uid, st)
    return {"text": best["text"], "done": best["done"], "left": sum(1 for it in items if not it.get("done"))}


# ------------------------------------------------------------------ вечерний разбор
REVIEW_PROMPT = (
    "Ты — JES, личный помощник {name}. Сейчас вечер: сделай КОРОТКИЙ РАЗБОР ДНЯ. {rules}\n"
    "Верни ТОЛЬКО JSON: {{\"done\": [\"что получилось — конкретно, с цифрами из данных, хвали за реальное\"], "
    "\"left\": [\"что не успели — по существу, без упрёков; если день был тяжёлым — поддержи\"], "
    "\"tip\": \"ОДНА идея на завтра: что поставить первым делом\", \"closing\": \"одна тёплая фраза на ночь\"}}\n"
    "done и left — до 4 коротких пунктов каждый. Не выдумывай ничего, чего нет в данных. Без markdown и эмодзи.\n\n"
    "ДАННЫЕ:\n{data}"
)


async def review_facts(profile: Profile) -> tuple[list[str], list[str]]:
    """(цифры для карточки — строит код, факты для модели)."""
    uid, today = profile.telegram_id, profile.today
    card: list[str] = []
    facts: list[str] = [f"Сегодня {weekday(profile, today)}, {today:%d.%m.%Y}, сейчас {profile.now:%H:%M}."]
    st = load(uid)
    if st.get("day") == today.isoformat() and st.get("items"):
        if await sync_done(profile, st):
            save(uid, st)
        items = st["items"]
        done = [it for it in items if it.get("done")]
        card.append(profile.tr(f"План: выполнено {len(done)} из {len(items)}", f"Reja: {len(done)} / {len(items)} bajarildi"))
        facts.append(f"План дня: выполнено {len(done)} из {len(items)}. Сделано: " + ("; ".join(it['text'] for it in done) or "ничего")
                     + ". Не сделано: " + ("; ".join(it['text'] for it in items if not it.get('done')) or "всё сделано"))
    try:
        rows = await db.list_tasks(uid, include_done=True) if db.available("tasks") else []
        done_t = [r for r in rows if r.get("done") and str(r.get("done_at") or "")[:10] == today.isoformat()]
        left_t = [r for r in rows if not r.get("done") and (r.get("due_date") and str(r["due_date"])[:10] <= today.isoformat())]
        card.append(profile.tr(f"Задачи: сделано {len(done_t)}, осталось на сегодня {len(left_t)}", f"Vazifalar: {len(done_t)} bajarildi, {len(left_t)} qoldi"))
        facts.append(f"Задачи: сделано {len(done_t)}" + (f" ({'; '.join(str(r.get('text'))[:40] for r in done_t[:5])})" if done_t else "")
                     + f"; осталось {len(left_t)}" + (f" ({'; '.join(str(r.get('text'))[:40] for r in left_t[:5])})" if left_t else ""))
    except Exception:
        logger.debug("review: задачи", exc_info=True)
    habits = [daily_tasks.describe(x, today) for x in daily_tasks.all_items(uid) if daily_tasks.is_today(x, today)]
    if habits:
        card.append(profile.tr("Ежедневные: ", "Har kunlik: ") + ", ".join(f"{'✅' if x['done_today'] else '☐'} {x['title'][:24]}" for x in habits[:4]))
        facts.append("Каждый день: " + "; ".join(f"{x['title']} — {'сделано' if x['done_today'] else 'не сделано'}" + (f" ({x['progress']})" if x.get("progress") else "") for x in habits))
    try:
        snap = await services.finance_snapshot(profile)
        plan = await services.nutrition_profile(uid)
        tot = nutri.totals(await services.today_calorie_logs(profile))
        goal = plan.get("daily_calories") if plan else None
        card.append(profile.tr(f"Еда: {int(tot['calories'])}" + (f" из {goal}" if goal else "") + " ккал", f"Ovqat: {int(tot['calories'])}" + (f" / {goal}" if goal else "") + " kkal"))
        card.append(f"{profile.tr('Траты', 'Xarajat')}: {fin.fmt_money(snap.today_expense)} {profile.currency}")
        facts.append(f"Траты сегодня: {fin.fmt_money(snap.today_expense)}, доход {fin.fmt_money(snap.today_income)}; баланс: карта {fin.fmt_money(snap.balances['card'])}, "
                     f"наличные {fin.fmt_money(snap.balances['cash'])}. Съедено {int(tot['calories'])} ккал" + (f" из {goal}" if goal else "")
                     + f" (Б{int(tot['protein'])}/Ж{int(tot['fat'])}/У{int(tot['carbs'])}).")
    except Exception:
        logger.debug("review: деньги и еда", exc_info=True)
    watched = [r for r in lessons.items(uid) if time.strftime("%Y-%m-%d", time.localtime(float(r.get("at") or 0))) == today.isoformat()
               and int(r.get("position") or 0) >= 60 and int(r.get("duration") or 0) >= 8 * 60]
    if watched:
        facts.append("Уроки/длинные видео: " + "; ".join(f"{r['title'][:50]} ({lessons.fmt(int(r['position']))})" for r in watched[:3]))
    todays = deeds.rows(uid, start=today, end=today)
    if todays:
        facts.append("Что делала JES сегодня: " + "; ".join(str(r.get("text"))[:50] for r in todays[-8:]))
    return card, facts


def render_review(profile: Profile, card: list[str], raw: dict[str, Any] | None, day: date) -> str:
    parts = [f"🌙 <b>{profile.tr('Разбор дня', 'Kun tahlili')} · {date_text(profile, day)}</b>"]
    if card:
        parts.append("📊 <b>" + profile.tr("Цифры", "Raqamlar") + "</b>\n<blockquote>" + "\n".join(h(c) for c in card) + "</blockquote>")
    raw = raw or {}

    def block(icon: str, title: str, rows: Any) -> None:
        rows = [str(r).strip() for r in rows if str(r).strip()][:4] if isinstance(rows, list) else []
        if rows:
            parts.append(f"{icon} <b>{title}</b>\n<blockquote>" + "\n".join("• " + h(r) for r in rows) + "</blockquote>")

    block("✅", profile.tr("Получилось", "Bajarildi"), raw.get("done"))
    block("➡️", profile.tr("Не успели", "Ulgurilmadi"), raw.get("left"))
    if raw.get("tip"):
        parts.append("💡 <b>" + profile.tr("На завтра", "Ertaga uchun") + "</b>\n<blockquote>" + h(str(raw["tip"])) + "</blockquote>")
    if raw.get("closing"):
        parts.append(f"<i>{h(str(raw['closing']))}</i>")
    return "\n\n".join(parts)[:4090]


def review_keyboard(profile: Profile):  # noqa: ANN201
    from aiogram.types import InlineKeyboardMarkup

    from .keyboards import _btn

    return InlineKeyboardMarkup(inline_keyboard=[[_btn("➡️ " + profile.tr("Перенести на завтра", "Ertaga ga ko'chirish"), "pl:m")],
                                                 [_btn("🗓 " + profile.tr("План на завтра", "Ertangi reja"), "pl:t")]])


async def send_review(bot, profile: Profile, persona) -> bool:  # noqa: ANN001
    from aiogram.types import LinkPreviewOptions

    card, facts = await review_facts(profile)
    raw = await _ask_json(REVIEW_PROMPT.format(name=profile.first_name or "пользователя", rules=_persona_rules(profile, persona), data="\n".join(facts)),
                          max_tokens=3000)
    await bot.send_message(profile.telegram_id, render_review(profile, card, raw, profile.today), reply_markup=review_keyboard(profile),
                           parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    return True


# ------------------------------------------------------------------ планёрка недели
WEEK_PROMPT = (
    "Ты — JES, личный помощник {name}. Сейчас воскресный вечер — проведи ПЛАНЁРКУ НЕДЕЛИ. {rules}\n"
    "Верни ТОЛЬКО JSON: {{\"intro\": \"одна строка\", \"goals\": [{{\"text\": \"цель недели: конкретная и достижимая, до 80 знаков\", "
    "\"days\": [\"пн\", \"ср\"]}}], \"focus\": \"главный фокус недели одной фразой\", \"closing\": \"одна тёплая фраза (барака)\"}}\n"
    "Ровно 3 цели — из его целей, открытых задач, долгов и платежей на неделю, уроков, итогов прошлой недели; каждой — дни недели, "
    "когда над ней работать (не все дни подряд: оставь воздух и пятницу для джума). Ничего не выдумывай. Без markdown и эмодзи.\n\n"
    "ДАННЫЕ:\n{data}"
)


async def send_week(bot, profile: Profile, persona) -> bool:  # noqa: ANN001
    """Воскресная планёрка: 3 цели недели по дням — утренний план дня опирается на них."""
    from aiogram.types import LinkPreviewOptions

    from . import agent_tools, weekly

    uid, today = profile.telegram_id, profile.today
    facts = [f"Сегодня {weekday(profile, today)}, {today:%d.%m.%Y}. Планируем неделю с понедельника {today + timedelta(days=1):%d.%m}."]
    try:
        facts.append(await agent_tools.snapshot(profile))
    except Exception:
        logger.debug("week: срез", exc_info=True)
    try:
        facts.append("ИТОГИ ПРОШЛОЙ НЕДЕЛИ:\n" + re.sub(r"<[^>]+>", "", weekly.text(profile, await weekly.facts(profile))))
    except Exception:
        logger.debug("week: итоги", exc_info=True)
    try:
        due = [r for r in await services.tasks(uid) if r.get("due_date") and today < date.fromisoformat(str(r["due_date"])[:10]) <= today + timedelta(days=8)]
        if due:
            facts.append("Задачи со сроком на неделю: " + "; ".join(f"{str(r['due_date'])[5:10]} {str(r.get('text'))[:40]}" for r in due[:10]))
    except Exception:
        logger.debug("week: задачи", exc_info=True)
    raw = await _ask_json(WEEK_PROMPT.format(name=profile.first_name or "пользователя", rules=_persona_rules(profile, persona), data="\n".join(facts)), max_tokens=3000)
    goals = [g for g in (raw or {}).get("goals") or [] if isinstance(g, dict) and str(g.get("text") or "").strip()][:3]
    if not goals or raw is None:
        return False
    nxt = today + timedelta(days=1)
    iso = nxt.isocalendar()
    save(uid, {"week": f"{int(iso[0]):04d}-W{int(iso[1]):02d}",
               "goals": [{"text": str(g["text"])[:100], "days": [str(d)[:3] for d in (g.get("days") or [])][:5]} for g in goals],
               "focus": str(raw.get("focus") or "")[:160], "created_at": time.time()}, "week")
    lines = [f"🗓 <b>{profile.tr('Планёрка недели', 'Hafta rejasi')} · {nxt:%d.%m}–{nxt + timedelta(days=6):%d.%m}</b>"]
    if raw.get("intro"):
        lines.append(f"<i>{h(str(raw['intro']))}</i>")
    lines.append("🎯 <b>" + profile.tr("Цели недели", "Hafta maqsadlari") + "</b>\n<blockquote>" + "\n".join(
        f"{i}. {h(str(g['text']))}" + (f" · <i>{', '.join(h(str(d)) for d in (g.get('days') or []))}</i>" if g.get("days") else "")
        for i, g in enumerate(goals, 1)) + "</blockquote>")
    if raw.get("focus"):
        lines.append("🔦 <b>" + profile.tr("Фокус", "Fokus") + "</b>\n<blockquote>" + h(str(raw["focus"])) + "</blockquote>")
    if raw.get("closing"):
        lines.append(f"<i>{h(str(raw['closing']))}</i>")
    lines.append("<i>" + profile.tr("Хотите поменять цели — просто напишите или надиктуйте. Утренний план дня будет опираться на них.",
                                    "O'zgartirmoqchi bo'lsangiz — yozing. Ertalabki reja shularga tayanadi.") + "</i>")
    await bot.send_message(uid, "\n\n".join(lines), parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    return True


async def maybe_send_week(bot, profile: Profile, week_key: str) -> bool:  # noqa: ANN001
    """Воскресенье вечером, один раз за неделю, только владельцу."""
    uid = profile.telegram_id
    us = await services.user_settings(uid)
    if us.get("week_plan_key") == week_key or us.get("day_plan") is False:
        return False
    await services.save_user_settings(uid, {"week_plan_key": week_key})
    return await send_week(bot, profile, await services.persona(uid))


__all__ = ["render", "keyboard", "send", "edit", "toggle", "mark_done", "load", "save", "is_plan_message", "place_info", "prayers",
           "mosques_for", "mosques_block", "gather", "build_state", "send_quran", "maybe_send_week", "sync_done", "review_facts", "render_review",
           "review_keyboard"]
