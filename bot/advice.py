"""Советы в течение дня (30.09, его выбор): коротко, по ситуации; с 07.10 — не чаще раза в день (он их не читал) и заметкой.

«До асра 25 минут — успеете позвонить Алишеру», «съедено мало, а уже 15:00 — пора пообедать», «вы остановились на уроке на 12:30 —
продолжить?» (только про видео, которые он сам прислал и поставил целью/задачей). Правила простые и точные (данные его, ничего не выдумывается); фразу пишет умная модель (не ответила — шаблон).
Подсказки бота по деньгам, долгам и питанию — отдельная система (bot/proactive.py); здесь — про сам день.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ai as ai_mod
from . import lessons, prayer, services
from . import nutrition as nutri
from .context import ai
from .profile import Profile, h

logger = logging.getLogger(__name__)

MAX_PER_DAY = 1   # 07.10: он удалял их, не читая, — один совет в день, и только по делу
MIN_GAP_S = 150 * 60
FROM_HOUR, TO_HOUR = 9, 20         # с 9:00 до 20:30 — ночью и рано утром не тревожим
PRAYER_LEFT = (15, 35)             # «до намаза 15–35 минут»
LESSON_AFTER_HOUR = 17             # урок в процессе, а сегодня не смотрел — с 17:00


@dataclass
class Tip:
    key: str
    facts: str                      # что известно — модели для фразы
    fallback: str                   # шаблон без модели (HTML-безопасный текст)
    url: str | None = None          # кнопка со ссылкой (продолжить урок)
    extra: dict[str, Any] = field(default_factory=dict)


def _file(uid: int) -> Path | None:
    folder = os.getenv("DATA_DIR")
    return Path(folder) / f"advice_{int(uid)}.json" if folder else None


def _state(uid: int, today: str) -> dict[str, Any]:
    path = _file(uid)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if data.get("day") != today:
        data = {"day": today, "keys": [], "last_at": 0}
    return data


def _save(uid: int, data: dict[str, Any]) -> None:
    path = _file(uid)
    if path is not None:
        try:
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            logger.warning("advice: не сохранил", exc_info=True)


async def candidates(profile: Profile) -> list[Tip]:
    """Что уместно подсказать прямо сейчас — от важного к менее важному."""
    from . import wake_runner

    uid, now, today = profile.telegram_id, profile.now, profile.today
    tips: list[Tip] = []
    # --- ближайший намаз и дело, которое как раз успеть
    try:
        s, _ = await wake_runner.plan_for(profile)
        rows = await prayer.timings(today, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
        nxt = prayer.next_prayer(rows, now, profile.lang) if rows else None
        tasks = [r for r in await services.tasks(uid)
                 if r.get("due_date") and str(r["due_date"])[:10] <= today.isoformat() and not r.get("due_time")]
        if nxt and PRAYER_LEFT[0] <= nxt[2] <= PRAYER_LEFT[1] and tasks and nxt[0] not in {"Бомдод", "Bomdod"}:
            task = str(tasks[0].get("text") or "")[:60]
            tips.append(Tip(
                key=f"prayer:{nxt[1]}", facts=f"До намаза {nxt[0]} ({nxt[1]}) осталось {nxt[2]} минут. Открытое дело на сегодня: «{task}».",
                fallback=profile.tr(f"До намаза {nxt[0]} около {nxt[2]} минут — как раз успеете: {h(task)}.",
                                    f"{nxt[0]} namoziga taxminan {nxt[2]} daqiqa — ulguring: {h(task)}.")))
    except Exception:
        logger.debug("advice: намаз/дела", exc_info=True)
    # --- питание
    try:
        plan = await services.nutrition_profile(uid)
        goal = int((plan or {}).get("daily_calories") or 0)
        if goal:
            eaten = int(nutri.totals(await services.today_calorie_logs(profile))["calories"])
            if now.hour >= 14 and eaten < goal * 0.3:
                tips.append(Tip(
                    key="food:low", facts=f"Уже {now:%H:%M}, а съедено только {eaten} ккал из {goal} на день.",
                    fallback=profile.tr(f"Уже {now:%H:%M}, съедено всего {eaten} из {goal} ккал — пора нормально поесть.",
                                        f"Soat {now:%H:%M}, faqat {eaten}/{goal} kkal yeyildi — ovqatlaning.")))
            elif now.hour >= 18 and eaten > goal * 1.05:
                tips.append(Tip(
                    key="food:over", facts=f"Съедено {eaten} ккал при плане {goal}; вечер.",
                    fallback=profile.tr(f"Съедено {eaten} из {goal} ккал — на ужин лучше что-то лёгкое и белковое.",
                                        f"{eaten}/{goal} kkal yeyildi — kechqurun yengil ovqat yeng.")))
    except Exception:
        logger.debug("advice: питание", exc_info=True)
    # --- дела на вечер
    try:
        left = [r for r in await services.tasks(uid) if r.get("due_date") and str(r["due_date"])[:10] <= today.isoformat()]
        if now.hour >= 17 and len(left) >= 2:
            small = str(left[0].get("text") or "")[:60]
            tips.append(Tip(
                key=f"tasks:{len(left)}", facts=f"Уже {now:%H:%M}; дел на сегодня осталось {len(left)}: " + "; ".join(str(r.get('text'))[:40] for r in left[:4]),
                fallback=profile.tr(f"Осталось дел на сегодня: {len(left)}. Начните с простого — {h(small)}; остальное можно перенести.",
                                    f"Bugun {len(left)} ta ish qoldi. Oddiysidan boshlang — {h(small)}; qolganini ko'chirsa bo'ladi.")))
    except Exception:
        logger.debug("advice: дела", exc_info=True)
    # --- урок в процессе
    try:
        if now.hour >= LESSON_AFTER_HOUR:
            # 30.09: только видео, которые он сам прислал и поставил целью/задачей (не «всё, что смотрел»)
            watching = [r for r in lessons.items(uid) if int(r.get("position") or 0) >= 60 and int(r.get("duration") or 0) >= 8 * 60
                        and lessons.tracked(uid, r) and not lessons.finished(r) and time.time() - float(r.get("at") or 0) < 10 * 86400
                        and time.strftime("%Y-%m-%d", time.localtime(float(r.get("at") or 0))) != today.isoformat()]
            if watching:
                r = watching[0]
                res = await lessons.resume(uid, r["title"][:40])
                tips.append(Tip(
                    key=f"lesson:{r['title'][:30]}", facts=f"Урок в процессе: «{r['title'][:70]}», остановился на {lessons.fmt(int(r['position']))}; сегодня не смотрел.",
                    fallback=profile.tr(f"Вы остановились на «{h(r['title'][:50])}» на {lessons.fmt(int(r['position']))} — продолжим?",
                                        f"«{h(r['title'][:50])}» darsida {lessons.fmt(int(r['position']))} da to'xtagan edingiz — davom etamizmi?"),
                    url=lessons.url(res) if res.get("video_id") else None))
    except Exception:
        logger.debug("advice: урок", exc_info=True)
    return tips


async def _phrase(profile: Profile, persona, tip: Tip) -> str:  # noqa: ANN001
    from .handlers.agent import render_reply
    from .plan import _persona_rules

    prompt = (f"Ты — JES, личный помощник {profile.first_name or 'пользователя'}. {_persona_rules(profile, persona)}\n"
              f"Напиши ОДИН короткий тёплый совет (1–2 предложения, до 200 знаков, без списков и лишних слов) по ситуации:\n{tip.facts}\n"
              "Ничего не выдумывай, не давай нравоучений. Верни только текст.")
    try:
        text = (await ai.generate([{"text": prompt}], model=ai_mod.smart_model(), temperature=0.7, json_mode=False,
                                  thinking_budget=0, max_tokens=200)).strip()
        return render_reply(text) if text else tip.fallback
    except Exception:
        logger.debug("advice: фраза не вышла", exc_info=True)
        return tip.fallback


async def maybe_send(bot, profile: Profile) -> bool:  # noqa: ANN001
    """Один совет за проход, если уместно и лимит дня не выбран. True — отправили."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    now = profile.now
    if not (FROM_HOUR <= now.hour < TO_HOUR or (now.hour == TO_HOUR and now.minute <= 30)):
        return False
    uid = profile.telegram_id
    st = _state(uid, profile.today.isoformat())
    if len(st["keys"]) >= MAX_PER_DAY or time.time() - float(st.get("last_at") or 0) < MIN_GAP_S:
        return False
    tips = [t for t in await candidates(profile) if t.key not in st["keys"]]
    if not tips:
        return False
    tip = tips[0]
    st["keys"].append(tip.key)          # сначала отмечаем — не задвоим при сбое отправки
    st["last_at"] = time.time()
    _save(uid, st)
    text = await _phrase(profile, await services.persona(uid), tip)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ " + profile.tr("Продолжить урок", "Darsni davom ettirish"), url=tip.url)]]) if tip.url else None
    from . import screen as screen_mod

    await screen_mod.send_note(bot, uid, "💡 " + text, kb, ttl=4 * 3600, parse_mode="HTML")   # исчезает при нажатии кнопки и через 4 часа
    logger.info("advice: совет %s отправлен %s (%d/%d сегодня)", tip.key, uid, len(st["keys"]), MAX_PER_DAY)
    return True


__all__ = ["maybe_send", "candidates", "Tip", "MAX_PER_DAY"]
