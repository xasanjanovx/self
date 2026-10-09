"""Канал «сервер → телефон» без открытого разговора (04.10.2026, часы Amazfit).

Раньше сервер мог что-то сказать телефону только в ответ на его запрос или во время разговора (websocket). Часы просят
«позвони маме», «где телефон?», «включи музыку» — это делает телефон, а разговор идёт на часах. Поэтому приложение JES держит
длинный запрос GET /jarvis/v1/phone/pull (до PULL_WAIT_S): как только для телефона есть действие — ответ приходит сразу.
Тем же запросом телефон сообщает, в руках ли он (экран включён и разблокирован) — тогда слушает телефон, а часы на паузе
(его выбор), и узнаёт, что он уснул (по часам) — микрофон телефона тоже спит.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

PULL_WAIT_S = 50.0                # nginx держит запрос до 60 с
IDLE_GAP_S = 60.0                 # часы не на связи — телефон спрашивает реже (батарея): пауза между запросами
ONLINE_S = PULL_WAIT_S + IDLE_GAP_S + 20.0   # телефон «на связи», если забирал действия недавно
IN_HAND_TTL_S = 15 * 60.0         # «в руках» без новостей дольше — считаем, что уже нет

_actions: dict[int, list[dict[str, Any]]] = {}
_events: dict[int, asyncio.Event] = {}
_seen: dict[int, float] = {}
_state: dict[int, dict[str, Any]] = {}
_asleep: dict[int, bool] = {}
_hurry_until: dict[int, float] = {}   # пока идёт подъём: приложению не спать между запросами (громкий сигнал дойдёт сразу)


def _event(uid: int) -> asyncio.Event:
    if uid not in _events:
        _events[uid] = asyncio.Event()
    return _events[uid]


def hurry(uid: int, seconds: float) -> None:
    """Подъём: ближайшие seconds секунд приложение опрашивает сервер без пауз между запросами (ответ pull содержит hurry=True)."""
    _hurry_until[uid] = max(_hurry_until.get(uid, 0.0), time.monotonic() + seconds)


def hurrying(uid: int) -> bool:
    return time.monotonic() < _hurry_until.get(uid, 0.0)


def online(uid: int) -> bool:
    return time.monotonic() - _seen.get(uid, -1e9) <= ONLINE_S


def push(uid: int, action: dict[str, Any]) -> bool:
    """Действие для телефона (тот же формат, что в разговоре: {"type": "call", …}). False — телефон сейчас не на связи
    (действие всё равно ждёт его 3 минуты)."""
    action = {**action, "_at": time.time()}
    _actions.setdefault(uid, []).append(action)
    _event(uid).set()
    return online(uid)


def _take(uid: int) -> list[dict[str, Any]]:
    now = time.time()
    items = [{k: v for k, v in a.items() if k != "_at"} for a in _actions.pop(uid, []) if now - a.get("_at", now) <= 180]
    _event(uid).clear()
    return items


def note_state(uid: int, *, screen: bool | None, locked: bool | None) -> bool:
    """Телефон сообщил экран/блокировку. → True, если «в руках» изменилось."""
    before = in_hand(uid)
    st = _state.setdefault(uid, {})
    if screen is not None:
        st["screen"] = bool(screen)
    if locked is not None:
        st["locked"] = bool(locked)
    st["at"] = time.monotonic()
    after = in_hand(uid)
    if before != after:
        from . import watch

        watch.phone_in_hand(uid, after)
    return before != after


def in_hand(uid: int) -> bool:
    st = _state.get(uid) or {}
    if time.monotonic() - st.get("at", -1e9) > IN_HAND_TTL_S:
        return False
    return bool(st.get("screen")) and st.get("locked") is False


def set_asleep(uid: int, asleep: bool) -> None:
    """Часы сказали «уснул» / «проснулся» — телефон узнает при следующем запросе (и сразу, если он ждёт)."""
    if _asleep.get(uid) == asleep:
        return
    _asleep[uid] = asleep
    _event(uid).set()


def asleep(uid: int) -> bool:
    return bool(_asleep.get(uid))


async def pull(uid: int, *, wait: float = PULL_WAIT_S) -> dict[str, Any]:
    _seen[uid] = time.monotonic()
    ev = _event(uid)
    sleeping = asleep(uid)
    if not _actions.get(uid):
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    _seen[uid] = time.monotonic()
    actions = _take(uid)
    if actions:
        logger.info("phone link: телефону %s", ", ".join(str(a.get("type")) for a in actions))
    from . import watch

    w = watch._watches.get(uid)
    out: dict[str, Any] = {"actions": actions, "asleep": asleep(uid), "watch": bool(w is not None and w.fresh), "hurry": hurrying(uid)}
    if asleep(uid) != sleeping:
        logger.info("phone link: телефону — %s", "он уснул, микрофон спит" if asleep(uid) else "он проснулся")
    return out


__all__ = ["hurry", "hurrying", "push", "pull", "online", "note_state", "in_hand", "set_asleep", "asleep", "PULL_WAIT_S"]
