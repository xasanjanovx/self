"""Сервисный слой: данные для панелей одним вызовом + кэш + инвалидация на запись."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import cache
from . import finance as fin
from . import nutrition as nutri
from .context import db
from .db import local_day_bounds_utc
from .profile import Profile

ENTRIES_TTL = 300.0
SETTINGS_TTL = 600.0
PROFILE_TTL = 600.0
LOGS_TTL = 180.0


# ------------------------------------------------------------------ finance
async def finance_entries(uid: int) -> list[dict[str, Any]]:
    return await cache.remember(uid, ("fin_entries",), ENTRIES_TTL, lambda: db.list_finance_entries_all(uid))


async def finance_settings(uid: int) -> dict[str, float]:
    return await cache.remember(uid, ("fin_settings",), SETTINGS_TTL, lambda: db.get_finance_settings(uid))


@dataclass
class FinanceSnapshot:
    entries: list[dict[str, Any]]
    settings: dict[str, float]
    balances: dict[str, float]
    today: date
    today_entries: list[dict[str, Any]] = field(default_factory=list)
    today_income: float = 0.0
    today_expense: float = 0.0
    month: fin.Stats | None = None
    quick: list[dict[str, Any]] = field(default_factory=list)

    @property
    def wallet(self) -> float:
        return self.balances["card"] + self.balances["cash"]


async def finance_snapshot(profile: Profile) -> FinanceSnapshot:
    entries, settings = await asyncio.gather(finance_entries(profile.telegram_id), finance_settings(profile.telegram_id))
    today = profile.today
    snap = FinanceSnapshot(entries=entries, settings=settings, balances=fin.compute_balances(entries, settings), today=today)
    snap.today_entries = fin.entries_between(entries, today, today)
    for row in snap.today_entries:
        if fin.is_transfer(row):
            continue
        amount = float(row.get("amount") or 0)
        if row.get("entry_type") == "income":
            snap.today_income += amount
        else:
            snap.today_expense += amount
    snap.month = fin.compute_stats(entries, fin.period_for("month", today))
    snap.quick = fin.top_operations(entries, limit=8, since=today - timedelta(days=120))
    return snap


async def add_finance_entries(profile: Profile, items: list[dict[str, Any]], *, source: str) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in items:
        amount = float(item.get("amount") or 0)
        if amount <= 0:
            continue
        if item.get("kind") == "transfer":
            src = fin.normalize_bucket(item.get("from_bucket"))
            dst = fin.normalize_bucket(item.get("to_bucket"))
            if src == dst:
                continue
            payload.append(
                {
                    "entry_type": "expense",
                    "amount": amount,
                    "category": "transfer",
                    "note": fin.note_with_transfer(item.get("note"), src, dst),
                    "source": source,
                }
            )
        else:
            kind = "income" if item.get("kind") == "income" else "expense"
            payload.append(
                {
                    "entry_type": kind,
                    "amount": amount,
                    "category": item.get("category") or ("other_in" if kind == "income" else "other"),
                    "note": fin.note_with_bucket(item.get("note"), item.get("bucket") or "card"),
                    "source": source,
                }
            )
    inserted = await db.add_finance_entries(profile.telegram_id, payload, entry_date=profile.today, source=source)
    cache.invalidate(profile.telegram_id, "fin_entries")
    return inserted


async def delete_finance_entry(uid: int, entry_id: str | int) -> None:
    await db.delete_finance_entry(uid, entry_id)
    cache.invalidate(uid, "fin_entries")


async def set_finance_category(uid: int, entry_id: str | int, category: str) -> None:
    await db.update_finance_entry(uid, entry_id, {"category": category})
    cache.invalidate(uid, "fin_entries")


async def save_finance_settings(uid: int, payload: dict[str, float]) -> None:
    await db.save_finance_settings(uid, payload)
    cache.invalidate(uid, "fin_settings")


# ------------------------------------------------------------------ nutrition
async def nutrition_profile(uid: int) -> dict[str, Any] | None:
    cached = cache.get(uid, ("nutri_profile",))
    if cached is not None:
        return cached or None
    value = await db.get_nutrition_profile(uid)
    cache.put(uid, ("nutri_profile",), value or {}, PROFILE_TTL)
    return value


async def save_nutrition_profile(uid: int, profile: dict[str, Any]) -> None:
    await db.save_nutrition_profile(uid, profile)
    cache.invalidate(uid, "nutri_profile")


async def today_calorie_logs(profile: Profile) -> list[dict[str, Any]]:
    async def load() -> list[dict[str, Any]]:
        start, end = local_day_bounds_utc(profile.today, profile.tz)
        return await db.list_calorie_logs_between(profile.telegram_id, start, end)

    return await cache.remember(profile.telegram_id, ("kcal_today", profile.today.isoformat()), LOGS_TTL, load)


async def calorie_logs(profile: Profile, days: int) -> list[dict[str, Any]]:
    return await cache.remember(
        profile.telegram_id,
        ("kcal_days", days, profile.today.isoformat()),
        LOGS_TTL,
        lambda: db.list_calorie_logs(profile.telegram_id, days=days, tz_name=profile.tz_name),
    )


async def top_meals(profile: Profile) -> list[dict[str, Any]]:
    logs = await calorie_logs(profile, 120)
    return nutri.top_meals(logs, limit=8)


async def add_calorie_logs(uid: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inserted = await db.add_calorie_logs(uid, items)
    cache.invalidate(uid, "kcal_today")
    cache.invalidate(uid, "kcal_days")
    return inserted


async def delete_calorie_log(uid: int, log_id: str | int) -> None:
    await db.delete_calorie_log(uid, log_id)
    cache.invalidate(uid, "kcal_today")
    cache.invalidate(uid, "kcal_days")


# ------------------------------------------------------------------ analytics
async def period_payload(profile: Profile, days: int) -> dict[str, Any]:
    """Финансы + калории за период (для дашборда/отчёта)."""
    end = profile.today
    start = end - timedelta(days=max(1, days) - 1)
    entries, logs = await asyncio.gather(finance_entries(profile.telegram_id), calorie_logs(profile, days))
    return {
        "start_date": start,
        "end_date": end,
        "finance_entries": fin.entries_between(entries, start, end),
        "all_finance_entries": entries,
        "calorie_logs": logs,
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ v2: settings / budgets / recurring
async def user_settings(uid: int) -> dict[str, Any]:
    return await cache.remember(uid, ("user_settings",), 600, lambda: db.get_user_settings(uid))


async def save_user_settings(uid: int, fields: dict[str, Any]) -> None:
    await db.save_user_settings(uid, fields)
    cache.invalidate(uid, "user_settings")


async def budgets(uid: int) -> dict[str, float]:
    if not db.available("budgets"):
        return {}
    return await cache.remember(uid, ("budgets",), 600, lambda: db.list_budgets(uid))


async def set_budget(uid: int, category: str, limit: float) -> None:
    await db.set_budget(uid, category, limit)
    cache.invalidate(uid, "budgets")


async def recurring(uid: int) -> list[dict[str, Any]]:
    if not db.available("recurring_payments"):
        return []
    return await cache.remember(uid, ("recurring",), 600, lambda: db.list_recurring(uid))


def invalidate_recurring(uid: int) -> None:
    cache.invalidate(uid, "recurring")


async def month_budget_statuses(profile: Profile) -> list[fin.BudgetStatus]:
    entries, limits = await asyncio.gather(finance_entries(profile.telegram_id), budgets(profile.telegram_id))
    if not limits:
        return []
    stats = fin.compute_stats(entries, fin.period_for("month", profile.today))
    return fin.budget_statuses(stats, limits)


# ------------------------------------------------------------------ reminders
async def reminders(uid: int) -> list[dict[str, Any]]:
    return await cache.remember(uid, ("reminders",), 600, lambda: db.list_reminders(uid))


def invalidate_reminders(uid: int) -> None:
    cache.invalidate(uid, "reminders")
