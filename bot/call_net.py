"""Надёжность и диагностика соединения P2P-звонка (pytgcalls/ntgcalls) — «соединение… не удалось».

Симптом: трубку взяли, на телефоне «Соединение…», через ~10 с — обрыв (TelegramServerError).
Бимодально: либо соединяется за ~1 с, либо никогда (ntgcalls#70).

Что нашли в pytgcalls 3.0.0rc3: входящие служебные пакеты звонка (signaling) от телефона
передаются в ntgcalls через `binding.send_signaling_data`, а ошибка «соединение ещё не готово»
МОЛЧА глотается (`except (ConnectionNotFound, ConnectionError): pass`). Если телефон прислал первые
пакеты раньше, чем мы вызвали `connect_p2p` (гонка в миллисекунды), они теряются — без них WebRTC
не договаривается, и через 10 с ntgcalls сдаётся. Отсюда «то работает, то нет».

Здесь:
- пакеты, которые ntgcalls отверг, потому что соединение ещё не готово, не выбрасываем, а придерживаем
  и отдаём сразу после `connect_p2p` (принятые проходят как раньше — поведение не хуже исходного);
- пакет с неизвестным id звонка (кэш pytgcalls ещё не знает звонок) — отдаём текущему звонку,
  если он единственный;
- по каждому звонку пишем в лог: версии протокола, p2p_allowed, relay-серверы, сколько пакетов
  пришло/ушло/придержано, смены состояния соединения со временем — чтобы следующий обрыв было
  видно по логам, а не гадать.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CallStats:
    t0: float = field(default_factory=time.monotonic)
    p2p_ready: bool = False
    buffer: list[bytes] = field(default_factory=list)
    sig_in: int = 0
    sig_out: int = 0
    held: int = 0
    errors: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    protocol: str = ""

    def at(self) -> str:
        return f"{time.monotonic() - self.t0:.1f}с"

    def summary(self) -> str:
        return (f"протокол [{self.protocol or '—'}] · сигналов пришло {self.sig_in}, ушло {self.sig_out}, придержано {self.held}"
                + (f" · ошибки {self.errors}" if self.errors else "") + f" · состояния {' → '.join(self.states) or '—'}")


_stats: dict[int, CallStats] = {}


def begin(uid: int) -> CallStats:
    """Новая попытка звонка — чистая статистика и пустой буфер."""
    st = CallStats()
    _stats[int(uid)] = st
    return st


def get(uid: int) -> CallStats | None:
    return _stats.get(int(uid))


def end(uid: int) -> None:
    _stats.pop(int(uid), None)


class _BindingProxy:
    """Обёртка над ntgcalls.NTgCalls: всё как есть, кроме signaling и connect_p2p."""

    def __init__(self, real: Any) -> None:
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def send_signaling_data(self, chat_id: int, data: bytes) -> Any:
        st = _stats.get(int(chat_id))
        if st is None:
            return await self._real.send_signaling_data(chat_id, data)
        st.sig_in += 1
        try:
            return await self._real.send_signaling_data(chat_id, data)
        except Exception as exc:
            if st.p2p_ready:
                st.errors.append(f"in:{type(exc).__name__}")
                raise
            # соединение ещё не готово — pytgcalls молча выбросил бы пакет; придерживаем до connect_p2p
            st.buffer.append(bytes(data))
            st.held += 1
            st.errors.append(f"early:{type(exc).__name__}")
            return None

    async def connect_p2p(self, chat_id: int, servers: Any, versions: Any, p2p_allowed: bool, custom_parameters: Any) -> Any:
        st = _stats.get(int(chat_id))
        if st is not None:
            turn = sum(1 for s in servers if getattr(s, "turn", False))
            stun = sum(1 for s in servers if getattr(s, "stun", False))
            tcp = sum(1 for s in servers if getattr(s, "tcp", False))
            v6 = sum(1 for s in servers if getattr(s, "ipv6", ""))
            st.protocol = f"{','.join(versions)} p2p={p2p_allowed} серверов {len(servers)} (turn {turn}, stun {stun}, tcp {tcp}, ipv6 {v6})"
            logger.info("call %s: connect_p2p %s · %s", chat_id, st.at(), st.protocol)
        result = await self._real.connect_p2p(chat_id, servers, versions, p2p_allowed, custom_parameters)
        if st is not None:
            st.p2p_ready = True
            held, st.buffer = st.buffer, []
            for data in held:
                try:
                    await self._real.send_signaling_data(chat_id, data)
                except Exception as exc:
                    st.errors.append(f"held:{type(exc).__name__}")
            if held:
                logger.info("call %s: отдали %s придержанных сигналов после connect_p2p", chat_id, len(held))
        return result


def instrument(calls: Any) -> None:
    """Подключить обёртки к запущенному PyTgCalls (один раз)."""
    if getattr(calls, "_self_net_instrumented", False):
        return
    calls._binding = _BindingProxy(calls._binding)

    app = calls._app
    orig_send = app.send_signaling

    async def send_signaling(user_id: int, data: bytes) -> Any:
        st = _stats.get(int(user_id))
        if st is not None:
            st.sig_out += 1
        return await orig_send(user_id, data)

    app.send_signaling = send_signaling

    cache = getattr(app, "_cache", None)
    if cache is not None and hasattr(cache, "get_user_id"):
        orig_get = cache.get_user_id

        def get_user_id(phone_call_id: Any) -> Any:
            uid = orig_get(phone_call_id)
            if uid is None and len(_stats) == 1:
                # кэш pytgcalls ещё не знает этот звонок — иначе пакет молча выброшен
                uid = next(iter(_stats))
                st = _stats[uid]
                st.errors.append("unknown_call_id")
            return uid

        cache.get_user_id = get_user_id

    orig_changed = calls._handle_connection_changed

    async def handle_connection_changed(chat_id: int, net_state: Any) -> Any:
        st = _stats.get(int(chat_id))
        if st is not None:
            state = str(getattr(net_state, "state", net_state)).split(".")[-1]
            kind = str(getattr(net_state, "kind", "")).split(".")[-1]
            st.states.append(f"{state}@{st.at()}")
            logger.info("call %s: соединение %s (%s) через %s", chat_id, state, kind, st.at())
        return await orig_changed(chat_id, net_state)

    calls._handle_connection_changed = handle_connection_changed
    calls._self_net_instrumented = True
    logger.info("caller: диагностика соединения звонков включена")


__all__ = ["instrument", "begin", "get", "end", "CallStats"]
