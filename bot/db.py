"""Асинхронный слой доступа к Supabase.

Все методы — `async` и не блокируют event loop (используется нативный
async-клиент supabase-py). Тяжёлые выборки (история финансов) читаются
постранично, чтобы не упираться в лимит PostgREST в 1000 строк.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from supabase import AsyncClient, AsyncClientOptions, create_async_client

from .config import Settings

logger = logging.getLogger(__name__)

PAGE = 1000

FINANCE_COLUMNS = "id,entry_type,amount,category,note,entry_date,created_at,source"
CALORIE_COLUMNS = "id,meal_desc,calories,protein,fat,carbs,confidence,created_at"


def zone(tz_name: str | None, default: str = "Asia/Tashkent") -> ZoneInfo | timezone:
    for key in (tz_name, default, "UTC"):
        if not key:
            continue
        try:
            return ZoneInfo(str(key))
        except Exception:
            continue
    return timezone.utc


def local_day_bounds_utc(day: date, tz: ZoneInfo | timezone) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.client: AsyncClient | None = None
        self.default_timezone = settings.app_timezone
        self.table_prefix = settings.db_table_prefix
        self.missing_tables: set[str] = set()

    async def connect(self) -> None:
        options = AsyncClientOptions(postgrest_client_timeout=25)
        self.client = await create_async_client(
            self._settings.supabase_url,
            self._settings.supabase_service_role_key,
            options=options,
        )

    def _t(self, name: str) -> str:
        return f"{self.table_prefix}{name}"

    def _table(self, name: str):
        assert self.client is not None, "Database.connect() was not awaited"
        return self.client.table(self._t(name))

    async def _all_pages(self, build: Callable[[], Any]) -> list[dict[str, Any]]:
        """Читает все строки запроса постранично. `build()` должен возвращать
        новый query-builder при каждом вызове (builder одноразовый)."""
        first = await build().range(0, PAGE - 1).execute()
        rows: list[dict[str, Any]] = list(first.data or [])
        total = getattr(first, "count", None)
        if total is None or total <= len(rows) or len(rows) < PAGE:
            return rows
        offsets = range(PAGE, int(total), PAGE)
        pages = await asyncio.gather(
            *(build().range(off, off + PAGE - 1).execute() for off in offsets)
        )
        for page in pages:
            rows.extend(page.data or [])
        return rows

    async def health_check(self) -> list[str]:
        """Проверяет наличие таблиц из миграций; возвращает список отсутствующих."""
        names = (
            "users", "finance_entries", "calorie_logs", "nutrition_profiles", "finance_settings", "report_preferences",
            "user_settings", "budgets", "recurring_payments",
        )

        async def probe(name: str) -> str | None:
            try:
                await self._table(name).select("telegram_id").limit(1).execute()
                return None
            except Exception as exc:
                logger.error("Table %s is not available: %s", self._t(name), str(exc)[:200])
                return self._t(name)

        results = await asyncio.gather(*(probe(n) for n in names))
        self.missing_tables = {r for r in results if r}
        return sorted(self.missing_tables)

    def available(self, name: str) -> bool:
        return self._t(name) not in self.missing_tables

    # ------------------------------------------------------------------ users
    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        res = await self._table("users").select("*").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None

    async def upsert_user(
        self,
        telegram_id: int,
        *,
        username: str | None,
        first_name: str | None,
        language: str,
        timezone_name: str,
        currency: str,
    ) -> dict[str, Any]:
        existing = await self.get_user(telegram_id)
        if existing:
            changes: dict[str, Any] = {}
            if (existing.get("username") or None) != (username or None):
                changes["username"] = username
            if (existing.get("first_name") or None) != (first_name or None):
                changes["first_name"] = first_name
            if changes:
                await self._table("users").update(changes).eq("telegram_id", telegram_id).execute()
                existing.update(changes)
            return existing
        payload = {
            "telegram_id": telegram_id,
            "username": username,
            "first_name": first_name,
            "language": language,
            "timezone": timezone_name,
            "currency": currency,
        }
        await self._table("users").upsert(payload, on_conflict="telegram_id").execute()
        return payload

    async def update_user_language(self, telegram_id: int, language: str) -> None:
        lang = (language or "ru").strip().lower()
        if lang not in {"ru", "uz"}:
            lang = "ru"
        await self._table("users").update({"language": lang}).eq("telegram_id", telegram_id).execute()

    async def get_screen_message_id(self, telegram_id: int) -> int | None:
        try:
            res = await self._table("users").select("screen_message_id").eq("telegram_id", telegram_id).limit(1).execute()
            rows = res.data or []
            if rows and rows[0].get("screen_message_id") is not None:
                return int(rows[0]["screen_message_id"])
        except Exception:
            logger.debug("screen_message_id column is not available")
        return None

    async def set_screen_message_id(self, telegram_id: int, message_id: int | None) -> None:
        try:
            await self._table("users").update({"screen_message_id": message_id}).eq("telegram_id", telegram_id).execute()
        except Exception:
            logger.debug("screen_message_id column is not available")

    async def list_users(self) -> list[dict[str, Any]]:
        res = await self._table("users").select("telegram_id,timezone,currency,language,first_name").execute()
        return res.data or []

    # -------------------------------------------------------------- nutrition
    @staticmethod
    def _shape_nutrition_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "mode": row.get("mode"),
            "title": row.get("title"),
            "daily_calories": int(row.get("daily_calories") or 0),
            "protein": row.get("protein"),
            "fat": row.get("fat"),
            "carbs": row.get("carbs"),
            "weight": row.get("weight"),
            "height": row.get("height"),
            "age": row.get("age"),
            "bmi": row.get("bmi"),
            "tdee": row.get("tdee"),
        }

    async def get_nutrition_profile(self, telegram_id: int) -> dict[str, Any] | None:
        res = await self._table("nutrition_profiles").select("*").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        return self._shape_nutrition_row(rows[0]) if rows else None

    async def save_nutrition_profile(self, telegram_id: int, profile: dict[str, Any]) -> None:
        def _num(key: str) -> float | None:
            value = profile.get(key)
            return float(value) if value is not None else None

        payload = {
            "telegram_id": telegram_id,
            "mode": profile.get("mode"),
            "title": profile.get("title"),
            "daily_calories": int(profile.get("daily_calories") or 0),
            "protein": _num("protein"),
            "fat": _num("fat"),
            "carbs": _num("carbs"),
            "weight": _num("weight"),
            "height": _num("height"),
            "age": int(profile["age"]) if profile.get("age") is not None else None,
            "bmi": _num("bmi"),
            "tdee": int(profile["tdee"]) if profile.get("tdee") is not None else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._table("nutrition_profiles").upsert(payload, on_conflict="telegram_id").execute()

    async def add_calorie_logs(self, telegram_id: int, items: list[dict[str, Any]]) -> None:
        payload = [
            {
                "telegram_id": telegram_id,
                "photo_url": item.get("photo_url"),
                "meal_desc": str(item.get("meal_desc") or "").strip(),
                "calories": item.get("calories"),
                "protein": item.get("protein"),
                "fat": item.get("fat"),
                "carbs": item.get("carbs"),
                "confidence": item.get("confidence"),
                "advice": item.get("advice"),
            }
            for item in items
            if str(item.get("meal_desc") or "").strip()
        ]
        if not payload:
            return
        await self._table("calorie_logs").insert(payload).execute()

    async def list_calorie_logs_between(
        self, telegram_id: int, start_utc: datetime, end_utc: datetime, *, columns: str = CALORIE_COLUMNS
    ) -> list[dict[str, Any]]:
        def build():
            return (
                self._table("calorie_logs")
                .select(columns, count="exact")
                .eq("telegram_id", telegram_id)
                .gte("created_at", start_utc.isoformat())
                .lt("created_at", end_utc.isoformat())
                .order("created_at", desc=True)
            )

        return await self._all_pages(build)

    async def list_calorie_logs(self, telegram_id: int, *, days: int, tz_name: str | None = None) -> list[dict[str, Any]]:
        tz = zone(tz_name, self.default_timezone)
        today = datetime.now(tz).date()
        start_utc, _ = local_day_bounds_utc(today - timedelta(days=max(0, days - 1)), tz)
        _, end_utc = local_day_bounds_utc(today, tz)
        return await self.list_calorie_logs_between(telegram_id, start_utc, end_utc)

    async def get_calorie_log(self, telegram_id: int, log_id: str | int) -> dict[str, Any] | None:
        res = await self._table("calorie_logs").select("*").eq("telegram_id", telegram_id).eq("id", log_id).limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None

    async def delete_calorie_log(self, telegram_id: int, log_id: str | int) -> None:
        await self._table("calorie_logs").delete().eq("telegram_id", telegram_id).eq("id", log_id).execute()

    # ---------------------------------------------------------------- finance
    async def get_finance_settings(self, telegram_id: int) -> dict[str, float]:
        res = await self._table("finance_settings").select("*").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        row = rows[0] if rows else {}
        return {
            "card_base": float(row.get("card_base") or 0.0),
            "cash_base": float(row.get("cash_base") or 0.0),
            "lent_base": float(row.get("lent_base") or 0.0),
            "debt_base": float(row.get("debt_base") or 0.0),
            "monthly_credit_payment": float(row.get("monthly_credit_payment") or 0.0),
        }

    async def save_finance_settings(self, telegram_id: int, payload: dict[str, float]) -> None:
        await self._table("finance_settings").upsert(
            {
                "telegram_id": telegram_id,
                "card_base": float(payload.get("card_base") or 0.0),
                "cash_base": float(payload.get("cash_base") or 0.0),
                "lent_base": float(payload.get("lent_base") or 0.0),
                "debt_base": float(payload.get("debt_base") or 0.0),
                "monthly_credit_payment": float(payload.get("monthly_credit_payment") or 0.0),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="telegram_id",
        ).execute()

    async def add_finance_entries(
        self,
        telegram_id: int,
        entries: list[dict[str, Any]],
        *,
        entry_date: date,
        source: str = "manual",
    ) -> list[dict[str, Any]]:
        payload = [
            {
                "telegram_id": telegram_id,
                "entry_type": str(entry.get("entry_type") or "expense"),
                "amount": float(entry.get("amount") or 0),
                "category": str(entry.get("category") or "other"),
                "note": entry.get("note"),
                "entry_date": entry_date.isoformat(),
                "source": str(entry.get("source") or source),
            }
            for entry in entries
            if float(entry.get("amount") or 0) > 0
        ]
        if not payload:
            return []
        res = await self._table("finance_entries").insert(payload).execute()
        return res.data or []

    async def list_finance_entries_all(self, telegram_id: int) -> list[dict[str, Any]]:
        """Вся история (компактные колонки), новые сверху."""

        def build():
            return (
                self._table("finance_entries")
                .select(FINANCE_COLUMNS, count="exact")
                .eq("telegram_id", telegram_id)
                .order("entry_date", desc=True)
                .order("created_at", desc=True)
            )

        return await self._all_pages(build)

    async def get_finance_entry(self, telegram_id: int, entry_id: str | int) -> dict[str, Any] | None:
        res = await self._table("finance_entries").select("*").eq("telegram_id", telegram_id).eq("id", entry_id).limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None

    async def update_finance_entry(self, telegram_id: int, entry_id: str | int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        await self._table("finance_entries").update(fields).eq("telegram_id", telegram_id).eq("id", entry_id).execute()

    async def delete_finance_entry(self, telegram_id: int, entry_id: str | int) -> None:
        await self._table("finance_entries").delete().eq("telegram_id", telegram_id).eq("id", entry_id).execute()

    # ---------------------------------------------------------------- reports
    async def get_report_preferences(self, telegram_id: int) -> dict[str, Any]:
        res = await self._table("report_preferences").select("*").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        if not rows:
            return {"enabled": True, "frequency": "weekly", "last_sent_key": None}
        row = rows[0]
        frequency = str(row.get("frequency") or "weekly").strip().lower()
        if frequency not in {"weekly", "monthly"}:
            frequency = "weekly"
        return {
            "enabled": bool(row.get("enabled", True)),
            "frequency": frequency,
            "last_sent_key": (str(row.get("last_sent_key") or "").strip() or None),
        }

    async def save_report_preferences(
        self, telegram_id: int, *, enabled: bool, frequency: str, last_sent_key: str | None = None
    ) -> dict[str, Any]:
        freq = str(frequency or "weekly").strip().lower()
        if freq not in {"weekly", "monthly"}:
            freq = "weekly"
        await self._table("report_preferences").upsert(
            {
                "telegram_id": telegram_id,
                "enabled": bool(enabled),
                "frequency": freq,
                "last_sent_key": last_sent_key or None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="telegram_id",
        ).execute()
        return {"enabled": bool(enabled), "frequency": freq, "last_sent_key": last_sent_key or None}

    # ---------------------------------------------------------- v2: settings
    async def get_user_settings(self, telegram_id: int) -> dict[str, Any]:
        res = await self._table("user_settings").select("*").eq("telegram_id", telegram_id).limit(1).execute()
        rows = res.data or []
        row = rows[0] if rows else {}
        return {
            "brief_morning": bool(row.get("brief_morning", True)),
            "brief_evening": bool(row.get("brief_evening", True)),
            "brief_morning_time": str(row.get("brief_morning_time") or "08:00")[:5],
            "brief_evening_time": str(row.get("brief_evening_time") or "21:00")[:5],
            "last_morning_key": row.get("last_morning_key"),
            "last_evening_key": row.get("last_evening_key"),
        }

    async def save_user_settings(self, telegram_id: int, fields: dict[str, Any]) -> None:
        payload = {"telegram_id": telegram_id, "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
        await self._table("user_settings").upsert(payload, on_conflict="telegram_id").execute()

    # ---------------------------------------------------------- v2: budgets
    async def list_budgets(self, telegram_id: int) -> dict[str, float]:
        res = await self._table("budgets").select("category,monthly_limit").eq("telegram_id", telegram_id).execute()
        return {str(r["category"]): float(r["monthly_limit"]) for r in (res.data or []) if r.get("category")}

    async def set_budget(self, telegram_id: int, category: str, monthly_limit: float) -> None:
        if monthly_limit <= 0:
            await self._table("budgets").delete().eq("telegram_id", telegram_id).eq("category", category).execute()
            return
        await self._table("budgets").upsert(
            {"telegram_id": telegram_id, "category": category, "monthly_limit": float(monthly_limit)},
            on_conflict="telegram_id,category",
        ).execute()

    # ---------------------------------------------------------- v2: recurring
    async def list_recurring(self, telegram_id: int) -> list[dict[str, Any]]:
        res = await self._table("recurring_payments").select("*").eq("telegram_id", telegram_id).order("day_of_month").execute()
        return res.data or []

    async def list_recurring_all(self) -> list[dict[str, Any]]:
        res = await self._table("recurring_payments").select("*").eq("enabled", True).execute()
        return res.data or []

    async def add_recurring(self, telegram_id: int, *, title: str, amount: float, category: str, bucket: str, day_of_month: int) -> dict[str, Any]:
        res = await self._table("recurring_payments").insert(
            {
                "telegram_id": telegram_id,
                "title": title,
                "amount": float(amount),
                "category": category,
                "bucket": bucket,
                "day_of_month": int(day_of_month),
                "enabled": True,
            }
        ).execute()
        rows = res.data or []
        return rows[0] if rows else {}

    async def update_recurring(self, telegram_id: int, rec_id: str | int, fields: dict[str, Any]) -> None:
        await self._table("recurring_payments").update(fields).eq("telegram_id", telegram_id).eq("id", rec_id).execute()

    async def get_recurring(self, telegram_id: int, rec_id: str | int) -> dict[str, Any] | None:
        res = await self._table("recurring_payments").select("*").eq("telegram_id", telegram_id).eq("id", rec_id).limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None

    async def delete_recurring(self, telegram_id: int, rec_id: str | int) -> None:
        await self._table("recurring_payments").delete().eq("telegram_id", telegram_id).eq("id", rec_id).execute()
