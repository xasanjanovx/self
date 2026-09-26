"""Живой разговор в Telegram-звонке: JES говорит, слушает ответ и отвечает.

Как устроено:
  play()   — то, что JES говорит (синтез Gemini TTS → PCM → файл);
  record() — входящий звук звонка приходит кадрами (PyTgCalls StreamFrames);
  VoiceBuffer — копит кадры и по паузе решает, что фраза закончилась (VAD по громкости);
  далее: расшифровка (Gemini) → короткий ответ JES → снова play().

Запасной путь, если Gemini Live недоступен. Разговор короткий и с одной целью: мотивировать встать
на намаз и убедиться по ответам, что человек реально встал, и не дать снова лечь. Он заканчивается,
когда подтверждение получено, или по лимиту реплик/времени — трубку кладём сами.

Чистые части (VoiceBuffer, реплики, решение «встал/не встал») тестируются без сети.
"""
from __future__ import annotations

import array
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000          # как у Gemini TTS: меньше пересчётов
CHANNELS = 1
SILENCE_RMS = 450            # тише этого считаем тишиной
MIN_SPEECH_MS = 400          # короче — не фраза, а щелчок
END_SILENCE_MS = 900         # столько тишины = человек договорил
MAX_UTTERANCE_MS = 12000
MAX_TURNS = 8
MAX_CALL_SECONDS = 180

_AWAKE_STRONG = ("встал", "проснул", "не сплю", "уже на ногах", "turdim", "uyg'ondim", "uygondim", "tikman")
_STILL_SLEEPING = ("сплю", "ещё пять", "еще пять", "щас", "сейчас встану", "yana", "besh daqiqa", "uxlayapman")


def rms(pcm: bytes) -> float:
    """Громкость куска PCM s16le."""
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(float(s) * s for s in samples) / len(samples))


