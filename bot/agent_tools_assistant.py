"""Инструменты «JES» уровня ассистента: заметки («запомни…»), задачи, цели
накоплений, сроки возврата долгов. Регистрируются в общем реестре `agent_tools.TOOLS`
(модуль импортируется в конце bot/agent_tools.py).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from . import cache
from . import caller
from . import finance as fin
from . import goals as goals_mod
from . import habits
from . import services
from . import places
from . import wake as wake_mod
from . import undo
from .agent_tools import ARR, DATE, ID, IDS, P, ToolContext, _bool, _int, _num, _str, _time_arg, parse_day, tool
from .context import db

logger = logging.getLogger(__name__)


def _migration_error(table: str) -> dict[str, Any]:
    return {"error": f"table {table} missing — run sql/migrations/005_assistant.sql in Supabase"}


# ------------------------------------------------------------------ views
def note_view(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(r.get("id")), "text": r.get("text"), "created": str(r.get("created_at") or "")[:10]}


def task_view(r: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    due = str(r.get("due_date") or "")[:10] or None
    out = {"id": str(r.get("id")), "text": r.get("text"), "due_date": due, "due_time": r.get("due_time"), "done": bool(r.get("done"))}
    if due and today and not out["done"]:
        try:
            out["days_left"] = (date.fromisoformat(due) - today).days
        except ValueError:
            pass
    return out


def deadline_view(r: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    due = str(r.get("due_date") or "")[:10]
    out = {"person": r.get("person"), "side": r.get("side"), "due_date": due, "note": r.get("note")}
    if today:
        try:
            out["days_left"] = (date.fromisoformat(due) - today).days
        except ValueError:
            pass
    return out


async def goals_with_status(ctx: ToolContext) -> list[dict[str, Any]]:
    """Статусы всех активных целей (любого вида) по реальным данным — см. bot/goals.py."""
    statuses, _ = await goals_mod.statuses_for(ctx.profile)
    return statuses


# ------------------------------------------------------------------ notes
@tool("list_notes", "Заметки пользователя («запомни, что…»): факты, даты, предпочтения. Без фильтра — все (до 200).", {"query": P("STRING", "фрагмент текста для поиска")})
async def _list_notes(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("notes"):
        return _migration_error("notes")
    rows = await services.notes(ctx.uid)
    q = (_str(a.get("query")) or "").casefold()
    if q:
        rows = [r for r in rows if q in str(r.get("text") or "").casefold()]
    return {"total": len(rows), "notes": [note_view(r) for r in rows[:60]]}


@tool("add_note", "Запомнить факт/заметку («запомни, что у брата день рождения 3 ноября», «мой размер обуви 42»). Даты-события дополнительно ставь задачей (add_task).",
      {"text": P("STRING", "текст заметки, коротко и по сути")}, ("text",))
async def _add_note(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("notes"):
        return _migration_error("notes")
    text = _str(a.get("text"))
    if not text:
        return {"error": "text required"}
    row = await db.add_note(ctx.uid, text)
    cache.invalidate(ctx.uid, "notes")
    undo.push(ctx.uid, {"type": "delete_notes", "ids": [row.get("id")]})
    ctx.mutated = True
    return {"added": note_view(row)}


@tool("update_note", "Изменить текст заметки.", {"id": ID, "text": P("STRING", "новый текст")}, ("id", "text"))
async def _update_note(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in await services.notes(ctx.uid) if str(r.get("id")) == _str(a.get("id"))]
    if not rows or not _str(a.get("text")):
        return {"error": "note not found or text empty"}
    row = rows[0]
    await db.update_note(ctx.uid, row["id"], {"text": _str(a.get("text"))})
    cache.invalidate(ctx.uid, "notes")
    undo.push(ctx.uid, {"type": "restore_notes_text", "note_id": row["id"], "text": row.get("text")})
    ctx.mutated = True
    return {"before": note_view(row), "after": note_view({**row, "text": _str(a.get("text"))})}


@tool("delete_notes", "Удалить заметки по id.", {"ids": IDS}, ("ids",))
async def _delete_notes(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    rows = [r for r in await services.notes(ctx.uid) if str(r.get("id")) in ids]
    if not rows:
        return {"error": "no matching notes"}
    await db.delete_notes(ctx.uid, [r["id"] for r in rows])
    cache.invalidate(ctx.uid, "notes")
    undo.push(ctx.uid, {"type": "restore_notes", "rows": rows})
    ctx.mutated = True
    return {"deleted": [note_view(r) for r in rows]}


# ------------------------------------------------------------------ tasks
@tool("list_tasks", "Задачи/дела пользователя: открытые (по умолчанию) или все. Возвращает id, текст, срок, дней до срока.", {"include_done": P("BOOLEAN", "включая выполненные")})
async def _list_tasks(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("tasks"):
        return _migration_error("tasks")
    rows = await db.list_tasks(ctx.uid, include_done=True) if _bool(a.get("include_done")) else await services.tasks(ctx.uid)
    return {"total": len(rows), "tasks": [task_view(r, ctx.profile.today) for r in rows[:60]]}


@tool("add_task", "Добавить задачу/дело («купить лампочку», «позвонить маме завтра в 18:00», «день рождения брата 3 ноября»). "
      "С датой — попадёт в утреннюю сводку того дня; с временем — бот напомнит в это время.",
      {"text": P("STRING", "что сделать"), "due_date": DATE, "due_time": P("STRING", "HH:MM, если есть время")}, ("text",))
async def _add_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("tasks"):
        return _migration_error("tasks")
    text = _str(a.get("text"))
    if not text:
        return {"error": "text required"}
    day = parse_day(a.get("due_date"), ctx.profile.today)
    hhmm = _time_arg(a.get("due_time")) if _str(a.get("due_time")) else None
    if hhmm and not day:
        day = ctx.profile.today
    row = await db.add_task(ctx.uid, text=text, due_date=day.isoformat() if day else None, due_time=hhmm)
    cache.invalidate(ctx.uid, "tasks")
    undo.push(ctx.uid, {"type": "delete_tasks", "ids": [row.get("id")]})
    ctx.mutated = True
    return {"added": task_view(row, ctx.profile.today)}


@tool("update_task", "Изменить задачу: текст, дату, время; done=true — отметить выполненной («сделал», «готово», «купил»).",
      {"id": ID, "text": P("STRING", "текст"), "due_date": DATE, "due_time": P("STRING", "HH:MM"), "done": P("BOOLEAN", "выполнена")}, ("id",))
async def _update_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in await db.list_tasks(ctx.uid, include_done=True) if str(r.get("id")) == _str(a.get("id"))]
    if not rows:
        return {"error": "task not found"}
    row = rows[0]
    fields: dict[str, Any] = {}
    if _str(a.get("text")):
        fields["text"] = _str(a.get("text"))
    if _str(a.get("due_date")):
        day = parse_day(a.get("due_date"), ctx.profile.today)
        fields["due_date"] = day.isoformat() if day else None
    if _str(a.get("due_time")):
        fields["due_time"] = _time_arg(a.get("due_time"))
        fields["notified_key"] = None
    if (done := _bool(a.get("done"))) is not None:
        fields["done"] = done
        fields["done_at"] = services.utc_now().isoformat() if done else None
    if not fields:
        return {"error": "nothing to change"}
    before = {k: row.get(k) for k in fields}
    await db.update_task(ctx.uid, row["id"], fields)
    cache.invalidate(ctx.uid, "tasks")
    undo.push(ctx.uid, {"type": "task_fields", "task_id": row["id"], "fields": before})
    ctx.mutated = True
    return {"before": task_view(row), "after": task_view({**row, **fields}, ctx.profile.today)}


@tool("complete_tasks", "Отметить задачи выполненными по id (несколько сразу).", {"ids": IDS}, ("ids",))
async def _complete_tasks(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    rows = [r for r in await services.tasks(ctx.uid) if str(r.get("id")) in ids]
    if not rows:
        return {"error": "no matching open tasks"}
    now = services.utc_now().isoformat()
    for r in rows:
        await db.update_task(ctx.uid, r["id"], {"done": True, "done_at": now})
        undo.push(ctx.uid, {"type": "task_fields", "task_id": r["id"], "fields": {"done": False, "done_at": None}})
    cache.invalidate(ctx.uid, "tasks")
    ctx.mutated = True
    return {"completed": [task_view(r) for r in rows]}


@tool("delete_tasks", "Удалить задачи по id.", {"ids": IDS}, ("ids",))
async def _delete_tasks(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    rows = [r for r in await db.list_tasks(ctx.uid, include_done=True) if str(r.get("id")) in ids]
    if not rows:
        return {"error": "no matching tasks"}
    await db.delete_tasks(ctx.uid, [r["id"] for r in rows])
    cache.invalidate(ctx.uid, "tasks")
    undo.push(ctx.uid, {"type": "restore_tasks", "rows": rows})
    ctx.mutated = True
    return {"deleted": [task_view(r) for r in rows]}


# ------------------------------------------------------------------ goals (any kind)
GOAL_KIND = P("STRING", "вид цели: save — накопить сумму; spend_cap — тратить не больше X в месяц (или «сэкономить X» → лимит = обычные траты − X); "
              "weight — дойти до X кг (набор/сброс); habit — N раз в неделю; custom — любая другая цель, прогресс в %",
              enum=["save", "spend_cap", "weight", "habit", "custom"])


@tool("list_goals", "Все цели с расчётом по реальным данным: накопления (нужно/мес, успеваем ли), лимит трат (потрачено, норма на день, прогноз, где урезать), "
      "вес (текущий, нужный темп кг/нед, нужная калорийность, сколько добрать сегодня), привычки (сколько раз на этой неделе, надо ли сегодня), свободные цели (% и отставание).")
async def _list_goals(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("savings_goals"):
        return _migration_error("savings_goals")
    return {"goals": await goals_with_status(ctx)}


@tool("add_goal", "Создать цель любого вида. Примеры: «накопить 10 млн на ноутбук к январю» → save, target=10000000; "
      "«тратить не больше 5 млн в месяц» → spend_cap, target=5000000; «сэкономить 2 млн в этом месяце» → spend_cap с save_amount=2000000 (лимит посчитается от обычных трат); "
      "«на транспорт не больше 500к» → spend_cap + category=transport; «набрать до 75 кг к декабрю» / «сбросить до 80» → weight, target=75, current_weight если сказал; "
      "«зал 3 раза в неделю», «бегать по будням» (=5) → habit, target=3; «выучить 500 слов к марту», «закрыть кредит», «прочитать 5 книг» → custom (target=100, progress_pct — если уже есть прогресс).",
      {"title": P("STRING", "короткое название цели"), "kind": GOAL_KIND, "target_amount": P("NUMBER", "сумма / кг / раз в неделю; для custom не нужно"),
       "deadline": DATE, "saved_amount": P("NUMBER", "save: уже отложено"), "save_amount": P("NUMBER", "spend_cap: сколько хочет сэкономить за месяц (лимит = обычные траты − это)"),
       "category": P("STRING", "spend_cap: ключ категории, если лимит только на неё"), "month": P("STRING", "spend_cap: YYYY-MM, если цель на конкретный месяц; иначе каждый месяц"),
       "current_weight": P("NUMBER", "weight: текущий вес, если назвал"), "progress_pct": P("NUMBER", "custom: текущий прогресс в %")},
      ("title",))
async def _add_goal(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("savings_goals"):
        return _migration_error("savings_goals")
    title, kind = _str(a.get("title")), (_str(a.get("kind")) or "save")
    if not title:
        return {"error": "title required"}
    if kind not in goals_mod.KINDS:
        kind = "save"
    target = _num(a.get("target_amount"))
    day = parse_day(a.get("deadline"), ctx.profile.today)
    params: dict[str, Any] = {}
    saved = 0.0
    unit = None
    today = ctx.profile.today
    if kind == "spend_cap":
        cat = _str(a.get("category"))
        if cat:
            params["category"] = cat
        if (m := _str(a.get("month"))):
            params["month"] = m[:7]
        save_amount = _num(a.get("save_amount"))
        if save_amount and not target:
            # «сэкономить X» — обычные траты за 3 прошлых месяца (той же категории, если задана) минус X
            entries = await services.finance_entries(ctx.uid)
            first_this = today.replace(day=1)
            start = first_this
            for _ in range(3):
                start = (start - timedelta(days=1)).replace(day=1)
            rows = [r for r in fin.entries_between(entries, start, first_this - timedelta(days=1)) if r.get("entry_type") != "income" and not fin.is_transfer(r)]
            if cat:
                rows = [r for r in rows if fin.entry_category_key(r) == cat]
            months = max(1, len({str(r.get("entry_date"))[:7] for r in rows}))
            baseline = sum(float(r.get("amount") or 0) for r in rows) / months
            if baseline <= 0:
                return {"error": "no spending history to compute baseline; ask the user for a monthly limit instead"}
            params["baseline"] = round(baseline)
            params["save_amount"] = save_amount
            target = baseline - save_amount
            if target <= 0:
                return {"error": f"usual monthly spending is {round(baseline)} — cannot save {save_amount}; ask for a realistic amount"}
        unit = "UZS"
    elif kind == "weight":
        unit = "kg"
        current = _num(a.get("current_weight"))
        plan = await services.nutrition_profile(ctx.uid)
        if current is None:
            weights = await services.weight_logs(ctx.uid)
            current = float(weights[0]["weight"]) if weights else (float(plan.get("weight")) if plan and plan.get("weight") else None)
        elif await db.ensure_available("weight_logs"):
            await services.log_weight(ctx.uid, weight=current, day=today)
        if current is not None:
            params["start_weight"] = current
        params["start_date"] = today.isoformat()
    elif kind == "habit":
        target = float(int(target or 1))
        params["per_week"] = int(target)
        unit = "per_week"
    elif kind == "custom":
        target = 100.0
        saved = max(0.0, min(100.0, _num(a.get("progress_pct")) or 0.0))
        unit = "%"
    else:
        saved = max(0.0, _num(a.get("saved_amount")) or 0.0)
        unit = "UZS"
    if not target or target <= 0:
        return {"error": "target_amount required for this kind"}
    row = await db.add_goal(ctx.uid, title=title, target_amount=target, saved_amount=saved, deadline=day.isoformat() if day else None, kind=kind, params=params, unit=unit)
    cache.invalidate(ctx.uid, "goals")
    undo.push(ctx.uid, {"type": "delete_goals", "ids": [row.get("id")]})
    ctx.mutated = True
    statuses, _ = await goals_mod.statuses_for(ctx.profile, goals=[row])
    return {"added": statuses[0] if statuses else row}


@tool("update_goal", "Изменить цель: add_amount — доложить сумму на накопление («отложил 500к на ноутбук»), saved_amount — задать накопленное; "
      "progress_pct — прогресс свободной цели («выучил 60%»); target_amount (сумма/кг/раз в неделю), deadline, title; done=true — закрыть.",
      {"id": ID, "add_amount": P("NUMBER", "добавить к накопленному (отрицательное — снять)"), "saved_amount": P("NUMBER", "накоплено всего"),
       "progress_pct": P("NUMBER", "custom: прогресс 0–100"), "target_amount": P("NUMBER", "новая цель"), "deadline": DATE, "title": P("STRING", "название"),
       "done": P("BOOLEAN", "закрыть цель")}, ("id",))
async def _update_goal(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in await db.list_goals(ctx.uid, include_done=True) if str(r.get("id")) == _str(a.get("id"))]
    if not rows:
        return {"error": "goal not found"}
    row = rows[0]
    fields: dict[str, Any] = {}
    if (add := _num(a.get("add_amount"))) is not None:
        fields["saved_amount"] = max(0.0, float(row.get("saved_amount") or 0) + add)
    if (saved := _num(a.get("saved_amount"))) is not None:
        fields["saved_amount"] = max(0.0, saved)
    if (pct := _num(a.get("progress_pct"))) is not None:
        fields["saved_amount"] = max(0.0, min(100.0, pct))
        if pct >= 100:
            fields["done"] = True
    if (target := _num(a.get("target_amount"))) and target > 0:
        fields["target_amount"] = target
        if goals_mod.kind_of(row) == "habit":
            fields["params"] = {**goals_mod.params_of(row), "per_week": int(target)}
    if _str(a.get("deadline")):
        day = parse_day(a.get("deadline"), ctx.profile.today)
        fields["deadline"] = day.isoformat() if day else None
    if _str(a.get("title")):
        fields["title"] = _str(a.get("title"))
    if (done := _bool(a.get("done"))) is not None:
        fields["done"] = done
    if not fields:
        return {"error": "nothing to change"}
    before = {k: row.get(k) for k in fields}
    await db.update_goal(ctx.uid, row["id"], fields)
    cache.invalidate(ctx.uid, "goals")
    undo.push(ctx.uid, {"type": "goal_fields", "goal_id": row["id"], "fields": before})
    ctx.mutated = True
    statuses, _ = await goals_mod.statuses_for(ctx.profile, goals=[{**row, **fields}])
    return {"goal": statuses[0] if statuses else {**row, **fields}}


@tool("delete_goals", "Удалить цели по id.", {"ids": IDS}, ("ids",))
async def _delete_goals(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    rows = [r for r in await db.list_goals(ctx.uid, include_done=True) if str(r.get("id")) in ids]
    if not rows:
        return {"error": "no matching goals"}
    await db.delete_goals(ctx.uid, [r["id"] for r in rows])
    cache.invalidate(ctx.uid, "goals")
    undo.push(ctx.uid, {"type": "restore_goals", "rows": rows})
    ctx.mutated = True
    return {"deleted": [r.get("title") for r in rows]}


@tool("goal_checkin", "Отметить выполнение привычки за день («сходил в зал», «сделал», «пробежал») или снять отметку (undo=true). Без date — сегодня.",
      {"id": P("STRING", "id цели-привычки (kind=habit)"), "date": DATE, "undo": P("BOOLEAN", "снять отметку"), "note": P("STRING", "комментарий")}, ("id",))
async def _goal_checkin(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("goal_checkins"):
        return {"error": "table goal_checkins missing — run sql/migrations/007_goals.sql in Supabase"}
    rows = [r for r in await services.goals(ctx.uid) if str(r.get("id")) == _str(a.get("id"))]
    if not rows:
        return {"error": "goal not found"}
    row = rows[0]
    if goals_mod.kind_of(row) != "habit":
        return {"error": "not a habit goal; for custom goals use update_goal(progress_pct)"}
    day = parse_day(a.get("date"), ctx.profile.today) or ctx.profile.today
    if _bool(a.get("undo")):
        await services.uncheck(ctx.uid, goal_id=row["id"], day=day)
        undo.push(ctx.uid, {"type": "restore_checkin", "goal_id": row["id"], "day": day.isoformat()})
    else:
        await services.checkin(ctx.uid, goal_id=row["id"], day=day, note=_str(a.get("note")))
        undo.push(ctx.uid, {"type": "delete_checkin", "goal_id": row["id"], "day": day.isoformat()})
    ctx.mutated = True
    statuses, _ = await goals_mod.statuses_for(ctx.profile, goals=[row])
    return {"goal": statuses[0] if statuses else row}


@tool("log_weight", "Записать вес («вес 72.5», «взвесился — 71.8», «вчера был 73»). Обновляет цели по весу. Без date — сегодня.",
      {"weight": P("NUMBER", "вес в кг"), "date": DATE}, ("weight",))
async def _log_weight(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("weight_logs"):
        return {"error": "table weight_logs missing — run sql/migrations/007_goals.sql in Supabase"}
    weight = _num(a.get("weight"))
    if not weight or not (25 <= weight <= 350):
        return {"error": "weight must be 25..350 kg"}
    day = parse_day(a.get("date"), ctx.profile.today) or ctx.profile.today
    prev = next((r for r in await services.weight_logs(ctx.uid) if str(r.get("day"))[:10] == day.isoformat()), None)
    await services.log_weight(ctx.uid, weight=weight, day=day)
    undo.push(ctx.uid, {"type": "restore_weight", "day": day.isoformat(), "row": prev})
    ctx.mutated = True
    weight_goals = [g for g in await services.goals(ctx.uid) if goals_mod.kind_of(g) == "weight"]
    statuses: list[dict[str, Any]] = []
    if weight_goals:
        statuses, _ = await goals_mod.statuses_for(ctx.profile, goals=weight_goals)
    plan = await services.nutrition_profile(ctx.uid)
    return {"logged": {"weight": weight, "date": day.isoformat()}, "weight_goals": statuses,
            "hint": "if a weight goal has flag plan_mismatch — offer to set_nutrition_plan to recommended_kcal" if statuses else None,
            "plan_kcal": (plan or {}).get("daily_calories")}


@tool("my_habits", "Что бот знает о привычках пользователя по его данным: типичный завтрак/обед/ужин (блюда, время, ккал), средняя калорийность дня; "
      "средний расход в день (будни/выходные), обязательные и «по желанию» траты в месяц, частые покупки. Используй для советов «что поесть», «где урезать», «сколько я обычно…».")
async def _my_habits(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    logs, entries = await services.calorie_logs(ctx.profile, 30), await services.finance_entries(ctx.uid)
    return {"meals": habits.meal_patterns(logs, tz=ctx.profile.tz, today=ctx.profile.today), "spending": habits.spending_patterns(entries, today=ctx.profile.today)}


# сроки долгов (set/clear/list_debt_deadlines) — в bot/agent_tools_debts.py: у каждого займа свой срок


# ------------------------------------------------------------------ подъём на фаджр
@tool("get_wake", "Настройки подъёма и план на завтра: во сколько звонить, такбир фаджра, времена намазов, статистика подъёмов за 2 недели. "
      "Вопросы «во сколько ты меня разбудишь?», «когда такбир?», «когда намаз?» — сюда.")
async def _get_wake(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("wake_settings"):
        return {"error": "table wake_settings missing — run sql/migrations/008_wake.sql in Supabase"}
    from . import prayer
    from . import wake_runner

    s, plan = await wake_runner.plan_for(ctx.profile)
    tomorrow = ctx.profile.today + timedelta(days=1)
    _, plan_tomorrow = await wake_runner.plan_for(ctx.profile, tomorrow)
    rows = await prayer.timings(ctx.profile.today, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
    history = await services.wake_history(ctx.uid, days=14)
    return {
        "settings": {"enabled": s.enabled, "mode": s.mode, "fixed_time": s.fixed_time, "offset_min": s.offset_min,
                     "takbir_offset_min": s.takbir_offset_min, "days_of_week": list(s.days_of_week), "call_enabled": s.call_enabled,
                     "voice_lang": s.voice_lang, "talk": s.talk, "skip_until": s.skip_until.isoformat() if s.skip_until else None},
        "today": {"active": plan.active, "reason": plan.reason, "wake_at": plan.wake_at.strftime("%H:%M") if plan.wake_at else None,
                  "fajr_azan": plan.fajr, "takbir": plan.takbir},
        "tomorrow": {"active": plan_tomorrow.active, "wake_at": plan_tomorrow.wake_at.strftime("%H:%M") if plan_tomorrow.wake_at else None,
                     "fajr_azan": plan_tomorrow.fajr, "takbir": plan_tomorrow.takbir},
        "prayer_times_today": rows,
        "caller_ready": caller.available(),
        "history": [{"day": str(r.get("day"))[:10], "woke_at": r.get("woke_at"), "before_takbir": r.get("before_takbir"),
                     "attempts": r.get("attempts")} for r in history],
    }


@tool("set_wake", "Изменить подъём словами: «буди за 30 минут до такбира» (offset_min), «такбир в 5:20» (takbir_time — пересчитает поправку), "
      "«буди в 6:30» (mode=fixed + fixed_time), «буди по будням» (days=[1,2,3,4,5]), «выключи будильник» (enabled=false), "
      "«не звони, пиши» (call_enabled=false), «не буди до понедельника» (skip_until). Заданий и упражнений при подъёме нет — будит словами. "
      "Будильник только на время фаджра (за 30 мин до азана … за 15 мин до восхода): другое время инструмент не примет.",
      {"enabled": P("BOOLEAN", "включить/выключить подъём"), "mode": P("STRING", "fajr — до такбира; fixed — фиксированное время", enum=["fajr", "fixed"]),
       "fixed_time": P("STRING", "HH:MM для mode=fixed"), "offset_min": P("NUMBER", "за сколько минут до такбира звонить"),
       "takbir_time": P("STRING", "во сколько такбир (HH:MM) — пересчитает поправку к азану"),
       "takbir_offset_min": P("NUMBER", "минут между азаном фаджра и такбиром"),
       "days": ARR({"type": "NUMBER"}, "дни недели 1=пн … 7=вс"), "call_enabled": P("BOOLEAN", "звонить или только писать"),
       "voice_lang": P("STRING", "на каком языке JES говорит в трубке: uz | ru | en", enum=["uz", "ru", "en"]),
       "talk": P("BOOLEAN", "живой разговор в трубке (true) или просто сказать и положить трубку (false)"),
       "skip_until": DATE, "skip_days": P("NUMBER", "не будить столько дней подряд, начиная с сегодня")})
async def _set_wake(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("wake_settings"):
        return {"error": "table wake_settings missing — run sql/migrations/008_wake.sql in Supabase"}
    from . import prayer
    from . import wake_runner

    current = await services.wake_settings(ctx.uid)
    s = wake_mod.WakeSettings.from_row(current)
    fields: dict[str, Any] = {}
    if (v := _bool(a.get("enabled"))) is not None:
        fields["enabled"] = v
    if (v := _bool(a.get("call_enabled"))) is not None:
        fields["call_enabled"] = v
    if (mode := _str(a.get("mode"))) in {"fajr", "fixed"}:
        fields["mode"] = mode
    if (ft := _time_arg(a.get("fixed_time"))):
        fields["fixed_time"] = ft
        fields.setdefault("mode", "fixed")
    if (off := _int(a.get("offset_min"))) is not None and 0 <= off <= 180:
        fields["offset_min"] = off
    if (tk := _time_arg(a.get("takbir_time"))):
        rows = await prayer.timings(ctx.profile.today, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
        delta = prayer.offset_from_takbir(rows.get("Fajr"), tk)
        if delta is None:
            return {"error": "takbir_time must be within 2 hours after the fajr azan"}
        fields["takbir_offset_min"] = delta
    if (tof := _int(a.get("takbir_offset_min"))) is not None and 0 <= tof <= 120:
        fields["takbir_offset_min"] = tof
    if a.get("days"):
        days = sorted({int(_num(d) or 0) for d in (a.get("days") or []) if _num(d) and 1 <= int(_num(d)) <= 7})
        if days:
            fields["days_of_week"] = days
    if (vl := _str(a.get("voice_lang"))) in {"uz", "ru", "en"}:
        fields["voice_lang"] = vl
    if (talk := _bool(a.get("talk"))) is not None:
        fields["talk"] = talk
    if (skip_days := _int(a.get("skip_days"))) is not None and skip_days > 0:
        fields["skip_until"] = (ctx.profile.today + timedelta(days=skip_days - 1)).isoformat()
    elif _str(a.get("skip_until")):
        day = parse_day(a.get("skip_until"), ctx.profile.today)
        fields["skip_until"] = day.isoformat() if day else None
    if not fields:
        return {"error": "nothing to change"}
    if {"fixed_time", "offset_min", "takbir_offset_min", "mode"} & set(fields):
        # только на фаджр (его правило 26.09 — чтобы будильником не злоупотребляли и не будили после восхода)
        mode = fields.get("mode", s.mode)
        err = await wake_runner.window_error(ctx.profile, s, fixed=fields.get("fixed_time", s.fixed_time) if mode == "fixed" else None,
                                             offset=fields.get("offset_min"), takbir_offset=fields.get("takbir_offset_min"))
        if err:
            return {"error": err, "ask_exactly": err}
    if fields.get("enabled") and not places.has_place(ctx.uid):
        return {"error": "место не выбрано", "ask_exactly": "Сначала выберите город или отправьте геолокацию: Будильник → 📍 Место."}
    before = {k: current.get(k) for k in fields}
    await services.save_wake_settings(ctx.uid, fields)
    undo.push(ctx.uid, {"type": "wake_settings", "fields": before})
    ctx.mutated = True
    _, plan = await wake_runner.plan_for(ctx.profile, ctx.profile.today + timedelta(days=1))
    return {"saved": fields, "tomorrow": {"wake_at": plan.wake_at.strftime("%H:%M") if plan.wake_at else None, "takbir": plan.takbir, "active": plan.active}}


@tool("mark_awake", "Отметить, что пользователь проснулся («проснулся», «uyg'ondim», «я встал») — звонки на сегодня прекращаются. "
      "snooze_minutes — отложить звонок на N минут («ещё 10 минут»).",
      {"snooze_minutes": P("NUMBER", "отложить на столько минут вместо подтверждения")})
async def _mark_awake(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import wake_runner

    minutes = _int(a.get("snooze_minutes"))
    if minutes and minutes > 0:
        until = await wake_runner.snooze(ctx.profile, minutes)
        return {"snoozed_until": until.astimezone(ctx.profile.tz).strftime("%H:%M")}
    if not await db.ensure_available("wake_log"):
        return {"error": "table wake_log missing — run sql/migrations/008_wake.sql in Supabase"}
    from .context import bot_instance

    result = await wake_runner.mark_awake(bot_instance(), ctx.profile, source="agent", notify=False)
    ctx.mutated = True
    return {"awake": True, "before_takbir": result["before_takbir"], "streak_days": result["streak"], "takbir": result["plan"].takbir}


@tool("test_wake_call", "Проверить будильник прямо сейчас: звонок ровно как утром (режим подъёма: мотивация, проверка по голосу, что встал). "
      "«давай проверим будильник», «протестируй подъём», «позвони как будильник», «budilnikni tekshir». В журнал подъёмов не пишется. "
      "Звонок идёт фоном: ответь одной строкой, что звонишь в режиме будильника.", {})
async def _test_wake_call(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import call_assistant

    if not caller.available():
        return {"error": "звонки не настроены: в .env нужны TG_CALLER_API_ID / TG_CALLER_API_HASH / TG_CALLER_SESSION"}
    if call_assistant.wake_test_in_background(ctx.profile) is None:
        return {"error": "уже идёт звонок"}
    return {"calling": True, "mode": "wake_test", "note": "звонок будильника начнётся через пару секунд"}


@tool("call_me", "Позвонить пользователю в Telegram прямо сейчас и поговорить голосом (он просит «позвони», «набери меня», «qo'ng'iroq qil»). "
      "Если просит проверить будильник/подъём — не это, а test_wake_call. "
      "В разговоре у тебя те же инструменты: можно ответить на вопросы по данным и записать траты/еду/задачи с голоса. "
      "topic — с чего начать («расскажи, сколько я потратил», «обсудим цели»); без topic просто поздороваешься и будешь слушать. "
      "Звонок идёт фоном: ответь пользователю одной строкой, что звонишь.",
      {"topic": P("STRING", "о чём начать разговор"), "lang": P("STRING", "язык разговора: uz | ru | en", enum=["uz", "ru", "en"])})
async def _call_me(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import call_assistant

    if not caller.available():
        return {"error": "звонки не настроены: в .env нужны TG_CALLER_API_ID / TG_CALLER_API_HASH / TG_CALLER_SESSION"}
    lang = _str(a.get("lang"))
    call_assistant.call_in_background(ctx.profile, topic=_str(a.get("topic")) or "", lang=lang if lang in {"uz", "ru", "en"} else None)
    return {"calling": True, "topic": _str(a.get("topic")), "note": "звонок начнётся через пару секунд"}


@tool("prayer_times", "Времена намаза на день (Андижан по умолчанию): азан фаджра, такбир джамоата, пешин, аср, шом, хуфтон; и какой намаз ближайший.",
      {"date": DATE})
async def _prayer_times(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import prayer
    from . import wake_runner

    day = parse_day(a.get("date"), ctx.profile.today) or ctx.profile.today
    s, _ = await wake_runner.plan_for(ctx.profile, day)
    rows = await prayer.timings(day, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)
    if not rows:
        return {"error": "prayer times are not available right now"}
    takbir = prayer.takbir_time(rows.get("Fajr"), s.takbir_offset_min)
    nxt = prayer.next_prayer(rows, ctx.profile.now, ctx.profile.lang) if day == ctx.profile.today else None
    return {"date": day.isoformat(), "times": rows, "fajr_takbir": takbir.strftime("%H:%M") if takbir else None,
            "next": {"name": nxt[0], "at": nxt[1], "in_minutes": nxt[2]} if nxt else None}


# ------------------------------------------------------------------ snapshot fragment
async def snapshot_lines(ctx_profile: Any) -> list[str]:
    """Строки для системного промпта: заметки, задачи, цели, сроки долгов."""
    uid = ctx_profile.telegram_id
    today = ctx_profile.today
    parts: list[str] = []

    async def safe(coro, default):  # noqa: ANN001
        try:
            return await coro
        except Exception:
            logger.debug("snapshot part failed", exc_info=True)
            return default

    # всё сразу, а не по очереди (каждое — поход в Supabase, если кэш остыл)
    notes, tasks, goals, logs, entries, deadlines = await asyncio.gather(
        services.notes(uid), services.tasks(uid), services.goals(uid),
        safe(services.calorie_logs(ctx_profile, 30), None), safe(services.finance_entries(uid), None), safe(services.debt_due_rows(uid), []))
    if notes:
        parts.append("Заметки (id · текст): " + "; ".join(f"[{r.get('id')}] {str(r.get('text') or '')[:80]}" for r in notes[:20]))
    if tasks:
        def _t(r: dict[str, Any]) -> str:
            due = str(r.get("due_date") or "")[:10]
            when = (f" до {due}" if due else "") + (f" {r.get('due_time')}" if r.get("due_time") else "")
            return f"[{r.get('id')}] {str(r.get('text') or '')[:60]}{when}"
        parts.append(f"Открытые задачи ({len(tasks)}): " + "; ".join(_t(r) for r in tasks[:15]))
    if goals:
        try:
            statuses, _ = await goals_mod.statuses_for(ctx_profile, goals=goals[:8])
            parts.append("Цели (id · вид · расчёт по данным): " + goals_mod.prompt_summary(statuses))
        except Exception:
            parts.append("Цели: " + "; ".join(f"[{g.get('id')}] {g.get('title')} ({goals_mod.kind_of(g)})" for g in goals[:8]))
    try:
        if logs is not None and entries is not None:
            parts.extend(habits.prompt_lines(habits.meal_patterns(logs, tz=ctx_profile.tz, today=today), habits.spending_patterns(entries, today=today)))
    except Exception:
        pass
    if deadlines:
        def _d(r: dict[str, Any]) -> str:
            due = str(r.get("due_date") or "")[:10]
            try:
                left = (date.fromisoformat(due) - today).days
                tail = f" (просрочено {-left} дн.)" if left < 0 else f" (через {left} дн.)"
            except ValueError:
                tail = ""
            return f"{r.get('person')} {'мне' if r.get('side') == 'lent' else 'я'} {fin.fmt_money(float(r.get('amount') or 0))} до {due}{tail}"
        parts.append("Сроки долгов (по займам): " + "; ".join(_d(r) for r in deadlines[:10]))
    return parts
