"""Инструменты «бухгалтера» JES: займы, погашения, перекредитование и сроки — по каждому займу отдельно.

- record_debt — любые операции с долгами одним вызовом; суммы, остатки, счета и сроки считает bot/debts.py,
  чего не хватает — сам спрашивает кнопками (ctx.ask), ничего не записав;
- list_debts — кредиторы и должники с займами (loan_id, остаток, срок);
- set_debt_deadline / clear_debt_deadline / list_debt_deadlines — сроки конкретных займов
  (тег [due:…] в операции займа; старая таблица debt_deadlines — только если займа-операции нет).

Регистрируются в общем реестре `agent_tools.TOOLS` (модуль импортируется в конце bot/agent_tools.py).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import cache
from . import debts
from . import finance as fin
from . import services
from . import undo
from .agent_tools import ARR, DATE, P, ToolContext, _bool, _str, parse_day, tool
from .context import db

logger = logging.getLogger(__name__)

_DEBT_ITEM = {
    "type": "OBJECT",
    "properties": {
        "action": P("STRING", "borrow — взял в долг/кредит/займ (деньги пришли); repay — вернул свой долг/погасил кредит; "
                              "lend — дал в долг/оплатил за друга; collect — мне вернули; owe_existing — я уже был должен до учёта; "
                              "owed_existing — мне уже были должны до учёта; buy_on_credit — купил в рассрочку/кредит (Uzum Nasiya и т.п.); "
                              "creditor_forgave — мне простили/списали долг; i_forgave — я простил, не вернут",
                    enum=list(debts.ACTIONS)),
        "person": P("STRING", "кто: человек или банк, как сказал пользователь (Узум, TEZ, Асилбек) — инструмент сам найдёт в учёте"),
        "amount": P("NUMBER", "сумма; для repay/collect без суммы НЕ передавай — закроется весь остаток или инструмент спросит"),
        "account": P("STRING", "card | cash — ТОЛЬКО если он сам сказал (карта/наличные/на карту/налом); иначе не передавай", enum=["card", "cash"]),
        "due_date": P("STRING", "срок возврата займа YYYY-MM-DD, если назвал («до 5 октября», «через месяц»)"),
        "loan_id": P("STRING", "какой именно займ гасится (id из «Долги» / list_debts), если он указал"),
        "interest": P("NUMBER", "часть платежа — проценты/комиссия (repay: расход; collect: доход)"),
        "keep_overpay": P("BOOLEAN", "он подтвердил: заплатил/получил больше долга — это переплата"),
        "existed_before": P("BOOLEAN", "он подтвердил: этот долг был до начала учёта"),
        "no_due": P("BOOLEAN", "он сказал: займ без срока"),
        "category": P("STRING", "buy_on_credit: ключ категории покупки"),
        "note": P("STRING", "buy_on_credit: что купил"),
        "date": DATE,
    },
    "required": ["action", "person"],
}


def _money_line(before: dict[str, float], after: dict[str, float]) -> dict[str, str]:
    labels = {"card": "карта", "cash": "наличные", "debt": "я должен", "lent": "мне должны"}
    return {labels[b]: f"{fin.fmt_money(before.get(b, 0))} → {fin.fmt_money(after.get(b, 0))}"
            for b in ("card", "cash", "debt", "lent") if abs(after.get(b, 0) - before.get(b, 0)) >= 1}


@tool(
    "record_debt",
    "БУХГАЛТЕР ДОЛГОВ: записать займы, возвраты, кредиты, рассрочку, «оплатил за друга», старые долги, прощённые долги — "
    "все операции фразы одним вызовом по порядку («взял у Uzum и погасил TEZ» = borrow Uzum + repay TEZ). "
    "Сам считает остатки (погашение без суммы = весь долг), гасит займ с ближайшим сроком, считает, сколько осталось на карте, "
    "выбирает карту/наличные; чего не хватает (карта или наличные у человека, срок займа у банка, какой займ, проценты при переплате) — "
    "сам спросит кнопками и ничего не запишет. Не считай суммы сам и не спрашивай до вызова.",
    {"items": ARR(_DEBT_ITEM, "операции по порядку")},
    ("items",),
)
async def _record_debt(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    items = [it for it in (a.get("items") or []) if isinstance(it, dict)]
    if not items:
        return {"error": "items required"}
    snap = await services.finance_snapshot(ctx.profile)
    book = fin.debt_book(snap.entries, snap.settings, await services.debt_deadlines(ctx.uid))
    p = debts.plan(items, book=book, balances=snap.balances, today=ctx.profile.today, text=ctx.text, lang=ctx.profile.lang)
    if p.error:
        return {"error": p.error}
    if p.ask:
        ctx.ask = {"question": p.ask["question"], "options": p.ask["options"], "by": "record_debt"}
        return {"asked": p.ask["question"], "options": p.ask["options"], "nothing_saved": True, "hint": p.ask["hint"]}
    if not p.rows:
        return {"error": "nothing to record"}
    if ctx.ask and ctx.ask.get("by") == "record_debt":
        ctx.ask = None  # повторный вызов в том же ходе уже с ответом — прошлый вопрос не нужен
    inserted = await db.add_finance_entries(ctx.uid, p.rows, entry_date=ctx.profile.today, source="agent")
    cache.invalidate(ctx.uid, "fin_entries")
    ids = [r.get("id") for r in inserted if r.get("id") is not None]
    undo.push(ctx.uid, {"type": "delete_entries", "ids": ids})
    ctx.mutated = True
    uz = ctx.profile.lang == "uz"
    balances = _money_line(p.before, p.after)
    debts_now = [debts.describe(cp, uz) for cp in p.people]
    reply = ["✅ " + line for line in p.lines] + ["📌 " + d for d in debts_now] + \
            [f"💼 {k.capitalize()}: {v}" for k, v in balances.items()] + ["⚠️ " + w for w in p.warnings]
    ctx.fixed_reply = "\n".join(filter(None, [ctx.fixed_reply, "\n".join(reply)]))
    return {
        "done": p.lines,
        "balances": balances,
        "wallet_now": fin.fmt_money(p.after.get("card", 0) + p.after.get("cash", 0)),
        "debts_now": debts_now,
        "warnings": p.warnings,
        "reply_rule": "Ответь коротко по этим данным: что записано, долг по кредитору и сроки, что изменилось на карте/наличных (balances); "
                      "строку про остаток займа на карте из done и warnings — обязательно. Своих цифр не добавляй.",
    }


def loan_view(t: fin.Tranche, today: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"loan_id": t.id, "date": t.date.isoformat() if t.date else None, "amount": round(t.amount, 2),
                           "left": round(t.left, 2), "due": t.due.isoformat() if t.due else None, "account": t.account}
    if t.due and today:
        out["days_left"] = (t.due - today).days
    return out


def person_view(cp: fin.Counterparty, today: Any = None, *, all_loans: bool = False) -> dict[str, Any]:
    loans = cp.tranches if all_loans else cp.open_tranches()
    out: dict[str, Any] = {"name": cp.name, "total": round(cp.total, 2), "loans": [loan_view(t, today) for t in loans]}
    if cp.credit >= 1:
        out["overpaid"] = round(cp.credit, 2)
    return out


@tool("list_debts", "Долги по займам: кто должен мне (lent) и кому должен я (debt); у каждого — займы с loan_id, остатком и сроком. "
      "Пустое имя = «без имени». closed=true — показать и закрытые займы.",
      {"closed": P("BOOLEAN", "включить закрытые займы и погашенных кредиторов")})
async def _list_debts(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    book = await services.debt_book(ctx.uid)
    snap = await services.finance_snapshot(ctx.profile)
    closed = bool(_bool(a.get("closed")))
    today = ctx.profile.today

    def side(name: str) -> list[dict[str, Any]]:
        people = [cp for cp in book[name].values() if closed or abs(cp.total) >= 1]
        return [person_view(cp, today, all_loans=closed) for cp in sorted(people, key=lambda c: -abs(c.total))]

    return {"lent": side("lent"), "debt": side("debt"),
            "totals": {"lent": round(snap.balances["lent"], 2), "debt": round(snap.balances["debt"], 2)},
            "deadlines": fin.effective_deadlines(book)}


# ------------------------------------------------------------------ сроки займов
def _find(book: dict[str, dict[str, fin.Counterparty]], name: str, side: str | None) -> tuple[fin.Counterparty | None, list[fin.Counterparty]]:
    sides = [side] if side in {"lent", "debt"} else ["lent", "debt"]
    found: list[fin.Counterparty] = []
    for s in sides:
        found += [cp for cp in fin.find_counterparty(book, s, name) if cp.total >= 1] or fin.find_counterparty(book, s, name)
    exact = [cp for cp in found if fin.person_key(cp.name) == fin.person_key(name)]
    if len(exact) == 1:
        return exact[0], found
    return (found[0] if len(found) == 1 else None), found


async def _retag(ctx: ToolContext, loan_ids: list[str], due: str | None) -> list[str]:
    """Поставить/снять срок у займов-операций. Возвращает id, которые удалось поменять."""
    changed = []
    for lid in loan_ids:
        if not lid.isdigit():
            continue
        row = await db.get_finance_entry(ctx.uid, lid)
        if not row:
            continue
        note = fin.renote(row.get("note"), fin.clean_note(row.get("note")), due=due)
        if note == row.get("note"):
            continue
        await db.update_finance_entry(ctx.uid, row["id"], {"note": note})
        undo.push(ctx.uid, {"type": "restore_fields", "entry_id": row["id"], "fields": {"note": row.get("note")}})
        changed.append(lid)
    if changed:
        cache.invalidate(ctx.uid, "fin_entries")
        ctx.mutated = True
    return changed


@tool("set_debt_deadline", "Срок возврата займа («Асилбек вернёт до 5 октября», «срок по Uzum 1 050 000 — 26 октября»). У каждого займа свой срок: "
      "если у человека несколько займов и не ясно какой — инструмент сам спросит. Бот напомнит за день и при просрочке.",
      {"person": P("STRING", "имя или банк, как в долгах"), "due_date": DATE,
       "side": P("STRING", "lent — мне должны, debt — я должен (если не ясно — не передавай)", enum=["lent", "debt"]),
       "loan_id": P("STRING", "id займа (из «Долги»), если назван конкретный займ"),
       "all_loans": P("BOOLEAN", "срок для всех открытых займов этого человека"),
       "note": P("STRING", "комментарий")},
      ("person", "due_date"))
async def _set_deadline(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    name = _str(a.get("person"))
    day = parse_day(a.get("due_date"), ctx.profile.today) or debts.parse_due(a.get("due_date"), ctx.profile.today)
    if not name or not day:
        return {"error": "person and due_date (YYYY-MM-DD) required"}
    book = await services.debt_book(ctx.uid)
    cp, found = _find(book, name, _str(a.get("side")))
    if cp is None and len(found) > 1:
        ctx.ask = {"question": "Кто именно?", "options": [f.name for f in found[:4]]}
        return {"asked": ctx.ask["question"], "hint": "ответ → повтори set_debt_deadline с person = выбранное имя"}
    if cp is not None:
        open_loans = cp.open_tranches()
        loan_id = _str(a.get("loan_id"))
        if loan_id:
            targets = [loan_id] if any(t.id == loan_id for t in cp.tranches) else []
            if not targets:
                return {"error": f"loan_id {loan_id} не найден у {cp.name}", "loans": [loan_view(t) for t in open_loans]}
        elif len(open_loans) <= 1 or _bool(a.get("all_loans")):
            targets = [t.id for t in open_loans]
        else:
            opts = [f"{fin.fmt_money(t.left)} · {debts._due_label(t.due, False)}" for t in open_loans[:3]] + ["Все займы"]
            ctx.ask = {"question": f"У {cp.name} несколько займов — какому поставить срок {day:%d.%m}?", "options": opts}
            return {"asked": ctx.ask["question"], "hint": "ответ → повтори set_debt_deadline с loan_id выбранного займа или all_loans=true; займы: "
                    + ", ".join(f"{t.id} = {fin.fmt_money(t.left)} {debts._due_label(t.due, False)}" for t in open_loans)}
        entry_ids = [t for t in targets if t.isdigit()]
        changed = await _retag(ctx, entry_ids, day.isoformat())
        if entry_ids:
            return {"deadline": {"person": cp.name, "side": cp.side, "due_date": day.isoformat(), "days_left": (day - ctx.profile.today).days},
                    "loans": changed or entry_ids, "amount_now": round(sum(t.left for t in open_loans if t.id in targets), 2), "matched_person": cp.name}
    # займа-операции нет (стартовая сумма «без имени» или человека нет в учёте) — старый срок «по человеку»
    if not await db.ensure_available("debt_deadlines"):
        return {"error": "у этого человека нет займа в учёте — сначала запиши долг (record_debt)"}
    person = cp.name if cp is not None else name
    side = _str(a.get("side")) or (cp.side if cp is not None else "lent")
    prev = next((r for r in await services.debt_deadlines(ctx.uid) if r.get("person") == person and r.get("side") == side), None)
    await db.upsert_debt_deadline(ctx.uid, person=person, side=side, due_date=day.isoformat(), note=_str(a.get("note")))
    cache.invalidate(ctx.uid, "debt_deadlines")
    undo.push(ctx.uid, {"type": "restore_debt_deadline", "person": person, "side": side, "row": prev})
    ctx.mutated = True
    return {"deadline": {"person": person, "side": side, "due_date": day.isoformat(), "days_left": (day - ctx.profile.today).days},
            "amount_now": round(cp.total, 2) if cp is not None else 0, "matched_person": cp.name if cp is not None else None}


@tool("clear_debt_deadline", "Убрать срок возврата (у всех займов человека или у одного — loan_id).",
      {"person": P("STRING", "имя"), "side": P("STRING", "lent | debt", enum=["lent", "debt"]), "loan_id": P("STRING", "id займа")}, ("person",))
async def _clear_deadline(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from .agent_tools import fuzzy_contains

    name = _str(a.get("person")) or ""
    side = _str(a.get("side"))
    book = await services.debt_book(ctx.uid)
    cp, _ = _find(book, name, side)
    cleared: list[dict[str, Any]] = []
    if cp is not None:
        loan_id = _str(a.get("loan_id"))
        ids = [t.id for t in cp.tranches if t.due and (not loan_id or t.id == loan_id)]
        for lid in await _retag(ctx, ids, None):
            cleared.append({"person": cp.name, "side": cp.side, "loan_id": lid})
    rows = [r for r in await services.debt_deadlines(ctx.uid)
            if fuzzy_contains(name, str(r.get("person") or "")) and (not side or r.get("side") == side)]
    for r in rows:
        await db.delete_debt_deadline(ctx.uid, person=str(r.get("person")), side=str(r.get("side")))
        undo.push(ctx.uid, {"type": "restore_debt_deadline", "person": r.get("person"), "side": r.get("side"), "row": r})
        cleared.append({"person": r.get("person"), "side": r.get("side"), "due_date": str(r.get("due_date") or "")[:10]})
    if rows:
        cache.invalidate(ctx.uid, "debt_deadlines")
        ctx.mutated = True
    if not cleared:
        return {"error": "no deadline for that person"}
    return {"cleared": cleared}


@tool("list_debt_deadlines", "Сроки возврата по займам: кто, сколько и до какого числа, просрочки.")
async def _list_deadlines(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    today = ctx.profile.today
    rows = await services.debt_due_rows(ctx.uid)
    return {"deadlines": [{**r, "amount": round(r["amount"], 2), "days_left": (date.fromisoformat(r["due_date"]) - today).days} for r in rows]}


# ------------------------------------------------------------------ для системного промпта
def snapshot_lines(book: dict[str, dict[str, fin.Counterparty]], today: Any) -> list[str]:
    """«Я должен: UZUM BANK 3 255 000 = [19] 2 205 000 до 01.10 + [65] 1 050 000 без срока; …»"""
    m = fin.fmt_money

    def side(name: str) -> str:
        people = sorted((cp for cp in book[name].values() if abs(cp.total) >= 1), key=lambda c: -abs(c.total))
        parts = []
        for cp in people[:12]:
            who = cp.name or "без имени"
            if cp.total < 0:
                parts.append(f"{who} переплата {m(-cp.total)}")
                continue
            loans = cp.open_tranches()
            detail = " + ".join(
                f"[{t.id}] {m(t.left)} " + (f"до {t.due:%d.%m}" + (f" (просрочен {(today - t.due).days} дн.)" if t.due < today else "") if t.due else "без срока")
                for t in loans[:5])
            parts.append(f"{who} {m(cp.total)}" + (f" = {detail}" if detail else ""))
        return "; ".join(parts) or "—"

    if not any(abs(cp.total) >= 1 for s in ("lent", "debt") for cp in book[s].values()):
        return []
    return [f"Долги по займам ([loan_id] остаток, срок) — Я должен: {side('debt')}. Мне должны: {side('lent')}."]
