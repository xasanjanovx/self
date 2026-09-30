"""Долг со сроком → задача за день до срока («Вернуть долг: Uzum — 1 050 000 сум, срок 26.10»).

Сверка, а не событие: sync() сравнивает открытые долги («я должен») со сроками и автозадачами (ref_key `debt:<кредитор>:<срок>`),
поэтому работает при любом способе внести долг, срок или погашение. Зовётся сразу после долговых операций и раз в 10 минут
из proactive_worker. Закрытую или удалённую пользователем задачу заново не создаёт (строка остаётся закрытой).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from . import finance as fin
from . import services
from .context import db
from .profile import Profile

logger = logging.getLogger(__name__)

PREFIX = "debt:"


def ref_key(person: str | None, due: date) -> str:
    return f"{PREFIX}{fin.person_key(person)}:{due.isoformat()}"


def task_text(person: str | None, amount: float, due: date, *, uz: bool, currency: str) -> str:
    who = (person or "").strip() or ("nomsiz" if uz else "без имени")
    money = f"{fin.fmt_money(amount)} {currency}".strip()
    return f"💳 Qarzni qaytarish: {who} — {money} (muddat {due:%d.%m})" if uz else f"💳 Вернуть долг: {who} — {money} (срок {due:%d.%m})"


def plan(rows: list[dict[str, Any]], existing: list[dict[str, Any]], today: date, *, uz: bool = False, currency: str = "") -> tuple[list[dict[str, Any]], list[tuple[Any, str]], list[Any]]:
    """rows — сроки открытых долгов (services.debt_due_rows), existing — автозадачи о долгах (и открытые, и закрытые).
    → (создать [{text, due_date, ref_key}], поправить текст [(id, text)], убрать [id])."""
    have = {str(r.get("ref_key")): r for r in existing if r.get("ref_key")}
    wanted: dict[str, tuple[dict[str, Any], date]] = {}
    for r in rows:
        if r.get("side") != "debt" or float(r.get("amount") or 0) < 1:
            continue
        try:
            due = date.fromisoformat(str(r.get("due_date"))[:10])
        except ValueError:
            continue
        wanted[ref_key(r.get("person"), due)] = (r, due)
    create: list[dict[str, Any]] = []
    retext: list[tuple[Any, str]] = []
    for key, (r, due) in wanted.items():
        text = task_text(r.get("person"), float(r["amount"]), due, uz=uz, currency=currency)
        row = have.get(key)
        if row is None:
            if due >= today:  # просроченный долг уже напоминает proactive.debt_alerts — задачу задним числом не заводим
                create.append({"text": text, "due_date": max(due - timedelta(days=1), today).isoformat(), "ref_key": key})
        elif not row.get("done") and row.get("text") != text:
            retext.append((row["id"], text))  # часть долга вернули — сумма в задаче обновилась
    drop = [r["id"] for k, r in have.items() if k not in wanted and not r.get("done")]  # долг закрыт или срок сдвинули
    return create, retext, drop


async def sync(profile: Profile) -> int:
    """Привести автозадачи в соответствие с долгами. → сколько изменений."""
    if not db.available("tasks"):
        return 0
    uid = profile.telegram_id
    rows = [r for r in await services.debt_due_rows(uid) if r.get("side") == "debt"]
    existing = await db.list_ref_tasks(uid, PREFIX)
    create, retext, drop = plan(rows, existing, profile.today, uz=profile.lang == "uz", currency=profile.currency)
    changed = 0
    for it in create:
        try:
            await db.add_task(uid, text=it["text"], due_date=it["due_date"], due_time=None, ref_key=it["ref_key"])
            changed += 1
        except Exception:
            logger.debug("debt task exists or failed: %s", it["ref_key"], exc_info=True)  # параллельная сверка уже создала — уникальный ключ
    for task_id, text in retext:
        await db.update_task(uid, task_id, {"text": text})
        changed += 1
    if drop:
        await db.delete_tasks(uid, drop, hard=True)
        changed += len(drop)
    if changed:
        services.invalidate(uid, "tasks")
    return changed


async def sync_safe(profile: Profile) -> None:
    """Для мест, где долг только что изменился: сбой сверки не должен ломать основной ответ."""
    try:
        await sync(profile)
    except Exception:
        logger.warning("debt tasks sync failed for %s", profile.telegram_id, exc_info=True)


__all__ = ["plan", "sync", "sync_safe", "ref_key", "task_text"]