def ms_of(pcm: bytes, *, rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> int:
    return int(len(pcm) / 2 / max(1, channels) / max(1, rate) * 1000)


@dataclass
class VoiceBuffer:
    """Собирает речь собеседника и отдаёт фразу целиком, когда он замолчал."""
    rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    silence_rms: float = SILENCE_RMS
    speech: bytearray = field(default_factory=bytearray)
    silence_ms: int = 0
    speech_ms: int = 0

    def reset(self) -> None:
        self.speech = bytearray()
        self.silence_ms = 0
        self.speech_ms = 0

    def feed(self, pcm: bytes) -> bytes | None:
        """Вернёт фразу (PCM), если она закончилась; иначе None."""
        chunk_ms = ms_of(pcm, rate=self.rate, channels=self.channels)
        loud = rms(pcm) >= self.silence_rms
        if loud:
            self.speech.extend(pcm)
            self.speech_ms += chunk_ms
            self.silence_ms = 0
        elif self.speech_ms:
            self.speech.extend(pcm)  # хвост тишины оставляем — так лучше распознаётся
            self.silence_ms += chunk_ms
        if self.speech_ms >= MAX_UTTERANCE_MS or (self.speech_ms >= MIN_SPEECH_MS and self.silence_ms >= END_SILENCE_MS):
            out = bytes(self.speech)
            self.reset()
            return out
        if not self.speech_ms and self.silence_ms:
            self.silence_ms = 0
        return None


def sounds_awake(text: str) -> bool:
    """Ответ в трубке звучит как «я действительно встал»."""
    low = f" {str(text or '').lower()} "
    if any(w in low for w in _STILL_SLEEPING):
        return False
    if any(w in low for w in _AWAKE_STRONG):
        return True
    return len(low.split()) >= 4  # связная фраза спросонья — уже признак, что человек проснулся


@dataclass
class DialogState:
    lang: str = "uz"
    name: str = ""
    takbir: str | None = None
    minutes_left: int | None = None
    task_text: str = ""
    turns: int = 0
    confirmed: bool = False
    transcript: list[str] = field(default_factory=list)


def greeting(state: DialogState) -> str:
    uz = state.lang != "ru"
    if uz:
        parts = [f"Assalomu alaykum, {state.name or 'do’st'}."]
        if state.takbir and state.minutes_left is not None:
            parts.append(f"Bomdod takbiriga {state.minutes_left} daqiqa qoldi, takbir {state.takbir} da.")
        parts.append("Turdingizmi? Gapiring, ovozingizni eshitay.")
        return " ".join(parts)
    parts = [f"Ассалому алайкум, {state.name or 'друг'}."]
    if state.takbir and state.minutes_left is not None:
        parts.append(f"До такбира {state.minutes_left} минут, такбир в {state.takbir}.")
    parts.append("Ты встал? Скажи что-нибудь, я слушаю.")
    return " ".join(parts)


def nudge(state: DialogState) -> str:
    """Если молчит — подталкиваем, каждый раз настойчивее."""
    uz = state.lang != "ru"
    steps_uz = ["Eshitmadim. Gapiring, iltimos.", "Turing, yotmang — namoz uyqudan yaxshiroq!", "Oyoqqa turing, Alloh sizdan rozi bo'lsin. Menga gapiring."]
    steps_ru = ["Не слышу. Скажите что-нибудь.", "Вставайте, не ложитесь — намаз лучше сна!", "Поднимайтесь, пусть Аллах будет доволен вами. Ответьте мне."]
    steps = steps_uz if uz else steps_ru
    return steps[min(state.turns, len(steps) - 1)]


def task_line(state: DialogState) -> str:
    uz = state.lang != "ru"
    if not state.task_text:
        return "Yaxshi, turdingiz. Barakalla." if uz else "Хорошо, ты встал. Молодец."
    return (f"Endi vazifa: {state.task_text}" if uz else f"Теперь задание: {state.task_text}")


def farewell(state: DialogState) -> str:
    uz = state.lang != "ru"
    if state.confirmed:
        if state.takbir:
            return (f"Zo'r. Takbir {state.takbir} da — kechikmang. Xayr." if uz else f"Отлично. Такбир в {state.takbir} — не опоздай. До связи.")
        return "Zo'r, xayr." if uz else "Отлично, до связи."
    return ("Men yana qo'ng'iroq qilaman." if uz else "Я перезвоню.")


def system_prompt(state: DialogState) -> str:
    """Промпт для коротких ответов в трубке — не длиннее одного предложения."""
    lang = "o'zbek tilida (lotin yozuvi)" if state.lang != "ru" else "по-русски"
    return (
        f"Ты — JES (читается «Джес»), личный помощник {state.name or 'пользователя'}. Сейчас раннее утро, ты ЗВОНИШЬ ему по телефону, "
        f"чтобы он встал на намаз фаджр. Отвечай {lang}, ОДНИМ коротким предложением (до 12 слов), как живой человек по телефону: "
        "спокойно, дружелюбно, без пафоса и религиозных нотаций.\n"
        f"Такбир: {state.takbir or 'скоро'}.\n"
        "Твоя цель: мотивировать встать добрыми словами («намаз лучше сна», «вы же не мунафик», «пусть Аллах будет доволен вами», "
        "«пусть вам будет рай») и убедиться, что он реально встал — пусть ответит бодро и связно. Никаких заданий и упражнений. "
        "Если он говорит, что встал, — коротко подтверди и напомни про время. Если просит ещё поспать — мягко, но твёрдо не соглашайся. "
        "Никаких списков, эмодзи и markdown — это устная речь."
    )


def decide(state: DialogState, text: str) -> dict[str, Any]:
    """Что делать после реплики собеседника: продолжать, подтвердить подъём, завершить."""
    state.turns += 1
    if text:
        state.transcript.append(f"он: {text}")
    if text and sounds_awake(text):
        state.confirmed = True
        return {"action": "confirm", "say": task_line(state)}
    if not text:
        return {"action": "nudge", "say": nudge(state)}
    return {"action": "reply", "say": None}  # ответ придумает модель


# ------------------------------------------------------------------ звук
async def pcm_to_file(pcm: bytes, *, rate: int = SAMPLE_RATE) -> str | None:
    """PCM → временный .wav для проигрывания в звонке (pytgcalls читает через ffmpeg)."""
    import wave

    if not pcm:
        return None
    fd, path = tempfile.mkstemp(prefix="say_", suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as fh:
            fh.setnchannels(CHANNELS)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            fh.writeframes(pcm)
        return path
    except Exception:
        logger.warning("pcm_to_file failed", exc_info=True)
        try:
            os.remove(path)
        except OSError:
            pass
        return None


async def pcm_to_ogg_file(pcm: bytes, *, rate: int = SAMPLE_RATE) -> str | None:
    """PCM → .ogg (для распознавания речи Gemini)."""
    from . import voice

    data = await voice.pcm_to_ogg(pcm, rate=rate)
    if not data:
        return None
    fd, path = tempfile.mkstemp(prefix="heard_", suffix=".ogg")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def cleanup(*paths: str | None) -> None:
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


__all__ = [
    "VoiceBuffer", "DialogState", "rms", "ms_of", "sounds_awake", "greeting", "nudge", "task_line", "farewell",
    "system_prompt", "decide", "pcm_to_file", "pcm_to_ogg_file", "cleanup",
    "SAMPLE_RATE", "MAX_TURNS", "MAX_CALL_SECONDS",
]
