"""Вход в аккаунт-помощник (для звонков) → строка сессии для TG_CALLER_SESSION.

Два шага, чтобы код подтверждения никуда не пересылался:

    python scripts/tg_login.py request           # запросить код (придёт в Telegram)
    TG_CODE=12345 python scripts/tg_login.py finish   # ввести код и получить сессию

Между шагами состояние лежит в `.tg_login_state.json` рядом с проектом, готовая
сессия — в `.tg_caller_session` (оба файла в .gitignore; после переноса на сервер
их можно удалить). Если включена двухфакторка — передай пароль в TG_2FA.

Параметры берутся из окружения: TG_CALLER_API_ID, TG_CALLER_API_HASH, TG_CALLER_PHONE.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".tg_login_state.json"
SESSION_FILE = ROOT / ".tg_caller_session"


try:  # windows-консоль по умолчанию cp1252 — иначе падает на кириллице
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"Нет переменной {name}")
    return value


async def request() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    phone = _env("TG_CALLER_PHONE")
    client = TelegramClient(StringSession(), int(_env("TG_CALLER_API_ID")), _env("TG_CALLER_API_HASH"))
    await client.connect()
    sent = await client.send_code_request(phone)
    STATE.write_text(json.dumps({"session": client.session.save(), "phone": phone, "hash": sent.phone_code_hash,
                                 "api_id": int(_env("TG_CALLER_API_ID")), "api_hash": _env("TG_CALLER_API_HASH")}), encoding="utf-8")
    await client.disconnect()
    print(f"Код отправлен в Telegram на {phone}. Тип: {type(sent.type).__name__}")
    print("Дальше:  TG_CODE=<код> python scripts/tg_login.py finish")


async def finish() -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    if not STATE.exists():
        raise SystemExit("Сначала запусти: python scripts/tg_login.py request")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    code = (os.getenv("TG_CODE") or "").strip().replace(" ", "")
    if not code:
        # спрашиваем прямо здесь: код никуда не пересылается
        code = input("Код из Telegram: ").strip().replace(" ", "")
    if not code:
        raise SystemExit("Код не введён")

    api_id = int(state.get("api_id") or os.getenv("TG_CALLER_API_ID") or 0)
    api_hash = str(state.get("api_hash") or os.getenv("TG_CALLER_API_HASH") or "")
    client = TelegramClient(StringSession(state["session"]), api_id, api_hash)
    await client.connect()
    try:
        await client.sign_in(state["phone"], code, phone_code_hash=state["hash"])
    except SessionPasswordNeededError:
        import getpass

        password = (os.getenv("TG_2FA") or "").strip()
        if not password:
            password = getpass.getpass("Облачный пароль (2FA, вводится вслепую): ").strip()
        if not password:
            raise SystemExit("Пароль не введён")
        await client.sign_in(password=password)
    me = await client.get_me()
    SESSION_FILE.write_text(client.session.save(), encoding="utf-8")
    await client.disconnect()
    STATE.unlink(missing_ok=True)
    print(f"Готово: вошли как {getattr(me, 'first_name', '')} @{getattr(me, 'username', '')} id={me.id}")
    print(f"Сессия сохранена в файл: {SESSION_FILE}")


async def sms() -> None:
    """Переслать код по SMS (если в приложение он не пришёл)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not STATE.exists():
        raise SystemExit("Сначала: python scripts/tg_login.py request")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    api_id = int(state.get("api_id") or os.getenv("TG_CALLER_API_ID") or 0)
    api_hash = str(state.get("api_hash") or os.getenv("TG_CALLER_API_HASH") or "")
    client = TelegramClient(StringSession(state["session"]), api_id, api_hash)
    await client.connect()
    sent = await client.send_code_request(state["phone"], force_sms=True)
    state["hash"] = sent.phone_code_hash
    state["session"] = client.session.save()
    STATE.write_text(json.dumps(state), encoding="utf-8")
    await client.disconnect()
    print(f"Код отправлен повторно. Тип: {type(sent.type).__name__}")


def main() -> None:
    step = (sys.argv[1] if len(sys.argv) > 1 else "request").lower()
    if step not in {"request", "finish", "sms"}:
        raise SystemExit("Используй: request | finish | sms")
    try:
        import telethon  # noqa: F401
    except ImportError:
        raise SystemExit("Сначала: pip install telethon")
    asyncio.run({"request": request, "finish": finish, "sms": sms}[step]())


if __name__ == "__main__":
    main()
