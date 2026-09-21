"""Инструменты «Джарвиса» уровня ассистента: заметки («запомни…»), задачи, цели
накоплений, сроки возврата долгов. Регистрируются в общем реестре `agent_tools.TOOLS`
(модуль импортируется в конце bot/agent_tools.py).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import analysis
from . import cache
from . import finance as fin
from . import services
from . import undo
from .agent_tools import DATE, ID, IDS, P, ToolContext, _bool, _num, _str, _time_arg, parse_day, tool
from .context import db


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
    rows = await services.goals(ctx.uid)
    if not rows:
        return []
    snap = await services.finance_snapshot(ctx.profile)
    recurring, budgets = await services.recurring(ctx.uid), await services.budgets(ctx.uid)
    fc = analysis.forecast(snap.entries, ctx.profile.today, balances=snap.balances, recurring=recurring, budgets=budgets)
    projected_saving = fc["income_this_month"] - fc["projected_month_expense"] - fc["recurring_remaining"]
    return [analysis.goal_status(g, ctx.profile.today, projected_saving_month=projected_saving) for g in rows]


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


# ------------------------------------------------------------------ savings goals
@tool("list_goals", "Цели накоплений: прогресс, сколько осталось, сколько нужно откладывать в месяц, успеваем ли при текущем темпе трат.")
async def _list_goals(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("savings_goals"):
        return _migration_error("savings_goals")
    return {"goals": await goals_with_status(ctx)}


@tool("add_goal", "Создать цель накопления («хочу накопить 10 млн на ноутбук к январю»). saved_amount — сколько уже отложено.",
      {"title": P("STRING", "на что"), "target_amount": P("NUMBER", "сумма цели"), "deadline": DATE, "saved_amount": P("NUMBER", "уже отложено")}, ("title", "target_amount"))
async def _add_goal(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("savings_goals"):
        return _migration_error("savings_goals")
    title, target = _str(a.get("title")), _num(a.get("target_amount"))
    if not title or not target or target <= 0:
        return {"error": "title and target_amount required"}
    day = parse_day(a.get("deadline"), ctx.profile.today)
    row = await db.add_goal(ctx.uid, title=title, target_amount=target, saved_amount=max(0.0, _num(a.get("saved_amount")) or 0.0), deadline=day.isoformat() if day else None)
    cache.invalidate(ctx.uid, "goals")
    undo.push(ctx.uid, {"type": "delete_goals", "ids": [row.get("id")]})
    ctx.mutated = True
    return {"added": analysis.goal_status(row, ctx.profile.today)}


@tool("update_goal", "Изменить цель: add_amount — доложить сумму («отложил 500к на ноутбук»), saved_amount — задать накопленное, target_amount, deadline, title; done=true — закрыть.",
      {"id": ID, "add_amount": P("NUMBER", "добавить к накопленному (отрицательное — снять)"), "saved_amount": P("NUMBER", "накоплено всего"),
       "target_amount": P("NUMBER", "новая сумма цели"), "deadline": DATE, "title": P("STRING", "название"), "done": P("BOOLEAN", "закрыть цель")}, ("id",))
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
    if (target := _num(a.get("target_amount"))) and target > 0:
        fields["target_amount"] = target
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
    return {"goal": analysis.goal_status({**row, **fields}, ctx.profile.today)}


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


# ------------------------------------------------------------------ debt deadlines
def match_person(name: str, entries: list[dict[str, Any]], settings: dict[str, float]) -> tuple[str | None, str | None, float]:
    """Подобрать имя из долгов по людям (нечётко). Возвращает (имя как в базе, side, сумма)."""
    from .agent_tools import fuzzy_contains

    ledger = fin.debt_ledger(entries, settings)
    for side in ("lent", "debt"):
        for person, amount in ledger[side]:
            if person and (fuzzy_contains(name, person) or fuzzy_contains(person, name)):
                return person, side, amount
    return None, None, 0.0


@tool("set_debt_deadline", "Задать срок возврата долга по человеку («Асилбек вернёт до 5 октября», «я должен вернуть Хамкорбанку до 1 ноября»). Бот напомнит за день и при просрочке.",
      {"person": P("STRING", "имя, как в долгах"), "due_date": DATE, "side": P("STRING", "lent — мне должны (по умолчанию), debt — я должен", enum=["lent", "debt"]), "note": P("STRING", "комментарий")},
      ("person", "due_date"))
async def _set_deadline(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("debt_deadlines"):
        return _migration_error("debt_deadlines")
    name, day = _str(a.get("person")), parse_day(a.get("due_date"), ctx.profile.today)
    if not name or not day:
        return {"error": "person and due_date (YYYY-MM-DD) required"}
    snap = await services.finance_snapshot(ctx.profile)
    matched, side_found, amount = match_person(name, snap.entries, snap.settings)
    side = _str(a.get("side")) or side_found or "lent"
    person = matched or name
    prev = next((r for r in await services.debt_deadlines(ctx.uid) if r.get("person") == person and r.get("side") == side), None)
    row = await db.upsert_debt_deadline(ctx.uid, person=person, side=side, due_date=day.isoformat(), note=_str(a.get("note")))
    cache.invalidate(ctx.uid, "debt_deadlines")
    undo.push(ctx.uid, {"type": "restore_debt_deadline", "person": person, "side": side, "row": prev})
    ctx.mutated = True
    return {"deadline": deadline_view(row or {"person": person, "side": side, "due_date": day.isoformat()}, ctx.profile.today), "amount_now": round(amount, 2), "matched_person": matched}


@tool("clear_debt_deadline", "Убрать срок возврата по человеку.", {"person": P("STRING", "имя"), "side": P("STRING", "lent | debt", enum=["lent", "debt"])}, ("person",))
async def _clear_deadline(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from .agent_tools import fuzzy_contains

    name = _str(a.get("person")) or ""
    rows = [r for r in await services.debt_deadlines(ctx.uid) if fuzzy_contains(name, str(r.get("person") or "")) and (not _str(a.get("side")) or r.get("side") == _str(a.get("side")))]
    if not rows:
        return {"error": "no deadline for that person"}
    for r in rows:
        await db.delete_debt_deadline(ctx.uid, person=str(r.get("person")), side=str(r.get("side")))
        undo.push(ctx.uid, {"type": "restore_debt_deadline", "person": r.get("person"), "side": r.get("side"), "row": r})
    cache.invalidate(ctx.uid, "debt_deadlines")
    ctx.mutated = True
    return {"cleared": [deadline_view(r) for r in rows]}


@tool("list_debt_deadlines", "Сроки возврата долгов (кто и до какого числа), просрочки.")
async def _list_deadlines(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("debt_deadlines"):
        return _migration_error("debt_deadlines")
    return {"deadlines": [deadline_view(r, ctx.profile.today) for r in await services.debt_deadlines(ctx.uid)]}


# ------------------------------------------------------------------ snapshot fragment
async def snapshot_lines(ctx_profile: Any) -> list[str]:
    """Строки для системного промпта: заметки, задачи, цели, сроки долгов."""
    uid = ctx_profile.telegram_id
    today = ctx_profile.today
    parts: list[str] = []
    notes = await services.notes(uid)
    if notes:
        parts.append("Заметки (id · текст): " + "; ".join(f"[{r.get('id')}] {str(r.get('text') or '')[:80]}" for r in notes[:20]))
    tasks = await services.tasks(uid)
    if tasks:
        def _t(r: dict[str, Any]) -> str:
            due = str(r.get("due_date") or "")[:10]
            when = (f" до {due}" if due else "") + (f" {r.get('due_time')}" if r.get("due_time") else "")
            return f"[{r.get('id')}] {str(r.get('text') or '')[:60]}{when}"
        parts.append(f"Открытые задачи ({len(tasks)}): " + "; ".join(_t(r) for r in tasks[:15]))
    goals = await services.goals(uid)
    if goals:
        parts.append("Цели накоплений: " + "; ".join(
            f"[{g.get('id')}] {g.get('title')} {fin.fmt_money(float(g.get('saved_amount') or 0))}/{fin.fmt_money(float(g.get('target_amount') or 0))}"
            + (f" до {str(g.get('deadline'))[:10]}" if g.get("deadline") else "") for g in goals[:8]))
    deadlines = await services.debt_deadlines(uid)
    if deadlines:
        def _d(r: dict[str, Any]) -> str:
            due = str(r.get("due_date") or "")[:10]
            try:
                left = (date.fromisoformat(due) - today).days
                tail = f" (просрочено {-left} дн.)" if left < 0 else f" (через {left} дн.)"
            except ValueError:
                tail = ""
            return f"{r.get('person')} {'мне' if r.get('side') == 'lent' else 'я'} до {due}{tail}"
        parts.append("Сроки долгов: " + "; ".join(_d(r) for r in deadlines[:10]))
    return parts
