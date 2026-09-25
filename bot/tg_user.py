"""Telegram от имени владельца (его личный аккаунт) — для голосового Nurai на телефоне.

Бот и аккаунт-помощник «Nurai» не могут писать маме от лица пользователя, поэтому здесь
отдельный Telethon-клиент под его собственным аккаунтом: найти чат, отправить сообщение,
прочитать непрочитанное.

Вход — из приложения на телефоне (номер → код из Telegram → облачный пароль, если есть):
см. login_start / login_finish. Сессия хранится в DATA_DIR/tg_user.session (том docker-compose,
переживает пересборку) или задаётся переменной TG_USER_SESSION. API id/hash — TG_USER_API_ID /
TG_USER_API_HASH, по умолчанию те же, что у аккаунта-помощника (TG_CALLER_API_ID / _HASH).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import names

logger = logging.getLogger(__name__)

DIALOGS_TTL = 300.0
DIALOGS_LIMIT = 300

_client: Any = None
_me: Any = None
_lock = asyncio.Lock()
_login: dict[str, Any] = {}
_dialogs: tuple[float, list[dict[str, Any]]] = (0.0, [])


def data_dir() -> Path:
    path = Path(os.getenv("DATA_DIR") or "/app/data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_file() -> Path:
    return data_dir() / "tg_user.session"


def _creds() -> tuple[int, str] | None:
    api_id = os.getenv("TG_USER_API_ID") or os.getenv("TG_CALLER_API_ID")
    api_hash = os.getenv("TG_USER_API_HASH") or os.getenv("TG_CALLER_API_HASH")
    if not api_id or not api_hash:
        return None
    try:
        return int(api_id), api_hash.strip()
    except ValueError:
        return None


def _saved_session() -> str:
    try:
        text = _session_file().read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or (os.getenv("TG_USER_SESSION") or "").strip()


def configured() -> bool:
    return _creds() is not None and bool(_saved_session())


def _new_client(session: str = "") -> Any:
    from telethon import TelegramClient  # type: ignore
    from telethon.sessions import StringSession  # type: ignore

    creds = _creds()
    if creds is None:
        raise RuntimeError("нет TG_CALLER_API_ID / TG_CALLER_API_HASH")
    kwargs: dict[str, Any] = {"device_model": "Jarvis Phone", "app_version": "1.0"}
    try:
        return TelegramClient(StringSession(session), creds[0], creds[1], receive_updates=False, **kwargs)
    except TypeError:  # старый Telethon без receive_updates
        return TelegramClient(StringSession(session), creds[0], creds[1], **kwargs)


def display_name(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None) or "", getattr(entity, "last_name", None) or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (getattr(entity, "username", None) or "") or str(getattr(entity, "id", ""))


async def client() -> Any | None:
    """Подключённый и авторизованный клиент владельца или None (не вошёл / сессия отозвана)."""
    global _client, _me
    if _client is not None and _client.is_connected():
        return _client
    if not configured():
        return None
    async with _lock:
        if _client is not None and _client.is_connected():
            return _client
        c = _new_client(_saved_session())
        await c.connect()
        if not await c.is_user_authorized():
            await c.disconnect()
            logger.warning("tg_user: session is not authorized any more")
            return None
        _client, _me = c, await c.get_me()
        logger.info("tg_user connected as %s (id=%s)", display_name(_me), getattr(_me, "id", None))
        return _client


async def status() -> dict[str, Any]:
    if _creds() is None:
        return {"connected": False, "reason": "no_api_credentials"}
    if not configured():
        return {"connected": False, "pending_login": bool(_login)}
    try:
        c = await client()
    except Exception as exc:
        logger.warning("tg_user status failed", exc_info=True)
        return {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
    if c is None:
        return {"connected": False, "reason": "session_revoked"}
    return {"connected": True, "name": display_name(_me), "username": getattr(_me, "username", None)}


# ------------------------------------------------------------------ login (из приложения)
async def login_start(phone: str) -> dict[str, Any]:
    phone = "+" + "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(phone) < 8:
        return {"error": "Неверный номер"}
    await login_cancel()
    c = _new_client()
    await c.connect()
    sent = await c.send_code_request(phone)
    _login.update({"client": c, "phone": phone, "hash": sent.phone_code_hash, "at": time.monotonic()})
    logger.info("tg_user: login code requested (%s)", type(sent.type).__name__)
    return {"status": "code_sent"}


async def login_finish(code: str | None = None, password: str | None = None) -> dict[str, Any]:
    from telethon.errors import (PasswordHashInvalidError, PhoneCodeExpiredError,  # type: ignore
                                 PhoneCodeInvalidError, SessionPasswordNeededError)

    c = _login.get("client")
    if c is None or time.monotonic() - float(_login.get("at") or 0) > 600:
        await login_cancel()
        return {"error": "Вход устарел — начни заново с номера"}
    try:
        if password:
            await c.sign_in(password=password)
        else:
            digits = "".join(ch for ch in str(code or "") if ch.isdigit())
            if not digits:
                return {"error": "Введи код из Telegram"}
            await c.sign_in(_login["phone"], digits, phone_code_hash=_login["hash"])
    except SessionPasswordNeededError:
        return {"status": "password_needed"}
    except PhoneCodeInvalidError:
        return {"error": "Код неверный"}
    except PhoneCodeExpiredError:
        await login_cancel()
        return {"error": "Код устарел — запроси новый"}
    except PasswordHashInvalidError:
        return {"status": "password_needed", "error": "Пароль неверный"}
    session = c.session.save()
    _session_file().write_text(session, encoding="utf-8")
    try:
        os.chmod(_session_file(), 0o600)
    except OSError:
        pass
    me = await c.get_me()
    await c.disconnect()
    _login.clear()
    await _drop_client()
    logger.info("tg_user: logged in as %s", display_name(me))
    return {"status": "ok", "name": display_name(me)}


async def login_cancel() -> None:
    c = _login.get("client")
    _login.clear()
    if c is not None:
        try:
            await c.disconnect()
        except Exception:
            pass


async def logout() -> dict[str, Any]:
    c = await client()
    if c is not None:
        try:
            await c.log_out()
        except Exception:
            logger.warning("tg_user log_out failed", exc_info=True)
    _session_file().unlink(missing_ok=True)
    await _drop_client()
    return {"status": "logged_out"}


async def _drop_client() -> None:
    global _client, _me, _dialogs
    c, _client, _me, _dialogs = _client, None, None, (0.0, [])
    if c is not None:
        try:
            await c.disconnect()
        except Exception:
            pass


async def stop() -> None:
    await login_cancel()
    await _drop_client()


# ------------------------------------------------------------------ chats
async def dialogs(*, fresh: bool = False) -> list[dict[str, Any]]:
    """Личные чаты и группы владельца (без каналов): id, имя, username, непрочитанные."""
    global _dialogs
    stamp, cached = _dialogs
    if cached and not fresh and time.monotonic() - stamp < DIALOGS_TTL:
        return cached
    c = await client()
    if c is None:
        return []
    out: list[dict[str, Any]] = []
    async for d in c.iter_dialogs(limit=DIALOGS_LIMIT):
        if d.is_channel and not d.is_group:
            continue
        entity = d.entity
        if getattr(entity, "bot", False) or getattr(entity, "is_self", False) or getattr(entity, "deleted", False):
            continue
        out.append({
            "id": d.id,
            "name": display_name(entity),
            "username": getattr(entity, "username", None) or "",
            "kind": "group" if d.is_group else "user",
            "unread": int(d.unread_count or 0),
            "muted": bool(getattr(getattr(d.dialog, "notify_settings", None), "mute_until", None)),
            "_peer": d.input_entity,
        })
    _dialogs = (time.monotonic(), out)
    return out


def public(chat: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in chat.items() if not k.startswith("_")}


async def find_chat(queries: list[str], alias: str | None = None) -> dict[str, Any]:
    """{"match": чат} | {} — лучший чат сразу, без «кому именно?»: сначала люди, недавние переписки чуть выше."""
    chats = await dialogs()
    people = [c for c in chats if c["kind"] == "user"]
    recent = {names.norm(c.get("name")): 0.03 for c in chats[:10]}
    found = names.pick(queries, people, name_keys=("name", "username"), boosts=recent, alias=alias)
    if not found:
        found = names.pick(queries, chats, name_keys=("name", "username"), boosts=recent, alias=alias)
    return found


async def send(chat: dict[str, Any], text: str) -> bool:
    c = await client()
    if c is None:
        return False
    await c.send_message(chat.get("_peer") or chat["id"], text)
    return True


def _msg_view(msg: Any, *, me_id: int | None, chat_name: str) -> dict[str, Any]:
    sender = "я" if getattr(msg, "out", False) or getattr(msg, "sender_id", None) == me_id else chat_name
    sender_entity = getattr(msg, "sender", None)
    if sender != "я" and sender_entity is not None:
        sender = display_name(sender_entity)
    text = getattr(msg, "message", "") or ""
    if not text and getattr(msg, "media", None) is not None:
        text = "[" + type(msg.media).__name__.replace("MessageMedia", "").lower() + "]"
    when = getattr(msg, "date", None)
    return {"from": sender, "text": text[:400], "time": when.astimezone().strftime("%d.%m %H:%M") if when else ""}


async def recent(chat: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    c = await client()
    if c is None:
        return []
    me_id = getattr(_me, "id", None)
    out = [_msg_view(m, me_id=me_id, chat_name=chat["name"]) async for m in c.iter_messages(chat.get("_peer") or chat["id"], limit=limit)]
    return list(reversed(out))


async def search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Поиск по всем его чатам, группам и каналам (как строка поиска в Telegram): свежие совпадения первыми."""
    c = await client()
    if c is None:
        return []
    me_id = getattr(_me, "id", None)
    out = []
    async for m in c.iter_messages(None, search=query, limit=limit):
        chat = getattr(m, "chat", None)
        name = getattr(chat, "title", None) or " ".join(x for x in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if x) or "чат"
        out.append(_msg_view(m, me_id=me_id, chat_name=name))
    return out


async def unread(limit_chats: int = 8, per_chat: int = 3) -> list[dict[str, Any]]:
    """Непрочитанное: личные чаты первыми, затем группы без звука в конце."""
    chats = [c for c in await dialogs(fresh=True) if c["unread"] > 0]
    chats.sort(key=lambda c: (c["kind"] != "user", c["muted"]))
    out = []
    for chat in chats[:limit_chats]:
        try:
            msgs = await recent(chat, limit=min(per_chat, chat["unread"]))
        except Exception:
            logger.warning("tg_user recent failed for %s", chat["id"], exc_info=True)
            msgs = []
        out.append({"chat": chat["name"], "kind": chat["kind"], "unread": chat["unread"], "messages": msgs})
    return out


__all__ = ["configured", "status", "login_start", "login_finish", "logout", "stop", "dialogs", "find_chat", "send", "recent", "unread", "public"]
