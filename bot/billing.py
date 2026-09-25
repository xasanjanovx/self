"""Расход Gemini и остаток предоплаты (Prepay в Google AI Studio).

У Google нет API, чтобы прочитать баланс, поэтому считаем сами: каждый ответ Gemini приходит
с usageMetadata (сколько токенов какого вида) → цена по прайсу → минус из остатка, который
владелец назвал сам («пополнил на $10», «баланс $7.40»). Когда он называет фактический остаток,
сверяем с нашим подсчётом и поправляем множитель (цены/скидки/налоги, которых мы не видим).

Кончились деньги — Gemini отвечает 402: сразу пишем в чат, а Nurai говорит об этом в разговоре.
Состояние — JSON в DATA_DIR (переживает перезапуск и передеплой).

Подробно: за день — сумма по видам (kinds: live/agent/stt/tts…), внутри — по типам токенов (detail: звук на вход/выход,
текст, кадры, кэш, «размышления»), и цена каждого разговора (sessions: телефон экономно/Live, звонки) — через Meter.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass, field
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
    "gemini-3.8-flash-lite-tts": {"text_in": 0.5, "audio_out": 6.0},
    "gemini-2.5-flash-preview-tts": {"text_in": 0.5, "audio_out": 10.0},
    "gemini-2.5-pro-preview-tts": {"text_in": 1.0, "audio_out": 20.0},
    "gemini-2.5-flash-lite": {"text_in": 0.1, "audio_in": 0.3, "image_in": 0.1, "text_out": 0.4},
    "gemini-2.5-flash": {"text_in": 0.3, "audio_in": 1.0, "image_in": 0.3, "text_out": 2.5},
    # Alibaba Model Studio, Сингапур (alibabacloud.com/help/en/model-studio/model-pricing) — отдельный счёт, не AI Studio
    "qwen3.8-omni-flash-realtime": {"text_in": 0.23, "audio_in": 0.93, "image_in": 0.23, "text_out": 0.70, "audio_out": 1.87},
}
OTHER_PROVIDERS = ("qwen",)  # их расход — отдельной строкой: он не уменьшает предоплату Gemini и не входит в её лимит
_DEFAULT = {"text_in": 1.0, "audio_in": 3.0, "image_in": 1.0, "text_out": 5.0, "audio_out": 12.0}

CACHED_SHARE = 0.1    # вход из кэша (неявный кэш Gemini 2.5+/3.x) — 10% обычной цены
KEEP_SESSIONS = 60
LOW_DAYS = 3.0        # «хватит меньше чем на 3 дня» — предупреждаем
URGENT_DAYS = 1.0     # меньше суток — срочно
LOW_USD = 2.0
URGENT_USD = 0.5
KEEP_DAYS = 45
# он выбрал: не больше $0.5 в день (по Ташкенту). Дошли — пишем в чат, Джарвис до конца дня в экономном режиме
DAILY_LIMIT_USD = float(os.getenv("GEMINI_DAILY_LIMIT_USD") or 0.5)
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


PARTS = ("text_in", "audio_in", "image_in", "cached", "text_out", "thoughts", "audio_out")


def breakdown(model: str, usage: dict[str, Any]) -> tuple[dict[str, float], dict[str, int]]:
    """Один ответ по usageMetadata (REST generateContent или Live API) → (USD по видам, токены по видам).
    Виды: text_in / audio_in / image_in (кадры) / cached (вход из кэша, 10% цены) / text_out / thoughts / audio_out."""
    usd = dict.fromkeys(PARTS, 0.0)
    tokens = dict.fromkeys(PARTS, 0)
    if not isinstance(usage, dict):
        return usd, tokens
    p = prices_for(model)
    prompt = int(usage.get("promptTokenCount") or 0)
    output = int(usage.get("candidatesTokenCount") or usage.get("responseTokenCount") or 0)
    thoughts = int(usage.get("thoughtsTokenCount") or 0)
    tool_prompt = int(usage.get("toolUsePromptTokenCount") or 0)
    cached_total = min(int(usage.get("cachedContentTokenCount") or 0), prompt)
    ins = _split(usage.get("promptTokensDetails"), prompt)
    cached = _split(usage.get("cacheTokensDetails"), cached_total) if cached_total else {"text": 0, "audio": 0, "image": 0}
    ins["text"] += tool_prompt
    price_in = {"text": p.get("text_in", 1.0), "audio": p.get("audio_in", p.get("text_in", 1.0)),
                "image": p.get("image_in", p.get("text_in", 1.0))}
    for kind, price in price_in.items():
        from_cache = min(cached[kind], ins[kind])
        tokens[f"{kind}_in"] = ins[kind] - from_cache
        usd[f"{kind}_in"] = (ins[kind] - from_cache) * price
        tokens["cached"] += from_cache
        usd["cached"] += from_cache * price * CACHED_SHARE
    outs = _split(usage.get("candidatesTokensDetails") or usage.get("responseTokensDetails"), output)
    # у TTS нет текстового выхода: весь ответ — звук, даже если модель не расписала виды
    text_out = p.get("text_out", p.get("audio_out", 5.0))
    tokens["text_out"], tokens["thoughts"], tokens["audio_out"] = outs["text"] + outs["image"], thoughts, outs["audio"]
    usd["text_out"] = tokens["text_out"] * text_out
    usd["thoughts"] = thoughts * text_out
    usd["audio_out"] = outs["audio"] * p.get("audio_out", text_out)
    return {k: v / 1_000_000 for k, v in usd.items()}, tokens


def cost(model: str, usage: dict[str, Any]) -> float:
    """Стоимость одного ответа в USD по usageMetadata (REST generateContent или Live API)."""
    return sum(breakdown(model, usage)[0].values())


# ------------------------------------------------------------------ цена одного разговора
@dataclass
class Meter:
    """Сколько стоил один разговор: всё, что записано в его задачах (и задачах, созданных из них), — сюда."""

    kind: str                       # phone | call | wake | incoming
    mode: str = ""                  # phone: economy | live
    started: float = field(default_factory=time.monotonic)
    usd: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)
    token: Any = None

    def add(self, kind: str, usd: float) -> None:
        self.usd += usd
        self.parts[kind] = self.parts.get(kind, 0.0) + usd


_meter: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("billing_meter", default=None)


def start_session(kind: str, mode: str = "") -> Meter:
    """Начать счёт разговора в текущей задаче (asyncio копирует контекст в задачи, созданные после этого)."""
    meter = Meter(kind=kind, mode=mode)
    meter.token = _meter.set(meter)
    return meter


def end_session(meter: Meter, **info: Any) -> None:
    """Разговор окончен — в журнал: когда, какой, сколько секунд и сколько стоил (и из чего сложилась цена)."""
    if meter.token is not None:
        try:
            _meter.reset(meter.token)  # дальше расход вызывающей задачи — уже не этого разговора
        except ValueError:
            pass
        meter.token = None
    st = _load()
    sessions = st.setdefault("sessions", [])
    sessions.append({"at": datetime.now(timezone(timedelta(hours=5))).isoformat(timespec="seconds"), "kind": meter.kind,
                     "mode": meter.mode, "sec": round(time.monotonic() - meter.started),
                     "usd": round(meter.usd * float(st.get("factor") or 1.0), 5),
                     "by": {k: round(v, 5) for k, v in meter.parts.items() if v > 0}, **info})
    del sessions[:-KEEP_SESSIONS]
    _schedule_save()
    logger.info("billing: разговор %s/%s — %d с, $%.4f %s", meter.kind, meter.mode, round(time.monotonic() - meter.started),
                meter.usd, {k: round(v, 4) for k, v in meter.parts.items()})


# ------------------------------------------------------------------ учёт
def _today() -> str:
    return datetime.now(timezone(timedelta(hours=5))).date().isoformat()


def record(model: str, usage: dict[str, Any] | None, *, kind: str = "text") -> float:
    """Записать расход одного ответа Gemini. kind: text | live | tts | stt | vision."""
    if not usage:
        return 0.0
    parts, tokens = breakdown(model, usage)
    usd = sum(parts.values())
    if usd <= 0:
        return 0.0
    if (meter := _meter.get()) is not None:
        meter.add(kind, usd)
    st = _load()
    day = st["days"].setdefault(_today(), {"usd": 0.0, "calls": 0, "kinds": {}})
    provider = next((p for p in OTHER_PROVIDERS if str(model).startswith(p)), None)
    if provider:
        other = day.setdefault("other", {})
        other[provider] = round(float(other.get(provider) or 0) + usd, 6)
        _schedule_save()
        return usd
    day["usd"] = round(day["usd"] + usd, 6)
    day["calls"] = int(day.get("calls") or 0) + 1
    day["kinds"][kind] = round(float(day["kinds"].get(kind) or 0) + usd, 6)
    detail = day.setdefault("detail", {}).setdefault(kind, {})
    counts = day.setdefault("tokens", {}).setdefault(kind, {})
    for part in PARTS:
        if parts[part] > 0:
            detail[part] = round(float(detail.get(part) or 0) + parts[part], 6)
        if tokens[part] > 0:
            counts[part] = int(counts.get(part) or 0) + tokens[part]
    if st.get("anchor"):
        st["spent_since"] = round(float(st.get("spent_since") or 0) + usd, 6)
    st.pop("exhausted_at", None)  # ответ пришёл — значит, деньги есть
    _schedule_save()
    _maybe_alert()
    _maybe_limit_alert(st, day)
    return usd


def spent_today() -> float:
    return float(((_load().get("days") or {}).get(_today()) or {}).get("usd") or 0.0) * float(_load().get("factor") or 1.0)


def over_limit() -> bool:
    """Сегодняшний расход дошёл до дневного лимита — экономный режим до полуночи."""
    return DAILY_LIMIT_USD > 0 and spent_today() >= DAILY_LIMIT_USD


def live_allowed(mode: str = "phone") -> bool:
    """Жёсткий лимит (он выбрал): после дневного лимита живой голос (Gemini Live) до полуночи выключен —
    телефон отвечает только в экономном режиме, звонков «позвони мне» нет. Подъём на фаджр — всегда."""
    return mode == "wake" or not over_limit()


def _maybe_limit_alert(st: dict[str, Any], day: dict[str, Any]) -> None:
    if DAILY_LIMIT_USD <= 0 or day.get("limit_alert") or float(day["usd"]) * float(st.get("factor") or 1.0) < DAILY_LIMIT_USD:
        return
    day["limit_alert"] = True
    _schedule_save()
    _notify(f"🟡 <b>Лимит Gemini на сегодня — ${DAILY_LIMIT_USD:g} — достигнут.</b>\nДо полуночи живой голос (Gemini Live) выключен: "
            "Nurai на телефоне выполняет команды и отвечает в экономном режиме, камера, экран и звонки «позвони мне» — завтра. "
            "Будильник на фаджр работает как обычно.")


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
    _notify("🔴 <b>Баланс Gemini закончился.</b>\nБот и Nurai не могут думать и говорить, пока не пополните предоплату "
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
        # Qwen (Alibaba) — отдельный счёт, для сравнения с Gemini
        "qwen_today_usd": round(float(((days.get(today) or {}).get("other") or {}).get("qwen") or 0), 4),
        "qwen_month_usd": round(sum(float((v.get("other") or {}).get("qwen") or 0) for k, v in days.items() if k.startswith(month)), 3),
        "calibration_factor": round(factor, 2),
        "daily_limit_usd": DAILY_LIMIT_USD,
        "live_voice_today": "выключен до полуночи (дневной лимит)" if over_limit() else "включён",
    }
    # из чего сложился расход сегодня: вид (live/agent/stt/tts…) → тип токенов (звук, текст, кадры, кэш, размышления)
    detail = (days.get(today) or {}).get("detail") or {}
    if detail:
        out["detail_today_usd"] = {k: {p: round(v * factor, 4) for p, v in d.items() if v * factor >= 0.0001} for k, d in detail.items()}
    sessions = [s for s in st.get("sessions") or [] if str(s.get("at") or "").startswith(today)]
    if sessions:
        by_mode: dict[str, dict[str, float]] = {}
        for s in sessions:
            key = f"{s.get('kind')}/{s.get('mode')}" if s.get("mode") else str(s.get("kind"))
            agg = by_mode.setdefault(key, {"count": 0, "usd": 0.0, "sec": 0})
            agg["count"] += 1
            agg["usd"] += float(s.get("usd") or 0)
            agg["sec"] += int(s.get("sec") or 0)
        out["conversations_today"] = {k: {"count": int(v["count"]), "usd": round(v["usd"], 3), "avg_usd": round(v["usd"] / v["count"], 4),
                                          "minutes": round(v["sec"] / 60, 1)} for k, v in by_mode.items()}
        out["last_conversations"] = [{k: s.get(k) for k in ("at", "kind", "mode", "sec", "usd", "said")} for s in sessions[-5:]]
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
    """Строка в системный промпт разговора: мало денег — пусть Nurai скажет об этом сам (раз в 6 часов)."""
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


__all__ = ["record", "cost", "breakdown", "status", "set_balance", "exhausted", "is_billing_error", "rate_limited", "voice_note", "flush",
           "PRICES", "Meter", "start_session", "end_session", "live_allowed"]
