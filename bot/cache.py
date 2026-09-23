"""Крошечный in-memory TTL-кэш на пользователя.

Бот — один процесс и (по факту) один пользователь, поэтому кэшировать ответы
Supabase в памяти безопасно: любая запись в БД для пользователя сбрасывает его
кэш целиком (`invalidate`), а TTL страхует от рассинхрона.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Hashable

_store: dict[int, dict[Hashable, tuple[float, Any]]] = {}


def get(user_id: int, key: Hashable) -> Any | None:
    bucket = _store.get(user_id)
    if not bucket:
        return None
    item = bucket.get(key)
    if item is None:
        return None
    expires, value = item
    if expires < time.monotonic():
        bucket.pop(key, None)
        return None
    return value


def put(user_id: int, key: Hashable, value: Any, ttl: float) -> Any:
    _store.setdefault(user_id, {})[key] = (time.monotonic() + ttl, value)
    return value


def invalidate(user_id: int, prefix: str | None = None) -> None:
    bucket = _store.get(user_id)
    if not bucket:
        return
    if prefix is None:
        bucket.clear()
        return
    for key in [k for k in bucket if isinstance(k, tuple) and k and k[0] == prefix]:
        bucket.pop(key, None)


def drop_expiring(user_id: int, within: float, prefixes: set[str]) -> int:
    """Убрать данные (только ключи с этими префиксами), которые истекут в ближайшие `within` секунд, —
    чтобы фоновая подгрузка обновила их заранее, а живой запрос не ждал базу."""
    bucket = _store.get(user_id)
    if not bucket:
        return 0
    limit = time.monotonic() + within
    stale = [k for k, (expires, _) in bucket.items() if isinstance(k, tuple) and k and k[0] in prefixes and expires < limit]
    for k in stale:
        bucket.pop(k, None)
    return len(stale)


async def remember(
    user_id: int,
    key: Hashable,
    ttl: float,
    loader: Callable[[], Awaitable[Any]],
) -> Any:
    cached = get(user_id, key)
    if cached is not None:
        return cached
    value = await loader()
    return put(user_id, key, value, ttl)
