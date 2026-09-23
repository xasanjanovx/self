"""Работа с базой «всё сразу»: найти любую запись по словам, исправить много записей одним ходом,
удалить ошибочный вес. Регистрируются в общем реестре `agent_tools.TOOLS` (чат, звонки, телефон).

Каждое изменение идёт через обычные инструменты (update_finance_entry, update_task…) — с их проверками
и шагами отката, поэтому «отмени» возвращает всю пачку разом.
"""
from __future__ import annotations

from typing import Any

from . import reminders as rem
from . import services
from . import undo
from .agent_tools import ARR, DATE, IDS, P, TOOLS, ToolContext, _bool, _num, _str, entry_view, fuzzy_contains, parse_day, tool
from .context import db

FIND_LIMIT = 30
BULK_LIMIT = 100
_TYPES = ["finance", "notes", "tasks", "reminders", "food", "goals", "recurring", "debts"]


def _hit(query: str, *texts: Any) -> bool:
    return any(t and fuzzy_contains(query, str(t)) for t in texts)


def _amount_hit(amount: float | None, value: Any) -> bool:
    try:
        return amount is not None and abs(float(value or 0) - amount) < 0.5
    except (TypeError, ValueError):
        return False


@tool("find_records",
      "Найти ЛЮБУЮ запись в его данных по словам или сумме, когда не знаешь, где она: операции, заметки, задачи (и выполненные), "
      "напоминания, еда за 60 дней, цели, регулярные платежи, сроки долгов. Возвращает тип, id, дату и текст — дальше меняй/удаляй "
      "обычными инструментами по id. «Найди, где я записал про Асилбека», «удали запись про 150 тысяч», «исправь заметку про пароль».",
      {"query": P("STRING", "слова или сумма («такси», «Асилбек», «150000»)"),
       "types": ARR({"type": "STRING", "enum": _TYPES}, "где искать (по умолчанию везде)")}, ("query",))
