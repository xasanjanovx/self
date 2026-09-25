"""Подъём на фаджр: во сколько будить, когда перезванивать, чем подтвердить подъём.

Здесь только чистая логика и тексты (без БД, Telegram и звонков) — её легко тестировать:
  plan_for_day()   — будим ли сегодня и во сколько (фаджр − offset или фиксированное время);
  should_call()    — пора ли звонить прямо сейчас и какая это попытка;
  motivation()     — слова, чтобы встать на намаз («намаз лучше сна», «вы же не мунафик»,
                     «пусть Аллах будет доволен вами»…) — вместо заданий и упражнений;
  looks_awake()    — «проснулся / uyg'ondim / встал» в свободном тексте;
  snooze_minutes() — «ещё 10 минут» из фразы.
Звонок делает bot/caller.py, расписание — bot/workers.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

CONFIRM_MINUTES = 3  # столько ждём подтверждения после звонка, потом звоним снова

# Мотивация встать на фаджр — вместо упражнений и заданий (по просьбе владельца). Только известное
# и достоверное: «намаз лучше сна» (из азана фаджра), хадисы о фаджре (Бухари, Муслим), пожелания.
MOTIVATION: dict[str, tuple[str, ...]] = {
    "ru": (
        "Пора вставать — намаз лучше сна!",
        "Вставайте, вы же не мунафик: для лицемеров нет намаза тяжелее фаджра.",
        "Пусть Аллах будет доволен вами!",
        "Пусть вам будет рай!",
        "Кто совершил утренний намаз — тот под защитой Аллаха.",
        "Два ракаата перед фаджром лучше этого мира и всего, что в нём.",
        "Шайтан завязал три узла — встаньте, помяните Аллаха, и они развяжутся.",
        "Ангелы собираются на фаджре — пусть запишут вас среди молящихся.",
    ),
    "uz": (
        "Turish vaqti — namoz uyqudan yaxshiroq!",
        "Turing, siz munofiq emassiz-ku: munofiqlarga bomdoddan og'irroq namoz yo'q.",
        "Alloh sizdan rozi bo'lsin!",
        "Jannat sizga nasib qilsin!",
        "Bomdodni o'qigan kishi Allohning himoyasida bo'ladi.",
        "Bomdodning ikki rakat sunnati dunyo va undagi narsalardan yaxshiroq.",
        "Shayton uch tugun bog'lagan — turing, Allohni zikr qiling, tugunlar yechiladi.",
        "Farishtalar bomdodda yig'iladi — sizni namozxonlar qatorida yozishsin.",
    ),
    "en": (
        "Time to get up — prayer is better than sleep!",
        "Get up, you're not a munafiq: no prayer is heavier for the hypocrites than Fajr.",
        "May Allah be pleased with you!",
        "May Paradise be yours!",
        "Whoever prays Fajr is under Allah's protection.",
        "The two rak'ahs before Fajr are better than this world and everything in it.",
        "Shaytan tied three knots — get up, remember Allah, and they come undone.",
        "The angels gather at Fajr — may they write you among those who pray.",
    ),
}

_AWAKE_WORDS = (
    "проснулся", "проснулась", "встал", "встала", "я встал", "не сплю", "уже встал", "просыпаюсь", "подъем", "подъём",
    "uyg'ondim", "uygondim", "turdim", "uyg'onib", "tura qoldim", "uyqudan turdim",
    "awake", "im up", "i'm up",
)
_SNOOZE_RE = re.compile(r"(?:ещ[её]|yana|через|keyin)\s*(\d{1,2})\s*(?:мин|min|daqiqa)?", re.IGNORECASE)


@dataclass
class WakeSettings:
    enabled: bool = True
    mode: str = "fajr"  # fajr | fixed
    fixed_time: str | None = None
    offset_min: int = 25
    takbir_offset_min: int = 20
    days_of_week: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)
    call_enabled: bool = True
    max_attempts: int = 30   # ~35 минут звонков (≈70 с на попытку); встал / нажал «Проснулся» — сразу хватит
    retry_seconds: int = 45
    voice_lang: str = "uz"   # на каком языке Джарвис говорит в трубке
    talk: bool = True        # живой диалог (слушает ответы) или просто говорит и кладёт трубку
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
        days = tuple(int(d) for d in (row.get("days_of_week") or (1, 2, 3, 4, 5, 6, 7)))
        return cls(
            # нет настроек — не будим: звоним только тем, кто сам включил будильник
            enabled=bool(row.get("enabled", False)) if row else False, mode=str(row.get("mode") or "fajr"), fixed_time=row.get("fixed_time"),
            offset_min=int(row.get("offset_min") or 25), takbir_offset_min=int(row.get("takbir_offset_min") or 20),
            days_of_week=days or (1, 2, 3, 4, 5, 6, 7), call_enabled=bool(row.get("call_enabled", True)),
            max_attempts=int(row.get("max_attempts") or 30), retry_seconds=int(row.get("retry_seconds") or 45),
            skip_until=_d(row.get("skip_until")),
            voice_lang=("ru" if str(row.get("voice_lang") or "uz") == "ru" else "uz"), talk=bool(row.get("talk", True)),
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


_SKIP_WORDS = ("не звони", "не звонить", "не надо звонить", "не буди", "не будить", "не надо будить", "перестань звонить",
               "хватит звонить", "qo'ng'iroq qilma", "qongiroq qilma", "uyg'otma", "uygotma", "uyg'otmang", "bezovta qilma")


def looks_skip(text: str) -> bool:
    """«Не звони», «не буди сегодня» — отменить подъём на сегодня (только в контексте будильника)."""
    low = f" {str(text or '').strip().lower()} "
    return any(w in low for w in _SKIP_WORDS)


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
def motivation(day: date, attempt: int = 1, lang: str = "ru") -> str:
    """Фраза дня для подъёма: каждый день и каждая попытка — другая, но без случайности между перезапусками."""
    phrases = MOTIVATION.get(lang, MOTIVATION["ru"])
    return phrases[(day.toordinal() + attempt - 1) % len(phrases)]


# ------------------------------------------------------------------ тексты
def call_script(*, name: str, takbir: str | None, minutes_left: int | None, motivation_text: str = "",
                next_thing: str = "", lang: str = "uz") -> str:
    """Что «ZEKI» говорит в трубку в режиме «просто говорит» (без разговора)."""
    if lang == "uz":
        parts = [f"Assalomu alaykum, {name}."]
        if takbir and minutes_left is not None:
            parts.append(f"Bomdod takbiri {takbir} da, {minutes_left} daqiqa qoldi.")
        elif takbir:
            parts.append(f"Bomdod takbiri {takbir} da.")
        parts.append(motivation_text or "Turing, iltimos.")
        if next_thing:
            parts.append(next_thing)
        parts.append("Turganingizni tasdiqlang.")
        return " ".join(parts)
    if lang == "en":
        parts = [f"Assalamu alaikum, {name}."]
        if takbir and minutes_left is not None:
            parts.append(f"Fajr takbir is at {takbir}, {minutes_left} minutes left.")
        parts.append(motivation_text or "Please get up.")
        if next_thing:
            parts.append(next_thing)
        parts.append("Confirm that you're up.")
        return " ".join(parts)
    parts = [f"Ассалому алайкум, {name}."]
    if takbir and minutes_left is not None:
        parts.append(f"Такбир фаджра в {takbir}, осталось {minutes_left} минут.")
    parts.append(motivation_text or "Вставайте.")
    if next_thing:
        parts.append(next_thing)
    parts.append("Подтвердите, что встали.")
    return " ".join(parts)


def wake_message(*, name: str, plan: DayPlan, attempt: int, lang: str = "ru") -> str:
    uz = lang == "uz"
    head = "⏰ <b>" + ("Turish vaqti" if uz else "Подъём") + "</b>"
    lines = [head]
    if plan.takbir:
        left = ""
        if plan.takbir_at and plan.wake_at:
            left = f" ({int((plan.takbir_at - plan.wake_at).total_seconds() // 60)} " + ("daqiqa qoldi" if uz else "мин до него") + ")"
        lines.append(("Bomdod takbiri" if uz else "Такбир фаджра") + f": <b>{plan.takbir}</b>{left}")
    lines.append("")
    lines.append(f"🤲 <i>{motivation(plan.day, attempt, 'uz' if uz else 'ru')}</i>")
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
    "WakeSettings", "DayPlan", "MOTIVATION", "CONFIRM_MINUTES", "plan_for_day", "should_call", "motivation",
    "looks_awake", "looks_skip", "snooze_minutes", "call_script", "wake_message", "done_message", "stats_line", "streak_days",
]
