"""Траты из SMS и пушей банков (28.09.2026, его выбор: «Траты из SMS банков», «Спрашивать кнопкой», баланс — не трогать).

Телефон присылает уведомления, где есть сумма и слова об оплате/списании/зачислении (коды подтверждения телефон НЕ
присылает). Здесь: разбор (умная модель через бесплатный ключ Gemini; сумма обязана быть в самом тексте — иначе
пропускаем), отсев дублей (одна покупка приходит и SMS, и пушем приложения банка) и вопрос в боте кнопками:
«💳 45 000 сум — Korzinka. Записать как «Продукты»? [Записать] [Другая категория] [Не нужно]».
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

from . import categories as cats

logger = logging.getLogger(__name__)

PENDING_TTL_S = 3 * 24 * 3600
DUP_WINDOW_S = 300.0            # одна и та же сумма за 5 минут — это та же покупка (SMS + пуш банка)
_recent: list[tuple[float, str, int]] = []   # (когда, вид, сумма) — для отсева дублей


def _file():  # noqa: ANN202
    from .tg_user import data_dir

    return data_dir() / "bank_pending.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if now - float(v.get("at") or 0) < PENDING_TTL_S}


def _save(data: dict[str, Any]) -> None:
    try:
        _file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("bank: не сохранилось", exc_info=True)


def pending(event_id: str) -> dict[str, Any] | None:
    return _load().get(event_id)


def drop(event_id: str) -> None:
    data = _load()
    data.pop(event_id, None)
    _save(data)


# ------------------------------------------------------------------ разбор
_DIGITS = re.compile(r"\d[\d\s  .,]*\d|\d")


def amount_in_text(amount: float, text: str) -> bool:
    """Сумма операции действительно есть в тексте (модель не выдумала): «45 000,00» / «45000» / «45.000» → 45000."""
    want = int(round(amount))
    for m in _DIGITS.finditer(text):
        raw = m.group(0)
        whole = re.split(r"[.,](?=\d{2}\b)", raw.replace(" ", "").replace(" ", "").replace(" ", ""))[0]
        digits = re.sub(r"\D", "", whole)
        if digits and int(digits) == want:
            return True
    return False


def _prompt(app: str, title: str, text: str) -> str:
    return (
        f"Это уведомление банка или платёжного приложения ({app or 'SMS'}). Разбери ОДНУ операцию.\n"
        f"Заголовок: «{title}»\nТекст: «{text}»\n"
        'Верни JSON: {"kind": "expense|income|withdraw|skip", "amount": число, "currency": "UZS|USD|RUB|…", '
        '"merchant": "где / кому / от кого — 1–4 слова", "category": "ключ категории", "why": "если skip — почему"}\n'
        "expense — оплата, покупка, списание, перевод другому человеку, оплата связи и услуг. "
        "income — зачисление, поступление, перевод вам, зарплата, кешбэк, возврат покупки. "
        "withdraw — снятие наличных в банкомате. "
        "skip — код подтверждения, отказ/отклонено/недостаточно средств, реклама, только остаток без операции, "
        "перевод между своими картами.\n"
        "amount — сумма самой операции (не остаток и не комиссия), как в тексте.\n"
        f"Категории расходов: {cats.prompt_catalog('expense')}.\nКатегории доходов: {cats.prompt_catalog('income')}."
    )


async def parse(app: str, title: str, text: str) -> dict[str, Any] | None:
    """{"kind", "amount", "currency", "merchant", "category"} или None (не операция / не разобрали)."""
    from . import ai as ai_mod
    from .context import ai

    free = ai_mod.use_free(ai_mod.FREE_SMART_MODEL)  # бесплатно, умной моделью; нет ключа — как обычно
    try:
        raw = await ai.generate([{"text": _prompt(app, title, text)}], temperature=0.0, json_mode=True, max_tokens=300)
    except Exception:
        logger.warning("bank: разбор не вышел", exc_info=True)
        return None
    finally:
        ai_mod.reset_free(free)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "skip").lower()
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if kind not in {"expense", "income", "withdraw"} or amount <= 0:
        return None
    if not amount_in_text(amount, f"{title}\n{text}"):
        logger.info("bank: сумма %s не найдена в тексте — пропускаю", amount)
        return None
    merchant = " ".join(str(data.get("merchant") or "").split())[:60]
    category = cats.normalize(str(data.get("category") or ""), "income" if kind == "income" else "expense", note=merchant or text)
    return {"kind": kind, "amount": amount, "currency": str(data.get("currency") or "UZS").upper()[:5], "merchant": merchant,
            "category": category}


def duplicate(kind: str, amount: float, now: float | None = None) -> bool:
    """Та же сумма того же вида за последние 5 минут — дубль (SMS и пуш одной покупки)."""
    now = now or time.monotonic()
    while _recent and now - _recent[0][0] > DUP_WINDOW_S:
        _recent.pop(0)
    key = (kind, int(round(amount)))
    if any((k, a) == key for _, k, a in _recent):
        return True
    _recent.append((now, key[0], key[1]))
    return False


# ------------------------------------------------------------------ вопрос в боте
def _money(amount: float) -> str:
    return f"{int(round(amount)):,}".replace(",", " ")


def question(ev: dict[str, Any], lang: str) -> str:
    uz = lang == "uz"
    who = ev.get("merchant") or ev.get("app") or ""
    head = f"💳 <b>{_money(ev['amount'])} {'so‘m' if uz else 'сум'}</b>" + (f" — {_esc(who)}" if who else "")
    src = f"\n<i>{_esc(ev.get('app') or 'SMS')}</i>"
    if ev["kind"] == "withdraw":
        ask = "Naqd pulga o‘tkazma sifatida yozaymi?" if uz else "Снятие наличных — записать как перевод с карты в наличные?"
    elif ev["kind"] == "income":
        ask = (f"Kirim sifatida yozaymi — «{cats.label(ev['category'], lang)}»?" if uz
               else f"Записать как доход «{cats.label(ev['category'], lang)}»?")
    else:
        ask = (f"«{cats.label(ev['category'], lang)}» deb yozaymi?" if uz else f"Записать как «{cats.label(ev['category'], lang)}»?")
    return f"{head}{src}\n{ask}"


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def receive(uid: int, data: dict[str, Any]) -> dict[str, Any]:
    """Уведомление с телефона → разбор → вопрос в боте. Возвращает, что сделали (для журнала и тестов)."""
    from . import secrets_guard
    from .context import bot_instance
    from .handlers.common import profile_by_id
    from .handlers.bank import question_keyboard

    app = str(data.get("app") or "")[:40]
    title = str(data.get("title") or "")[:200]
    text = str(data.get("text") or "")[:1000]
    if not text or secrets_guard.find(f"{title} {text}"):
        return {"skipped": "пусто или похоже на код/ключ"}
    ev = await parse(app, title, text)
    if ev is None:
        return {"skipped": "не операция"}
    profile = await profile_by_id(uid)
    if ev["currency"] not in {"UZS", "СУМ", "SUM", "SO'M", ""} and ev["currency"] != str(profile.currency).upper():
        return {"skipped": f"валюта {ev['currency']}"}
    if duplicate(ev["kind"], ev["amount"]):
        return {"skipped": "дубль"}
    try:
        when = datetime.fromtimestamp(int(data.get("t")) / 1000, profile.tz).date() if data.get("t") else profile.today
    except Exception:
        when = profile.today
    event_id = uuid.uuid4().hex[:10]
    ev.update({"id": event_id, "app": app, "day": when.isoformat(), "at": time.time()})
    store = _load()
    store[event_id] = ev
    _save(store)
    bot = bot_instance()
    if bot is None:
        return {"skipped": "бот не запущен"}
    await bot.send_message(uid, question(ev, profile.lang), reply_markup=question_keyboard(event_id, profile.lang), parse_mode="HTML")
    logger.info("bank: %s %s — %s (%s), спросил", ev["kind"], ev["amount"], ev["merchant"], app)
    return {"asked": event_id, **ev}


async def record(uid: int, ev: dict[str, Any], category: str | None = None) -> list[dict[str, Any]]:
    """Записать операцию (после «Записать» / выбранной категории). Возвращает добавленные записи."""
    from datetime import date

    from . import cache
    from . import finance as fin
    from .context import db

    day = date.fromisoformat(ev["day"])
    note = ev.get("merchant") or ev.get("app") or ""
    if ev["kind"] == "withdraw":
        item = {"entry_type": "expense", "amount": ev["amount"], "category": cats.TRANSFER_KEY,
                "note": fin.note_with_transfer(note or "банкомат", "card", "cash"), "entry_date": day.isoformat()}
    else:
        kind = "income" if ev["kind"] == "income" else "expense"
        item = {"entry_type": kind, "amount": ev["amount"], "category": cats.normalize(category or ev["category"], kind, note=note),
                "note": fin.note_with_bucket(note, "card"), "entry_date": day.isoformat()}
    inserted = await db.add_finance_entries(uid, [item], entry_date=day, source="bank")
    cache.invalidate(uid, "fin_entries")
    return inserted


__all__ = ["receive", "record", "parse", "question", "pending", "drop", "duplicate", "amount_in_text"]
