"""Надёжность соединения звонка: ранние signaling-пакеты не теряются (без сети и ntgcalls)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import call_net


class _FakeBinding:
    """Как ntgcalls: до connect_p2p часть пакетов отвергает («соединение не готово»)."""

    def __init__(self) -> None:
        self.ready = False
        self.delivered: list[bytes] = []

    async def send_signaling_data(self, chat_id, data):  # noqa: ANN001
        if not self.ready and data.startswith(b"early"):
            raise ConnectionError("not ready")
        self.delivered.append(data)

    async def connect_p2p(self, chat_id, servers, versions, p2p_allowed, custom):  # noqa: ANN001
        self.ready = True
        return None

    def other(self) -> str:
        return "passthrough"


def test_early_signaling_is_held_and_delivered_after_connect():
    real = _FakeBinding()
    proxy = call_net._BindingProxy(real)
    uid = 4242
    st = call_net.begin(uid)

    async def scenario():
        await proxy.send_signaling_data(uid, b"ok-1")          # принят сразу — как раньше
        await proxy.send_signaling_data(uid, b"early-1")       # раньше pytgcalls его молча выбрасывал
        assert real.delivered == [b"ok-1"] and st.held == 1
        servers = [SimpleNamespace(turn=True, stun=False, tcp=False, ipv6="")]
        await proxy.connect_p2p(uid, servers, ["11.0.0"], False, None)
        assert real.delivered == [b"ok-1", b"early-1"]        # доставлен после connect_p2p
        await proxy.send_signaling_data(uid, b"late")
        assert real.delivered[-1] == b"late"

    try:
        asyncio.run(scenario())
        assert "11.0.0" in st.protocol and "p2p=False" in st.protocol
        assert "придержано 1" in st.summary()
        assert proxy.other() == "passthrough"
    finally:
        call_net.end(uid)
    assert call_net.get(uid) is None


def test_signaling_for_calls_without_stats_passes_through():
    real = _FakeBinding()
    proxy = call_net._BindingProxy(real)
    asyncio.run(proxy.send_signaling_data(1, b"ok"))
    assert real.delivered == [b"ok"]
