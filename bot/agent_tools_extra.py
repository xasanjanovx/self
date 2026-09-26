"""Инструменты «JES», которые делают его ассистентом, а не только оператором БД:

- ask_user — уточняющий вопрос с вариантами-кнопками (вместо «не понял»);
- remember_about_me — долгая память: факты о пользователе в user_memory (006);
- currency_rates — курсы ЦБ РУз (cbu.uz), конвертация;
- calculate — безопасный калькулятор;
- web_search — поиск в интернете (Gemini + Google Search grounding);
- weather — погода (Open-Meteo, без ключа).

Регистрируются в общем реестре `agent_tools.TOOLS` (модуль импортируется в конце bot/agent_tools.py).
"""
from __future__ import annotations

import ast
import logging
import operator
import time
from typing import Any

import httpx

from . import services
from .agent_tools import ARR, P, ToolContext, _num, _str, tool
from .context import ai, db

logger = logging.getLogger(__name__)

MAX_FACTS = 40
FACT_MAX_LEN = 140
RECENT_MAX_LINES = 20


# ------------------------------------------------------------------ ask_user
@tool(
    "ask_user",
    "Задать пользователю ОДИН короткий уточняющий вопрос с вариантами-кнопками, когда без ответа нельзя выполнить точно: "
    "какая из нескольких записей (≤4), расход или долг, какая дата/сумма, что имелось в виду. Варианты — конкретные и короткие "
    "(«Такси вчера 25 000», «Обед 40 000»), 2–4 штуки; пользователь может и написать ответ текстом. "
    "НЕ используй для подтверждений «точно?» и когда смысл ясен — тогда просто действуй.",
    {"question": P("STRING", "вопрос, 1 строка"), "options": ARR({"type": "STRING"}, "2–4 варианта ответа, до 40 символов каждый")},
    ("question", "options"),
)
async def _ask_user(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    question = _str(a.get("question"))
    options = [str(o).strip()[:40] for o in (a.get("options") or []) if _str(o)][:4]
    if not question:
        return {"error": "question required"}
    ctx.ask = {"question": question, "options": options}
    return {"status": "asked; wait for the user's answer in the next message"}


# ------------------------------------------------------------------ memory
def parse_facts(text: str) -> list[str]:
    return [line.strip(" •-–\t") for line in str(text or "").splitlines() if line.strip(" •-–\t")]


def merge_facts(current: list[str], add: list[str], forget: list[str]) -> list[str]:
    """Новые факты — в конец; похожие старые (по подстроке без регистра) заменяются; forget — удаляет по подстроке."""
    out = list(current)
    for f in forget:
        key = f.lower().strip()
        if key:
            out = [x for x in out if key not in x.lower()]
    for f in add:
        f = f.strip()[:FACT_MAX_LEN]
        if not f:
            continue
        key = f.lower()
        out = [x for x in out if not (x.lower() in key or key in x.lower())]
        out.append(f)
    return out[-MAX_FACTS:]


@tool(
    "remember_about_me",
    "Долгая память о пользователе. Сохраняй БЕЗ просьбы устойчивые факты, которые пригодятся потом: люди и кто они («Асилбек — брат»), "
    "привычки и предпочтения («обедаю в Evos», «не ем свинину»), даты («зарплата 5-го числа»), суммы («зарплата ~5 млн»), "
    "цели, работа, семья, любимые формулировки. НЕ сохраняй разовые операции и то, что уже есть в данных. forget — убрать устаревшее.",
    {"add": ARR({"type": "STRING"}, "факты коротко, по одному"), "forget": ARR({"type": "STRING"}, "фрагменты фактов, которые больше не верны")},
)
async def _remember(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("user_memory"):
        return {"error": "table user_memory missing — run sql/migrations/006_agent_memory.sql in Supabase"}
    add = [str(x) for x in (a.get("add") or []) if _str(x)]
    forget = [str(x) for x in (a.get("forget") or []) if _str(x)]
    if not add and not forget:
        return {"error": "nothing to remember"}
    mem = await services.user_memory(ctx.uid)
    facts = merge_facts(parse_facts(mem.get("facts") or ""), add, forget)
    await services.save_user_memory(ctx.uid, {"facts": "\n".join(facts)})
    return {"facts_total": len(facts), "added": add, "forgotten": forget}


async def remember_exchange(uid: int, user_text: str, reply: str, *, when: str) -> None:
    """Дайджест реплик за прошлые дни (виден агенту в промпте, переживает перезапуск)."""
    if not db.available("user_memory") or not user_text:
        return
    try:
        mem = await services.user_memory(uid)
        lines = [line for line in str(mem.get("recent") or "").splitlines() if line.strip()]
        u = " ".join(user_text.split())[:90]
        r = " ".join((reply or "").split())[:90]
        lines.append(f"{when} · я: {u}" + (f" → бот: {r}" if r else ""))
        await services.save_user_memory(uid, {"recent": "\n".join(lines[-RECENT_MAX_LINES:])})
    except Exception:
        logger.debug("remember_exchange failed", exc_info=True)


async def memory_prompt(uid: int) -> str:
    """Блок «ПАМЯТЬ» для системного промпта."""
    try:
        mem = await services.user_memory(uid)
    except Exception:
        return ""
    facts = parse_facts(mem.get("facts") or "")
    recent = [line for line in str(mem.get("recent") or "").splitlines() if line.strip()]
    parts = []
    if facts:
        parts.append("ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ (факты, которые он сообщал раньше):\n" + "\n".join(f"• {f}" for f in facts))
    if recent:
        parts.append("НЕДАВНИЕ РЕПЛИКИ (прошлые дни, для контекста «как вчера», «ему же»):\n" + "\n".join(recent[-12:]))
    try:
        if intent := await services.photo_intent(uid):
            parts.append(f"ДОГОВОРЁННОСТЬ О ФОТО (expect_photo, действует): {intent}")
    except Exception:
        pass
    return "\n\n".join(parts)


# ------------------------------------------------------------------ currency (CBU)
_CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/"
_rates_cache: dict[str, Any] = {"at": 0.0, "rates": {}, "date": ""}
_RATES_TTL = 6 * 3600


async def cbu_rates() -> tuple[dict[str, float], str]:
    """Курсы ЦБ РУз: {code: сум за 1 единицу}, дата. Кэш 6 часов."""
    now = time.monotonic()
    if _rates_cache["rates"] and now - _rates_cache["at"] < _RATES_TTL:
        return _rates_cache["rates"], _rates_cache["date"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(_CBU_URL)
        res.raise_for_status()
        data = res.json()
    rates: dict[str, float] = {}
    date = ""
    for item in data if isinstance(data, list) else []:
        code = str(item.get("Ccy") or "").upper()
        try:
            rate = float(str(item.get("Rate") or "").replace(",", "."))
            nominal = float(str(item.get("Nominal") or "1").replace(",", ".")) or 1.0
        except ValueError:
            continue
        if code:
            rates[code] = rate / nominal
            date = str(item.get("Date") or date)
    if rates:
        _rates_cache.update({"at": now, "rates": rates, "date": date})
    return rates, date


def convert(amount: float, src: str, dst: str, rates: dict[str, float]) -> float | None:
    src, dst = src.upper(), dst.upper()
    per_uzs = {"UZS": 1.0, "СУМ": 1.0, "SUM": 1.0, "SO'M": 1.0, **rates}
    if src not in per_uzs or dst not in per_uzs:
        return None
    return amount * per_uzs[src] / per_uzs[dst]


@tool(
    "currency_rates",
    "Курсы валют ЦБ Узбекистана (USD, EUR, RUB, KZT, …) в сумах и конвертация: «сколько это в долларах», «курс доллара», «200$ в сумах».",
    {"codes": ARR({"type": "STRING"}, "коды валют, по умолчанию USD, EUR, RUB"), "amount": P("NUMBER", "сумма для конвертации"),
     "from_currency": P("STRING", "из какой валюты (UZS, USD, …)"), "to_currency": P("STRING", "в какую валюту")},
)
async def _currency(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    rates, date = await cbu_rates()
    if not rates:
        return {"error": "cbu.uz недоступен"}
    codes = [str(c).upper() for c in (a.get("codes") or []) if _str(c)] or ["USD", "EUR", "RUB"]
    out: dict[str, Any] = {"date": date, "rates_uzs_per_unit": {c: round(rates[c], 2) for c in codes if c in rates}}
    amount = _num(a.get("amount"))
    src, dst = _str(a.get("from_currency")), _str(a.get("to_currency"))
    if amount is not None and src and dst:
        value = convert(amount, src, dst, rates)
        out["conversion"] = {"amount": amount, "from": src.upper(), "to": dst.upper(), "result": round(value, 2) if value is not None else None}
    return out


# ------------------------------------------------------------------ calculator
_OPS: dict[type, Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_eval(expr: str) -> float:
    """Арифметика без eval: + - * / // % ** и скобки. Суммы («1.5 млн») модель переводит в числа сама."""
    expr = str(expr or "").replace(",", ".").replace("×", "*").replace("х", "*").replace("÷", "/").replace("^", "**").replace(" ", "")
    if not expr or len(expr) > 200:
        raise ValueError("empty or too long")

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            if isinstance(node.op, ast.Pow) and abs(ev(node.right)) > 64:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"unsupported: {type(node).__name__}")

    return ev(ast.parse(expr, mode="eval"))


@tool(
    "calculate",
    "Точный калькулятор для любых расчётов с деньгами и числами («сколько в день до зарплаты», «12% от 3 млн», «(5000000-1200000)/9»). "
    "Выражение только из чисел и + - * / ( ) ** — суммы переведи в числа сам (1.5 млн → 1500000, 12% → *0.12).",
    {"expression": P("STRING", "арифметическое выражение")}, ("expression",),
)
async def _calc(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    try:
        value = safe_eval(_str(a.get("expression")) or "")
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        return {"error": f"bad expression: {str(exc)[:80]}"}
    return {"result": round(value, 6)}


# ------------------------------------------------------------------ web search
@tool(
    "web_search",
    "Поиск в интернете по свежим фактам: новости, цены, адреса, расписания, правила, «что такое …», всё, чего нет в данных пользователя. "
    "Верни пользователю суть в 2–6 строках, без ссылок-простыней.",
    {"query": P("STRING", "поисковый запрос, конкретный")}, ("query",),
)
async def _search(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    query = _str(a.get("query"))
    if not query:
        return {"error": "query required"}
    try:
        answer = await ai.search(query, lang=ctx.profile.lang)
    except Exception as exc:
        logger.exception("web_search failed")
        return {"error": f"search failed: {str(exc)[:120]}"}
    return {"answer": (answer or "")[:3000], "note": "summarize for the user in their language"}


# ------------------------------------------------------------------ expect_photo
@tool(
    "expect_photo",
    "Договориться, что делать с фото, которые он пришлёт («пришлю фото челленджа — отмечай выполненное», «буду слать чеки — записывай траты»). "
    "Пока договорённость действует, каждое его фото приходит ТЕБЕ вместе с этой инструкцией (а не в подсчёт калорий). "
    "days — сколько дней действует (1 — только сегодня, 30 — месяц, 365 — постоянно); days=0 — отменить. "
    "Вызывай ВСЕГДА, когда обещаешь что-то сделать с будущим фото — иначе фото уйдёт в питание.",
    {"purpose": P("STRING", "что сделать с фото: подробно, с деталями (какие цели отмечать, как понять выполненное)"),
     "days": P("INTEGER", "сколько дней действует; 0 — отменить")},
    ("purpose",),
)
async def _expect_photo(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    uid = ctx.profile.telegram_id
    if not db.available("assistant_settings"):
        return {"error": "assistant_settings unavailable"}
    days = int(_num(a.get("days")) if a.get("days") is not None else 1)
    purpose = (_str(a.get("purpose")) or "")[:600]
    if days <= 0 or not purpose:
        await services.save_persona(uid, {"photo_intent": None, "photo_intent_until": None})
        return {"ok": True, "cancelled": True}
    until = datetime.now(timezone.utc) + timedelta(days=min(days, 365))
    await services.save_persona(uid, {"photo_intent": purpose, "photo_intent_until": until.isoformat()})
    return {"ok": True, "until": until.date().isoformat(), "note": "теперь его фото придут тебе с этой инструкцией"}


# ------------------------------------------------------------------ weather (Open-Meteo)
_WMO = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман", 48: "туман с изморозью",
    51: "морось", 53: "морось", 55: "сильная морось", 61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 80: "ливень", 81: "ливень", 82: "сильный ливень",
    95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}


@tool(
    "weather",
    "Погода сейчас и на 3 дня по городу (Open-Meteo). По умолчанию — Ташкент.",
    {"city": P("STRING", "город, например Tashkent, Samarkand, Москва")},
)
async def _weather(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    city = _str(a.get("city")) or "Tashkent"
    async with httpx.AsyncClient(timeout=15.0) as client:
        geo = await client.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1, "language": "ru"})
        geo.raise_for_status()
        places = (geo.json() or {}).get("results") or []
        if not places:
            return {"error": f"city not found: {city}"}
        place = places[0]
        res = await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 3,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
        })
        res.raise_for_status()
        data = res.json() or {}
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    days = []
    for i, day in enumerate(daily.get("time") or []):
        days.append({
            "date": day, "min": daily["temperature_2m_min"][i], "max": daily["temperature_2m_max"][i],
            "sky": _WMO.get(int(daily["weather_code"][i]), "—"),
            "rain_prob_pct": (daily.get("precipitation_probability_max") or [None] * 3)[i],
        })
    return {
        "place": f"{place.get('name')}, {place.get('country')}",
        "now": {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"), "sky": _WMO.get(int(cur.get("weather_code") or 0), "—"),
                "wind_kmh": cur.get("wind_speed_10m"), "humidity": cur.get("relative_humidity_2m")},
        "days": days,
    }


# ------------------------------------------------------------------ баланс Gemini и версия
@tool(
    "ai_status",
    "Про самого JES: остаток предоплаты Gemini (AI Studio), на сколько дней хватит, расход сегодня/за месяц/в среднем, "
    "нужно ли пополнять, какая версия бота и приложения, какие модели. «Сколько осталось на балансе?», «когда пополнять?», «какая у тебя версия?».",
)
async def _ai_status(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import billing, version
    from .context import ai
    from .live_call import MODELS

    out: dict[str, Any] = {"money": billing.status(), "version": version.info(),
                           "models": {"chat": ai.agent_model, "text": ai.text_model, "voice_calls": MODELS[0], "tts": ai.tts_model}}
    out["how_counted"] = ("остаток считает сам бот по ценам Google из каждого ответа Gemini; точный — в AI Studio → Billing. "
                          "Скажет фактический остаток — вызови set_ai_balance, счёт станет точнее")
    return out


@tool(
    "set_ai_balance",
    "Он назвал остаток предоплаты Gemini в AI Studio («на балансе 7.40$») — balance_usd; или пополнил («пополнил на 10 долларов») — topup_usd. "
    "Суммы в долларах США.",
    {"balance_usd": P("NUMBER", "фактический остаток, $"), "topup_usd": P("NUMBER", "сумма пополнения, $")},
)
async def _set_ai_balance(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import billing

    balance, topup = _num(a.get("balance_usd")), _num(a.get("topup_usd"))
    if balance is None and topup is None:
        return {"error": "назови сумму: остаток или пополнение в долларах"}
    return billing.set_balance(usd=balance, topup=topup if balance is None else None)
