"""02.10: умный дом голосом — свет, ТВ, кондиционер через Tuya Cloud (сеть подменена)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from bot import agent_tools as tools
from bot import instant, smarthome
from bot.profile import Profile


def _run(coro):
    return asyncio.run(coro)


def _profile(uid: int = 41) -> Profile:
    return Profile(telegram_id=uid, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


class FakeTuya:
    """Минимальный Tuya: токен, список устройств, спецификация лампы, команды. Запоминает запросы и проверяет подпись."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        body = json.loads(request.content) if request.content else None
        h = request.headers
        token = h.get("access_token", "")
        raw = request.content.decode() if request.content else ""
        to_sign = "\n".join([request.method, hashlib.sha256(raw.encode()).hexdigest(), "", path])
        expected = hmac.new(b"secret", (h["client_id"] + token + h["t"] + h["nonce"] + to_sign).encode(), hashlib.sha256).hexdigest().upper()
        if h["sign"] != expected or h["client_id"] != "id123":
            return httpx.Response(200, json={"success": False, "code": 1004, "msg": "sign invalid"})
        self.calls.append((request.method, path, body))
        if path.startswith("/v1.0/token"):
            return httpx.Response(200, json={"success": True, "result": {"access_token": "tok", "expire_time": 7200}})
        if token != "tok":
            return httpx.Response(200, json={"success": False, "code": 1010, "msg": "token invalid"})
        if path == "/v1.0/users/U1/devices":
            return httpx.Response(200, json={"success": True, "result": [
                {"id": "lamp1", "name": "Свет в комнате", "category": "dj"},
                {"id": "plug1", "name": "Чайник", "category": "cz"},
                {"id": "hub1", "name": "ИК хаб", "category": "wnykq"}]})
        if path == "/v2.0/infrareds/hub1/remotes":
            return httpx.Response(200, json={"success": True, "result": [
                {"remote_id": "r_tv", "category_id": 2, "brand_name": "Samsung ТВ"}, {"remote_id": "r_ac", "category_id": 5, "brand_name": "Кондиционер"}]})
        if path == "/v1.0/devices/plug1/specifications":
            return httpx.Response(200, json={"success": True, "result": {"functions": [{"code": "switch_1", "values": "{}"}]}})
        if path == "/v1.0/devices/lamp1/specifications":
            return httpx.Response(200, json={"success": True, "result": {"functions": [
                {"code": "switch_led", "values": "{}"}, {"code": "bright_value_v2", "values": json.dumps({"min": 10, "max": 1000})}]}})
        return httpx.Response(200, json={"success": True, "result": True})


