"""Идеи целей по его данным (30.09): экран «Цели» больше не пустой — до 4 готовых предложений, добавляются одной кнопкой.

Всё считает код по его же цифрам (ничего не выдумывается): план питания (набор/снижение веса), траты по категориям за 30 дней
(лимит на самую «лишнюю» категорию), обязательные расходы (подушка на 3 месяца), урок YouTube, который он смотрит и не
закончил. У каждой идеи — готовые аргументы `add_goal`: нажал — цель создана.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any

from . import categories as cats
from . import finance as fin
from . import goals as goals_mod
from . import habits, lessons, services
from .profile import Profile

logger = logging.getLogger(__name__)

MAX_IDEAS = 4
_DISCRETIONARY = set(goals_mod.DISCRETIONARY)
# «учёба», а не развлечение: пробная проверка 30.09 предложила цель «4 раза в неделю» для видео Mimic Party
_LEARN = re.compile(r"(урок|курс|лекци|обучен|учим|учу|учить|мастер-?класс|lesson|course|tutorial|lecture|learn|how to|dars|kurs|ma'ruza|"
                    r"пдд|pdd|english|англий|ingliz|corан|коран|quran|qur'an|сура|surah|sura|python|программир)", re.IGNORECASE)


def _round(v: float, step: int = 1000) -> int:
    return max(step, int(round(v / step)) * step)


async def ideas(profile: Profile, existing: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """[{"key", "icon", "title", "why", "args"}] — чего ещё нет среди его целей. args — для add_goal."""
    uid, today, uz = profile.telegram_id, profile.today, profile.lang == "uz"
    have = existing if existing is not None else await services.goals(uid)
    kinds = {goals_mod.kind_of(g) for g in have}
    caps = {str(goals_mod.params_of(g).get("category") or "") for g in have if goals_mod.kind_of(g) == "spend_cap"}
    titles = " ".join(str(g.get("title") or "").lower() for g in have)
    out: list[dict[str, Any]] = []

    # --- вес: план питания на набор/снижение
    try:
        plan = await services.nutrition_profile(uid)
        mode = (plan or {}).get("mode")
        weight = float((plan or {}).get("weight") or 0)
        if "weight" not in kinds and mode in {"gain", "muscle", "loss"} and weight:
            target = round(weight + (3 if mode in {"gain", "muscle"} else -4), 1)
            word = ("Vazn " if uz else "Набрать до " if mode in {"gain", "muscle"} else "Сбросить до ")
            title = (f"{word}{target:g} kg" if uz else f"{word}{target:g} кг")
            out.append({"key": "weight", "icon": "⚖️", "title": title,
                        "why": (f"ovqatlanish rejasi {plan.get('daily_calories')} kkal — maqsad unga ma'no beradi" if uz
                                else f"у вас план питания {plan.get('daily_calories')} ккал/день — цель даст плану смысл и темп: +3 кг за 3 месяца"),
                        "args": {"title": title, "kind": "weight", "target_amount": target, "current_weight": weight,
                                 "deadline": (today + timedelta(days=90)).isoformat()}})
    except Exception:
        logger.debug("ideas: вес", exc_info=True)
    # --- траты: лимит на самую «лишнюю» категорию и подушка безопасности
    try:
        entries = await services.finance_entries(uid)
        sp = habits.spending_patterns(entries, today=today)
        if sp.get("days"):
            for c in sp["top_categories"]:
                monthly = c["per_day"] * 30
                if c["category"] in _DISCRETIONARY and c["category"] not in caps and monthly >= 200_000:
                    limit = _round(monthly * 0.85)
                    label = cats.label(c["category"], profile.lang, with_emoji=False)
                    title = (f"{label}: oyiga ≤ {fin.fmt_money(limit)}" if uz else f"{label}: не больше {fin.fmt_money(limit)} в месяц")
                    out.append({"key": f"cap:{c['category']}", "icon": "💸", "title": title,
                                "why": (f"hozir ~{fin.fmt_money(monthly)}/oy" if uz else f"сейчас уходит ~{fin.fmt_money(monthly)}/мес — лимит на 15% ниже"),
                                "args": {"title": title, "kind": "spend_cap", "target_amount": limit, "category": c["category"]}})
                    break
            essential = float(sp.get("essential_per_month") or 0)
            if "save" not in kinds and essential >= 300_000 and "подуш" not in titles:
                target = _round(essential * 3, 10_000)
                title = ("Xavfsizlik yostig'i" if uz else "Подушка безопасности")
                out.append({"key": "cushion", "icon": "🛟", "title": f"{title}: {fin.fmt_money(target)}",
                            "why": (f"3 oylik majburiy xarajat (~{fin.fmt_money(essential)}/oy)" if uz else f"3 месяца обязательных расходов (~{fin.fmt_money(essential)}/мес)"),
                            "args": {"title": title, "kind": "save", "target_amount": target, "deadline": (today + timedelta(days=180)).isoformat()}})
    except Exception:
        logger.debug("ideas: траты", exc_info=True)
    # --- учёба: урок, который начал и не закончил
    try:
        watching = [r for r in lessons.items(uid) if int(r.get("position") or 0) >= 60 and int(r.get("duration") or 0) >= 8 * 60
                    and not lessons.finished(r) and time.time() - float(r.get("at") or 0) < 14 * 86400 and _LEARN.search(str(r.get("title") or ""))]
        if watching and "habit" not in kinds:
            r = watching[0]
            name = r["title"][:38].rstrip() + ("…" if len(r["title"]) > 38 else "")
            title = (f"«{name}»: haftasiga 4 marta" if uz else f"«{name}»: 4 раза в неделю")
            out.append({"key": "lesson", "icon": "🎓", "title": title,
                        "why": (f"boshlagansiz: {lessons.fmt(int(r['position']))}" if uz else f"вы остановились на {lessons.fmt(int(r['position']))} — привычка доведёт до конца"),
                        "args": {"title": title, "kind": "habit", "target_amount": 4}})
    except Exception:
        logger.debug("ideas: урок", exc_info=True)
    return out[:MAX_IDEAS]


__all__ = ["ideas", "MAX_IDEAS"]
