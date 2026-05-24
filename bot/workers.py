"""Фоновые рабочие задачи: напоминания, weekly-report."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .db import Database

logger = logging.getLogger(__name__)


async def reminder_worker(bot: Bot, db: Database, check_seconds: int = 60) -> None:
    """Проверяет напоминания каждые `check_seconds` секунд и отправляет due.

    `db.get_due_reminders(now_utc)` сам ставит `last_sent_key`, чтобы не отправлять
    одно и то же напоминание дважды в одну минуту.
    """
    interval = max(15, int(check_seconds))
    logger.info("Reminder worker started (interval=%ds)", interval)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            due = await asyncio.to_thread(db.get_due_reminders, now_utc)
            for reminder in due:
                telegram_id = int(reminder.get("telegram_id") or 0)
                text = str(reminder.get("reminder_text") or "").strip()
                if not telegram_id or not text:
                    continue
                try:
                    await bot.send_message(telegram_id, f"⏰ {text}")
                except TelegramForbiddenError:
                    logger.info("Skip reminder: user %s blocked the bot", telegram_id)
                except TelegramRetryAfter as exc:
                    logger.warning("Reminder flood control: sleep %.1fs", float(exc.retry_after))
                    await asyncio.sleep(float(exc.retry_after) + 1)
                except Exception:
                    logger.exception("Failed to send reminder to %s", telegram_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder worker iteration failed")
        await asyncio.sleep(interval)


__all__ = ["reminder_worker"]
