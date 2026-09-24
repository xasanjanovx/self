"""Расход Gemini и остаток предоплаты (Prepay в Google AI Studio).

У Google нет API, чтобы прочитать баланс, поэтому считаем сами: каждый ответ Gemini приходит
с usageMetadata (сколько токенов какого вида) → цена по прайсу → минус из остатка, который
владелец назвал сам («пополнил на $10», «баланс $7.40»). Когда он называет фактический остаток,
сверяем с нашим подсчётом и поправляем множитель (цены/скидки/налоги, которых мы не видим).

Кончились деньги — Gemini отвечает 402: сразу пишем в чат, а Джарвис говорит об этом в разговоре.
Состояние — JSON в DATA_DIR (переживает перезапуск и передеплой).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# USD за 1M токенов (платный уровень, ai.google.dev/gemini-api/docs/pricing, сентябрь 2026)
PRICES: dict[str, dict[str, float]] = {
    "gemini-3.8-live": {"text_in": 0.75, "audio_in": 3.0, "image_in": 1.0, "text_out": 4.5, "audio_out": 12.0},
    "gemini-3.1-flash-live": {"text_in": 0.75, "audio_in": 3.0, "image_in": 1.0, "text_out": 4.5, "audio_out": 12.0},
    "gemini-2.5-flash-native-audio": {"text_in": 0.5, "audio_in": 3.0, "image_in": 3.0, "text_out": 2.0, "audio_out": 12.0},
    "gemini-3.5-flash-lite": {"text_in": 0.3, "audio_in": 0.3, "image_in": 0.3, "text_out": 2.5},
    "gemini-3.5-flash": {"text_in": 1.5, "audio_in": 1.5, "image_in": 1.5, "text_out": 9.0},
    "gemini-3.1-flash-lite": {"text_in": 0.25, "audio_in": 0.5, "image_in": 0.25, "text_out": 1.5},
    "gemini-2.5-flash-preview-tts": {"text_in": 0.5, "audio_out": 10.0},
    "gemini-2.5-pro-preview-tts": {"text_in": 1.0, "audio_out": 20.0},
    "gemini-2.5-flash-lite": {"text_in": 0.1, "audio_in": 0.3, "image_in": 0.1, "text_out": 0.4},
    "gemini-2.5-flash": {"text_in": 0.3, "audio_in": 1.0, "image_in": 0.3, "text_out": 2.5},
}
_DEFAULT = {"text_in": 1.0, "audio_in": 3.0, "image_in": 1.0, "text_out": 5.0, "audio_out": 12.0}

LOW_DAYS = 3.0        # «хватит меньше чем на 3 дня» — предупреждаем
URGENT_DAYS = 1.0     # меньше суток — срочно
LOW_USD = 2.0
URGENT_USD = 0.5
KEEP_DAYS = 45
TOPUP_URL = "https://aistudio.google.com/"

_state: dict[str, Any] | None = None
_dirty = False
_save_task: asyncio.Task | None = None
_last_check = 0.0


# ------------------------------------------------------------------ хранение
def _file() -> Path:
    from .tg_user import data_dir

    return data_dir() / "billing.json"


def _load() -> dict[str, Any]:
    global _state
    if _state is None:
        try:
            _state = json.loads(_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _state = {}
        _state.setdefault("days", {})
        _state.setdefault("alerts", {})
        _state.setdefault("factor", 1.0)
        _state.setdefault("spent_since", 0.0)
    return _state


def _save_now() -> None:
    global _dirty
    if _state is None:
        return
    days = _state.get("days") or {}
    for key in sorted(days)[:-KEEP_DAYS]:
        days.pop(key, None)
    try:
        tmp = _file().with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_file())
        _dirty = False
    except OSError:
        logger.warning("billing: не сохранил состояние", exc_info=True)


def _schedule_save() -> None:
    """Пишем на диск не чаще раза в 20 с — записей много (каждая реплика Gemini)."""
    global _dirty, _save_task
    _dirty = True
    if _save_task is not None and not _save_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _save_now()
        return

    async def later() -> None:
        await asyncio.sleep(20)
        if _dirty:
            _save_now()

    _save_task = loop.create_task(later())


def flush() -> None:
    if _dirty:
        _save_now()


# ------------------------------------------------------------------ цена
def prices_for(model: str) -> dict[str, float]:
    name = str(model or "").removeprefix("models/")
    for key in sorted(PRICES, key=len, reverse=True):
        if name.startswith(key):
            return PRICES[key]
    return _DEFAULT


def _split(details: Any, total: int) -> dict[str, int]:
    """Токены по видам (TEXT/AUDIO/IMAGE/VIDEO); то, что не расписано, считаем текстом."""
    out = {"text": 0, "audio": 0, "image": 0}
    counted = 0
    for d in details or []:
        if not isinstance(d, dict):
            continue
        n = int(d.get("tokenCount") or 0)
        mod = str(d.get("modality") or "").upper()
        kind = "audio" if mod == "AUDIO" else "image" if mod in {"IMAGE", "VIDEO"} else "text"
        out[kind] += n
        counted += n
    if total > counted:
        out["text"] += total - counted
    return out


def cost(model: str, usage: dict[str, Any]) -> float:
    """Стоимость одного ответа в USD по usageMetadata (REST generateContent или Live API)."""
    if not isinstance(usage, dict):
        return 0.0
    p = prices_for(model)
    prompt = int(usage.get("promptTokenCount") or 0)
    output = int(usage.get("candidatesTokenCount") or usage.get("responseTokenCount") or 0)
    thoughts = int(usage.get("thoughtsTokenCount") or 0)
    tool_prompt = int(usage.get("toolUsePromptTokenCount") or 0)
    ins = _split(usage.get("promptTokensDetails"), prompt)
    outs = _split(usage.get("candidatesTokensDetails") or usage.get("responseTokensDetails"), output)
    usd = (ins["text"] + tool_prompt) * p.get("text_in", 1.0)
    usd += ins["audio"] * p.get("audio_in", p.get("text_in", 1.0))
    usd += ins["image"] * p.get("image_in", p.get("text_in", 1.0))
    # у TTS нет текстового выхода: весь ответ — звук, даже если модель не расписала виды
    text_out = p.get("text_out", p.get("audio_out", 5.0))
    usd += (outs["text"] + outs["image"] + thoughts) * text_out
    usd += outs["audio"] * p.get("audio_out", text_out)
    return usd / 1_000_000


# ------------------------------------------------------------------ учёт
def _today() -> str:
    return datetime.now(timezone(timedelta(hours=5))).date().isoformat()


def record(model: str, usage: dict[str, Any] | None, *, kind: str = "text") -> float:
    """Записать расход одного ответа Gemini. kind: text | live | tts | stt | vision."""
    if not usage:
        return 0.0
    usd = cost(model, usage)
    if usd <= 0:
        return 0.0
    st = _load()
    day = st["days"].setdefault(_today(), {"usd": 0.0, "calls": 0, "kinds": {}})
    day["usd"] = round(day["usd"] + usd, 6)
    day["calls"] = int(day.get("calls") or 0) + 1
    day["kinds"][kind] = round(float(day["kinds"].get(kind) or 0) + usd, 6)
    if st.get("anchor"):
        st["spent_since"] = round(float(st.get("spent_since") or 0) + usd, 6)
    st.pop("exhausted_at", None)  # ответ пришёл — значит, деньги есть
    _schedule_save()
    _maybe_alert()
    return usd


def rate_limited() -> None:
    st = _load()
    st.setdefault("rate_limited", {})
    st["rate_limited"][_today()] = int(st["rate_limited"].get(_today()) or 0) + 1
    _schedule_save()


def is_billing_error(status: int | None, text: str = "") -> bool:
    low = str(text or "").lower()
    return (status == 402 or "payment required" in low or ("prepay" in low and ("balance" in low or "depleted" in low))
            or "insufficient credit" in low or "credits are depleted" in low)


def exhausted(detail: str = "") -> None:
    """Gemini ответил 402: предоплата кончилась — все ключи стоят. Пишем владельцу сразу."""
    st = _load()
    now = datetime.now(timezone.utc)
    st["exhausted_at"] = now.isoformat()
    last = st["alerts"].get("empty")
    _schedule_save()
    if last and now - datetime.fromisoformat(last) < timedelta(hours=3):
        return
    st["alerts"]["empty"] = now.isoformat()
    logger.error("billing: баланс Gemini закончился (402) %s", detail[:200])
    _notify("🔴 <b>Баланс Gemini закончился.</b>\nБот и Джарвис не могут думать и говорить, пока не пополните предоплату "
            "в AI Studio → Billing.\n\nПополнили — напишите мне: «пополнил на 10$».")


# ------------------------------------------------------------------ остаток и прогноз
def _daily_burn(st: dict[str, Any]) -> float:
    """Средний расход в день за последние 7 дней (сегодняшний — пропорционально прошедшей части суток)."""
    days = st.get("days") or {}
    today = _today()
    keys = [k for k in sorted(days) if k < today][-6:]
    total = sum(float(days[k].get("usd") or 0) for k in keys)
    n = len(keys)
    now = datetime.now(timezone(timedelta(hours=5)))
    part = (now.hour * 60 + now.minute) / 1440
    today_usd = float((days.get(today) or {}).get("usd") or 0)
    if part > 0.25:  # к полудню сегодняшний день уже о чём-то говорит
        total += today_usd / part
        n += 1
    elif not n and today_usd:
        total, n = today_usd / max(part, 0.1), 1
    return (total / n) * float(st.get("factor") or 1.0) if n else 0.0


def status() -> dict[str, Any]:
    """Остаток, прогноз и расход — для инструмента ai_status и предупреждений."""
    st = _load()
    factor = float(st.get("factor") or 1.0)
    days = st.get("days") or {}
    today = _today()
    month = today[:7]
    out: dict[str, Any] = {
        "spent_today_usd": round(float((days.get(today) or {}).get("usd") or 0) * factor, 3),
        "spent_month_usd": round(sum(float(v.get("usd") or 0) for k, v in days.items() if k.startswith(month)) * factor, 2),
        "daily_average_usd": round(_daily_burn(st), 3),
        "calls_today": int((days.get(today) or {}).get("calls") or 0),
        "by_kind_today_usd": {k: round(v * factor, 3) for k, v in ((days.get(today) or {}).get("kinds") or {}).items()},
        "calibration_factor": round(factor, 2),
    }
    anchor = st.get("anchor")
    if anchor:
        remaining = float(anchor["usd"]) - float(st.get("spent_since") or 0) * factor
        out["balance_usd"] = round(remaining, 2)
        out["balance_known_at"] = anchor.get("at")
        burn = out["daily_average_usd"]
        if burn > 0:
            out["days_left"] = round(max(remaining, 0) / burn, 1)
        out["need_topup"] = remaining <= LOW_USD or (burn > 0 and remaining / burn <= LOW_DAYS)
        out["urgent"] = remaining <= URGENT_USD or (burn > 0 and remaining / burn <= URGENT_DAYS)
    else:
        out["balance_usd"] = None
        out["note"] = "остаток неизвестен — попроси сказать, сколько сейчас на балансе в AI Studio (Billing), и вызови set_ai_balance"
    if st.get("exhausted_at"):
        out["exhausted"] = True
        out["urgent"] = True
    rl = (st.get("rate_limited") or {}).get(today)
    if rl:
        out["rate_limited_today"] = rl
    return out


def set_balance(usd: float | None = None, topup: float | None = None) -> dict[str, Any]:
    """Он назвал фактический остаток (usd) или сумму пополнения (topup)."""
    st = _load()
    now = datetime.now(timezone.utc).isoformat()
    anchor = st.get("anchor")
    spent = float(st.get("spent_since") or 0)
    if usd is not None:
        usd = max(0.0, float(usd))
        if anchor and spent > 0.2 and topup is None:
            # сверка: сколько реально ушло с прошлой отметки против нашего подсчёта
            real = float(anchor["usd"]) - usd
            if real > 0:
                ratio = max(0.3, min(3.0, real / spent))
                st["factor"] = round(float(st.get("factor") or 1.0) * 0.5 + ratio * 0.5, 3)
        st["anchor"] = {"usd": usd, "at": now}
    elif topup is not None:
        current = (float(anchor["usd"]) - spent * float(st.get("factor") or 1.0)) if anchor else 0.0
        st["anchor"] = {"usd": round(max(current, 0.0) + max(0.0, float(topup)), 2), "at": now}
    else:
        return {"error": "нужна сумма: остаток (balance_usd) или пополнение (topup_usd)"}
    st["spent_since"] = 0.0
    st["alerts"] = {k: v for k, v in (st.get("alerts") or {}).items() if k == "empty"}
    st.pop("exhausted_at", None)
    _save_now()
    return {"ok": True, **status()}


# ------------------------------------------------------------------ предупреждения
def _maybe_alert() -> None:
    global _last_check
    if time.monotonic() - _last_check < 300:
        return
    _last_check = time.monotonic()
    s = status()
    if s.get("balance_usd") is None or not s.get("need_topup"):
        return
    st = _load()
    level = "urgent" if s.get("urgent") else "low"
    now = datetime.now(timezone.utc)
    last = st["alerts"].get(level)
    repeat = timedelta(hours=12 if level == "urgent" else 72)
    if last and now - datetime.fromisoformat(last) < repeat:
        return
    st["alerts"][level] = now.isoformat()
    _schedule_save()
    _notify(alert_text(s))


def alert_text(s: dict[str, Any]) -> str:
    left = f"≈ ${s['balance_usd']:.2f}"
    days = s.get("days_left")
    when = f", хватит примерно на {days:g} дн." if days is not None else ""
    head = "🔴 <b>Срочно пополните Gemini</b>" if s.get("urgent") else "⚠️ <b>Скоро пополнять Gemini</b>"
    return (f"{head}\nОсталось {left}{when} (тратим ~${s.get('daily_average_usd', 0):.2f} в день).\n"
            "AI Studio → Billing → пополнить. Потом напишите мне: «пополнил на 10$».")


def voice_note() -> str:
    """Строка в системный промпт разговора: мало денег — пусть Джарвис скажет об этом сам (раз в 6 часов)."""
    s = status()
    if not (s.get("need_topup") or s.get("exhausted")):
        return ""
    st = _load()
    now = datetime.now(timezone.utc)
    last = st["alerts"].get("voice")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=6):
        return ""
    st["alerts"]["voice"] = now.isoformat()
    _schedule_save()
    days = s.get("days_left")
    return ("\nВАЖНО: на балансе Gemini (AI Studio) осталось ≈ $" + f"{s.get('balance_usd') or 0:.2f}"
            + (f", хватит примерно на {days:g} дн." if days is not None else ".")
            + " Один раз за разговор, после ответа на его просьбу, коротко скажи, что пора пополнить баланс в AI Studio.")


def _notify(text: str) -> None:
    async def send() -> None:
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            from .context import bot_instance, settings

            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Открыть AI Studio", url=TOPUP_URL)]])
            for uid in sorted(settings.allowed_telegram_ids)[:1]:
                await bot_instance().send_message(uid, text, reply_markup=kb)
        except Exception:
            logger.warning("billing: не отправил предупреждение", exc_info=True)

    try:
        asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        pass


__all__ = ["record", "cost", "status", "set_balance", "exhausted", "is_billing_error", "rate_limited", "voice_note", "flush", "PRICES"]