@pytest.fixture()
def tuya(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for k, v in {"TUYA_ACCESS_ID": "id123", "TUYA_ACCESS_SECRET": "secret", "TUYA_UID": "U1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TUYA_ENDPOINT", raising=False)
    fake = FakeTuya()
    monkeypatch.setattr(smarthome, "TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setitem(smarthome._token, "value", "")
    monkeypatch.setitem(smarthome._token, "exp", 0.0)
    return fake


def test_signature_follows_tuya_algorithm():
    got = smarthome.sign("GET", "/v1.0/token?grant_type=1", "", client_id="id", secret="s", t="1700000000000", nonce="n")
    string_to_sign = "GET\n" + hashlib.sha256(b"").hexdigest() + "\n\n/v1.0/token?grant_type=1"
    assert got == hmac.new(b"s", ("id" + "1700000000000" + "n" + string_to_sign).encode(), hashlib.sha256).hexdigest().upper()
    assert smarthome.sign("POST", "/x", "{}", client_id="id", secret="s", token="T", t="1", nonce="n") != smarthome.sign("POST", "/x", "{}", client_id="id", secret="s", t="1", nonce="n")


def test_not_connected_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for k in ("TUYA_ACCESS_ID", "TUYA_ACCESS_SECRET"):
        monkeypatch.delenv(k, raising=False)
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("home_control", {"target": "свет", "action": "on"}, ctx))
    assert "не подключён" in out["error"] and "Smart Life" in out["setup"] and "TUYA_ACCESS_ID" in out["setup"]
    listed = _run(tools.run("home_list", {}, ctx))
    assert listed["connected"] is False and listed["setup"]


def test_scan_finds_lamp_plug_and_ir_remotes_under_the_hub(tuya):
    out = _run(smarthome.scan(41))
    assert out["found"] == 3
    rows = {r["name"]: r for r in smarthome.devices(41)}
    assert rows["Свет в комнате"]["type"] == "light" and rows["Чайник"]["type"] == "switch"
    assert rows["Samsung ТВ"] == {"aliases": [], "id": "hub1:r_tv", "name": "Samsung ТВ", "type": "tv", "hub": "hub1", "remote": "r_tv", "category": 2}
    assert rows["Кондиционер"]["type"] == "ac" and rows["Кондиционер"]["remote"] == "r_ac"
    assert _run(smarthome.scan(41))["added"] == []                   # повторный поиск ничего не дублирует и не ломает его имена


def test_light_on_off_and_brightness_use_codes_from_the_device_spec(tuya):
    _run(smarthome.scan(41))
    out = _run(smarthome.control(41, "свет в комнате", "on"))
    assert out["ok"] and out["server_done"] and out["did"] == [{"device": "Свет в комнате", "result": "включила"}]
    assert ("POST", "/v1.0/devices/lamp1/commands", {"commands": [{"code": "switch_led", "value": True}]}) in tuya.calls
    _run(smarthome.control(41, "свет", "brightness", 50))
    assert ("POST", "/v1.0/devices/lamp1/commands", {"commands": [{"code": "switch_led", "value": True}, {"code": "bright_value_v2", "value": 505}]}) in tuya.calls
    _run(smarthome.control(41, "свет", "off"))
    assert tuya.calls[-1][2] == {"commands": [{"code": "switch_led", "value": False}]}


def test_light_command_never_touches_a_plug_without_light_in_its_name(tuya):
    _run(smarthome.scan(41))
    _run(smarthome.control(41, "свет", "on"))
    assert not any("plug1" in path for _, path, _ in tuya.calls)         # «включи свет» не включит чайник
    smarthome.rename(41, "чайник", aliases=["розетка на кухне"])
    out = _run(smarthome.control(41, "розетка на кухне", "on"))           # а по своему имени розетка включается (через агента: home_control)
    assert out["ok"] and ("POST", "/v1.0/devices/plug1/commands", {"commands": [{"code": "switch_1", "value": True}]}) in tuya.calls


def test_ac_sends_full_state_and_remembers_it(tuya):
    _run(smarthome.scan(41))
    out = _run(smarthome.control(41, "кондиционер", "temperature", 22))
    assert out["ok"] and "22°" in out["did"][0]["result"]
    path = "/v2.0/infrareds/hub1/air-conditioners/r_ac/scenes/command"
    assert ("POST", path, {"power": 1, "mode": 0, "temp": 22, "wind": 0}) in tuya.calls
    _run(smarthome.control(41, "кондиционер", "mode", "heat"))
    assert tuya.calls[-1][2] == {"power": 1, "mode": 1, "temp": 22, "wind": 0}      # температура помнится
    _run(smarthome.control(41, "кондиционер", "off"))
    assert tuya.calls[-1][2]["power"] == 0 and tuya.calls[-1][2]["temp"] == 22
    assert "режим кондиционера" in _run(smarthome.control(41, "кондиционер", "mode", "turbo"))["error"]


def test_tv_power_and_volume_keys(tuya):
    _run(smarthome.scan(41))
    out = _run(smarthome.control(41, "тв", "on"))
    assert out["ok"] and "ИК" in out["ir_note"]
    assert ("POST", "/v2.0/infrareds/hub1/remotes/r_tv/raw/command", {"category_id": 2, "key": "power"}) in tuya.calls
    _run(smarthome.control(41, "телевизор", "volume_up"))
    assert tuya.calls[-1][2] == {"category_id": 2, "key": "vol+"}


def test_several_tvs_ask_which_one(tuya):
    _run(smarthome.scan(41))
    rows = smarthome.devices(41)
    rows.append({"id": "hub1:r_tv2", "name": "ТВ на кухне", "type": "tv", "hub": "hub1", "remote": "r_tv2", "category": 2, "aliases": []})
    smarthome.save_devices(41, rows)
    out = _run(smarthome.control(41, "тв", "on"))
    assert out["ask_exactly"] and len(out["devices"]) == 2
    ok = _run(smarthome.control(41, "тв на кухне", "on"))
    assert ok["ok"] and ok["did"][0]["device"] == "ТВ на кухне"


def test_wrong_secret_is_reported_not_crashing(tuya, monkeypatch):
    monkeypatch.setenv("TUYA_ACCESS_SECRET", "wrong")
    rows = [{"id": "lamp1", "name": "Свет в комнате", "type": "light", "aliases": []}]
    smarthome.save_devices(41, rows)
    out = _run(smarthome.control(41, "свет", "on"))
    assert "error" in out and "sign invalid" in out["error"]


def test_tool_marks_mutation_and_validates_action(tuya):
    _run(smarthome.scan(41))
    ctx = tools.ToolContext(profile=_profile(), text="")
    assert "error" in _run(tools.run("home_control", {"target": "свет", "action": "explode"}, ctx))
    out = _run(tools.run("home_control", {"target": "свет", "action": "on"}, ctx))
    assert out["ok"] and ctx.mutated
    listed = _run(tools.run("home_list", {}, ctx))
    assert listed["connected"] and {d["type"] for d in listed["devices"]} == {"light", "switch", "tv", "ac"}
    renamed = _run(tools.run("home_rename", {"device": "свет в комнате", "aliases": ["люстра"]}, ctx))
    assert renamed["ok"] and "люстра" in renamed["device"]["aliases"]


# ------------------------------------------------------------------ мгновенные фразы без модели
@pytest.mark.parametrize("phrase,args", [
    ("включи свет", {"target": "свет", "action": "on"}),
    ("выключи свет в комнате", {"target": "свет в комнате", "action": "off"}),
    ("включи телевизор", {"target": "телевизор", "action": "on"}),
    ("выключи тв", {"target": "тв", "action": "off"}),
    ("выключи кондиционер", {"target": "кондиционер", "action": "off"}),
    ("поставь кондиционер на двадцать четыре", {"target": "кондиционер", "action": "temperature", "value": "24"}),
    ("Включи, пожалуйста, свет!", {"target": "свет", "action": "on"}),
])
def test_instant_phrases_for_home(phrase, args):
    cmd = instant.parse(phrase)
    assert cmd is not None and cmd.tool == "home_control" and cmd.args == args


@pytest.mark.parametrize("phrase", ["включи фонарик", "выключи звук", "включи музыку", "включи шахзоду", "поставь кондиционер на сорок", "включи будильник"])
def test_instant_leaves_other_phrases_alone(phrase):
    cmd = instant.parse(phrase)
    assert cmd is None or cmd.tool != "home_control"


def test_wanted_type_and_matching_words():
    assert smarthome.wanted_type("свет в комнате") == ("light", ["komnate"])
    assert smarthome.wanted_type("кондей") == ("ac", []) and smarthome.wanted_type("телик") == ("tv", [])
    assert smarthome.wanted_type("чайник")[0] is None
