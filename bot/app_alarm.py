"""Будильник на фаджр в самом приложении JES (26.09.2026, его выбор «у меня — на программе, у других — Telegram»).

Приложение раз в несколько часов берёт время подъёма (GET /jarvis/v1/wake_plan) и ставит настоящий будильник
Android: он звенит через канал будильника — громко, на беззвучном и в «Не беспокоить», без интернета и бесплатно.
Нажал «Проснулся» — сервер отмечает подъём (Telegram не звонит), и JES голосом: доброе утро + вопрос дня.
«Ещё 5 минут» — пауза. Не ответил за APP_GRACE после звонка будильника — запасной звонок в Telegram, как у всех.
Состояние — DATA_DIR/app_alarm.json: {uid: {"day": "2026-09-27", "at_ms": …}}.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

APP_GRACE = timedelta(minutes=5)


def _file():
    from .tg_user import data_dir

    return data_dir() / "app_alarm.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        _file().write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        logger.warning("app alarm: не сохранил", exc_info=True)


def scheduled(uid: int, day_iso: str, at_ms: int) -> None:
    """Приложение поставило будильник Android на этот день и время."""
    data = _load()
    data[str(uid)] = {"day": day_iso, "at_ms": int(at_ms), "set_at": datetime.now(timezone.utc).isoformat()}
    _save(data)


def holds_telegram(uid: int, day_iso: str, wake_at: datetime, now: datetime) -> bool:
    """Будильник в приложении на этот день стоит, и его время + APP_GRACE ещё не прошло — Telegram пока молчит."""
    st = _load().get(str(uid)) or {}
    if st.get("day") != day_iso:
        return False
    return now < wake_at + APP_GRACE


async def next_plan(profile) -> dict[str, Any]:  # noqa: ANN001
    """Ближайший подъём: сегодня (если ещё впереди и не встал) или завтра."""
    from . import services, wake_runner

    from . import places

    now = datetime.now(timezone.utc)
    s = None
    for day in (profile.today, profile.today + timedelta(days=1)):
        s, plan = await wake_runner.plan_for(profile, day)
        if not plan.active or plan.wake_at is None or plan.wake_at <= now:
            continue
        log = await services.wake_log(profile.telegram_id, day) or {}
        if log.get("woke_at"):
            continue
        return {"enabled": True, "day": day.isoformat(), "at_ms": int(plan.wake_at.timestamp() * 1000), "wake_at": plan.wake_at.strftime("%H:%M"),
                "takbir": plan.takbir, "fajr": plan.fajr, "window": list(plan.window) if plan.window else None,
                "offset_min": s.offset_min, "place": places.label(profile.telegram_id, profile.lang)}
    return {"enabled": False, "on": bool(s and s.enabled), "offset_min": s.offset_min if s else None,
            "place": places.label(profile.telegram_id, profile.lang)}


def morning_note(profile, persona) -> str:  # noqa: ANN001
    """Пометка для JES на телефоне: он только что встал по будильнику — доброе утро и вопрос дня (как в звонке)."""
    from . import islam_quiz

    title = {"shef": "Шеф", "ser": "Сэр", "boss": "Босс", "mix": "Шеф"}.get(persona.honorific, profile.first_name or "")
    quiz = islam_quiz.for_day(profile.telegram_id, profile.today)
    return (f"[Утро: он только что встал по будильнику JES. Бодро и тепло: «Доброе утро, {title}!» — и сразу ВОПРОС ДНЯ ниже, "
            "один, коротко, как викторину. Ответит: верно — коротко похвали; неверно или не знает — спокойно скажи правильный ответ; "
            "дуа или аят — арабский текст ТОЧНО как написан, слово в слово, потом коротко смысл. В конце одной фразой — "
            "«Пусть Аллах примет ваш намаз». Коротко, без лекций.]\n" + islam_quiz.prompt_block(quiz))


__all__ = ["APP_GRACE", "scheduled", "holds_telegram", "next_plan", "morning_note"]
