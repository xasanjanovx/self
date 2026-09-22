"""Подъём на фаджр: во сколько будить, когда перезванивать, чем подтвердить подъём.

Здесь только чистая логика и тексты (без БД, Telegram и звонков) — её легко тестировать:
  plan_for_day()   — будим ли сегодня и во сколько (фаджр − offset или фиксированное время);
  should_call()    — пора ли звонить прямо сейчас и какая это попытка;
  make_task()      — задание дня: вода (фото пустого стакана), приседания (голосом),
                     вопрос (счёт в уме), хадис вслух; набор задан в настройках;
  check_answer()   — принят ли ответ на задание;
  looks_awake()    — «проснулся / uyg'ondim / встал» в свободном тексте;
  snooze_minutes() — «ещё 10 минут» из фразы.
Звонок делает bot/caller.py, расписание — bot/workers.py.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

TASKS = ("water", "squats", "question", "hadith")
DEFAULT_TASKS = ("water", "squats", "question", "hadith")
CONFIRM_MINUTES = 3  # столько ждём подтверждения после звонка, потом звоним снова
MIN_VOICE_SECONDS = 5

_AWAKE_WORDS = (
    "проснулся", "проснулась", "встал", "встала", "я встал", "не сплю", "уже встал", "просыпаюсь", "подъем", "подъём",
    "uyg'ondim", "uygondim", "turdim", "uyg'onib", "tura qoldim", "uyqudan turdim",
    "awake", "im up", "i'm up",
)
_SNOOZE_RE = re.compile(r"(?:ещ[её]|yana|через|keyin)\s*(\d{1,2})\s*(?:мин|min|daqiqa)?", re.IGNORECASE)
_SQUAT_WORDS = ("присед", "отжим", "cho'kkalash", "chokkalash", "otjimanie", "qaddi")


@dataclass
class WakeSettings:
    enabled: bool = True
    mode: str = "fajr"  # fajr | fixed
    fixed_time: str | None = None
    offset_min: int = 25
    takbir_offset_min: int = 20
    days_of_week: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)
    call_enabled: bool = True
    max_attempts: int = 20
    retry_seconds: int = 45
    confirm_tasks: tuple[str, ...] = DEFAULT_TASKS
    hardness: str = "normal"
    skip_until: date | None = None
    latitude: float = 40.7821
    longitude: float = 72.3442
    calc_method: int = 3

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "WakeSettings":
        row = row or {}
        def _d(value: Any) -> date | None:
            try:
                return date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                return None
        tasks = tuple(t for t in (row.get("confirm_tasks") or DEFAULT_TASKS) if t in TASKS) or DEFAULT_TASKS
        days = tuple(int(d) for d in (row.get("days_of_week") or (1, 2, 3, 4, 5, 6, 7)))
        return cls(
            enabled=bool(row.get("enabled", True)), mode=str(row.get("mode") or "fajr"), fixed_time=row.get("fixed_time"),
            offset_min=int(row.get("offset_min") or 25), takbir_offset_min=int(row.get("takbir_offset_min") or 20),
            days_of_week=days or (1, 2, 3, 4, 5, 6, 7), call_enabled=bool(row.get("call_enabled", True)),
            max_attempts=int(row.get("max_attempts") or 20), retry_seconds=int(row.get("retry_seconds") or 45),
            confirm_tasks=tasks, hardness=str(row.get("hardness") or "normal"), skip_until=_d(row.get("skip_until")),
            latitude=float(row.get("latitude") or 40.7821), longitude=float(row.get("longitude") or 72.3442),
            calc_method=int(row.get("calc_method") or 3),
        )


@dataclass
class DayPlan:
    day: date
    active: bool = False              # будим ли сегодня
    reason: str = ""                  # off | day_off | skip | no_times
    wake_at: datetime | None = None   # когда звонить
    takbir_at: datetime | None = None
    fajr: str | None = None
    takbir: str | None = None
    flags: list[str] = field(default_factory=list)


def _combine(day: date, t: time, tz: Any) -> datetime:
    return datetime.combine(day, t, tzinfo=tz)


def plan_for_day(s: WakeSettings, day: date, *, tz: Any, timings: dict[str, str] | None = None) -> DayPlan:
    """Во сколько будить в этот день. `timings` — из bot/prayer.timings()."""
    from . import prayer

    plan = DayPlan(day=day, fajr=(timings or {}).get("Fajr"))
    if not s.enabled:
        plan.reason = "off"
        return plan
    if (day.weekday() + 1) not in s.days_of_week:
        plan.reason = "day_off"
        return plan
    if s.skip_until and day <= s.skip_until:
        plan.reason = "skip"
        return plan
    if s.mode == "fixed":
        t = prayer.parse_hhmm(s.fixed_time)
        if t is None:
            plan.reason = "no_times"
            return plan
        plan.wake_at = _combine(day, t, tz)
        plan.active = True
        if timings:
            tk = prayer.takbir_time(plan.fajr, s.takbir_offset_min)
            if tk:
                plan.takbir, plan.takbir_at = tk.strftime("%H:%M"), _combine(day, tk, tz)
        return plan
    tk = prayer.takbir_time(plan.fajr, s.takbir_offset_min)
    if tk is None:
        plan.reason = "no_times"
        return plan
    plan.takbir, plan.takbir_at = tk.strftime("%H:%M"), _combine(day, tk, tz)
    plan.wake_at = plan.takbir_at - timedelta(minutes=max(0, s.offset_min))
    plan.active = True
    return plan


def should_call(plan: DayPlan, log: dict[str, Any] | None, now: datetime, s: WakeSettings) -> tuple[bool, str]:
    """(звонить ли сейчас, причина). Звоним, пока не подтвердил подъём."""
    if not plan.active or plan.wake_at is None:
        return False, plan.reason or "inactive"
    log = log or {}
    if log.get("woke_at"):
        return False, "already_awake"
    if now < plan.wake_at:
        return False, "too_early"
    if now - plan.wake_at > timedelta(hours=2):
        return False, "too_late"
    attempts = int(log.get("attempts") or 0)
    if attempts >= max(1, s.max_attempts):
        return False, "max_attempts"
    last = log.get("last_attempt_at")
    if isinstance(last, datetime) and (now - last).total_seconds() < max(20, s.retry_seconds):
        return False, "cooldown"
    return True, "call"


def looks_awake(text: str) -> bool:
    low = f" {str(text or '').strip().lower()} "
    return any(w in low for w in _AWAKE_WORDS)


def snooze_minutes(text: str) -> int | None:
    low = str(text or "").lower()
    if not any(w in low for w in ("ещ", "yana", "через", "keyin", "snooze", "пять", "десять")):
        return None
    m = _SNOOZE_RE.search(low)
    if m:
        value = int(m.group(1))
        return value if 1 <= value <= 30 else None
    if "пять" in low or "besh" in low:
        return 5
    if "десять" in low or "o'n" in low:
        return 10
    return None


# ------------------------------------------------------------------ задания
def _question(rnd: random.Random, hard: bool) -> tuple[str, str]:
    if hard:
        a, b = rnd.randint(12, 29), rnd.randint(12, 19)
        return f"{a} × {b}", str(a * b)
    a, b = rnd.randint(6, 12), rnd.randint(6, 12)
    return f"{a} × {b}", str(a * b)


def make_task(s: WakeSettings, day: date, *, lang: str = "ru", verse_ref: str | None = None) -> dict[str, Any]:
    """Задание дня: один и тот же день → одно и то же задание (не зависит от перезапусков)."""
    allowed = [t for t in s.confirm_tasks if t in TASKS] or list(DEFAULT_TASKS)
    rnd = random.Random(day.toordinal())
    kind = allowed[rnd.randrange(len(allowed))]
    hard = s.hardness == "hard"
    uz = lang == "uz"
    if kind == "water":
        reps = "2" if hard else "1"
        text = (f"{reps} stakan suv iching va bo'sh stakanni suratga olib yuboring 💧" if uz
                else f"Выпей {reps} стакан{'а' if hard else ''} воды и пришли фото пустого стакана 💧")
        return {"kind": kind, "text": text, "answer": None, "expects": "photo"}
    if kind == "squats":
        count = 20 if hard else 10
        text = (f"{count} marta cho'kkalab turing va ovozli xabarda sanab yuboring 🏋️" if uz
                else f"{count} приседаний — считай вслух и пришли голосовое 🏋️")
        return {"kind": kind, "text": text, "answer": str(count), "expects": "voice"}
    if kind == "hadith":
        text = ("Kun oyatini ovoz chiqarib o'qing va ovozli xabar qilib yuboring 📖" if uz
                else "Прочитай аят дня вслух и пришли голосовым 📖") + (f" ({verse_ref})" if verse_ref else "")
        return {"kind": kind, "text": text, "answer": None, "expects": "voice"}
    question, answer = _question(rnd, hard)
    text = (f"Javob bering: {question} = ?" if uz else f"Ответь: {question} = ?")
    return {"kind": kind, "text": text, "answer": answer, "expects": "text"}


def check_answer(task: dict[str, Any], *, text: str | None = None, has_photo: bool = False, voice_seconds: int = 0,
                 transcript: str | None = None) -> tuple[bool, str]:
    """(принято ли, короткий комментарий). Мы не придираемся: важно, что человек встал."""
    expects = str(task.get("expects") or "text")
    body = f"{text or ''} {transcript or ''}".strip()
    if expects == "photo":
        if has_photo:
            return True, "ok"
        if looks_awake(body) and len(body) > 0:
            return False, "need_photo"
        return False, "need_photo"
    if expects == "voice":
        if voice_seconds >= MIN_VOICE_SECONDS:
            return True, "ok"
        if voice_seconds > 0:
            return False, "too_short"
        return False, "need_voice"
    answer = str(task.get("answer") or "").strip()
    digits = re.findall(r"-?\d+", body)
    if answer and digits and any(d == answer for d in digits):
        return True, "ok"
    if answer and digits:
        return False, "wrong"
    return False, "need_answer"


# ------------------------------------------------------------------ тексты
def call_script(*, name: str, takbir: str | None, minutes_left: int | None, task_text: str, verse_text: str = "",
                next_thing: str = "", lang: str = "uz") -> str:
    """Что «Джарвис» говорит в трубку — коротко, на узбекском по умолчанию."""
    if lang == "uz":
        parts = [f"Assalomu alaykum, {name}." ]
        if takbir and minutes_left is not None:
            parts.append(f"Bomdod takbiri {takbir} da, {minutes_left} daqiqa qoldi.")
        elif takbir:
            parts.append(f"Bomdod takbiri {takbir} da.")
        parts.append("Turing, iltimos.")
        if task_text:
            parts.append(f"Vazifa: {task_text}")
        if verse_text:
            parts.append(verse_text)
        if next_thing:
            parts.append(next_thing)
        parts.append("Turganingizni tasdiqlang.")
        return " ".join(parts)
    parts = [f"Ассалому алайкум, {name}."]
    if takbir and minutes_left is not None:
        parts.append(f"Такбир фаджра в {takbir}, осталось {minutes_left} минут.")
    parts.append("Вставай.")
    if task_text:
        parts.append(f"Задание: {task_text}")
    if verse_text:
        parts.append(verse_text)
    if next_thing:
        parts.append(next_thing)
    parts.append("Подтверди, что встал.")
    return " ".join(parts)


def wake_message(*, name: str, plan: DayPlan, task: dict[str, Any], attempt: int, lang: str = "ru") -> str:
    uz = lang == "uz"
    head = "⏰ <b>" + ("Turish vaqti" if uz else "Подъём") + "</b>"
    lines = [head]
    if plan.takbir:
        left = ""
        if plan.takbir_at and plan.wake_at:
            left = f" ({int((plan.takbir_at - plan.wake_at).total_seconds() // 60)} " + ("daqiqa qoldi" if uz else "мин до него") + ")"
        lines.append(("Bomdod takbiri" if uz else "Такбир фаджра") + f": <b>{plan.takbir}</b>{left}")
    lines.append("")
    lines.append(("Vazifa" if uz else "Задание") + f": {task.get('text')}")
    if attempt > 1:
        lines.append("")
        lines.append(("Urinish" if uz else "Попытка") + f" {attempt}")
    return "\n".join(lines)


def done_message(*, plan: DayPlan, now: datetime, lang: str = "ru", streak: int = 0) -> str:
    uz = lang == "uz"
    lines = ["✅ <b>" + ("Barakalla!" if uz else "Молодец!") + "</b>"]
    if plan.takbir_at:
        left = int((plan.takbir_at - now).total_seconds() // 60)
        if left >= 0:
            lines.append((f"Takbirgacha {left} daqiqa bor." if uz else f"До такбира {left} мин."))
        else:
            lines.append(("Takbir o'tib ketdi — ertaga erta turamiz." if uz else "Такбир уже прошёл — завтра встаём раньше."))
    if streak > 1:
        lines.append(f"🔥 {streak} " + ("kun ketma-ket" if uz else "дн. подряд"))
    return "\n".join(lines)


def stats_line(rows: list[dict[str, Any]], lang: str = "ru") -> str | None:
    """Строка для недельного разбора: сколько раз встал до такбира и за сколько попыток."""
    if not rows:
        return None
    uz = lang == "uz"
    woke = [r for r in rows if r.get("woke_at")]
    before = len([r for r in woke if r.get("before_takbir")])
    attempts = [int(r.get("attempts") or 0) for r in woke if r.get("attempts")]
    avg = round(sum(attempts) / len(attempts), 1) if attempts else 0
    return (f"⏰ {len(woke)}/{len(rows)} kun turdingiz, {before} marta takbirgacha, o'rtacha {avg} qo'ng'iroq." if uz
            else f"⏰ Встал {len(woke)} из {len(rows)} дней, до такбира — {before}, в среднем {avg} звонка.")


def streak_days(rows: list[dict[str, Any]], today: date) -> int:
    """Серия дней подряд, когда встал до такбира (сегодня считается, если уже встал)."""
    by_day = {str(r.get("day"))[:10]: r for r in rows}
    streak, day = 0, today
    while True:
        row = by_day.get(day.isoformat())
        if not row or not row.get("woke_at") or not row.get("before_takbir"):
            break
        streak += 1
        day -= timedelta(days=1)
    return streak


__all__ = [
    "WakeSettings", "DayPlan", "TASKS", "CONFIRM_MINUTES", "plan_for_day", "should_call", "make_task", "check_answer",
    "looks_awake", "snooze_minutes", "call_script", "wake_message", "done_message", "stats_line", "streak_days",
]
