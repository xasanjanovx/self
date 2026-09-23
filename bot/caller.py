"""Звонок в Telegram от аккаунта-помощника (userbot) — Telethon + pytgcalls.

Обычный бот звонить не умеет, поэтому звонит отдельный аккаунт «Джарвис»: у тебя
на экране обычный входящий Telegram-звонок, в трубке — синтезированный голос.

Включается, только если в .env заданы TG_CALLER_API_ID / TG_CALLER_API_HASH /
TG_CALLER_SESSION и установлены зависимости (requirements-caller.txt). Иначе
`available()` = False и бот будит сообщениями — ничего не падает.

Сессию получают один раз скриптом `scripts/tg_login.py` (код из Telegram вводит
сам пользователь, нам он не нужен).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None
_calls: Any = None
helper_id: int | None = None  # id аккаунта-помощника (для кнопки «открыть Джарвиса» / добавить в контакты)
helper_username: str | None = None
_lock = asyncio.Lock()
_import_error: str | None = None


def configured() -> bool:
    return bool(os.getenv("TG_CALLER_API_ID") and os.getenv("TG_CALLER_API_HASH") and os.getenv("TG_CALLER_SESSION"))


def available() -> bool:
    return configured() and _import_error is None


def status() -> dict[str, Any]:
    return {"configured": configured(), "started": _calls is not None, "error": _import_error}


async def start() -> bool:
    """Поднять userbot. Возвращает False, если не настроен или не установлены зависимости."""
    global _client, _calls, _import_error
    if not configured():
        return False
    if _calls is not None:
        return True
    async with _lock:
        if _calls is not None:
            return True
        try:
            from pytgcalls import PyTgCalls  # type: ignore
            from telethon import TelegramClient  # type: ignore
            from telethon.sessions import StringSession  # type: ignore
        except Exception as exc:  # зависимости не поставлены — работаем без звонков
            _import_error = f"{type(exc).__name__}: {exc}"
            logger.warning("caller disabled: %s", _import_error)
            return False
        try:
            _client = TelegramClient(StringSession(os.environ["TG_CALLER_SESSION"]), int(os.environ["TG_CALLER_API_ID"]), os.environ["TG_CALLER_API_HASH"])
            await _client.start()
            logging.getLogger("pytgcalls").setLevel(logging.INFO)
            _calls = PyTgCalls(_client)
            await _calls.start()
            try:
                from . import call_net

                call_net.instrument(_calls)  # ранние signaling-пакеты не теряются + лог соединения
            except Exception:
                logger.warning("call diagnostics not installed", exc_info=True)
            me = await _client.get_me()
            global helper_id, helper_username
            helper_id = getattr(me, "id", None)
            helper_username = getattr(me, "username", None)
            logger.info("caller started as @%s (id=%s)", getattr(me, "username", None), helper_id)
            return True
        except Exception as exc:
            _import_error = f"{type(exc).__name__}: {exc}"
            logger.exception("caller start failed")
            _client = _calls = None
            return False


async def resolve_peer(user_id: int, username: str | None = None) -> Any:
    """Найти собеседника для звонка.

    Telethon умеет звонить только тому, чей «адрес» (access_hash) ему известен.
    У свежего аккаунта кэш пуст, поэтому: пробуем id → подгружаем диалоги →
    ищем по @username. Если и это не вышло — звонить некому, и мы честно
    говорим об этом, а не молчим.
    """
    uid = int(user_id)
    try:
        return await _client.get_input_entity(uid)
    except Exception:
        logger.info("caller: %s не в кэше, подгружаю диалоги", uid)
    try:
        await _client.get_dialogs(limit=100)
        return await _client.get_input_entity(uid)
    except Exception:
        pass
    name = (username or "").lstrip("@").strip()
    if name:
        try:
            entity = await _client.get_entity(name)
            logger.info("caller: собеседник найден по @%s", name)
            return await _client.get_input_entity(entity)
        except Exception:
            logger.warning("caller: не удалось найти @%s", name, exc_info=True)
    raise LookupError("peer_unknown")


async def stop() -> None:
    global _client, _calls
    try:
        if _calls is not None:
            await _calls.stop()
    except Exception:
        logger.debug("calls stop failed", exc_info=True)
    try:
        if _client is not None:
            await _client.disconnect()
    except Exception:
        logger.debug("client disconnect failed", exc_info=True)
    _client = _calls = None


async def call(user_id: int, audio_path: str, *, ring_seconds: int = 45, play_seconds: int = 40, username: str | None = None) -> dict[str, Any]:
    """Позвонить и проиграть файл. Возвращает {'answered': bool, 'error': str|None}.

    Телеграм звонит, пока не возьмут трубку или не истечёт `ring_seconds`; если
    трубку взяли — проигрываем аудио и кладём трубку.
    """
    if not await start():
        return {"answered": False, "error": _import_error or "not configured"}
    try:
        from pytgcalls.types import CallConfig  # type: ignore
    except Exception as exc:
        return {"answered": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        await resolve_peer(int(user_id), username)
    except LookupError:
        return {"answered": False, "error": "peer_unknown"}
    try:
        await _calls.play(int(user_id), audio_path, config=CallConfig(timeout=ring_seconds))
    except Exception as exc:
        name = type(exc).__name__
        # частые случаи: не взяли трубку / отклонили / занято — это не ошибка, просто «не ответил»
        if any(k in name.lower() for k in ("timeout", "timedout", "discarded", "busy", "declined")):
            return {"answered": False, "error": None}
        logger.warning("call to %s failed: %s", user_id, exc)
        return {"answered": False, "error": f"{name}: {exc}"}
    # трубку взяли: даём доиграть и вешаем
    await asyncio.sleep(max(5, play_seconds))
    try:
        await _calls.leave_call(int(user_id))
    except Exception:
        logger.debug("leave_call failed", exc_info=True)
    return {"answered": True, "error": None}


# ------------------------------------------------------------------ живой разговор
_incoming: dict[int, "asyncio.Queue[bytes]"] = {}
_hooked = False


def _hook_updates() -> None:
    """Подписка на входящие аудиокадры звонка (один раз на процесс)."""
    global _hooked
    if _hooked or _calls is None:
        return
    try:
        from pytgcalls.types import Direction, StreamFrames  # type: ignore
    except Exception:
        return

    @_calls.on_update()
    async def _on_update(_client, update):  # noqa: ANN001
        try:
            if isinstance(update, StreamFrames):
                if update.direction != Direction.INCOMING:
                    return
                queue = _incoming.get(int(update.chat_id))
                if queue is None:
                    return
                for frame in update.frames:
                    queue.put_nowait(bytes(frame.frame))
                return
            # всё остальное (ответили, положили трубку, смена состояния) — в лог: без этого
            # не видно, на каком шаге рвётся звонок
            status = getattr(update, "status", "") or getattr(update, "state", "")
            logger.info("call update: %s %s", type(update).__name__, status)
            # собеседник положил трубку / занято → разговор окончен (и НЕ перезваниваем сами)
            if any(k in str(status) for k in ("DISCARDED", "BUSY", "KICKED", "CLOSED")):
                chat_id = int(getattr(update, "chat_id", 0) or 0)
                event = _ended.get(chat_id)
                if event is not None:
                    event.set()
        except Exception:
            logger.debug("frame hook failed", exc_info=True)

    _hooked = True


# ------------------------------------------------------------------ потоковый звонок (Gemini Live)
_ended: dict[int, asyncio.Event] = {}
FRAME_MS = 10
LIVE_RATE = 24000                      # Gemini Live отдаёт 24 кГц моно; в звонок шлём так же
FRAME_BYTES = LIVE_RATE // 1000 * FRAME_MS * 2   # 10 мс s16le моно = 480 байт
MEDIA_RETRIES = 3          # перезвонов, если трубку взяли, а медиа не соединилось (TelegramServerError)
MEDIA_RETRY_DELAY = 8.0    # через 2 и 5 с телефон ещё «занят» прошлым звонком — ждём дольше
BUSY_RETRY_DELAY = 4.0
RETRY_WINDOW = 45.0        # все перезвоны после обрыва — в пределах этого окна


async def open_stream_call(user_id: int, *, username: str | None = None, ring_seconds: int = 45) -> dict[str, Any]:
    """Позвонить так, чтобы звук можно было слать потоком (send_frame) и слушать входящий.

    Возвращает {'answered': bool, 'error': str|None, 'incoming': Queue, 'ended': Event}.
    """
    if not await start():
        return {"answered": False, "error": _import_error or "not configured"}
    try:
        from pytgcalls.types import AudioQuality, CallConfig, ExternalMedia, MediaStream, RecordStream  # type: ignore
    except Exception as exc:
        return {"answered": False, "error": f"{type(exc).__name__}: {exc}"}
    uid = int(user_id)
    _hook_updates()
    try:
        await resolve_peer(uid, username)
    except LookupError:
        return {"answered": False, "error": "peer_unknown"}
    if uid in _incoming or uid in _ended:
        # прошлый звонок не закрыли (сбой посреди разговора) — иначе «соединимся» с мёртвым звонком
        logger.info("call %s: закрываю висящий прошлый звонок", uid)
        await hang_up(uid)
        await asyncio.sleep(1.5)
    from . import call_net

    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=3000)
    _incoming[uid] = queue
    last_exc: Exception | None = None
    media_fails = 0
    deadline = time.monotonic() + RETRY_WINDOW
    attempt = 0
    while True:
        attempt += 1
        # у каждой попытки своё событие «трубку положили»: запоздалое «занято» от прошлой попытки
        # раньше висело в общем событии, и новый, уже принятый звонок бот сразу же клал сам
        _ended[uid] = asyncio.Event()
        stats = call_net.begin(uid)
        logger.info("call %s: набираю (поток, попытка %s, гудки до %s с)", uid, attempt, ring_seconds)
        try:
            await _calls.play(uid, MediaStream(ExternalMedia.AUDIO, audio_parameters=AudioQuality.LOW), config=CallConfig(timeout=ring_seconds))
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            name = type(exc).__name__.lower()
            logger.info("call %s: не состоялся — %s: %s · %s", uid, type(exc).__name__, str(exc)[:200], stats.summary())
            call_net.end(uid)
            # трубку взяли, но медиа не соединилось (ntgcalls#70) — перезваниваем. Сразу нельзя:
            # телефон ещё несколько секунд «занят» прошлым звонком.
            if "telegramserver" in name and media_fails < MEDIA_RETRIES and time.monotonic() < deadline:
                media_fails += 1
                await asyncio.sleep(MEDIA_RETRY_DELAY)
                continue
            if "busy" in name and media_fails and time.monotonic() < deadline:
                await asyncio.sleep(BUSY_RETRY_DELAY)
                continue
            break
    if last_exc is not None:
        _incoming.pop(uid, None)
        _ended.pop(uid, None)
        return {"answered": False, "error": classify_error(last_exc)}
    # свежее событие после ответа: всё, что пришло про прошлые попытки, больше не считается
    ended = asyncio.Event()
    _ended[uid] = ended
    logger.info("call %s: соединение через %s · %s", uid, stats.at(), stats.summary())
    logger.info("call %s: трубку взяли", uid)
    try:
        await _calls.record(uid, RecordStream(audio=True, audio_parameters=AudioQuality.LOW))
    except Exception:
        logger.warning("record() failed — собеседника не слышно", exc_info=True)
    return {"answered": True, "error": None, "incoming": queue, "ended": ended}


def classify_error(exc: Exception) -> str | None:
    """Почему звонок не состоялся — чтобы сказать человеку, ЧТО сделать, а не «не удалось».

    privacy   — его настройки «Кто может мне звонить» не пускают аккаунт Джарвиса;
    no_answer — звонило, но трубку не взяли (часто: звонящего нет в контактах → телефон глушит);
    None      — отклонил / занято (сам решил не брать)."""
    name = type(exc).__name__.lower()
    if "privacy" in name:
        return "privacy"
    if any(k in name for k in ("timeout", "timedout", "notanswer")):  # pytgcalls: TimedOutAnswer
        return "no_answer"
    if any(k in name for k in ("discarded", "busy", "declined")):
        return None
    return f"{type(exc).__name__}: {exc}"


async def send_audio(user_id: int, pcm: bytes) -> bool:
    """Отправить в звонок кусок PCM (24 кГц моно, кратно 10 мс). False — звонка уже нет."""
    try:
        from pytgcalls.types import Device  # type: ignore

        await _calls.send_frame(int(user_id), Device.MICROPHONE, pcm)
        return True
    except Exception as exc:
        if "notincall" in type(exc).__name__.lower():
            event = _ended.get(int(user_id))
            if event is not None:
                event.set()
            return False
        logger.debug("send_frame failed", exc_info=True)
        return True


async def hang_up(user_id: int) -> None:
    from . import call_net

    uid = int(user_id)
    _incoming.pop(uid, None)
    _ended.pop(uid, None)
    call_net.end(uid)
    try:
        await _calls.leave_call(uid)
    except Exception:
        logger.debug("leave_call failed", exc_info=True)


async def talk(user_id: int, *, greeting_pcm: bytes, on_utterance, ring_seconds: int = 45,
               max_seconds: int = 180, username: str | None = None) -> dict[str, Any]:
    """Позвонить и РАЗГОВАРИВАТЬ: проигрываем фразу, слушаем ответ, отвечаем.

    `on_utterance(pcm | None)` — колбэк уровня бота: получает записанную фразу
    собеседника (или None, если тот молчит) и возвращает {'pcm': bytes|None, 'stop': bool}.
    Возвращает {'answered': bool, 'error': str|None}.
    """
    from . import call_dialog as cd

    if not await start():
        return {"answered": False, "error": _import_error or "not configured"}
    try:
        from pytgcalls.types import AudioQuality, CallConfig, RecordStream  # type: ignore
    except Exception as exc:
        return {"answered": False, "error": f"{type(exc).__name__}: {exc}"}

    uid = int(user_id)
    _hook_updates()
    try:
        await resolve_peer(uid, username)
    except LookupError:
        return {"answered": False, "error": "peer_unknown"}
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    _incoming[uid] = queue
    path = await cd.pcm_to_file(greeting_pcm)
    try:
        logger.info("call %s: набираю (гудки до %s с)", uid, ring_seconds)
        try:
            await _calls.play(uid, path, config=CallConfig(timeout=ring_seconds))
        except Exception as exc:
            logger.info("call %s: не состоялся — %s: %s", uid, type(exc).__name__, str(exc)[:200])
            return {"answered": False, "error": classify_error(exc)}
        # трубку взяли
        logger.info("call %s: соединение установлено, говорю приветствие", uid)
        try:
            await _calls.record(uid, RecordStream(audio=True, audio_parameters=AudioQuality.LOW))
            logger.info("call %s: слушаю собеседника", uid)
        except Exception:
            logger.warning("record() failed — разговор без слуха", exc_info=True)
        buffer = cd.VoiceBuffer()
        deadline = asyncio.get_running_loop().time() + max_seconds
        silence_rounds = 0
        while asyncio.get_running_loop().time() < deadline:
            utterance: bytes | None = None
            try:
                while True:
                    chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
                    utterance = buffer.feed(chunk)
                    if utterance:
                        break
            except asyncio.TimeoutError:
                utterance = None
            if utterance is None:
                silence_rounds += 1
                if silence_rounds < 6:  # ~6 секунд тишины — ещё ждём
                    continue
                silence_rounds = 0
            else:
                silence_rounds = 0
            result = await on_utterance(utterance)
            reply_pcm = (result or {}).get("pcm")
            if reply_pcm:
                say_path = await cd.pcm_to_file(reply_pcm)
                if say_path:
                    try:
                        await _calls.play(uid, say_path)
                        await asyncio.sleep(min(30, max(1, cd.ms_of(reply_pcm) / 1000 + 0.6)))
                    except Exception:
                        logger.debug("play reply failed", exc_info=True)
                    finally:
                        cd.cleanup(say_path)
            if (result or {}).get("stop"):
                break
        return {"answered": True, "error": None}
    finally:
        _incoming.pop(uid, None)
        cd.cleanup(path)
        try:
            await _calls.leave_call(uid)
        except Exception:
            logger.debug("leave_call failed", exc_info=True)


async def send_message(user_id: int, text: str) -> bool:
    """Сообщение от лица «Джарвиса» (например, если звонок не прошёл)."""
    if not await start():
        return False
    try:
        await _client.send_message(int(user_id), text)
        return True
    except Exception:
        logger.warning("caller send_message failed", exc_info=True)
        return False


__all__ = ["available", "configured", "status", "start", "stop", "call", "talk", "resolve_peer", "send_message"]
