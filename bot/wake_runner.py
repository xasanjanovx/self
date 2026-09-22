"""Исполнение подъёма: позвонить, поговорить, дождаться подтверждения, перезвонить.

Чистая логика — в bot/wake.py, разговор — bot/call_dialog.py, сам звонок — bot/caller.py,
расписание — bot/workers.py. Здесь склейка: что сказать, что прислать в чат, как
отметить подъём и когда перестать звонить.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from . import call_dialog as cd
from . import caller
from . import prayer
from . import services
from . import voice
from . import wake as wake_mod
from .context import ai
from .profile import Profile

logger = logging.getLogger(__name__)

# состояние текущих подъёмов в памяти: uid → {"task": …, "plan": …, "attempts": int, "last": datetime}
_active: dict[int, dict[str, Any]] = {}
# uid -> id сообщения «Пора вставать» с кнопками: держим одно на утро, после подъёма убираем
_wake_msg: dict[int, int] = {}
DONE_TTL = 15 * 60  # итог подъёма висит 15 минут и исчезает


async def _drop_wake_message(bot: Bot, uid: int) -> None:
    mid = _wake_msg.pop(uid, None)
    if mid:
        try:
            await bot.delete_message(uid, mid)
        except Exception:
            pass


def active_task(uid: int) -> dict[str, Any] | None:
    """Задание, которое сейчас ждёт подтверждения (для роутера сообщений)."""
    state = _active.get(uid)
    if not state:
        return None
    if state.get("expires") and datetime.now(timezone.utc) > state["expires"]:
        _active.pop(uid, None)
        return None
    return state.get("task")


def clear(uid: int) -> None:
    _active.pop(uid, None)


def wake_keyboard(lang: str) -> InlineKeyboardMarkup:
    from .keyboards import _btn

    uz = lang == "uz"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ " + ("Turdim" if uz else "Проснулся"), "wake:up", style="primary")],
        [_btn("😴 +10 " + ("daqiqa" if uz else "мин"), "wake:snooze"), _btn("🚫 " + ("Bugun uyg'otmang" if uz else "Не будить сегодня"), "wake:skip")],
    ])


async def _settings(uid: int) -> wake_mod.WakeSettings:
    return wake_mod.WakeSettings.from_row(await services.wake_settings(uid))


async def plan_for(profile: Profile, day: date | None = None) -> tuple[wake_mod.WakeSettings, wake_mod.DayPlan]:
    s = await _settings(profile.telegram_id)
    day = day or profile.today
    rows = await prayer.timings(day, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
    return s, wake_mod.plan_for_day(s, day, tz=profile.tz, timings=rows)


# ------------------------------------------------------------------ голос
async def _say(text: str) -> bytes | None:
    """Фраза Джарвиса → PCM (24 кГц, моно) для проигрывания в звонке."""
    if not text.strip():
        return None
    try:
        return await ai.synthesize(voice.speakable(text))
    except Exception:
        logger.warning("tts failed", exc_info=True)
        return None


async def _hear(pcm: bytes) -> str:
    """Фраза собеседника из звонка → текст."""
    path = await cd.pcm_to_ogg_file(pcm)
    if not path:
        return ""
    try:
        return (await ai.transcribe_voice(path)).strip()
    except Exception:
        logger.warning("transcribe failed", exc_info=True)
        return ""
    finally:
        cd.cleanup(path)


async def _speech_file(text: str) -> str | None:
    """Текст → OGG-файл (режим без разговора: просто сказать и положить трубку)."""
    audio = await voice.make_voice(text)
    if not audio:
        return None
    fd, path = tempfile.mkstemp(prefix="wake_", suffix=".ogg")
    with os.fdopen(fd, "wb") as fh:
        fh.write(audio)
    return path


async def _task_for(profile: Profile, s: wake_mod.WakeSettings, plan: wake_mod.DayPlan) -> dict[str, Any]:
    return wake_mod.make_task(s, plan.day, lang=profile.lang)


# ------------------------------------------------------------------ разговор в трубке
async def _dialog_call(profile: Profile, s: wake_mod.WakeSettings, plan: wake_mod.DayPlan, task: dict[str, Any],
                       minutes_left: int | None) -> dict[str, Any]:
    """Звонок-разговор на Gemini Live: живой голос, будит, пока не услышит, что встал."""
    from . import live_call

    live = await live_call.run(profile, mode="wake", ring_seconds=max(20, s.retry_seconds),
                               wake={"takbir": plan.takbir, "minutes_left": minutes_left, "task": str(task.get("text") or "")})
    state = cd.DialogState(lang=s.voice_lang, name=profile.first_name or "", takbir=plan.takbir,
                           minutes_left=minutes_left, task_text=str(task.get("text") or ""))
    state.confirmed = live.confirmed
    state.transcript = list(live.transcript)
    if live.snooze_minutes:
        await snooze(profile, live.snooze_minutes)
    if live.model:  # до Gemini Live достучались — итог звонка берём оттуда, даже если трубку не взяли
        return {"answered": live.answered, "error": live.error, "state": state}
    # Gemini Live недоступен — запасной путь: старый пошаговый разговор
    logger.warning("wake: live недоступен (%s), пошаговый режим", live.error)
    greeting_pcm = await _say(cd.greeting(state))
    if not greeting_pcm:
        return {"answered": False, "error": "tts unavailable", "state": state}
    history: list[dict[str, Any]] = []

    async def on_utterance(pcm: bytes | None) -> dict[str, Any]:
        text = await _hear(pcm) if pcm else ""
        step = cd.decide(state, text)
        if step["action"] == "reply":
            history.append({"role": "user", "parts": [{"text": text}]})
            try:
                out = await ai.agent_step(list(history), system=cd.system_prompt(state), tools=[], temperature=0.6, max_tokens=160)
                answer = (out.text or "").strip() or cd.nudge(state)
            except Exception:
                logger.warning("dialog reply failed", exc_info=True)
                answer = cd.nudge(state)
            history.append({"role": "model", "parts": [{"text": answer}]})
        else:
            answer = str(step.get("say") or "")
        state.transcript.append(f"я: {answer}")
        stop = bool(state.confirmed) or state.turns >= cd.MAX_TURNS
        if stop:
            answer = f"{answer} {cd.farewell(state)}".strip()
        return {"pcm": await _say(answer), "stop": stop}

    result = await caller.talk(profile.telegram_id, greeting_pcm=greeting_pcm, on_utterance=on_utterance,
                               ring_seconds=max(20, s.retry_seconds), max_seconds=cd.MAX_CALL_SECONDS, username=profile.username)
    result["state"] = state
    return result


async def run_attempt(bot: Bot, profile: Profile, s: wake_mod.WakeSettings, plan: wake_mod.DayPlan, log: dict[str, Any] | None) -> dict[str, Any]:
    """Одна попытка разбудить: звонок (разговор или просто голос) + сообщение с заданием."""
    uid = profile.telegram_id
    now = datetime.now(timezone.utc)
    attempts = int((log or {}).get("attempts") or 0) + 1
    state = _active.get(uid) or {}
    task = state.get("task") or await _task_for(profile, s, plan)
    minutes_left = int((plan.takbir_at - now.astimezone(profile.tz)).total_seconds() // 60) if plan.takbir_at else None
    _active[uid] = {"task": task, "plan": plan, "attempts": attempts, "last": now, "expires": now + timedelta(hours=3)}

    answered = False
    confirmed = False
    call_error: str | None = None
    dialog_text = ""
    if s.call_enabled and caller.available():
        if s.talk:
            result = await _dialog_call(profile, s, plan, task, minutes_left)
            answered, call_error = bool(result.get("answered")), result.get("error")
            dstate = result.get("state")
            confirmed = bool(getattr(dstate, "confirmed", False))
            dialog_text = " | ".join(getattr(dstate, "transcript", []) or [])
        else:
            script = wake_mod.call_script(
                name=profile.first_name or ("do'st" if s.voice_lang == "uz" else "друг"),
                takbir=plan.takbir, minutes_left=minutes_left, task_text=str(task.get("text") or ""), lang=s.voice_lang,
            )
            path = await _speech_file(script)
            if path:
                try:
                    result = await caller.call(uid, path, ring_seconds=max(20, s.retry_seconds), play_seconds=45, username=profile.username)
                    answered, call_error = bool(result.get("answered")), result.get("error")
                finally:
                    cd.cleanup(path)
            else:
                call_error = "tts unavailable"

    text = wake_mod.wake_message(name=profile.first_name or "", plan=plan, task=task, attempt=attempts, lang=profile.lang)
    try:
        await _drop_wake_message(bot, uid)  # прошлая попытка — не копим «Пора вставать» в чате
        sent = await bot.send_message(uid, text, reply_markup=wake_keyboard(profile.lang), disable_notification=False)
        _wake_msg[uid] = sent.message_id
    except Exception:
        logger.exception("wake message failed for %s", uid)

    fields: dict[str, Any] = {"attempts": attempts, "planned_at": plan.wake_at.isoformat() if plan.wake_at else None,
                              "takbir_at": plan.takbir, "task_kind": task.get("kind"), "task_text": task.get("text")}
    if attempts == 1:
        fields["first_call_at"] = now.isoformat()
    if dialog_text:
        fields["dialog"] = dialog_text[:2000]
    await services.save_wake_log(uid, plan.day, fields)
    if call_error:
        logger.info("wake call error for %s: %s", uid, call_error)
    if confirmed:
        await mark_awake(bot, profile, source="call")  # подтвердил голосом — больше не звоним
    return {"attempts": attempts, "answered": answered, "confirmed": confirmed, "task": task, "call_error": call_error}


# ------------------------------------------------------------------ подтверждение подъёма
async def mark_awake(bot: Bot, profile: Profile, *, source: str, notify: bool = True) -> dict[str, Any]:
    """Отметить подъём: звонки прекращаются, приходит короткий итог."""
    uid = profile.telegram_id
    now = datetime.now(timezone.utc)
    _, plan = await plan_for(profile)
    local_now = now.astimezone(profile.tz)
    before = bool(plan.takbir_at and local_now <= plan.takbir_at)
    state = _active.pop(uid, None)
    fields: dict[str, Any] = {"woke_at": now.isoformat(), "woke_source": source, "before_takbir": before,
                              "takbir_at": plan.takbir, "planned_at": plan.wake_at.isoformat() if plan.wake_at else None}
    if state and state.get("task"):
        fields.update({"task_kind": state["task"].get("kind"), "task_text": state["task"].get("text")})
    await services.save_wake_log(uid, plan.day, fields)
    history = await services.wake_history(uid, days=60)
    rows = [r for r in history if str(r.get("day"))[:10] != plan.day.isoformat()]
    rows.append({"day": plan.day.isoformat(), "woke_at": fields["woke_at"], "before_takbir": before})
    streak = wake_mod.streak_days(rows, plan.day)
    await _drop_wake_message(bot, uid)
    if notify:
        try:
            from . import screen as screen_mod

            await screen_mod.send_ephemeral(bot, uid, wake_mod.done_message(plan=plan, now=local_now, lang=profile.lang, streak=streak),
                                            keep_previous=True, ttl=DONE_TTL)
        except Exception:
            logger.debug("wake done message failed", exc_info=True)
    # утренняя сводка «после подъёма» — сразу, как только встал
    if source != "skip":
        try:
            us = await services.user_settings(uid)
            if str(us.get("brief_morning_time") or "") == "wake":
                from .workers import send_morning

                await send_morning(bot, profile, us)
        except Exception:
            logger.debug("morning brief after wake failed", exc_info=True)
    return {"before_takbir": before, "streak": streak, "plan": plan}


async def task_done(bot: Bot, profile: Profile, *, ok: bool) -> None:
    if ok:
        await services.save_wake_log(profile.telegram_id, profile.today, {"task_done_at": datetime.now(timezone.utc).isoformat()})
        _active.pop(profile.telegram_id, None)


async def snooze(profile: Profile, minutes: int) -> datetime:
    """Отложить звонки на N минут."""
    uid = profile.telegram_id
    state = _active.get(uid) or {}
    until = datetime.now(timezone.utc) + timedelta(minutes=max(1, min(30, minutes)))
    state["snooze_until"] = until
    state.setdefault("expires", until + timedelta(hours=2))
    _active[uid] = state
    return until


def snoozed_until(uid: int) -> datetime | None:
    return (_active.get(uid) or {}).get("snooze_until")


async def skip_today(profile: Profile, bot: Bot | None = None) -> None:
    await services.save_wake_log(profile.telegram_id, profile.today, {"woke_at": datetime.now(timezone.utc).isoformat(), "woke_source": "skip"})
    _active.pop(profile.telegram_id, None)
    if bot is not None:
        await _drop_wake_message(bot, profile.telegram_id)


async def morning_extra(profile: Profile) -> str:
    """Строка с временами намаза для утренней сводки."""
    s, plan = await plan_for(profile)
    rows = await prayer.timings(profile.today, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
    if not rows:
        return ""
    return "🕌 " + prayer.summary(rows, profile.lang, takbir=plan.takbir)


__all__ = ["run_attempt", "mark_awake", "task_done", "snooze", "snoozed_until", "skip_today", "plan_for",
           "active_task", "clear", "wake_keyboard", "morning_extra"]
