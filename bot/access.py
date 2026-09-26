"""Кто может пользоваться ботом: владелец (ALLOWED_TELEGRAM_IDS в .env) + приглашённые (bot_members).

Приглашение — одноразовая ссылка t.me/<бот>?start=inv_<code> на 7 дней. Её создаёт только
владелец (Настройки → 👥 Пользователи), он же удаляет людей. Данные у каждого свои (всё в базе
по telegram_id), приглашённым доступны звонки JES и будильник; публикация вакансий в канал
владельца — только владельцу.

Список участников держим в памяти (обновляется при изменениях и раз в 5 минут), чтобы проверка
доступа на каждом сообщении не ходила в базу.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .context import db, settings

logger = logging.getLogger(__name__)

INVITE_DAYS = 7
INVITE_PREFIX = "inv_"
REFRESH_SECONDS = 300

_members: set[int] = set()
_loaded_at = 0.0


def is_owner(uid: int | None) -> bool:
    """Владелец — тот, кто прописан в .env. Если список пуст (бот для всех) — владельцев нет."""
    return uid is not None and int(uid) in settings.allowed_telegram_ids


def is_member(uid: int | None) -> bool:
    return uid is not None and int(uid) in _members


def is_allowed(uid: int | None) -> bool:
    return settings.is_allowed(uid) or is_member(uid)


def user_ids() -> list[int]:
    """Все, кому бот что-то шлёт сам (сводки, напоминания, будильник): владелец + приглашённые."""
    return sorted(set(settings.allowed_telegram_ids) | _members)


async def refresh(force: bool = False) -> None:
    global _members, _loaded_at
    if not force and time.monotonic() - _loaded_at < REFRESH_SECONDS:
        return
    if not db.available("bot_members"):
        return
    try:
        _members = {int(r["telegram_id"]) for r in await db.list_members()}
        _loaded_at = time.monotonic()
    except Exception:
        logger.warning("members refresh failed", exc_info=True)


async def members() -> list[dict[str, Any]]:
    if not db.available("bot_members"):
        return []
    return await db.list_members()


async def create_invite(owner_id: int) -> str:
    code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    await db.create_invite(code, created_by=owner_id, expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS))
    return code


def invite_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={INVITE_PREFIX}{code}"


def invite_code(text: str | None) -> str | None:
    """«/start inv_abc» → «abc»."""
    parts = str(text or "").strip().split(maxsplit=1)
    if len(parts) == 2 and parts[0].split("@")[0] == "/start" and parts[1].startswith(INVITE_PREFIX):
        return parts[1][len(INVITE_PREFIX):].strip() or None
    return None


async def redeem(code: str, *, telegram_id: int, first_name: str | None, username: str | None) -> str:
    """Принять приглашение. Возвращает ok | already | invalid | expired | used."""
    if is_allowed(telegram_id):
        return "already"
    if not db.available("bot_invites"):
        return "invalid"
    row = await db.get_invite(code)
    if not row:
        return "invalid"
    if row.get("used_by"):
        return "used"
    try:
        expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "invalid"
    if expires < datetime.now(timezone.utc):
        return "expired"
    if not await db.use_invite(code, telegram_id):
        return "used"
    await db.add_member(telegram_id, first_name=first_name, username=username, invited_by=row.get("created_by"))
    _members.add(int(telegram_id))
    logger.info("member %s joined by invite from %s", telegram_id, row.get("created_by"))
    return "ok"


async def remove(telegram_id: int) -> None:
    await db.remove_member(telegram_id)
    _members.discard(int(telegram_id))


__all__ = ["is_owner", "is_member", "is_allowed", "user_ids", "refresh", "members", "create_invite", "invite_link",
           "invite_code", "redeem", "remove", "INVITE_DAYS"]
