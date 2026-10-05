"""Память ТЕКУЩЕГО разговора (05.10.2026): «спрашиваю одно, он отвечает, спрашиваю дальше — а он уже про другое».

Причина: после ответа телефон закрывает сессию (15 с тишины), следующая фраза — новая сессия Gemini без прошлой,
а в «недавних репликах» от ответа оставалось 90 знаков без предмета. Здесь — короткая живая память в оперативной памяти
процесса (без базы, без токенов на запись): последние несколько обменов за ~25 минут и «предмет» разговора
(фильм, человек, город — из поиска). Всё, что пишет remember_exchange (чат, телефон, звонок, часы), попадает сюда само.

Блок идёт в промпты голоса, звонков, помощника-из-чата и в запросы поиска. Тишина дольше TTL — блок пустой и ничего
не стоит. Для голоса он короткий: Gemini Live оплачивает инструкцию в каждом ответе.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

TTL_S = 25 * 60          # дольше — разговор считается новым
TOPIC_TTL_S = 12 * 60    # «предмет» (фильм, человек) живёт короче: тема уходит быстрее, чем помнится сам обмен
MAX_TURNS = 6
SAID_W = 150
ANSWER_W = 230

_MARKS = re.compile(r"^(?:[📱⌚📞💬🎙️]\s*)+")

BLOCK_HEAD = ("ТЕКУЩИЙ РАЗГОВОР (минуты назад; он продолжает ЭТУ тему: короткий вопрос без предмета — «а сколько ему лет?», «а второй?», "
              "«ещё», «почему?», «а там?» — про предмет последнего обмена, а не новая тема; ясно новый вопрос — отвечай на него):")


@dataclass
class _State:
    turns: deque = field(default_factory=lambda: deque(maxlen=MAX_TURNS))
    topic: str = ""
    topic_at: float = 0.0


_state: dict[int, _State] = {}


def _clip(text: str, width: int) -> str:
    s = " ".join(str(text or "").split())
    s = _MARKS.sub("", s)
    if len(s) <= width:
        return s
    cut = s[:width]
    space = cut.rfind(" ")
    return (cut[:space] if space > width * 0.6 else cut).rstrip(" ,.;:—-") + "…"


def _clip_tail(text: str, width: int) -> str:
    """Склеенные «реплика / реплика / реплика» (сессия целиком): при нехватке места теряем начало — последний обмен важнее."""
    s = " ".join(str(text or "").split())
    if len(s) <= width or " / " not in s:
        return _clip(s, width)
    kept: list[str] = []
    for part in reversed(s.split(" / ")):
        if kept and sum(len(p) + 3 for p in kept) + len(part) > width:
            break
        kept.append(part)
    return _clip(" / ".join(reversed(kept)), width)


def _mask(text: str) -> str:
    try:
        from .secrets_guard import mask

        return mask(text)
    except Exception:
        return text


def note(uid: int, said: str, answer: str = "") -> None:
    """Один обмен: что он сказал и что ответили (или «сделано: …»). Пустая реплика не пишется."""
    said = _clip_tail(_mask(said), SAID_W)
    if not said:
        return
    st = _state.setdefault(uid, _State())
    st.turns.append((time.monotonic(), said, _clip_tail(_mask(answer), ANSWER_W)))


def note_turns(uid: int, user_lines: list[str], jarvis_lines: list[str], n: int = 3) -> None:
    """Сессия телефона/часов закончилась: последние n обменов по отдельности. Строки сверяются с конца — у команд, выполненных
    молча, ответа нет, и сдвиг считается от последнего обмена, самого важного для «а теперь…»."""
    users = [u for u in user_lines if str(u or "").strip()]
    answers = [a for a in jarvis_lines if str(a or "").strip()]
    pairs = [(users[-1 - i], answers[-1 - i] if i < len(answers) else "") for i in range(min(n, len(users)))]
    for said, answer in reversed(pairs):
        note(uid, said, answer)


def set_topic(uid: int, topic: str) -> None:
    """Предмет разговора, который знает инструмент: «фильм: Интерстеллар», «человек: Илон Маск». Свободный текст ≤ 80 знаков."""
    topic = _clip(topic, 80)
    if not topic:
        return
    st = _state.setdefault(uid, _State())
    st.topic, st.topic_at = topic, time.monotonic()


def topic(uid: int) -> str:
    st = _state.get(uid)
    if st and st.topic and time.monotonic() - st.topic_at <= TOPIC_TTL_S:
        return st.topic
    return ""


def _fresh(uid: int, n: int) -> list[tuple[float, str, str]]:
    st = _state.get(uid)
    if not st:
        return []
    now = time.monotonic()
    return [t for t in list(st.turns)[-n:] if now - t[0] <= TTL_S]


def block(uid: int, *, n: int = 3, head: bool = True) -> str:
    """Готовый кусок промпта; пусто, если разговор давно закончился."""
    turns = _fresh(uid, n)
    subject = topic(uid)
    if not turns and not subject:
        return ""
    now = time.monotonic()
    lines = [BLOCK_HEAD] if head else []
    if subject:
        lines.append(f"Предмет: {subject}")
    for at, said, answer in turns:
        ago = max(1, round((now - at) / 60))
        lines.append(f"• {ago} мин назад — он: {said}" + (f" → ты: {answer}" if answer else ""))
    return "\n".join(lines)


def last_exchange(uid: int) -> tuple[str, str] | None:
    turns = _fresh(uid, 1)
    return (turns[-1][1], turns[-1][2]) if turns else None


def forget(uid: int) -> None:
    _state.pop(uid, None)


__all__ = ["note", "note_turns", "set_topic", "topic", "block", "last_exchange", "forget", "BLOCK_HEAD"]
