"""«Следующий шаг» у цели и прогресс без ручного ввода (30.09, его выбор).

Под каждой целью — одна строка: что сделать СЕГОДНЯ, чтобы двигаться к ней. Шаг считает код по её данным (накопления — сколько
отложить на неделе, лимит трат — сколько можно сегодня, вес — сколько ккал и когда взвеситься, привычка — сделать и отметить);
у свободных целей («выучить 500 слов», «закрыть кредит») умная модель один раз раскладывает цель на 3–6 этапов, и прогресс
считается по ним сам.

Шаг попадает в план дня, а ✅ в плане двигает саму цель — вводить ничего не надо:
  • привычка — отметка «сегодня сделано» (goal_checkins);
  • накопления — в «отложено» прибавляется сумма шага (отмена ✅ — вычитается);
  • свободная цель — очередной этап отмечается выполненным, процент пересчитывается;
  • лимит трат и вес двигаются сами по операциям, взвешиваниям и дневнику еды.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import cache
from . import finance as fin
from .context import ai, db
from .profile import Profile

logger = logging.getLogger(__name__)

MAX_STEPS = 6


def _m(v: float) -> str:
    return fin.fmt_money(v)


def _thousands(v: float) -> float:
    return max(1000.0, round(v / 1000.0) * 1000.0)


def step(st: dict[str, Any], lang: str = "ru") -> dict[str, Any] | None:
    """{"text", "amount"?} — что сделать сегодня по цели. Чистая функция над статусом цели (bot/goals.py); нет шага — None."""
    if st.get("done"):
        return None
    uz = lang == "uz"
    k = st.get("kind")
    if k == "save":
        need = float(st.get("needed_per_month") or 0)
        remaining = float(st.get("remaining") or 0)
        if remaining <= 0:
            return None
        weekly = _thousands(need * 7 / 30) if need else _thousands(float(st.get("target") or remaining) / 52)
        weekly = min(weekly, _thousands(remaining))
        return {"text": (f"shu hafta ~{_m(weekly)} qo'ying" if uz else f"отложите ~{_m(weekly)} на этой неделе"), "amount": weekly}
    if k == "spend_cap":
        if "spent" not in st:
            return None
        if float(st.get("remaining") or 0) < 0:
            return {"text": "bugun xarajatsiz" if uz else "сегодня без трат"}
        text = (f"bugun ≤ {_m(st['allowed_per_day'])}" if uz else f"сегодня — до {_m(st['allowed_per_day'])}")
        cut = st.get("cut_candidates") or []
        if cut and st.get("on_track") is False:
            from . import categories as cats

            text += (" · qisqartiring: " if uz else " · урежьте: ") + cats.label(cut[0]["category"], "uz" if uz else "ru", with_emoji=False)
        return {"text": text}
    if k == "weight":
        if st.get("current") is None:
            return {"text": "hozirgi vazningizni yozing" if uz else "запишите текущий вес"}
        if "weigh_in_due" in st.get("flags", []):
            return {"text": "o'lchaning va vaznni yozing" if uz else "взвесьтесь и запишите вес"}
        if st.get("daily_target"):
            rem = int(st.get("today_remaining") or 0)
            base = f"{int(st['daily_target'])} kkal" if uz else f"{int(st['daily_target'])} ккал"
            if st.get("direction") == "gain" and rem > 150:
                return {"text": (f"bugun yana {rem} kkal yeng" if uz else f"сегодня добрать ещё {rem} ккал (норма {base})")}
            return {"text": (f"bugun {base} ichida" if uz else f"уложитесь в {base}") + (f" (hozir {st['today_kcal']})" if uz else f" (сейчас {st['today_kcal']})")}
        return None
    if k == "habit":
        if int(st.get("remaining_this_week") or 0) <= 0 or st.get("today_checked"):
            return None
        text = (f"bugun bajaring va belgilang ({st['this_week']}/{st['per_week']})" if uz
                else f"сегодня сделайте и отметьте ({st['this_week']}/{st['per_week']})")
        if "must_today" in st.get("flags", []):
            text += " — bugun kerak!" if uz else " — сегодня надо!"
        return {"text": text}
    if k == "custom":
        text = st.get("next_step_text")
        return {"text": str(text)} if text else None
    return None


# ------------------------------------------------------------------ этапы свободных целей
def milestones(goal: dict[str, Any]) -> list[dict[str, Any]]:
    p = goal.get("params")
    steps = (p or {}).get("steps") if isinstance(p, dict) else None
    return [s for s in steps if isinstance(s, dict) and s.get("text")] if isinstance(steps, list) else []


STEPS_PROMPT = (
    "Разложи каждую цель человека на 3–6 КОНКРЕТНЫХ этапов по порядку: каждый — один проверяемый результат (что сделано), с глагола, "
    "до 70 знаков, реалистичный для одной-двух недель; последний этап — завершение цели. Язык — как в названии цели. Ничего не "
    "выдумывай сверх названия. Верни ТОЛЬКО JSON: {{\"goals\": [{{\"id\": \"id цели\", \"steps\": [\"...\", \"...\"]}}]}}\n\nЦЕЛИ:\n{goals}")


async def ensure_steps(profile: Profile, rows: list[dict[str, Any]], *, timeout: float = 9.0) -> int:
    """Свободным целям без этапов — этапы умной моделью (одним запросом). Возвращает, у скольких появились."""
    from . import ai as ai_mod
    from .ai import extract_json
    from .goals import kind_of

    todo = [g for g in rows if kind_of(g) == "custom" and not g.get("done") and not milestones(g)]
    if not todo or not db.available("goal_checkins"):
        return 0
    listing = "\n".join(f"[{g.get('id')}] {g.get('title')}" + (f" (срок {str(g['deadline'])[:10]})" if g.get("deadline") else "") for g in todo)
    try:
        raw = await asyncio.wait_for(ai.generate([{"text": STEPS_PROMPT.format(goals=listing)}], model=ai_mod.smart_model(), temperature=0.4,
                                                 json_mode=True, thinking_budget=0, max_tokens=2500), timeout)
        data = extract_json(raw) or {}
    except Exception:
        logger.warning("goal_steps: этапы не получены", exc_info=True)
        return 0
    made = 0
    by_id = {str(g.get("id")): g for g in todo}
    for item in data.get("goals") or []:
        g = by_id.get(str(item.get("id"))) if isinstance(item, dict) else None
        texts = [" ".join(str(t).split())[:80] for t in (item.get("steps") or []) if str(t).strip()][:MAX_STEPS] if g else []
        if len(texts) < 2:
            continue
        params = dict(g.get("params") or {}) if isinstance(g.get("params"), dict) else {}
        params["steps"] = [{"id": f"s{i}", "text": t, "done": False} for i, t in enumerate(texts, 1)]
        try:
            await db.update_goal(profile.telegram_id, g["id"], {"params": params})
            g["params"] = params
            made += 1
        except Exception:
            logger.warning("goal_steps: этапы не сохранил", exc_info=True)
    if made:
        cache.invalidate(profile.telegram_id, "goals")
    return made


# ------------------------------------------------------------------ отметка в плане → цель
async def _goal_row(profile: Profile, goal_id: str) -> dict[str, Any] | None:
    from . import services

    return next((g for g in await services.goals(profile.telegram_id) if str(g.get("id")) == str(goal_id)), None)


async def apply(profile: Profile, goal_id: str, done: bool, effect: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """✅ (или снятие ✅) у пункта плана со ссылкой на цель → сама цель. Возвращает запись эффекта для отмены (положить в пункт)."""
    from . import goals as goals_mod

    uid = profile.telegram_id
    row = await _goal_row(profile, goal_id)
    if row is None:
        return None
    kind = goals_mod.kind_of(row)
    try:
        if kind == "habit":
            day = profile.today.isoformat()
            if done:
                await db.upsert_checkin(uid, goal_id=row["id"], day=day)
            else:
                await db.delete_checkin(uid, goal_id=row["id"], day=day)
            cache.invalidate(uid, "checkins", "goals")
            return {"kind": "habit", "day": day} if done else None
        if kind == "save":
            amount = float((effect or {}).get("amount") or 0)
            if not amount:
                statuses, _ = await goals_mod.statuses_for(profile, goals=[row])
                s = step(statuses[0], profile.lang) if statuses else None
                amount = float((s or {}).get("amount") or 0)
            if amount <= 0:
                return None
            saved = float(row.get("saved_amount") or 0)
            await db.update_goal(uid, row["id"], {"saved_amount": max(0.0, saved + amount if done else saved - amount)})
            cache.invalidate(uid, "goals")
            return {"kind": "save", "amount": amount} if done else None
        if kind == "custom":
            steps = milestones(row)
            if not steps:
                return None
            params = dict(row.get("params") or {})
            if done:
                nxt = next((s for s in steps if not s.get("done")), None)
                if nxt is None:
                    return None
                nxt["done"], nxt["at"] = True, time.time()
                sid = nxt["id"]
            else:
                sid = (effect or {}).get("step") or next((s["id"] for s in reversed(steps) if s.get("done")), None)
                for s in steps:
                    if s.get("id") == sid:
                        s["done"] = False
            params["steps"] = steps
            await db.update_goal(uid, row["id"], {"params": params})
            cache.invalidate(uid, "goals")
            return {"kind": "custom", "step": sid} if done else None
    except Exception:
        logger.warning("goal_steps: отметка не дошла до цели", exc_info=True)
    return None


__all__ = ["step", "milestones", "ensure_steps", "apply"]