async def _find_records(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    query = _str(a.get("query"))
    if not query:
        return {"error": "что искать?"}
    types = {t for t in (a.get("types") or []) if t in _TYPES} or set(_TYPES)
    amount = _num(query) if any(ch.isdigit() for ch in query) else None
    uid = ctx.uid
    out: list[dict[str, Any]] = []

    if "finance" in types:
        for r in await services.finance_entries(uid):
            v = entry_view(r)
            if _hit(query, v.get("note"), v.get("category")) or _amount_hit(amount, r.get("amount")):
                out.append({"type": "finance", "id": v["id"], "date": v["date"], "amount": v["amount"],
                            "text": " · ".join(str(x) for x in (v.get("category") or v.get("kind"), v.get("note")) if x)})
    if "notes" in types:
        for r in await services.notes(uid):
            if _hit(query, r.get("text")):
                out.append({"type": "note", "id": str(r.get("id")), "date": str(r.get("created_at") or "")[:10], "text": r.get("text")})
    if "tasks" in types and db.available("tasks"):
        for r in await db.list_tasks(uid, include_done=True):
            if _hit(query, r.get("text")):
                out.append({"type": "task", "id": str(r.get("id")), "date": str(r.get("due_date") or "")[:10], "text": r.get("text"),
                            "done": bool(r.get("done"))})
    if "reminders" in types:
        for r in await services.reminders(uid):
            if _hit(query, rem.title(r)):
                out.append({"type": "reminder", "id": str(r.get("id")), "text": rem.title(r)})
    if "food" in types:
        for r in await services.calorie_logs(ctx.profile, 60):
            if _hit(query, r.get("meal_desc")) or _amount_hit(amount, r.get("calories")):
                out.append({"type": "food", "id": str(r.get("id")), "date": str(r.get("created_at") or "")[:10],
                            "text": r.get("meal_desc"), "kcal": r.get("calories")})
    if "goals" in types:
        for r in await services.goals(uid):
            if _hit(query, r.get("title"), r.get("note")):
                out.append({"type": "goal", "id": str(r.get("id")), "text": r.get("title")})
    if "recurring" in types:
        for r in await services.recurring(uid):
            if _hit(query, r.get("title")) or _amount_hit(amount, r.get("amount")):
                out.append({"type": "recurring", "id": str(r.get("id")), "text": r.get("title"), "amount": r.get("amount")})
    if "debts" in types:
        for r in await services.debt_deadlines(uid):
            if _hit(query, r.get("person"), r.get("note")):
                out.append({"type": "debt_deadline", "person": r.get("person"), "side": r.get("side"), "date": str(r.get("due_date") or "")[:10]})
    out.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    if not out:
        return {"found": [], "note": "ничего не нашлось — спроси, как ещё это могло быть записано, или поищи другими словами"}
    return {"total": len(out), "found": out[:FIND_LIMIT]}


async def _each(ctx: ToolContext, tool_name: str, ids: list[str], changes: dict[str, Any]) -> dict[str, Any]:
    handler = TOOLS[tool_name].handler
    done, failed = [], []
    for rid in ids[:BULK_LIMIT]:
        res = await handler(ctx, {**changes, "id": rid})
        if isinstance(res, dict) and res.get("error"):
            failed.append({"id": rid, "error": res["error"]})
        else:
            done.append(rid)
    return {"changed": len(done), "failed": failed[:10], "note": "всё можно вернуть одной командой «отмени» (undo_last)"}


@tool("update_finance_entries",
      "Исправить МНОГО операций одним ходом: одинаково поменять категорию, дату, счёт или комментарий («все такси за неделю — в Транспорт», "
      "«вчерашние траты перенеси на сегодня», «эти три — с карты»). id возьми из list_finance_entries / find_records.",
      {"ids": IDS, "category": P("STRING", "новый ключ категории"), "date": DATE, "bucket": P("STRING", "card | cash", enum=["card", "cash"]),
       "note": P("STRING", "новый комментарий")}, ("ids",))
async def _update_entries(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = [str(x) for x in (a.get("ids") or []) if _str(x)]
    changes = {k: a[k] for k in ("category", "date", "bucket", "note") if a.get(k) is not None}
    if not ids or not changes:
        return {"error": "нужны ids и что поменять"}
    return await _each(ctx, "update_finance_entry", ids, changes)


@tool("update_tasks",
      "Изменить МНОГО задач одним ходом: перенести на дату/время, отметить выполненными или вернуть в работу "
      "(«перенеси все сегодняшние задачи на завтра», «отметь всё выполненным»).",
      {"ids": IDS, "due_date": DATE, "due_time": P("STRING", "HH:MM"), "done": P("BOOLEAN", "выполнены")}, ("ids",))
async def _update_tasks(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = [str(x) for x in (a.get("ids") or []) if _str(x)]
    changes = {k: a[k] for k in ("due_date", "due_time") if _str(a.get(k))}
    if (done := _bool(a.get("done"))) is not None:
        changes["done"] = done
    if not ids or not changes:
        return {"error": "нужны ids и что поменять"}
    return await _each(ctx, "update_task", ids, changes)


@tool("delete_weight", "Удалить ошибочные записи веса по датам («удали вес за вчера»).",
      {"dates": ARR({"type": "STRING"}, "даты YYYY-MM-DD / today / yesterday")}, ("dates",))
async def _delete_weight(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    days = {d.isoformat() for x in (a.get("dates") or []) if (d := parse_day(x, ctx.profile.today))}
    rows = [r for r in await services.weight_logs(ctx.uid) if str(r.get("day"))[:10] in days]
    if not rows:
        return {"error": "за эти даты веса нет"}
    await db.delete_weight_logs(ctx.uid, [str(r.get("day"))[:10] for r in rows])
    from . import cache

    cache.invalidate(ctx.uid, "weights")
    for r in rows:
        undo.push(ctx.uid, {"type": "restore_weight", "day": str(r.get("day"))[:10], "row": r})
    ctx.mutated = True
    return {"deleted": [{"date": str(r.get("day"))[:10], "weight": r.get("weight")} for r in rows]}


__all__ = ["FIND_LIMIT", "BULK_LIMIT"]
