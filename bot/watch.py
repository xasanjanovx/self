"""Часы Amazfit Active 2 (Zepp OS) ⇄ JES (04.10.2026, его просьба: «полноценно, вызвать JES микрофоном часов;
говорю в часы — отвечают часы, в телефон — телефон»).

Часы — приложение jes-watch (Zepp OS): экран-циферблат «Кольцо намазов» (он выбрал вариант B), микрофон пишет Opus-файлами
по ~1 с; куски идут через приложение Zepp на телефоне (Side Service → HTTPS сюда). Здесь:
  • ожидание «Джес»: куски → PCM → фразы (phone_cheap.Segmenter) → та же проверка имени и голоса, что у телефона
    (phone_api.judge_wake);
  • разговор: экономный мозг телефона (phone_cheap.PhoneCheap) через «websocket» _Link: ответ (PCM 24 кГц) собираем
    и отдаём часам MP3 + текст; действия телефона (звонок, музыка…) уходят на телефон каналом bot/phone_link.py;
  • кто отвечает: «Джес» услышал и телефон, а часы на руке и слушают, телефон заблокирован — отвечают часы (его выбор
    «Часы, если надеты»); телефон в руках (разблокирован) — слушает телефон, часы на паузе;
  • умный ответ (его выбор): ночью (23–05) и во время намаза — только текст и вибрация, без голоса;
  • паузы: намаз (NAMAZ_PAUSE_MIN после азана), телефон в руках; сон, снятые часы, заряд < 30 % — решают сами часы;
  • здоровье: события часов (уснул/проснулся, снял/надел, пульс, стресс) и сводка раз в 30 мин → DATA_DIR/watch/;
    уснул — микрофон телефона тоже спит (phone_link.set_asleep);
  • фаджр: часы вибрируют вместе со звонком Telegram (будильник на часах ставится на wake_at из hello) — звонок будит
    всегда, часы только добавляются; вечером — предупредить, если часы сняты или почти разряжены.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import phone_cheap
from . import phone_link

logger = logging.getLogger(__name__)

RATE = 16000                  # микрофон часов → PCM 16 кГц моно (как у телефона)
FRAME_BYTES = RATE // 10 * 2  # 0.1 с — так Segmenter различает речь и паузы внутри секундного куска
WAKE_SILENCE_MS = 600         # в ожидании «Джес» фраза кончается быстрее, чем в разговоре
FRESH_S = 45.0                # часы «слушают», если выходили на связь недавно (опрос сервера — каждые ≤25 с)
NAMAZ_PAUSE_MIN = 25          # после азана столько минут JES на часах не слушает (он молится)
NIGHT_FROM, NIGHT_TO = 23, 5  # ночью — ответы только текстом и вибрацией
MAX_MP3_BYTES = 160_000       # длиннее — только текст (по Bluetooth слишком долго)
HEALTH_FRESH_S = 3 * 3600
LOW_BATTERY_EVENING = 20

PRAYERS = [("fajr", "Fajr", "Фаджр", "ДО ФАДЖРА"), ("dhuhr", "Dhuhr", "Зухр", "ДО ЗУХРА"), ("asr", "Asr", "Аср", "ДО АСРА"),
           ("maghrib", "Maghrib", "Магриб", "ДО МАГРИБА"), ("isha", "Isha", "Иша", "ДО ИШИ")]

WATCH_NOTE = ("Он говорит с тобой через ЧАСЫ на руке (Amazfit): ответ прозвучит из маленького динамика часов и появится на их "
              "круглом экране — одна-две короткие фразы. Звонки, музыку, приложения делает его телефон (он может быть в кармане).")


class DecodeError(RuntimeError):
    pass


# ------------------------------------------------------------------ звук
async def _ffmpeg(args: list[str], data: bytes, timeout: float = 8.0) -> bytes:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-loglevel", "error", *args, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise DecodeError("ffmpeg timeout")
    if proc.returncode != 0:
        raise DecodeError(err.decode("utf-8", "replace")[:200])
    return out


# 05.10: запись часов Zepp OS — НЕ Ogg: это «сырые» пакеты Opus в формате утилиты opus_demo — [длина u32 BE][контрольное число u32 BE]
# [пакет Opus]… (16 кГц, моно, SILK WB, кадры по 20 мс). ffmpeg такое не читает («Invalid data»), поэтому декодируем libopus напрямую.
MAX_OPUS_PACKET = 1275
_opus = None


def parse_frames(data: bytes) -> list[bytes]:
    """Пакеты Opus из записи часов. Пустой пакет (длина 0) — потерянный кадр. Обрыв посередине — берём то, что целое."""
    out: list[bytes] = []
    i = 0
    n = len(data)
    while i + 8 <= n:
        size = int.from_bytes(data[i: i + 4], "big")
        if size > MAX_OPUS_PACKET or i + 8 + size > n:
            break
        out.append(data[i + 8: i + 8 + size])
        i += 8 + size
    return out


def _load_opus():  # noqa: ANN202
    import ctypes
    import ctypes.util

    lib = ctypes.CDLL(ctypes.util.find_library("opus") or "libopus.so.0")
    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_decode.restype = ctypes.c_int
    lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    return lib


def decode_packets(packets: list[bytes]) -> bytes:
    """Пакеты Opus → PCM s16le 16 кГц моно (libopus через ctypes)."""
    import ctypes

    global _opus
    if _opus is None:
        _opus = _load_opus()
    err = ctypes.c_int(0)
    dec = _opus.opus_decoder_create(RATE, 1, ctypes.byref(err))
    if not dec or err.value != 0:
        raise DecodeError(f"opus_decoder_create: {err.value}")
    try:
        pcm = bytearray()
        buf = (ctypes.c_int16 * 960)()   # до 60 мс на пакет
        for packet in packets:
            got = _opus.opus_decode(dec, packet or None, len(packet), buf, 960, 0)
            if got < 0:
                continue   # битый пакет — пропускаем, остальные декодируются
            pcm += bytes(memoryview(buf).cast("B")[: got * 2])
        return bytes(pcm)
    finally:
        _opus.opus_decoder_destroy(dec)


async def opus_to_pcm(data: bytes) -> bytes:
    """Запись часов → PCM s16le 16 кГц моно. Ogg Opus (на случай другой прошивки) — через ffmpeg, «сырые» пакеты — libopus."""
    if data[:4] == b"OggS":
        return await _ffmpeg(["-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"], data)
    packets = parse_frames(data)
    if not packets:
        raise DecodeError("в записи нет пакетов Opus")
    try:
        return await asyncio.get_running_loop().run_in_executor(None, decode_packets, packets)
    except (OSError, AttributeError) as exc:   # нет libopus
        raise DecodeError(f"libopus: {exc}")


async def pcm_to_mp3(pcm: bytes, rate: int = 24000) -> bytes:
    """Голос ответа (PCM 24 кГц) → MP3 для динамика часов: моно 22 кГц 32 кбит/с, чуть громче (динамик маленький)."""
    return await _ffmpeg(["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0", "-af", "volume=1.6,alimiter=limit=0.95",
                          "-codec:a", "libmp3lame", "-b:a", "32k", "-ar", "22050", "-ac", "1", "-f", "mp3", "pipe:1"], pcm, timeout=15)


# ------------------------------------------------------------------ хранилище
def _dir() -> Path:
    from .tg_user import data_dir

    path = data_dir() / "watch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_file(uid: int) -> Path:
    return _dir() / f"state_{uid}.json"


def load_state(uid: int) -> dict[str, Any]:
    try:
        data = json.loads(_state_file(uid).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(uid: int, patch: dict[str, Any]) -> dict[str, Any]:
    data = load_state(uid)
    data.update(patch)
    tmp = _state_file(uid).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(_state_file(uid))
    return data


def _append(uid: int, name: str, row: dict[str, Any]) -> None:
    try:
        with open(_dir() / f"{name}_{uid}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.warning("watch: не записал %s", name, exc_info=True)


# ------------------------------------------------------------------ «websocket» для экономного мозга телефона
class _Msg:
    __slots__ = ("type", "data", "extra")

    def __init__(self, kind, data) -> None:  # noqa: ANN001
        self.type = kind
        self.data = data
        self.extra = None


class _Link:
    """PhoneCheap думает, что говорит с телефоном по websocket: читает отсюда звук, пишет сюда текст, звук и действия."""

    def __init__(self, conv: "Conversation") -> None:
        self.conv = conv
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def __aiter__(self):  # noqa: ANN204
        return self

    async def __anext__(self) -> _Msg:
        msg = await self.q.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    def feed(self, pcm: bytes) -> None:
        import aiohttp

        if not self.closed:
            self.q.put_nowait(_Msg(aiohttp.WSMsgType.BINARY, pcm))

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.q.put_nowait(None)

    async def send_bytes(self, data: bytes) -> None:
        self.conv.audio.extend(data)

    async def send_str(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except ValueError:
            return
        await self.conv.on_message(payload)


class _Speaker(phone_cheap.Speaker):
    """Голос ответа для часов; ночью и на намазе — без голоса (только текст и вибрация, его выбор)."""

    def __init__(self, profile, persona, send, quiet) -> None:  # noqa: ANN001
        super().__init__(profile, persona, send)
        self.quiet = quiet

    async def say(self, text: str) -> bool:
        if self.quiet():
            return False
        return await super().say(text)


class WatchCheap(phone_cheap.PhoneCheap):
    def __init__(self, profile, persona, link, device, memory, *, watch: "Watch") -> None:  # noqa: ANN001
        super().__init__(profile, persona, link, device, memory)
        self.watch = watch
        self.speaker = _Speaker(profile, persona, self.to_phone, quiet=lambda: watch.quiet)

    def _stamp(self) -> str:
        first = self._first
        stamp = super()._stamp()
        if not first:
            return stamp
        extra = WATCH_NOTE
        health = health_line(self.uid)
        if health:
            extra += " " + health
        return stamp[:-1] + ". " + extra + "]"


# действия телефона → что показать на часах, если JES ничего не сказала
_ACTION_TEXT = {
    "call": "Звоню{who}", "sms": "Отправляю SMS{who}", "play": "Включаю{what}", "alarm": "Будильник{time}", "timer": "Таймер поставлен",
    "flashlight": "Фонарик", "media": "Готово", "volume": "Громкость", "open_app": "Открываю на телефоне", "url": "Открываю на телефоне",
    "navigate": "Маршрут на телефоне", "taxi": "Вызываю такси", "whatsapp": "WhatsApp{who}", "ring_phone": "Телефон звонит — ищите по звуку",
    "calendar_add": "В календаре", "tv": "ТВ: готово", "dnd": "Не беспокоить", "ringer": "Звук телефона", "brightness": "Яркость",
}


def _action_text(action: dict[str, Any]) -> str:
    kind = str(action.get("type") or "")
    tpl = _ACTION_TEXT.get(kind, "Готово")
    who = action.get("name") or action.get("who") or ""
    what = action.get("title") or action.get("query") or ""
    when = action.get("time") or ""
    return tpl.format(who=f": {who}" if who else "", what=f": {what}" if what else "", time=f" на {when}" if when else "")


class Conversation:
    def __init__(self, watch: "Watch", by: str) -> None:
        self.watch = watch
        self.by = by
        self.link = _Link(self)
        self.audio = bytearray()
        self.said: list[str] = []
        self.cards: list[str] = []
        self.actions: list[str] = []
        self.first: bytes | None = None
        self.task: asyncio.Task | None = None
        self.started = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.task is not None and not self.task.done() and not self.link.closed

    def stop(self) -> None:
        self.link.close()

    async def on_message(self, m: dict[str, Any]) -> None:
        kind = m.get("type")
        w = self.watch
        if kind == "user" and m.get("text"):
            w.push({"t": "heard", "text": str(m["text"])[:200]})
        elif kind == "jarvis" and m.get("text"):
            self.said.append(str(m["text"]))
        elif kind == "status" and m.get("text"):
            w.push({"t": "status", "text": str(m["text"])[:60]})
        elif kind == "card" and isinstance(m.get("card"), dict):
            card = m["card"]
            self.cards.append(" · ".join(str(card.get(k) or "") for k in ("title", "subtitle") if card.get(k))[:160])
        elif kind == "action" and isinstance(m.get("action"), dict):
            action = dict(m["action"])
            online = phone_link.push(w.uid, action)
            self.actions.append(_action_text(action) if online else "Телефон не на связи — откройте JES на телефоне")
            logger.info("watch: действие телефону %s%s", action.get("type"), "" if online else " (телефон не на связи)")
        elif kind == "unlock":
            w.push({"t": "status", "text": "Разблокируйте телефон"})
        elif kind == "error" and m.get("text"):
            self.said.append(str(m["text"]))
        elif kind == "turn_complete":
            await self._flush()
        elif kind == "end":
            await self._flush()
            self.link.close()

    async def _flush(self) -> None:
        text = " ".join(self.said).strip()
        pcm = bytes(self.audio)
        cards, actions = self.cards, self.actions
        self.said, self.audio, self.cards, self.actions = [], bytearray(), [], []
        if not text:
            text = " · ".join(x for x in (cards + actions) if x)
        if not text and not pcm:
            return
        ev: dict[str, Any] = {"t": "say", "text": text, "listen": True}
        if pcm and not self.watch.quiet:
            try:
                mp3 = await pcm_to_mp3(pcm)
                if len(mp3) <= MAX_MP3_BYTES:
                    ev["audio"] = base64.b64encode(mp3).decode()
                else:
                    logger.info("watch: ответ длинный (%s КБ MP3) — на часы только текст", len(mp3) // 1024)
            except DecodeError as exc:
                logger.warning("watch: MP3 не собрался: %s", exc)
        self.watch.push(ev)

    async def run(self) -> None:
        from . import agent_tools_extra as extra
        from . import phone
        from . import services
        from . import undo
        from .handlers.common import profile_by_id

        w = self.watch
        uid = w.uid
        started = time.monotonic()
        try:
            profile, persona, memory = await asyncio.gather(profile_by_id(uid), services.persona(uid), extra.memory_prompt(uid))
            device = {"source": "watch", "locked": not phone_link.in_hand(uid)}
            sess = WatchCheap(profile, persona, self.link, device, memory, watch=w)
            # кнопкой/тапом его вызвал он сам (часы у него на руке); микрофон часов звучит иначе, чем телефон, —
            # строгая проверка голоса только после «Джес» (там есть образец голоса этого разговора)
            sess.owner_check = sess.owner_check and self.by in {"wake", "phone"}
            if self.first:
                sess.queue.put_nowait(("audio", self.first))
            undo.begin_turn(uid)
            try:
                upgrade = await sess.run({})
            finally:
                undo.end_turn(uid)
                await sess.speaker.close()
            if upgrade is not None:
                w.push({"t": "say", "text": "Камера, экран и долгий разговор — на телефоне: скажите «Джес» телефону.", "listen": False})
            said = " / ".join(x for x in sess.user_lines if x)[:400]
            answered = " / ".join(x for x in sess.jarvis_lines if x)[:400]
            logger.info("watch: разговор (%s) %.0f с, ходов %s, действия %s, «%s» → «%s»", self.by, time.monotonic() - started, sess.turns,
                        sess.result.actions, said[:80], answered[:80])
            if said:
                phone._later(services.log_agent(uid, text=said, kind="watch", tools=",".join(sess.result.actions), reply=answered, ok=True))
                phone._later(extra.remember_exchange(uid, "⌚ " + said, answered, when=profile.now.strftime("%d.%m %H:%M")))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("watch: разговор сорвался")
            w.push({"t": "say", "text": "Не получилось — повторите, пожалуйста", "listen": False})
        finally:
            self.link.closed = True
            if w.conv is self:
                w.conv = None
            w.push({"t": "end"})


# ------------------------------------------------------------------ часы
class Watch:
    def __init__(self, uid: int) -> None:
        self.uid = uid
        self.sid = ""
        self.events: list[dict[str, Any]] = []
        self._evt: asyncio.Event | None = None
        self.seen = -1e9
        self.mode = "idle"
        self.wear = -1
        self.bat = -1
        self.sleeping = -1
        self.pauses: set[str] = set()
        self.seg = phone_cheap.Segmenter(rate=RATE, silence_ms=WAKE_SILENCE_MS)
        self.last_f = 0
        self.conv: Conversation | None = None
        self.decode_fail = 0
        self.fajr_since = 0.0
        self._profile: Any = None
        self._profile_at = -1e9
        self._prayer_day: Any = None
        self._prayers: list[dict[str, Any]] = []
        self.chunks = 0
        self.rms_max = 0.0

    # --- события для часов
    @property
    def evt(self) -> asyncio.Event:
        if self._evt is None:
            self._evt = asyncio.Event()
        return self._evt

    def push(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        self.evt.set()

    def take(self) -> list[dict[str, Any]]:
        out, self.events = self.events, []
        self.evt.clear()
        return out

    def reply(self, **extra: Any) -> dict[str, Any]:
        return {"events": self.take(), **extra}

    # --- состояние
    def touch(self, data: dict[str, Any]) -> None:
        sid = str(data.get("sid") or "")
        if sid and sid != self.sid:
            if self.sid:
                logger.info("watch: новый запуск экрана JES (%s)", sid)
            self.sid = sid
            self.events = []
            self.seg = phone_cheap.Segmenter(rate=RATE, silence_ms=WAKE_SILENCE_MS)
            self.last_f = 0
            if self.conv is not None:
                self.conv.stop()
        self.seen = time.monotonic()
        if data.get("st"):
            self.mode = str(data["st"])
        for key in ("wear", "bat", "sleeping"):
            if isinstance(data.get(key), (int, float)) and data[key] >= -1:
                setattr(self, key, int(data[key]))

    @property
    def fresh(self) -> bool:
        return time.monotonic() - self.seen <= FRESH_S

    @property
    def worn(self) -> bool:
        return self.wear > 0

    async def profile(self):  # noqa: ANN201
        from .handlers.common import profile_by_id

        if self._profile is None or time.monotonic() - self._profile_at > 600:
            self._profile = await profile_by_id(self.uid)
            self._profile_at = time.monotonic()
        return self._profile

    async def prayers(self) -> list[dict[str, Any]]:
        from . import prayer, wake_runner

        profile = await self.profile()
        if self._prayer_day != profile.today:
            try:
                s = await wake_runner._settings(self.uid)
                rows = await prayer.timings(profile.today, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
            except Exception:
                logger.warning("watch: времена намаза не получил", exc_info=True)
                return self._prayers
            out = []
            for key, src, name, to in PRAYERS:
                t = prayer.parse_hhmm(rows.get(src))
                if t is not None:
                    out.append({"k": key, "n": name, "to": to, "m": t.hour * 60 + t.minute})
            self._prayers, self._prayer_day = out, profile.today
        return self._prayers

    def _now_min(self) -> int:
        now = self._profile.now if self._profile is not None else datetime.now()
        return now.hour * 60 + now.minute

    @property
    def namaz(self) -> bool:
        m = self._now_min()
        return any(p["m"] <= m < p["m"] + NAMAZ_PAUSE_MIN for p in self._prayers)

    @property
    def quiet(self) -> bool:
        """Только текст и вибрация: ночь или время намаза."""
        h = self._now_min() // 60
        return h >= NIGHT_FROM or h < NIGHT_TO or self.namaz

    async def tick(self) -> None:
        """Паузы сервера: намаз; телефон в руках — пусть слушает телефон."""
        try:
            await self.prayers()
        except Exception:
            pass
        want = set()
        if self.namaz:
            want.add("namaz")
        if phone_link.in_hand(self.uid):
            want.add("phone")
        if self.conv is not None and self.conv.alive:
            want.discard("phone")   # разговор уже идёт на часах — не обрываем
        if want == self.pauses:
            return
        self.pauses = want
        if want:
            reason = "namaz" if "namaz" in want else "phone"
            self.push({"t": "pause", "reason": reason})
        else:
            self.push({"t": "resume"})

    # --- звук
    async def feed(self, pcm: bytes, data: dict[str, Any]) -> None:
        self.chunks += 1
        if self.conv is not None and self.conv.alive:
            self.conv.link.feed(pcm)
            return
        f = int(data.get("f") or 0)
        if self.seg.active and self.last_f and f != self.last_f + 1:
            self._seg(b"\0" * int(RATE * 0.8) * 2)   # часы пропустили тихие куски — фраза кончилась
        self.last_f = f
        self._seg(pcm)

    def _seg(self, pcm: bytes) -> None:
        import numpy as np

        for i in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[i: i + FRAME_BYTES]
            if len(frame) >= 64:
                x = np.frombuffer(frame[: len(frame) // 2 * 2], dtype=np.int16).astype(np.float32)
                self.rms_max = max(self.rms_max, float(np.sqrt(np.mean(x * x))) if len(x) else 0.0)
            _, utterance, _ = self.seg.feed(frame)
            if utterance:
                asyncio.create_task(self._check(utterance), name="watch-wake")

    async def _check(self, pcm: bytes) -> None:
        from .phone_api import judge_wake
        from .phone_live import pcm_to_wav

        try:
            verdict = await judge_wake(self.uid, pcm_to_wav(pcm, RATE), source="часы")
        except Exception:
            logger.warning("watch: проверка «Джес» сорвалась", exc_info=True)
            return
        if not verdict.get("ok") or verdict.get("unchecked"):
            return
        if self.pauses:
            logger.info("watch: «%s» — пауза (%s), не отвечаю", verdict.get("text"), ", ".join(sorted(self.pauses)))
            return
        self.start("wake", heard=str(verdict.get("text") or ""), first=pcm if verdict.get("after") else None)

    def start(self, by: str, *, heard: str = "", first: bytes | None = None) -> None:
        if self.conv is not None and self.conv.alive:
            return
        if by in {"wake", "phone"}:
            self.push({"t": "wake", "text": heard[:120]})
        conv = Conversation(self, by)
        conv.first = first
        self.conv = conv
        conv.task = asyncio.create_task(conv.run(), name="watch-conv")
        logger.info("watch: разговор начат (%s)%s", by, f" «{heard[:60]}»" if heard else "")


_watches: dict[int, Watch] = {}


def get(uid: int) -> Watch:
    if uid not in _watches:
        _watches[uid] = Watch(uid)
    return _watches[uid]


# ------------------------------------------------------------------ телефон ⇄ часы
def should_answer(uid: int, *, phone_locked: bool) -> bool:
    """«Джес» услышал телефон: отвечать ли часам (на руке, слушают, не на паузе; телефон заблокирован)."""
    w = _watches.get(uid)
    if w is None or not w.fresh or not w.worn or not phone_locked:
        return False
    return not w.pauses and w.mode in {"idle", "conv", "speak"}


def take_over(uid: int, heard: str, wav: bytes | None) -> None:
    """Телефон услышал «Джес», а отвечают часы: открыть разговор на часах (часы слушают дальше сами).
    wav — запись телефона (16 кГц), если после имени была команда: она станет первой репликой."""
    first = None
    if wav:
        first = wav[44:] if wav[:4] == b"RIFF" else wav
    get(uid).start("phone", heard=heard, first=first)


def phone_in_hand(uid: int, on: bool) -> None:
    w = _watches.get(uid)
    if w is None or not w.fresh:
        return
    logger.info("watch: телефон %s", "в руках — часы на паузе" if on else "убран — часы снова слушают")
    try:
        asyncio.get_running_loop().create_task(w.tick())
    except RuntimeError:
        pass


def buzz(uid: int, text: str = "Я здесь!") -> bool:
    """«Где часы?» — часы вибрируют и показывают текст (если экран JES открыт)."""
    w = _watches.get(uid)
    if w is None or not w.fresh:
        return False
    w.push({"t": "notify", "text": text, "label": "JES"})
    return True


def _note_sleep(w: Watch, sleeping: int, why: str) -> None:
    if sleeping not in (0, 1):
        return
    asleep = sleeping == 1
    if phone_link.asleep(w.uid) != asleep:
        logger.info("watch: %s (%s)", "он уснул — микрофон телефона спит" if asleep else "он проснулся", why)
    phone_link.set_asleep(w.uid, asleep)


# ------------------------------------------------------------------ запросы часов
async def hello(uid: int, data: dict[str, Any]) -> dict[str, Any]:
    from . import app_alarm

    w = get(uid)
    w.touch(data)
    _save_state(uid, {"version": str(data.get("v") or ""), "device": data.get("device") or {}, "wear": w.wear, "bat": w.bat,
                      "sleeping": w.sleeping, "seen_at": time.time(), "launch": str(data.get("launch") or "")})
    logger.info("watch: на связи, версия %s, %s, заряд %s%%, надеты %s, запуск «%s»", data.get("v"), data.get("device"), w.bat, w.wear,
                data.get("launch") or "")
    _note_sleep(w, w.sleeping, "hello")
    profile = await w.profile()
    prayers = await w.prayers()
    wake_at = 0
    try:
        plan = await app_alarm.next_plan(profile)
        if plan.get("on") and plan.get("at_ms"):
            wake_at = int(plan["at_ms"]) // 1000
    except Exception:
        logger.warning("watch: план подъёма не получил", exc_info=True)
    await w.tick()
    return w.reply(prayers=prayers, wake_at=wake_at, cfg={"gate": "auto", "gateK": 1.5, "keepScreen": True, "listen": True})


async def audio(uid: int, data: dict[str, Any]) -> dict[str, Any]:
    w = get(uid)
    w.touch(data)
    try:
        raw = base64.b64decode(str(data.get("b") or ""), validate=False)
    except (binascii.Error, ValueError):
        raw = b""
    if raw:
        try:
            pcm = await opus_to_pcm(raw)
        except DecodeError as exc:
            w.decode_fail += 1
            pcm = b""
            if w.decode_fail <= 3 or w.decode_fail % 100 == 0:
                logger.warning("watch: кусок не раскодировался (%s байт, начало %s): %s", len(raw), raw[:16].hex(), exc)
                _dump_raw(uid, raw, w.decode_fail)
        if pcm:
            await w.feed(pcm, data)
    await w.tick()
    return w.reply()


def _dump_raw(uid: int, raw: bytes, n: int) -> None:
    """Первые не раскодированные куски — в файл: разобрать формат вручную."""
    if n > 3:
        return
    try:
        (_dir() / f"raw_{uid}_{n}.bin").write_bytes(raw)
    except OSError:
        pass


# Zepp обрывает запрос к серверу примерно через 10 с (в логах nginx 499) — держим опрос заметно короче
POLL_WAIT_S = 8.0


async def poll(uid: int, sid: str, wait: float = POLL_WAIT_S) -> dict[str, Any]:
    w = get(uid)
    w.touch({"sid": sid})
    await w.tick()
    if w.fajr_since and await _woke(uid):
        w.fajr_since = 0.0
        w.push({"t": "fajr_stop"})
    if not w.events:
        try:
            await asyncio.wait_for(w.evt.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    w.seen = time.monotonic()
    return w.reply()


async def _woke(uid: int) -> bool:
    from . import services

    try:
        profile = await get(uid).profile()
        log = await services.wake_log(uid, profile.today) or {}
        return bool(log.get("woke_at"))
    except Exception:
        return False


async def event(uid: int, data: dict[str, Any]) -> dict[str, Any]:
    w = get(uid)
    w.touch(data)
    kind = str(data.get("kind") or "")
    if kind == "start":
        w.start(str(data.get("by") or "button"))
    elif kind == "stop":
        if w.conv is not None:
            w.conv.stop()
    elif kind == "played":
        pass
    elif kind == "fajr_ring":
        w.fajr_since = time.monotonic()
        logger.info("watch: фаджр — часы вибрируют")
    elif kind == "state":
        _save_state(uid, {"wear": w.wear, "bat": w.bat, "sleeping": w.sleeping, "seen_at": time.time()})
        _note_sleep(w, w.sleeping, "часы")
        _append(uid, "events", {"at": time.time(), "ev": "state", "wear": w.wear, "bat": w.bat, "sleeping": w.sleeping})
    elif kind == "sys":
        for item in (data.get("list") or [])[:60]:
            if not isinstance(item, dict):
                continue
            _append(uid, "events", {"at": item.get("t") or time.time(), **{k: v for k, v in item.items() if k != "t"}})
            name = str(item.get("ev") or "")
            if "sleep_status" in name:
                _note_sleep(w, int(item.get("sleeping", -1)), "событие сна")
            if "heart_rate_abnl" in name:
                logger.info("watch: часы отметили необычный пульс (%s)", item.get("hr"))
        logger.info("watch: события часов: %s", ", ".join(str(i.get("ev")) for i in (data.get("list") or [])[:10] if isinstance(i, dict)))
    elif kind == "bye":
        logger.info("watch: экран JES закрыт, статистика %s", json.dumps(data.get("stats") or {}, ensure_ascii=False)[:400])
        if w.conv is not None:
            w.conv.stop()
    return w.reply()


async def health(uid: int, data: dict[str, Any]) -> dict[str, Any]:
    w = get(uid)
    w.touch(data)
    row = {k: data.get(k) for k in ("hr", "hr_rest", "stress", "steps", "kcal", "spo2", "bat", "wear", "sleeping", "sleep")}
    row["at"] = time.time()
    _append(uid, "health", row)
    _save_state(uid, {"health": row, "wear": w.wear, "bat": w.bat, "sleeping": w.sleeping, "seen_at": time.time()})
    _note_sleep(w, w.sleeping, "сводка")
    return w.reply()


def note_log(uid: int, data: dict[str, Any]) -> dict[str, Any]:
    from . import journal

    w = get(uid)
    w.touch(data)
    text = str(data.get("text") or "")[:300]
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    logger.info("watch log: %s | %s | сервер: кусков %s, громкость до %.0f, не раскодировано %s", text,
                json.dumps(stats, ensure_ascii=False)[:500], w.chunks, w.rms_max, w.decode_fail)
    if text and text != "periodic":
        journal.miss(uid, "app_error", f"часы: {text}")
    _save_state(uid, {"stats": stats, "seen_at": time.time()})
    return w.reply()


# ------------------------------------------------------------------ для промптов и брифа
def _fmt_int(v: Any) -> str:
    try:
        return f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return ""


def health_line(uid: int, *, max_age_s: float = HEALTH_FRESH_S) -> str:
    """«ЧАСЫ (14:20): пульс 72, покоя 58, шагов 3 400, стресс 35, SpO2 97%, сон 6 ч 10 мин (оценка 78), заряд 64%»."""
    st = load_state(uid)
    h = st.get("health") if isinstance(st.get("health"), dict) else None
    if not h or time.time() - float(h.get("at") or 0) > max_age_s:
        return ""
    parts = []
    if h.get("hr"):
        parts.append(f"пульс {h['hr']}" + (f" (покоя {h['hr_rest']})" if h.get("hr_rest") else ""))
    if h.get("steps") is not None:
        parts.append(f"шагов {_fmt_int(h['steps'])}")
    if h.get("stress"):
        parts.append(f"стресс {h['stress']}/100")
    if h.get("spo2"):
        parts.append(f"SpO2 {h['spo2']}%")
    sl = h.get("sleep") if isinstance(h.get("sleep"), dict) else None
    if sl and sl.get("total"):
        total = int(sl["total"])
        parts.append(f"сон {total // 60} ч {total % 60} мин" + (f" (оценка {sl['score']})" if sl.get("score") else ""))
    if isinstance(h.get("bat"), (int, float)) and h["bat"] >= 0:
        parts.append(f"заряд часов {int(h['bat'])}%")
    if not parts:
        return ""
    at = datetime.fromtimestamp(float(h["at"]))
    return f"ЧАСЫ ({at:%H:%M}): " + ", ".join(parts) + "."


def sleep_line(uid: int) -> str:
    """Для утреннего брифа: «⌚ Сон по часам: 6 ч 10 мин, глубокий 1 ч 20 мин, оценка 78»."""
    st = load_state(uid)
    h = st.get("health") if isinstance(st.get("health"), dict) else None
    if not h or time.time() - float(h.get("at") or 0) > 14 * 3600:
        return ""
    sl = h.get("sleep") if isinstance(h.get("sleep"), dict) else None
    if not sl or not sl.get("total"):
        return ""
    total, deep = int(sl["total"]), int(sl.get("deep") or 0)
    line = f"⌚ Сон по часам: {total // 60} ч {total % 60} мин"
    if deep:
        line += f", глубокий {deep // 60} ч {deep % 60} мин"
    if sl.get("score"):
        line += f", оценка {sl['score']}"
    return line


async def evening_guard(bot, profile) -> bool:  # noqa: ANN001
    """Вечером, один раз: утром часы должны провибрировать с будильником — сняты или почти разряжены → сказать сейчас."""
    from . import app_alarm

    now = profile.now
    if (now.hour, now.minute) < (21, 30):
        return False
    uid = profile.telegram_id
    st = load_state(uid)
    if not st or time.time() - float(st.get("seen_at") or 0) > 3 * 86400:
        return False   # часов нет или давно не выходили на связь
    today = profile.today.isoformat()
    if st.get("guard") == today:
        return False
    _save_state(uid, {"guard": today})
    try:
        plan = await app_alarm.next_plan(profile)
    except Exception:
        plan = {}
    if not plan.get("on"):
        return False
    bat = st.get("bat")
    text = ""
    if isinstance(bat, (int, float)) and 0 <= bat < LOW_BATTERY_EVENING:
        text = (f"⌚ На часах {int(bat)}% — зарядите, чтобы утром{(' в ' + plan['wake_at']) if plan.get('wake_at') else ''} они "
                "провибрировали. Звонок Telegram разбудит в любом случае.")
    elif st.get("wear") == 0 and now.hour >= 21:
        text = ("⌚ Часы сейчас сняты — утром они не провибрируют на руке. Наденьте на ночь, если хотите вибрацию. "
                "Звонок Telegram разбудит в любом случае.")
    if not text:
        return False
    try:
        await bot.send_message(uid, text, disable_notification=False)
    except Exception:
        logger.warning("watch guard: не отправилось", exc_info=True)
        return False
    return True


__all__ = ["hello", "audio", "poll", "event", "health", "note_log", "should_answer", "take_over", "phone_in_hand", "buzz",
           "health_line", "sleep_line", "evening_guard", "load_state", "get", "opus_to_pcm", "pcm_to_mp3"]
