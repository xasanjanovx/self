from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from supabase import Client, create_client

from .config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self.client: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)
        self.default_timezone = settings.app_timezone

    def _zone(self, tz_name: str | None = None) -> ZoneInfo | timezone:
        key = str(tz_name or self.default_timezone or "UTC")
        try:
            return ZoneInfo(key)
        except Exception:
            try:
                return ZoneInfo("UTC")
            except Exception:
                return timezone.utc

    def ensure_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        language: str = "ru",
        timezone_name: str = "Asia/Tashkent",
        currency: str = "UZS",
    ) -> None:
        existing = self.get_user(telegram_id)
        payload = {"telegram_id": telegram_id, "username": username, "first_name": first_name}
        if not existing:
            payload["language"] = language
            payload["timezone"] = timezone_name
            payload["currency"] = currency
        self.client.table("users").upsert(payload, on_conflict="telegram_id").execute()

    def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        data = (
            self.client.table("users")
            .select("*")
            .eq("telegram_id", telegram_id)
            .limit(1)
            .execute()
            .data
        )
        return data[0] if data else None

    def get_nutrition_profile(self, telegram_id: int) -> dict[str, Any] | None:
        goals = self.list_goals(telegram_id, only_active=False)
        prefix = "NUTRI_V1:"
        for goal in goals:
            title = str(goal.get("title") or "")
            if goal.get("goal_type") != "weight" or not title.startswith(prefix):
                continue
            raw = title[len(prefix) :]
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            payload["goal_id"] = goal.get("id")
            payload["daily_calories"] = int(payload.get("daily_calories") or goal.get("target_value") or 0)
            return payload
        return None

    def save_nutrition_profile(self, telegram_id: int, profile: dict[str, Any]) -> None:
        existing = self.get_nutrition_profile(telegram_id)
        payload = dict(profile)
        payload["daily_calories"] = int(payload.get("daily_calories") or 0)
        title = "NUTRI_V1:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        if existing and existing.get("goal_id"):
            self.client.table("goals").update(
                {
                    "goal_type": "weight",
                    "title": title,
                    "target_value": float(payload["daily_calories"]),
                    "active": True,
                }
            ).eq("telegram_id", telegram_id).eq("id", existing["goal_id"]).execute()
            return

        self.client.table("goals").insert(
            {
                "telegram_id": telegram_id,
                "goal_type": "weight",
                "title": title,
                "target_value": float(payload["daily_calories"]),
                "active": True,
            }
        ).execute()

    def add_goal(self, telegram_id: int, goal_type: str, title: str, target_value: float | None) -> None:
        self.client.table("goals").insert(
            {
                "telegram_id": telegram_id,
                "goal_type": goal_type,
                "title": title,
                "target_value": target_value,
            }
        ).execute()

    def list_goals(self, telegram_id: int, only_active: bool = True) -> list[dict[str, Any]]:
        query = self.client.table("goals").select("*").eq("telegram_id", telegram_id).order("created_at", desc=False)
        if only_active:
            query = query.eq("active", True)
        return query.execute().data or []

    def add_habit(self, telegram_id: int, name: str, target_per_week: int = 7) -> None:
        self.client.table("habits").insert(
            {
                "telegram_id": telegram_id,
                "name": name,
                "target_per_week": target_per_week,
            }
        ).execute()

    def list_habits(self, telegram_id: int, only_active: bool = True) -> list[dict[str, Any]]:
        query = self.client.table("habits").select("*").eq("telegram_id", telegram_id).order("created_at", desc=False)
        if only_active:
            query = query.eq("active", True)
        return query.execute().data or []

    def mark_habit_done(self, telegram_id: int, habit_id: str, log_date: date | None = None, note: str | None = None) -> None:
        if log_date is None:
            log_date = date.today()

        self.client.table("habit_logs").upsert(
            {
                "habit_id": habit_id,
                "telegram_id": telegram_id,
                "log_date": log_date.isoformat(),
                "completed": True,
                "note": note,
            },
            on_conflict="habit_id,log_date",
        ).execute()

    def list_today_habits(self, telegram_id: int, tz_name: str | None = None) -> list[dict[str, Any]]:
        habits = self.list_habits(telegram_id)
        if not habits:
            return []

        tz = self._zone(tz_name)
        today = datetime.now(tz).date().isoformat()

        logs = (
            self.client.table("habit_logs")
            .select("habit_id")
            .eq("telegram_id", telegram_id)
            .eq("log_date", today)
            .execute()
            .data
            or []
        )
        completed_ids = {entry["habit_id"] for entry in logs}

        for habit in habits:
            habit["completed_today"] = habit["id"] in completed_ids

        return habits

    def add_finance_entry(
        self,
        telegram_id: int,
        entry_type: str,
        amount: float,
        category: str,
        note: str | None = None,
        entry_date: date | None = None,
        source: str = "manual",
    ) -> None:
        if entry_date is None:
            entry_date = date.today()
        self.client.table("finance_entries").insert(
            {
                "telegram_id": telegram_id,
                "entry_type": entry_type,
                "amount": amount,
                "category": category,
                "note": note,
                "entry_date": entry_date.isoformat(),
                "source": source,
            }
        ).execute()

    def list_finance_entries(self, telegram_id: int, days: int = 7) -> list[dict[str, Any]]:
        start = (date.today() - timedelta(days=days - 1)).isoformat()
        return (
            self.client.table("finance_entries")
            .select("*")
            .eq("telegram_id", telegram_id)
            .gte("entry_date", start)
            .order("entry_date", desc=True)
            .execute()
            .data
            or []
        )

    def list_today_finance_entries(self, telegram_id: int, tz_name: str | None = None) -> list[dict[str, Any]]:
        tz = self._zone(tz_name)
        local_today = datetime.now(tz).date().isoformat()
        return (
            self.client.table("finance_entries")
            .select("id,entry_type,amount,category,note,entry_date,created_at")
            .eq("telegram_id", telegram_id)
            .eq("entry_date", local_today)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )

    def delete_finance_entry(self, telegram_id: int, entry_id: str | int) -> None:
        self.client.table("finance_entries").delete().eq("telegram_id", telegram_id).eq("id", entry_id).execute()

    def get_finance_entry(self, telegram_id: int, entry_id: str | int) -> dict[str, Any] | None:
        rows = (
            self.client.table("finance_entries")
            .select("*")
            .eq("telegram_id", telegram_id)
            .eq("id", entry_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    def list_finance_entries_all(self, telegram_id: int) -> list[dict[str, Any]]:
        return (
            self.client.table("finance_entries")
            .select("id,entry_type,amount,category,note,entry_date,created_at")
            .eq("telegram_id", telegram_id)
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )

    def get_today_finance_totals(self, telegram_id: int, tz_name: str | None = None) -> dict[str, float]:
        tz = self._zone(tz_name)
        local_today = datetime.now(tz).date().isoformat()
        rows = (
            self.client.table("finance_entries")
            .select("entry_type,amount")
            .eq("telegram_id", telegram_id)
            .eq("entry_date", local_today)
            .execute()
            .data
            or []
        )

        income = Decimal("0")
        expense = Decimal("0")
        for row in rows:
            amount = Decimal(str(row.get("amount") or "0"))
            if row.get("entry_type") == "income":
                income += amount
            else:
                expense += amount

        return {
            "income": float(income),
            "expense": float(expense),
            "count": float(len(rows)),
        }

    def add_daily_checkin(
        self,
        telegram_id: int,
        checkin_date: date,
        mood: int | None,
        energy: int | None,
        weight: float | None,
        note: str | None,
    ) -> None:
        self.client.table("daily_checkins").upsert(
            {
                "telegram_id": telegram_id,
                "checkin_date": checkin_date.isoformat(),
                "mood": mood,
                "energy": energy,
                "weight": weight,
                "note": note,
            },
            on_conflict="telegram_id,checkin_date",
        ).execute()

    def list_checkins(self, telegram_id: int, days: int = 30) -> list[dict[str, Any]]:
        start = (date.today() - timedelta(days=days - 1)).isoformat()
        return (
            self.client.table("daily_checkins")
            .select("*")
            .eq("telegram_id", telegram_id)
            .gte("checkin_date", start)
            .order("checkin_date", desc=True)
            .execute()
            .data
            or []
        )

    def has_checkin_today(self, telegram_id: int, tz_name: str | None = None) -> bool:
        tz = self._zone(tz_name)
        local_today = datetime.now(tz).date().isoformat()
        rows = (
            self.client.table("daily_checkins")
            .select("id")
            .eq("telegram_id", telegram_id)
            .eq("checkin_date", local_today)
            .limit(1)
            .execute()
            .data
            or []
        )
        return bool(rows)

    def get_checkin_streak(self, telegram_id: int, tz_name: str | None = None) -> int:
        tz = self._zone(tz_name)
        today = datetime.now(tz).date()

        checkins = self.list_checkins(telegram_id, days=120)
        days_set = {date.fromisoformat(item["checkin_date"]) for item in checkins}

        streak = 0
        cursor = today
        while cursor in days_set:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    def add_calorie_log(
        self,
        telegram_id: int,
        photo_url: str | None,
        meal_desc: str,
        calories: int | None,
        protein: float | None,
        fat: float | None,
        carbs: float | None,
        confidence: float | None,
        advice: str | None,
    ) -> None:
        self.client.table("calorie_logs").insert(
            {
                "telegram_id": telegram_id,
                "photo_url": photo_url,
                "meal_desc": meal_desc,
                "calories": calories,
                "protein": protein,
                "fat": fat,
                "carbs": carbs,
                "confidence": confidence,
                "advice": advice,
            }
        ).execute()

    def list_calorie_logs(self, telegram_id: int, days: int = 7) -> list[dict[str, Any]]:
        start = datetime.now(timezone.utc) - timedelta(days=days)
        return (
            self.client.table("calorie_logs")
            .select("*")
            .eq("telegram_id", telegram_id)
            .gte("created_at", start.isoformat())
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )

    def list_today_calorie_entries(self, telegram_id: int, tz_name: str | None = None) -> list[dict[str, Any]]:
        tz = self._zone(tz_name)
        local_day = datetime.now(tz).date()
        local_start = datetime.combine(local_day, time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        utc_start = local_start.astimezone(timezone.utc)
        utc_end = local_end.astimezone(timezone.utc)

        return (
            self.client.table("calorie_logs")
            .select("id,meal_desc,calories,created_at")
            .eq("telegram_id", telegram_id)
            .gte("created_at", utc_start.isoformat())
            .lt("created_at", utc_end.isoformat())
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )

    def delete_calorie_log(self, telegram_id: int, log_id: str | int) -> None:
        self.client.table("calorie_logs").delete().eq("telegram_id", telegram_id).eq("id", log_id).execute()

    def get_calorie_log(self, telegram_id: int, log_id: str | int) -> dict[str, Any] | None:
        rows = (
            self.client.table("calorie_logs")
            .select("*")
            .eq("telegram_id", telegram_id)
            .eq("id", log_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    def get_today_nutrition_totals(self, telegram_id: int, tz_name: str | None = None) -> dict[str, float]:
        tz = self._zone(tz_name)
        local_day = datetime.now(tz).date()
        local_start = datetime.combine(local_day, time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        utc_start = local_start.astimezone(timezone.utc)
        utc_end = local_end.astimezone(timezone.utc)

        rows = (
            self.client.table("calorie_logs")
            .select("calories,protein,fat,carbs")
            .eq("telegram_id", telegram_id)
            .gte("created_at", utc_start.isoformat())
            .lt("created_at", utc_end.isoformat())
            .execute()
            .data
            or []
        )

        totals = {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "meals": 0.0}
        for row in rows:
            totals["meals"] += 1
            if row.get("calories") is not None:
                totals["calories"] += float(row["calories"])
            if row.get("protein") is not None:
                totals["protein"] += float(row["protein"])
            if row.get("fat") is not None:
                totals["fat"] += float(row["fat"])
            if row.get("carbs") is not None:
                totals["carbs"] += float(row["carbs"])
        return totals

    def get_today_calorie_totals(self, telegram_id: int, tz_name: str | None = None) -> dict[str, float]:
        tz = self._zone(tz_name)
        local_day = datetime.now(tz).date()
        local_start = datetime.combine(local_day, time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        utc_start = local_start.astimezone(timezone.utc)
        utc_end = local_end.astimezone(timezone.utc)

        rows = (
            self.client.table("calorie_logs")
            .select("calories")
            .eq("telegram_id", telegram_id)
            .gte("created_at", utc_start.isoformat())
            .lt("created_at", utc_end.isoformat())
            .execute()
            .data
            or []
        )

        calories_sum = 0.0
        for row in rows:
            if row.get("calories") is None:
                continue
            calories_sum += float(row["calories"])

        return {
            "calories": calories_sum,
            "meals": float(len(rows)),
        }

    def add_reminder(self, telegram_id: int, text: str, reminder_time: str, days_of_week: list[int], tz_name: str) -> None:
        self.client.table("reminders").insert(
            {
                "telegram_id": telegram_id,
                "reminder_text": text,
                "reminder_time": reminder_time,
                "days_of_week": days_of_week,
                "timezone": tz_name,
                "enabled": True,
            }
        ).execute()

    def list_reminders(self, telegram_id: int) -> list[dict[str, Any]]:
        return (
            self.client.table("reminders")
            .select("*")
            .eq("telegram_id", telegram_id)
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )

    def delete_reminder(self, reminder_id: str, telegram_id: int) -> None:
        self.client.table("reminders").delete().eq("id", reminder_id).eq("telegram_id", telegram_id).execute()

    def get_due_reminders(self, now_utc: datetime) -> list[dict[str, Any]]:
        reminders = self.client.table("reminders").select("*").eq("enabled", True).execute().data or []
        due: list[dict[str, Any]] = []

        for reminder in reminders:
            tz = self._zone(str(reminder.get("timezone") or self.default_timezone))

            local_now = now_utc.astimezone(tz)
            weekdays = {int(day) for day in (reminder.get("days_of_week") or [])}
            if (local_now.weekday() + 1) not in weekdays:
                continue

            reminder_time = str(reminder.get("reminder_time") or "")
            parts = reminder_time.split(":")
            if len(parts) < 2:
                continue

            hour = int(parts[0])
            minute = int(parts[1])

            if local_now.hour != hour or local_now.minute != minute:
                continue

            sent_key = f"{local_now.date().isoformat()}-{hour:02d}:{minute:02d}"
            if reminder.get("last_sent_key") == sent_key:
                continue

            reminder["_sent_key"] = sent_key
            due.append(reminder)

        for reminder in due:
            self.client.table("reminders").update({"last_sent_key": reminder["_sent_key"]}).eq("id", reminder["id"]).execute()

        return due

    def claim_weekly_report(self, telegram_id: int, iso_year: int, iso_week: int) -> bool:
        existing = (
            self.client.table("weekly_report_runs")
            .select("id")
            .eq("telegram_id", telegram_id)
            .eq("iso_year", iso_year)
            .eq("iso_week", iso_week)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing:
            return False

        self.client.table("weekly_report_runs").insert(
            {"telegram_id": telegram_id, "iso_year": iso_year, "iso_week": iso_week}
        ).execute()
        return True

    def list_users(self) -> list[dict[str, Any]]:
        return self.client.table("users").select("telegram_id,timezone,currency").execute().data or []

    def get_weekly_payload(self, telegram_id: int, end_date: date | None = None) -> dict[str, Any]:
        if end_date is None:
            end_date = date.today()
        start_date = end_date - timedelta(days=6)

        finance_entries = (
            self.client.table("finance_entries")
            .select("*")
            .eq("telegram_id", telegram_id)
            .gte("entry_date", start_date.isoformat())
            .lte("entry_date", end_date.isoformat())
            .order("entry_date", desc=False)
            .execute()
            .data
            or []
        )

        checkins = (
            self.client.table("daily_checkins")
            .select("*")
            .eq("telegram_id", telegram_id)
            .gte("checkin_date", start_date.isoformat())
            .lte("checkin_date", end_date.isoformat())
            .order("checkin_date", desc=False)
            .execute()
            .data
            or []
        )

        habit_logs = (
            self.client.table("habit_logs")
            .select("*")
            .eq("telegram_id", telegram_id)
            .gte("log_date", start_date.isoformat())
            .lte("log_date", end_date.isoformat())
            .order("log_date", desc=False)
            .execute()
            .data
            or []
        )

        habits = self.list_habits(telegram_id)

        start_dt = datetime.combine(start_date, time.min).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(end_date + timedelta(days=1), time.min).replace(tzinfo=timezone.utc)
        calorie_logs = (
            self.client.table("calorie_logs")
            .select("*")
            .eq("telegram_id", telegram_id)
            .gte("created_at", start_dt.isoformat())
            .lt("created_at", end_dt.isoformat())
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )

        goals = self.list_goals(telegram_id)

        return {
            "start_date": start_date,
            "end_date": end_date,
            "finance_entries": finance_entries,
            "checkins": checkins,
            "habit_logs": habit_logs,
            "habits": habits,
            "calorie_logs": calorie_logs,
            "goals": goals,
        }

    def get_ai_context(self, telegram_id: int) -> dict[str, Any]:
        finance_entries = self.list_finance_entries(telegram_id, days=30)
        checkins = self.list_checkins(telegram_id, days=30)
        calorie_logs = self.list_calorie_logs(telegram_id, days=30)
        habits = self.list_habits(telegram_id)

        income_total = Decimal("0")
        expense_total = Decimal("0")
        category_totals: dict[str, Decimal] = {}

        for entry in finance_entries:
            amount = Decimal(str(entry.get("amount") or "0"))
            if entry.get("entry_type") == "income":
                income_total += amount
            else:
                expense_total += amount
                category = str(entry.get("category") or "прочее").lower()
                category_totals[category] = category_totals.get(category, Decimal("0")) + amount

        top_categories = sorted(category_totals.items(), key=lambda x: x[1], reverse=True)[:5]
        checkin_streak = self.get_checkin_streak(telegram_id)

        return {
            "income_total_30d": float(income_total),
            "expense_total_30d": float(expense_total),
            "net_30d": float(income_total - expense_total),
            "top_expense_categories": [(name, float(value)) for name, value in top_categories],
            "checkins_count_30d": len(checkins),
            "calorie_logs_count_30d": len(calorie_logs),
            "habits_count": len(habits),
            "checkin_streak": checkin_streak,
        }
