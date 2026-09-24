"""Qwen3.8-Omni-Flash-Realtime (Alibaba Model Studio, Сингапур) — дешёвая замена Gemini Live для голоса.

Цены (сентябрь 2026, за 1M токенов): текст на вход $0.23, звук на вход $0.93, текст на выход $0.70, звук на выход
$1.87 — против $0.75 / $3 / $4.5 / $12 у Gemini 3.8 Live. Протокол — как OpenAI Realtime (session.update,
input_audio_buffer.append, response.audio.delta …).

QwenBridge притворяется веб-сокетом Gemini Live: принимает те же сообщения (setup, realtimeInput, clientContent,
toolResponse) и отдаёт те же (setupComplete, serverContent, toolCall, usageMetadata). Поэтому телефон, звонки
в Telegram и все инструменты работают без переделки, а если Qwen недоступен — сессия сама откатывается на Gemini.

Ключ — DASHSCOPE_API_KEY в .env или команда боту «/qwen <ключ> [workspace-id]» (DATA_DIR/secrets.json).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

MODEL = "qwen3.8-omni-flash-realtime"
DEFAULT_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
WORKSPACE_URL = "wss://{ws}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"
# женские голоса, которые говорят по-русски (список Alibaba для qwen3.8-omni-flash-realtime)
VOICES = {"Tina": "Тёплая", "Katerina": "Уверенная", "Maia": "Умная и мягкая", "Serena": "Нежная"}
DEFAULT_VOICE = "Tina"
INPUT_RATE = 16000
ASR_MODEL = "qwen3-asr-flash-realtime"


# ------------------------------------------------------------------ ключ
def _secrets_file() -> Path:
    from .tg_user import data_dir

    return data_dir() / "secrets.json"


def _secrets() -> dict[str, Any]:
    try:
        data = json.loads(_secrets_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def api_key() -> str:
    return (os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY") or str(_secrets().get("dashscope_api_key") or "")).strip()


def ws_url() -> str:
    url = (os.getenv("QWEN_WS_URL") or "").strip()
    if url:
        return url
    workspace = str(_secrets().get("dashscope_workspace") or "").strip()
    return WORKSPACE_URL.format(ws=workspace) if workspace else DEFAULT_URL


def save_key(key: str, workspace: str = "") -> None:
    data = _secrets()
    data["dashscope_api_key"] = key.strip()
    if workspace.strip():
        data["dashscope_workspace"] = workspace.strip()
    path = _secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def available() -> bool:
    return bool(api_key())


class QwenError(RuntimeError):
    """Qwen не принял соединение (ключ, баланс, регион) — сессия идёт на Gemini."""


_last_report = 0.0


def report_failure(reason: str) -> None:
    """Qwen не подключился — один раз в 6 часов пишем владельцу, почему (разговор уже идёт на Gemini)."""
    global _last_report
    import time

    now = time.monotonic()
    if _last_report and now - _last_report < 6 * 3600:
        return
    _last_report = now

    async def send() -> None:
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            from .context import bot_instance, settings

            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="💳 Alibaba Model Studio", url="https://modelstudio.console.alibabacloud.com/")]])
            for uid in sorted(settings.allowed_telegram_ids)[:1]:
                await bot_instance().send_message(uid, "🟠 <b>Qwen не подключился</b> — Джарвис говорит через Gemini.\n"
                                                  f"Причина: <code>{reason[:200]}</code>", reply_markup=kb)
        except Exception:
            logger.warning("qwen: не отправил предупреждение", exc_info=True)

    try:
        asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        pass


# ------------------------------------------------------------------ перевод схем Gemini → JSON Schema
def _schema(s: Any) -> Any:
    if not isinstance(s, dict):
        return s
    out: dict[str, Any] = {}
    for k, v in s.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {name: _schema(p) for name, p in v.items()}
        elif k == "items":
            out[k] = _schema(v)
        else:
            out[k] = v
    return out


def tools_from_setup(setup: dict[str, Any]) -> list[dict[str, Any]]:
    tools = []
    for block in setup.get("tools") or []:
        for d in block.get("functionDeclarations") or []:
            tools.append({"type": "function", "name": d["name"], "description": d.get("description", ""),
                          "parameters": _schema(d.get("parameters") or {"type": "OBJECT", "properties": {}})})
    return tools


def session_from_setup(setup: dict[str, Any], voice: str) -> dict[str, Any]:
    system = " ".join(p.get("text", "") for p in ((setup.get("systemInstruction") or {}).get("parts") or []))
    vad = ((setup.get("realtimeInputConfig") or {}).get("automaticActivityDetection") or {})
    session: dict[str, Any] = {
        "modalities": ["text", "audio"],
        "voice": voice if voice in VOICES else DEFAULT_VOICE,
        "instructions": system,
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "input_audio_transcription": {"model": ASR_MODEL},
        "turn_detection": {"type": "server_vad", "silence_duration_ms": int(vad.get("silenceDurationMs") or 800)},
    }
    tools = tools_from_setup(setup)
    if tools:
        session["tools"] = tools
    return session


def usage_to_gemini(u: dict[str, Any]) -> dict[str, Any]:
    """usage Qwen → usageMetadata в формате Gemini (так его считает billing)."""
    ind = u.get("input_tokens_details") or {}
    outd = u.get("output_tokens_details") or {}
    return {
        "promptTokenCount": int(u.get("input_tokens") or 0),
        "responseTokenCount": int(u.get("output_tokens") or 0),
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": int(ind.get("text_tokens") or 0)},
                                {"modality": "AUDIO", "tokenCount": int(ind.get("audio_tokens") or 0)},
                                {"modality": "IMAGE", "tokenCount": int(ind.get("image_tokens") or 0)}],
        "responseTokensDetails": [{"modality": "TEXT", "tokenCount": int(outd.get("text_tokens") or 0)},
                                  {"modality": "AUDIO", "tokenCount": int(outd.get("audio_tokens") or 0)}],
    }


def _resample(pcm: bytes, rate: int) -> bytes:
    if rate == INPUT_RATE or not pcm:
        return pcm
    import numpy as np

    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    n = int(len(x) * INPUT_RATE / rate)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


# ------------------------------------------------------------------ мост
@dataclass
class Msg:
    """Как aiohttp.WSMessage: live_call._decode смотрит на type.name и data."""
    type: aiohttp.WSMsgType
    data: Any = None
    extra: Any = None


class QwenBridge:
    def __init__(self, http: aiohttp.ClientSession, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._http = http
        self._ws = ws
        self._queue: asyncio.Queue[Msg] = asyncio.Queue()
        self._responding = False
        self._calls: list[dict[str, Any]] = []
        self._reader: asyncio.Task | None = None
        self.error = ""

    # --- как у веб-сокета
    @property
    def closed(self) -> bool:
        return self._ws.closed

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        await self._ws.close()

    async def receive(self) -> Msg:
        return await self._queue.get()

    def _emit(self, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(Msg(aiohttp.WSMsgType.TEXT, json.dumps(payload, ensure_ascii=False)))

    async def _send(self, event: dict[str, Any]) -> None:
        await self._ws.send_str(json.dumps(event, ensure_ascii=False))

    # --- Gemini → Qwen
    async def send_str(self, raw: str) -> None:
        data = json.loads(raw)
        if "realtimeInput" in data:
            ri = data["realtimeInput"]
            if isinstance(ri.get("audio"), dict):
                mime = str(ri["audio"].get("mimeType") or "")
                rate = int(mime.split("rate=")[1]) if "rate=" in mime else INPUT_RATE
                pcm = _resample(base64.b64decode(ri["audio"]["data"]), rate)
                await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
            if isinstance(ri.get("video"), dict):
                await self._send({"type": "input_image_buffer.append", "image": ri["video"]["data"]})
        elif "clientContent" in data:
            cc = data["clientContent"]
            for turn in cc.get("turns") or []:
                text = " ".join(p.get("text", "") for p in turn.get("parts") or [] if p.get("text"))
                if text:
                    await self._send({"type": "conversation.item.create", "item": {
                        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
            if cc.get("turnComplete"):
                await self._send({"type": "response.create"})
        elif "toolResponse" in data:
            for r in data["toolResponse"].get("functionResponses") or []:
                await self._send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": r.get("id"),
                    "output": json.dumps(r.get("response"), ensure_ascii=False, default=str)[:12000]}})
            await self._send({"type": "response.create"})

    # --- Qwen → Gemini
    async def _read(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    self.translate(json.loads(msg.data))
                except ValueError:
                    continue
        finally:
            self._queue.put_nowait(Msg(aiohttp.WSMsgType.CLOSED, None, self.error or "qwen closed"))

    def translate(self, ev: dict[str, Any]) -> None:
        kind = str(ev.get("type") or "")
        if kind == "response.created":
            self._responding = True
            self._calls = []
        elif kind == "response.audio.delta" and ev.get("delta"):
            self._emit({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": ev["delta"]}}]}}})
        elif kind == "response.audio_transcript.delta" and ev.get("delta"):
            self._emit({"serverContent": {"outputTranscription": {"text": ev["delta"]}}})
        elif kind == "response.text.delta" and ev.get("delta"):
            self._emit({"serverContent": {"outputTranscription": {"text": ev["delta"]}}})
        elif kind == "conversation.item.input_audio_transcription.completed" and ev.get("transcript"):
            self._emit({"serverContent": {"inputTranscription": {"text": ev["transcript"]}}})
        elif kind == "input_audio_buffer.speech_started" and self._responding:
            self._emit({"serverContent": {"interrupted": True}})  # перебил — Qwen сам обрывает ответ
        elif kind == "response.function_call_arguments.done":
            try:
                args = json.loads(ev.get("arguments") or "{}")
            except ValueError:
                args = {}
            self._calls.append({"id": ev.get("call_id"), "name": ev.get("name"), "args": args if isinstance(args, dict) else {}})
        elif kind == "response.done":
            self._responding = False
            resp = ev.get("response") or {}
            if isinstance(resp.get("usage"), dict):
                self._emit({"usageMetadata": usage_to_gemini(resp["usage"])})
            if self._calls:
                self._emit({"toolCall": {"functionCalls": self._calls}})
                self._calls = []
            else:
                self._emit({"serverContent": {"turnComplete": True}})
        elif kind == "error":
            err = ev.get("error") or {}
            self.error = f"{err.get('code') or ''}: {err.get('message') or ''}".strip(": ")
            logger.warning("qwen: %s", self.error[:300])


async def _open(http: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
    key = api_key()
    if not key:
        raise QwenError("нет ключа DASHSCOPE_API_KEY")
    try:
        return await http.ws_connect(f"{ws_url()}?model={MODEL}", headers={"Authorization": f"Bearer {key}"},
                                     heartbeat=20, max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=5))
    except aiohttp.WSServerHandshakeError as exc:
        raise QwenError(f"Alibaba не пустил: {exc.status} {exc.message}") from exc


async def _configure(ws: aiohttp.ClientWebSocketResponse, session: dict[str, Any]) -> None:
    """session.update и ждём session.updated (или ошибку — ключ, баланс, неверный голос)."""
    await ws.send_str(json.dumps({"type": "session.update", "session": session}, ensure_ascii=False))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 12
    while (left := deadline - loop.time()) > 0:
        msg = await asyncio.wait_for(ws.receive(), timeout=left)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise QwenError(f"соединение закрыто: {msg.extra or msg.type.name}")
        ev = json.loads(msg.data)
        if ev.get("type") == "session.updated":
            return
        if ev.get("type") == "error":
            err = ev.get("error") or {}
            raise QwenError(f"{err.get('code')}: {err.get('message')}")
    raise QwenError("нет ответа на session.update")


async def connect(setup_payload: dict[str, Any], *, voice: str) -> QwenBridge:
    """Соединение, которое для live_call выглядит как Gemini Live после setupComplete."""
    setup = setup_payload.get("setup") or {}
    http = aiohttp.ClientSession()
    try:
        ws = await _open(http)
        await _configure(ws, session_from_setup(setup, voice))
    except BaseException:
        await http.close()
        raise
    bridge = QwenBridge(http, ws)
    bridge._reader = asyncio.create_task(bridge._read(), name="qwen-read")
    bridge._reader.add_done_callback(lambda _t: asyncio.ensure_future(http.close()))
    return bridge


async def say(text: str, *, voice: str = DEFAULT_VOICE, instructions: str = "") -> tuple[bytes, str]:
    """Произнести фразу голосом Qwen (образец голоса, отклики «Да, сэр»): (PCM 24 кГц, что сказала)."""
    session = {"modalities": ["text", "audio"], "voice": voice if voice in VOICES else DEFAULT_VOICE,
               "instructions": instructions or "Ты диктор. Произнеси ровно тот текст, что тебе дали, — ни слова больше.",
               "input_audio_format": "pcm", "output_audio_format": "pcm"}
    pcm, said = bytearray(), []
    async with aiohttp.ClientSession() as http:
        ws = await _open(http)
        try:
            await _configure(ws, session)
            await ws.send_str(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Произнеси: {text}"}]}}, ensure_ascii=False))
            await ws.send_str(json.dumps({"type": "response.create"}))
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout=30)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                if ev.get("type") == "response.audio.delta" and ev.get("delta"):
                    pcm.extend(base64.b64decode(ev["delta"]))
                elif ev.get("type") == "response.audio_transcript.delta" and ev.get("delta"):
                    said.append(ev["delta"])
                elif ev.get("type") == "response.done":
                    usage = (ev.get("response") or {}).get("usage")
                    if isinstance(usage, dict):
                        from . import billing

                        billing.record(MODEL, usage_to_gemini(usage), kind="qwen")
                    break
                elif ev.get("type") == "error":
                    raise QwenError(str((ev.get("error") or {}).get("message") or "error"))
        finally:
            await ws.close()
    return bytes(pcm), "".join(said)


async def check() -> str:
    """Проверка ключа для команды /qwen: пустая строка — всё хорошо, иначе — что не так."""
    try:
        pcm, _ = await say("Проверка связи.")
    except Exception as exc:  # noqa: BLE001 — пользователю нужна причина текстом
        return str(exc)[:300] or type(exc).__name__
    return "" if pcm else "Qwen ответил без звука"


__all__ = ["MODEL", "VOICES", "DEFAULT_VOICE", "QwenBridge", "QwenError", "available", "connect", "say", "check", "save_key"]
