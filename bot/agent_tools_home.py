"""Инструменты умного дома (02.10): свет, ТВ, кондиционер голосом — bot/smarthome.py (Tuya Cloud / Smart Life).

Регистрируются в общем реестре `agent_tools.TOOLS` (модуль импортируется в конце bot/agent_tools.py) — ими пользуются и чат, и голос
(через bot_task), а самые частые фразы («включи свет») выполняет без модели bot/instant.py.
"""
from __future__ import annotations

from typing import Any

from . import smarthome
from .agent_tools import ARR, P, ToolContext, _str, tool

ACTIONS = ["on", "off", "brightness", "temperature", "mode", "wind", "volume_up", "volume_down", "mute", "channel_up", "channel_down", "key"]


@tool(
    "home_control",
    "УМНЫЙ ДОМ: включить/выключить и настроить свет, ТВ, кондиционер голосом, как Алиса. target — как он сказал: «свет», «свет в комнате», «ТВ», "
    "«кондиционер». action: on/off; brightness (value 1–100) — яркость света; temperature (value 16–30), mode (cool|heat|auto|fan|dry), "
    "wind (auto|low|medium|high) — кондиционер; volume_up/volume_down/mute/channel_up/channel_down/key (value — название клавиши) — ТВ. "
    "«Сделай кондиционер на 24» → temperature 24. «Включи свет» без уточнения — весь свет. Нескольких ТВ/кондиционеров — инструмент сам вернёт "
    "ask_exactly: спроси, какой. Не подключено — вернёт setup: перескажи, что нужно сделать.",
    {"target": P("STRING", "что включить/выключить, как он сказал"), "action": P("STRING", "что сделать", enum=ACTIONS),
     "value": P("STRING", "число или значение для brightness/temperature/mode/wind/key")},
    ("target", "action"),
)
async def _home_control(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    action = _str(a.get("action")) or ""
    if action not in ACTIONS:
        return {"error": f"action: одно из {', '.join(ACTIONS)}"}
    out = await smarthome.control(ctx.uid, _str(a.get("target")) or "", action, a.get("value"))
    if out.get("ok"):
        ctx.mutated = True
    return out


@tool("home_list", "Какие устройства умного дома подключены (свет, ТВ, кондиционер) и подключён ли дом вообще; «что у меня в умном доме?».")
async def _home_list(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    rows = smarthome.devices(ctx.uid)
    out: dict[str, Any] = {"connected": smarthome.configured(), "devices": [{"name": r["name"], "type": r["type"], "aliases": r.get("aliases") or []} for r in rows]}
    if not smarthome.configured():
        out["setup"] = smarthome.SETUP
    elif not rows:
        out["hint"] = "устройств пока нет — «найди устройства дома» (home_scan)"
    return out


@tool("home_scan", "Найти устройства дома в аккаунте Smart Life/Tuya и добавить новые (свет, реле, ТВ и кондиционер на ИК-пульте): «найди устройства дома», «обнови умный дом».")
async def _home_scan(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not smarthome.configured():
        return {"error": "умный дом не подключён", "setup": smarthome.SETUP}
    try:
        return {"ok": True, **await smarthome.scan(ctx.uid)}
    except smarthome.SmartHomeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@tool("home_rename", "Переименовать устройство дома или добавить ему прозвища («лампу назови «свет в комнате»», «кондиционер ещё зови кондей»).",
      {"device": P("STRING", "какое устройство (как называется сейчас)"), "name": P("STRING", "новое имя"),
       "aliases": ARR({"type": "STRING"}, "прозвища, как он его называет")}, ("device",))
async def _home_rename(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    row = smarthome.rename(ctx.uid, _str(a.get("device")) or "", _str(a.get("name")), [str(x) for x in a.get("aliases") or []])
    return {"ok": True, "device": {"name": row["name"], "aliases": row.get("aliases")}} if row else {"error": "не нашла такое устройство или их несколько — назови точнее"}
