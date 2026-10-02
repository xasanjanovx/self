"""Умный дом голосом (02.10, его просьба: «включить и выключить как Алиса» — свет в комнате, ТВ, кондиционер, и так далее).

Бэкенд — Tuya Cloud (приложение Smart Life / Tuya Smart: самые дешёвые и доступные устройства, один API на всё):
  • лампы, реле, розетки — команда самому устройству (/v1.0/devices/{id}/commands; коды берём из спецификации устройства);
  • ТВ и кондиционер — через ИК-пульт Tuya (хаб-«ИК-бластер»): ТВ — клавиши (…/remotes/{remote}/raw/command), кондиционер —
    полное состояние (…/air-conditioners/{remote}/scenes/command: power, mode, temp, wind).
Ключи — в .env сервера: TUYA_ACCESS_ID, TUYA_ACCESS_SECRET, TUYA_ENDPOINT (по умолчанию Центральная Европа), TUYA_UID (id аккаунта
приложения из «Link App Account» в консоли Tuya). Список устройств — DATA_DIR/home_<uid>.json: «найди устройства» (home_scan) заполняет
его с именами из приложения, переименовать — home_rename. Не подключено — JES честно скажет, что нужно сделать (SETUP).
Голос: «Джес, включи свет» разбирает bot/instant.py без модели; остальное — инструмент home_control (bot/agent_tools_home.py).

ИК-устройства не сообщают своё состояние: «включи/выключи ТВ» шлёт клавишу питания (у большинства ТВ она одна — переключатель),
состояние кондиционера бот помнит сам (последнее, что отправил).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://openapi.tuyaeu.com"      # Central Europe; India — openapi.tuyain.com, US West — openapi.tuyaus.com
LIGHT_CATEGORIES = {"dj", "dd", "xdd", "fwd", "dc", "tgq", "tgkg"}
SWITCH_CATEGORIES = {"kg", "cz", "pc", "tdq"}
HUB_CATEGORIES = {"wnykq", "hwktwkq"}
IR_TV, IR_AC = 2, 5                                    # category_id пульта в Tuya IR
AC_MODES = {"cool": 0, "heat": 1, "auto": 2, "fan": 3, "dry": 4}
AC_WINDS = {"auto": 0, "low": 1, "medium": 2, "high": 3}
TV_KEYS = {"volume_up": "vol+", "volume_down": "vol-", "mute": "mute", "channel_up": "ch+", "channel_down": "ch-"}
ON_CODES = ("switch_led", "switch_1", "switch")
BRIGHT_CODES = ("bright_value_v2", "bright_value")

SETUP = (
    "Умный дом ещё не подключён. Что нужно (один раз): 1) в приложении Smart Life (Tuya Smart) добавить устройства — Wi-Fi лампу/реле для света "
    "и ИК-пульт (хаб) для ТВ и кондиционера; 2) на iot.tuya.com → Cloud → Create Cloud Project (Smart Home), регион как у аккаунта приложения; "
    "3) в проекте Devices → Link App Account — отсканировать QR из приложения; там же увидишь UID; подписаться на сервисы IoT Core и IR Control Hub; "
    "4) Access ID и Access Secret проекта дать мне — я внесу в .env сервера (TUYA_ACCESS_ID, TUYA_ACCESS_SECRET, TUYA_ENDPOINT, TUYA_UID). "
    "Потом скажи «найди устройства дома» — и «включи свет», «выключи кондиционер» заработают голосом.")


class SmartHomeError(Exception):
    pass


# ------------------------------------------------------------------ настройки и подпись
def endpoint() -> str:
    return (os.getenv("TUYA_ENDPOINT") or DEFAULT_ENDPOINT).strip().rstrip("/")


def configured() -> bool:
    return bool((os.getenv("TUYA_ACCESS_ID") or "").strip() and (os.getenv("TUYA_ACCESS_SECRET") or "").strip())


def sign(method: str, url: str, body: str, *, client_id: str, secret: str, token: str = "", t: str, nonce: str = "") -> str:
    """Подпись запроса Tuya OpenAPI (HMAC-SHA256, прописными): client_id + [access_token] + t + nonce + stringToSign,
    stringToSign = METHOD \\n sha256(body) \\n \\n url (путь и отсортированные параметры)."""
    string_to_sign = "\n".join([method.upper(), hashlib.sha256(body.encode()).hexdigest(), "", url])
    payload = client_id + token + t + nonce + string_to_sign
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest().upper()


def _url(path: str, query: dict[str, Any] | None) -> str:
    return path + ("?" + urlencode(sorted((k, str(v)) for k, v in query.items())) if query else "")


_token: dict[str, Any] = {"value": "", "exp": 0.0}
TRANSPORT = None  # для тестов: httpx.MockTransport


def _client():  # noqa: ANN202
    import httpx

    return httpx.AsyncClient(timeout=10, transport=TRANSPORT)


async def _send(http, method: str, path: str, body: dict[str, Any] | None, query: dict[str, Any] | None, token: str) -> dict[str, Any]:  # noqa: ANN001
    client_id, secret = os.environ["TUYA_ACCESS_ID"].strip(), os.environ["TUYA_ACCESS_SECRET"].strip()
    url, raw = _url(path, query), json.dumps(body, ensure_ascii=False) if body is not None else ""
    t, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
    headers = {"client_id": client_id, "sign": sign(method, url, raw, client_id=client_id, secret=secret, token=token, t=t, nonce=nonce),
               "t": t, "nonce": nonce, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"}
    if token:
        headers["access_token"] = token
    res = await http.request(method, endpoint() + url, headers=headers, content=raw.encode() if raw else None)
    res.raise_for_status()
    data = res.json()
    if not data.get("success"):
        raise SmartHomeError(f"Tuya: {data.get('msg') or 'ошибка'} (код {data.get('code')})")
    return data


async def call(method: str, path: str, body: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> Any:
    """Запрос к Tuya OpenAPI с токеном (берём и кэшируем сам). Возвращает result."""
    if not configured():
        raise SmartHomeError(SETUP)
    async with _client() as http:
        if not _token["value"] or time.time() >= float(_token["exp"]) - 60:
            data = await _send(http, "GET", "/v1.0/token", None, {"grant_type": 1}, "")
            res = data.get("result") or {}
            _token.update(value=str(res.get("access_token") or ""), exp=time.time() + int(res.get("expire_time") or 7200))
        return (await _send(http, method, path, body, query, str(_token["value"]))).get("result")


# ------------------------------------------------------------------ список устройств
def _file(uid: int):  # noqa: ANN202
    from .tg_user import data_dir

    return data_dir() / f"home_{uid}.json"


def devices(uid: int) -> list[dict[str, Any]]:
    try:
        return list(json.loads(_file(uid).read_text(encoding="utf-8")).get("devices") or [])
    except (OSError, ValueError):
        return []


def save_devices(uid: int, rows: list[dict[str, Any]]) -> None:
    _file(uid).write_text(json.dumps({"devices": rows}, ensure_ascii=False), encoding="utf-8")


def device_type(category: str | None) -> str | None:
    category = str(category or "")
    if category in LIGHT_CATEGORIES:
        return "light"
    if category in SWITCH_CATEGORIES:
        return "switch"
    return None


async def scan(uid: int) -> dict[str, Any]:
    """Подтянуть устройства из аккаунта Tuya: лампы/реле/розетки и пульты ТВ/кондиционера под ИК-хабами. Свои имена и алиасы не трогаем."""
    tuya_uid = (os.getenv("TUYA_UID") or "").strip()
    if not tuya_uid:
        raise SmartHomeError("Не задан TUYA_UID (id аккаунта приложения: консоль Tuya → проект → Devices → Link App Account)")
    found = await call("GET", f"/v1.0/users/{tuya_uid}/devices") or []
    if isinstance(found, dict):
        found = found.get("devices") or found.get("list") or []
    rows = devices(uid)
    known = {r["id"] for r in rows}
    added: list[str] = []

    def add(row: dict[str, Any]) -> None:
        if row["id"] not in known:
            known.add(row["id"])
            rows.append({"aliases": [], **row})
            added.append(row["name"])

    for d in found:
        kind = device_type(d.get("category"))
        if kind:
            add({"id": str(d["id"]), "name": str(d.get("name") or d["id"]), "type": kind})
        elif d.get("category") in HUB_CATEGORIES:
            try:
                remotes = await call("GET", f"/v2.0/infrareds/{d['id']}/remotes") or []
            except SmartHomeError as exc:
                logger.warning("smarthome: пульты хаба %s не получены: %s", d.get("id"), exc)
                continue
            if isinstance(remotes, dict):
                remotes = remotes.get("remote_list") or remotes.get("remotes") or []
            for r in remotes:
                cat = int(r.get("category_id") or 0)
                if cat in (IR_TV, IR_AC):
                    name = str(r.get("remote_name") or r.get("brand_name") or ("ТВ" if cat == IR_TV else "Кондиционер"))
                    add({"id": f"{d['id']}:{r['remote_id']}", "name": name, "type": "tv" if cat == IR_TV else "ac",
                         "hub": str(d["id"]), "remote": str(r["remote_id"]), "category": cat})
    save_devices(uid, rows)
    return {"found": len(found), "added": added, "devices": [{"name": r["name"], "type": r["type"]} for r in rows]}


def rename(uid: int, device: str, name: str | None = None, aliases: list[str] | None = None) -> dict[str, Any] | None:
    rows = devices(uid)
    hit = _match(rows, device)
    if len(hit) != 1:
        return None
    row = hit[0]
    if name:
        row["name"] = name.strip()[:60]
    if aliases:
        row["aliases"] = sorted({*row.get("aliases", []), *[a.strip().lower() for a in aliases if a.strip()]})
    save_devices(uid, rows)
    return row


# ------------------------------------------------------------------ поиск устройства по словам («свет в комнате», «тв», «кондей»)
_STOP = {"v", "na", "u", "vo", "moem", "moej", "moyom", "moya", "moi", "nash", "nashem", "tam", "tut", "k", "po", "dlya"}


def _stems() -> dict[str, tuple[tuple[str, ...], set[str]]]:
    from .names import norm

    def n(words: str) -> tuple[str, ...]:
        return tuple(norm(w) for w in words.split())

    return {"light": (n("свет ламп люстр подсвет светильник торшер ночник"), set()),
            "tv": (n("телевиз телик телек"), {norm("тв"), "tv"}),
            "ac": (n("кондиц кондей кондер кондёр сплит"), {"ac"})}


def wanted_type(query: str) -> tuple[str | None, list[str]]:
    """Что хочет: вид устройства по слову («свет» → light) и остальные слова («в комнате» → [«komnate»])."""
    from .names import norm

    tokens = [t for t in norm(query).split() if t]
    kind = None
    rest: list[str] = []
    stems = _stems()
    for t in tokens:
        hit = next((k for k, (pre, exact) in stems.items() if t in exact or any(t.startswith(p) for p in pre if p)), None)
        if hit and kind is None:
            kind = hit
        elif t not in _STOP:
            rest.append(t)
    return kind, rest


def _hay(row: dict[str, Any]) -> str:
    from .names import norm

    return norm(" ".join([row.get("name", ""), row.get("room", ""), *row.get("aliases", [])]))


def _is_light_row(row: dict[str, Any]) -> bool:
    """Лампа — или реле/розетка, которую он назвал светом («Свет в коридоре»); чайник в розетке «включи свет» не включит."""
    return row.get("type") == "light" or (row.get("type") == "switch" and wanted_type(" ".join([row.get("name", ""), *row.get("aliases", [])]))[0] == "light")


def _match(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    kind, rest = wanted_type(query)
    pool = [r for r in rows if kind is None or (_is_light_row(r) if kind == "light" else r.get("type") == kind)]
    if kind is None and not rest:
        return []
    if not rest:
        return pool   # «включи свет» — весь свет (если ламп несколько)
    return [r for r in pool if all(w in _hay(r) or any(h.startswith(w[:4]) for h in _hay(r).split()) for w in rest)]


# ------------------------------------------------------------------ команды
async def _codes(device_id: str) -> dict[str, dict[str, Any]]:
    spec = await call("GET", f"/v1.0/devices/{device_id}/specifications") or {}
    return {f["code"]: f for f in spec.get("functions") or [] if f.get("code")}


def _range(func: dict[str, Any]) -> tuple[int, int]:
    try:
        values = json.loads(func.get("values") or "{}")
        return int(values.get("min", 10)), int(values.get("max", 1000))
    except (TypeError, ValueError):
        return 10, 1000


async def _light(row: dict[str, Any], action: str, value: Any) -> str:
    funcs = await _codes(row["id"])
    on_code = next((c for c in ON_CODES if c in funcs), None)
    if on_code is None:
        raise SmartHomeError(f"«{row['name']}» не умеет включаться и выключаться (нет команды питания)")
    if action in {"on", "off"}:
        await call("POST", f"/v1.0/devices/{row['id']}/commands", {"commands": [{"code": on_code, "value": action == "on"}]})
        return "включила" if action == "on" else "выключила"
    if action == "brightness":
        code = next((c for c in BRIGHT_CODES if c in funcs), None)
        if code is None:
            raise SmartHomeError(f"«{row['name']}» не регулирует яркость")
        pct = max(1, min(100, int(float(value))))
        lo, hi = _range(funcs[code])
        await call("POST", f"/v1.0/devices/{row['id']}/commands",
                   {"commands": [{"code": on_code, "value": True}, {"code": code, "value": round(lo + (hi - lo) * pct / 100)}]})
        return f"яркость {pct}%"
    raise SmartHomeError(f"для света не бывает «{action}»")


async def _ac(uid: int, rows: list[dict[str, Any]], row: dict[str, Any], action: str, value: Any) -> str:
    state = {"power": 0, "mode": 0, "temp": 24, "wind": 0, **(row.get("state") or {})}
    if action == "on":
        state["power"] = 1
    elif action == "off":
        state["power"] = 0
    elif action == "temperature":
        state["temp"], state["power"] = max(16, min(30, int(float(value)))), 1
    elif action == "mode":
        if str(value) not in AC_MODES:
            raise SmartHomeError("режим кондиционера: cool, heat, auto, fan, dry")
        state["mode"], state["power"] = AC_MODES[str(value)], 1
    elif action == "wind":
        if str(value) not in AC_WINDS:
            raise SmartHomeError("скорость обдува: auto, low, medium, high")
        state["wind"], state["power"] = AC_WINDS[str(value)], 1
    else:
        raise SmartHomeError(f"для кондиционера не бывает «{action}»")
    await call("POST", f"/v2.0/infrareds/{row['hub']}/air-conditioners/{row['remote']}/scenes/command", state)
    row["state"] = state
    save_devices(uid, rows)
    return {"on": "включила", "off": "выключила"}.get(action, f"{state['temp']}°, режим {next(k for k, v in AC_MODES.items() if v == state['mode'])}")


async def _tv(row: dict[str, Any], action: str, value: Any) -> str:
    if action in {"on", "off"}:
        key = "power"          # ИК: клавиша питания у ТВ обычно одна (переключатель)
    elif action in TV_KEYS:
        key = TV_KEYS[action]
    elif action == "key" and value:
        key = str(value)
    else:
        raise SmartHomeError(f"для ТВ не бывает «{action}»")
    await call("POST", f"/v2.0/infrareds/{row['hub']}/remotes/{row['remote']}/raw/command", {"category_id": row.get("category") or IR_TV, "key": key})
    return {"on": "включила", "off": "выключила"}.get(action, f"клавиша {key}")


async def control(uid: int, query: str, action: str, value: Any = None) -> dict[str, Any]:
    """«свет в комнате» + on → результат: {"ok", "did": [{"device", "result"}], "errors": […]}. ИК-устройства — отправка без подтверждения."""
    if not configured():
        return {"error": "умный дом не подключён", "setup": SETUP}
    rows = devices(uid)
    if not rows:
        return {"error": "устройств нет — сначала «найди устройства дома» (home_scan)", "setup": None if configured() else SETUP}
    hit = _match(rows, query)
    if not hit:
        return {"error": f"не нашла устройство «{query}»", "devices": [r["name"] for r in rows]}
    kind = hit[0].get("type")
    if kind in {"tv", "ac"} and len(hit) > 1:
        return {"error": f"подходит несколько: {', '.join(r['name'] for r in hit)} — уточни, какое", "ask_exactly": True, "devices": [r["name"] for r in hit]}
    did: list[dict[str, str]] = []
    errors: list[str] = []
    for row in hit:
        try:
            if row["type"] in {"light", "switch"}:
                text = await _light(row, action, value)
            elif row["type"] == "ac":
                text = await _ac(uid, rows, row, action, value)
            else:
                text = await _tv(row, action, value)
            did.append({"device": row["name"], "result": text})
        except SmartHomeError as exc:
            errors.append(f"{row['name']}: {exc}")
        except Exception as exc:
            logger.warning("smarthome: %s не вышло", row.get("name"), exc_info=True)
            errors.append(f"{row['name']}: {type(exc).__name__}")
    if not did:
        return {"error": "; ".join(errors) or "не вышло"}
    return {"ok": True, "server_done": True, "did": did, "errors": errors, "ir_note": "ИК-команда отправлена; состояние ТВ не проверить" if any(r["type"] == "tv" for r in hit) else None}


__all__ = ["configured", "control", "scan", "devices", "rename", "wanted_type", "sign", "call", "SETUP", "SmartHomeError"]
