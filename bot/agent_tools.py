"""Инструменты «Nurai»: всё, что агент может смотреть и менять.

Каждый инструмент — декларация для Gemini (function calling) + async-обработчик.
Обработчики работают через `services`/`db`, любое изменение кладёт шаг отката в
`undo` (см. bot/undo.py), поэтому весь ход агента можно откатить одной кнопкой.

Результаты инструментов — компактные JSON-словари: списки обрезаются (`limit`),
даты — локальные (часовой пояс пользователя), суммы — числа.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from . import analysis
from . import cache
from . import categories as cats
from . import finance as fin
from . import nutrition as nutri
from . import reminders as rem
from . import services
from . import undo
from .context import db
from .profile import Profile

logger = logging.getLogger(__name__)

MAX_LIST = 60
DEFAULT_LIST = 30


# ------------------------------------------------------------------ context
@dataclass
class ToolContext:
    profile: Profile
    text: str
    handoff: tuple[str, str] | None = None  # (finance|food|vacancy, текст) — передать специализированному парсеру
    open_screen: str | None = None  # экран, который надо показать после ответа
    ask: dict[str, Any] | None = None  # уточняющий вопрос с вариантами-кнопками (ask_user)
    mutated: bool = False
    calls: list[str] = field(default_factory=list)  # имена вызванных инструментов (для логов)

    @property
    def uid(self) -> int:
        return self.profile.telegram_id


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    handler: Handler

    def declaration(self) -> dict[str, Any]:
        params: dict[str, Any] = {"type": "OBJECT", "properties": self.properties}
        if self.required:
            params["required"] = list(self.required)
        return {"name": self.name, "description": self.description, "parameters": params}


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        TOOLS[name] = Tool(name, description, properties or {}, required, fn)
        return fn

    return deco


def declarations() -> list[dict[str, Any]]:
    return [t.declaration() for t in TOOLS.values()]


async def run(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Выполнить инструмент; ошибки возвращаются модели текстом, а не падают."""
    t = TOOLS.get(name)
    if t is None:
        return {"error": f"unknown tool {name}"}
    ctx.calls.append(name)
    try:
        return await t.handler(ctx, args or {})
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


# ------------------------------------------------------------------ schema helpers
def P(type_: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "description": description, **extra}


def ARR(items: dict[str, Any], description: str) -> dict[str, Any]:
    return {"type": "ARRAY", "description": description, "items": items}


ID = P("STRING", "id записи (из результата list_* или из контекста)")
IDS = ARR({"type": "STRING"}, "список id")
DATE = P("STRING", "дата YYYY-MM-DD, либо today / yesterday")


# ------------------------------------------------------------------ value helpers
def _str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in {"null", "none"} else None


def _num(v: Any) -> float | None:
    s = _str(v)
    if s is None:
        return None
    parsed = fin.parse_amount(s)
    if parsed:
        return parsed[0]
    try:
        return float(s.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(round(n)) if n is not None else None


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = _str(v)
    if s is None:
        return None
    return s.lower() in {"true", "1", "yes", "да", "on", "ha"}


def _limit(v: Any, default: int = DEFAULT_LIST) -> int:
    n = _int(v)
    return max(1, min(MAX_LIST, n)) if n else default


def parse_day(value: Any, today: date) -> date | None:
    s = (_str(value) or "").lower()
    if not s:
        return None
    if s in {"today", "сегодня", "bugun"}:
        return today
    if s in {"yesterday", "вчера", "kecha"}:
        return today - timedelta(days=1)
    if s in {"tomorrow", "завтра", "ertaga"}:
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _local_dt(value: Any, profile: Profile) -> datetime | None:
    s = _str(value)
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(profile.tz)


def _created_at_for(day: date | None, profile: Profile) -> str | None:
    """UTC-время для записи еды задним числом (полдень локального дня)."""
    if day is None or day == profile.today:
        return None
    return datetime.combine(day, time(12, 0), tzinfo=profile.tz).astimezone(timezone.utc).isoformat()


# ------------------------------------------------------------------ views
def entry_view(row: dict[str, Any]) -> dict[str, Any]:
    transfer = fin.transfer_from_note(row.get("note"))
    out: dict[str, Any] = {
        "id": str(row.get("id")),
        "date": str(row.get("entry_date") or "")[:10],
        "amount": float(row.get("amount") or 0),
        "note": fin.clean_note(row.get("note")),
    }
    if transfer:
        out["kind"] = "transfer"
        out["from"], out["to"] = transfer
        if "lent" in transfer:
            out["debt"] = "lent"
        elif "debt" in transfer:
            out["debt"] = "debt"
    else:
        out["kind"] = "income" if row.get("entry_type") == "income" else "expense"
        out["category"] = fin.entry_category_key(row)
        out["bucket"] = fin.bucket_from_note(row.get("note"))
    return out


_TRANSLIT = str.maketrans({"ё": "е", "ъ": "", "ь": "", "ў": "у", "қ": "к", "ғ": "г", "ҳ": "х", "'": "", "ʼ": "", "’": ""})


def _norm(text: str) -> str:
    return str(text or "").casefold().translate(_TRANSLIT).strip()


def fuzzy_contains(fragment: str, text: str) -> bool:
    """«Асельбек» найдёт «Асилбек», «uzum» — «UZUM BANK»: подстрока, общий префикс или близость слов."""
    frag, body = _norm(fragment), _norm(text)
    if not frag or not body:
        return False
    if frag in body:
        return True
    frag_words = frag.split()
    body_words = body.split()
    for fw in frag_words:
        if len(fw) < 3:
            continue
        for bw in body_words:
            if len(bw) >= 4 and len(fw) >= 4 and fw[:4] == bw[:4]:
                return True
            if difflib.SequenceMatcher(None, fw, bw).ratio() >= 0.75:
                return True
    return False


def entry_kind(row: dict[str, Any]) -> str:
    return "transfer" if fin.transfer_from_note(row.get("note")) else ("income" if row.get("entry_type") == "income" else "expense")


def filter_entries(
    entries: list[dict[str, Any]],
    *,
    today: date,
    date_from: Any = None,
    date_to: Any = None,
    kind: Any = None,
    category: Any = None,
    amount: Any = None,
    min_amount: Any = None,
    max_amount: Any = None,
    note_contains: Any = None,
    unnamed: Any = None,
) -> list[dict[str, Any]]:
    """Отбор операций под фильтр агента (новые сверху). Чистая функция — тестируется отдельно."""
    start = parse_day(date_from, today)
    end = parse_day(date_to, today) or (start if start and not _str(date_to) else None)
    k = (_str(kind) or "any").lower()
    cat_raw = _str(category)
    cat = cats.normalize(cat_raw, "income" if k == "income" else "expense") if cat_raw else None
    if cat_raw and cat in {"other", "other_in"} and cat_raw.lower() not in {"other", "other_in", "прочее"}:
        cat = cat_raw.lower()  # неизвестная категория — ищем как есть
    exact = _num(amount)
    lo, hi = _num(min_amount), _num(max_amount)
    frag = _str(note_contains) or ""
    only_unnamed = bool(_bool(unnamed))

    out = []
    for row in entries:
        day = str(row.get("entry_date") or "")[:10]
        if start and day < start.isoformat():
            continue
        if end and day > end.isoformat():
            continue
        transfer = fin.transfer_from_note(row.get("note"))
        rk = entry_kind(row)
        if k in {"lent", "debt"}:
            if not transfer or k not in transfer:
                continue
        elif k != "any" and rk != k:
            continue
        if cat and not transfer and fin.entry_category_key(row) != cat:
            continue
        val = float(row.get("amount") or 0)
        if exact is not None and abs(val - exact) > 0.5:
            continue
        if lo is not None and val < lo:
            continue
        if hi is not None and val > hi:
            continue
        clean = fin.clean_note(row.get("note")) or ""
        if frag and not fuzzy_contains(frag, clean):
            continue
        if only_unnamed and clean:
            continue
        out.append(row)
    return out


def calorie_view(row: dict[str, Any], profile: Profile) -> dict[str, Any]:
    dt = _local_dt(row.get("created_at"), profile)
    return {
        "id": str(row.get("id")),
        "datetime": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
        "meal": row.get("meal_desc"),
        "calories": int(float(row.get("calories") or 0)),
        "protein": row.get("protein"), "fat": row.get("fat"), "carbs": row.get("carbs"),
    }


def recurring_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r.get("id")), "title": r.get("title"), "amount": float(r.get("amount") or 0), "category": r.get("category"),
        "bucket": r.get("bucket"), "day_of_month": r.get("day_of_month"), "enabled": bool(r.get("enabled", True)),
    }


def reminder_view(r: dict[str, Any]) -> dict[str, Any]:
    p = rem.payload_of(r)
    return {
        "id": str(r.get("id")), "text": p.get("text"), "time": str(r.get("reminder_time") or "")[:5],
        "days": r.get("days_of_week"), "once": bool(p.get("once")), "date": p.get("date"),
        "links": p.get("links") or [], "enabled": bool(r.get("enabled", True)),
    }


# ------------------------------------------------------------------ finance: read
@tool(
    "list_finance_entries",
    "Найти операции (расходы/доходы/переводы/долги) по фильтру. Возвращает записи с id — используй их для удаления/правки. Без фильтров — последние операции.",
    {
        "date_from": DATE, "date_to": DATE,
        "kind": P("STRING", "expense | income | transfer | lent (дал в долг / мне вернули) | debt (взял в долг / я вернул) | any", enum=["expense", "income", "transfer", "lent", "debt", "any"]),
        "category": P("STRING", "ключ категории (food, transport, …)"),
        "amount": P("NUMBER", "точная сумма"), "min_amount": P("NUMBER", "сумма от"), "max_amount": P("NUMBER", "сумма до"),
        "note_contains": P("STRING", "фрагмент комментария (имя человека, слово)"),
        "unnamed": P("BOOLEAN", "только без комментария/имени"),
        "limit": P("INTEGER", f"сколько вернуть (по умолчанию {DEFAULT_LIST}, макс. {MAX_LIST})"),
    },
)
async def _list_entries(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    entries = await services.finance_entries(ctx.uid)
    found = filter_entries(entries, today=ctx.profile.today, **{k: a.get(k) for k in ("date_from", "date_to", "kind", "category", "amount", "min_amount", "max_amount", "note_contains", "unnamed")})
    lim = _limit(a.get("limit"))
    out: dict[str, Any] = {"total": len(found), "entries": [entry_view(r) for r in found[:lim]], "sum": round(sum(float(r.get("amount") or 0) for r in found), 2)}
    if not found and _str(a.get("note_contains")):
        names = sorted({fin.clean_note(r.get("note")) for r in entries if fin.clean_note(r.get("note"))}, key=str.casefold)
        out["hint"] = "ничего не найдено по комментарию; известные комментарии/имена: " + ", ".join(names[:40])
    return out


def _period(a: dict[str, Any], today: date, entries: list[dict[str, Any]]) -> fin.Period:
    code = (_str(a.get("period")) or "").lower()
    start, end = parse_day(a.get("date_from"), today), parse_day(a.get("date_to"), today)
    if start or end:
        start = start or (end or today)
        end = end or today
        span = (end - start).days + 1
        return fin.Period("custom", start, end, start - timedelta(days=span), start - timedelta(days=1))
    if code == "all":
        first = min((fin._entry_date(r) for r in entries if fin._entry_date(r)), default=today)
        return fin.Period("all", first, today, first, first)
    return fin.period_for(code or "month", today)


@tool(
    "get_finance_stats",
    "Балансы счетов и статистика за период: расход, доход, по категориям, число операций, лимиты. Для вопросов «сколько потратил…».",
    {
        "period": P("STRING", "day | week | month | prev_month | year | all", enum=["day", "week", "month", "prev_month", "year", "all"]),
        "date_from": DATE, "date_to": DATE,
    },
)
async def _stats(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    snap = await services.finance_snapshot(ctx.profile)
    period = _period(a, ctx.profile.today, snap.entries)
    st = fin.compute_stats(snap.entries, period)
    limits = await services.budgets(ctx.uid)
    statuses = fin.budget_statuses(st, limits) if limits and period.code == "month" else []
    return {
        "period": {"code": period.code, "from": period.start.isoformat(), "to": period.end.isoformat(), "days": period.days},
        "balances": {k: round(v, 2) for k, v in snap.balances.items()},
        "expense": round(st.expense, 2), "income": round(st.income, 2), "net": round(st.net, 2), "ops": st.ops,
        "prev_expense": round(st.prev_expense, 2), "prev_income": round(st.prev_income, 2),
        "avg_expense_per_day": round(st.avg_per_day, 2),
        "expense_by_category": [{"category": k, "amount": round(v, 2), "count": c} for k, v, c in st.by_category],
        "income_by_category": [{"category": k, "amount": round(v, 2), "count": c} for k, v, c in st.income_by_category],
        "top_day": {"date": st.top_day[0].isoformat(), "amount": round(st.top_day[1], 2)} if st.top_day else None,
        "budgets": [{"category": b.category, "limit": b.limit, "spent": round(b.spent, 2), "ratio": round(b.ratio, 2)} for b in statuses],
    }


@tool("list_debts", "Долги по людям: кто должен мне (lent) и кому должен я (debt). Пустое имя = «без имени».")
async def _debts(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    snap = await services.finance_snapshot(ctx.profile)
    ledger = fin.debt_ledger(snap.entries, snap.settings)
    debt_rows = [r for r in snap.entries if (tr := fin.transfer_from_note(r.get("note"))) and ("lent" in tr or "debt" in tr)]
    return {
        "lent": [{"name": n or "", "amount": round(v, 2)} for n, v in ledger["lent"]],
        "debt": [{"name": n or "", "amount": round(v, 2)} for n, v in ledger["debt"]],
        "totals": {"lent": round(snap.balances["lent"], 2), "debt": round(snap.balances["debt"], 2)},
        "entries": [entry_view(r) for r in debt_rows[:MAX_LIST]],
    }


# ------------------------------------------------------------------ finance: write
_ITEM = {
    "type": "OBJECT",
    "properties": {
        "kind": P("STRING", "expense | income | transfer", enum=["expense", "income", "transfer"]),
        "amount": P("NUMBER", "сумма"),
        "category": P("STRING", "ключ категории"),
        "note": P("STRING", "комментарий (имя человека для долгов)"),
        "bucket": P("STRING", "card | cash — откуда/куда деньги", enum=["card", "cash"]),
        "from_bucket": P("STRING", "для transfer: card | cash | lent | debt | init", enum=["card", "cash", "lent", "debt", "init"]),
        "to_bucket": P("STRING", "для transfer: card | cash | lent | debt", enum=["card", "cash", "lent", "debt"]),
        "date": DATE,
    },
    "required": ["kind", "amount"],
}


@tool(
    "add_finance_entries",
    "Записать операции напрямую (когда указана дата в прошлом или несколько операций сразу). Переводы: снял с карты = card→cash; дал в долг = card/cash→lent; мне вернули = lent→card/cash; взял в долг = debt→card/cash; вернул долг = card/cash→debt; старый долг без движения денег = init→lent/debt.",
    {"items": ARR(_ITEM, "операции")},
    ("items",),
)
async def _add_entries(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    today = ctx.profile.today
    payload: list[dict[str, Any]] = []
    for it in a.get("items") or []:
        if not isinstance(it, dict):
            continue
        amount = _num(it.get("amount"))
        if not amount or amount <= 0:
            continue
        kind = (_str(it.get("kind")) or "expense").lower()
        day = parse_day(it.get("date"), today) or today
        note = _str(it.get("note"))
        if kind == "transfer":
            src, dst = fin.normalize_bucket(it.get("from_bucket")), fin.normalize_bucket(it.get("to_bucket"))
            if src == dst:
                continue
            payload.append({"entry_type": "expense", "amount": amount, "category": cats.TRANSFER_KEY, "note": fin.note_with_transfer(note, src, dst), "entry_date": day.isoformat()})
        else:
            kind = "income" if kind == "income" else "expense"
            category = cats.normalize(_str(it.get("category")), kind, note=note)
            payload.append({"entry_type": kind, "amount": amount, "category": category, "note": fin.note_with_bucket(note, _str(it.get("bucket")) or "card"), "entry_date": day.isoformat()})
    if not payload:
        return {"error": "nothing to add"}
    inserted = await db.add_finance_entries(ctx.uid, payload, entry_date=today, source="agent")
    cache.invalidate(ctx.uid, "fin_entries")
    ids = [r.get("id") for r in inserted if r.get("id") is not None]
    undo.push(ctx.uid, {"type": "delete_entries", "ids": ids})
    ctx.mutated = True
    return {"added": [entry_view(r) for r in inserted]}


@tool(
    "update_finance_entry",
    "Исправить операцию: сумму, категорию, комментарий, дату, счёт.",
    {"id": ID, "amount": P("NUMBER", "новая сумма"), "category": P("STRING", "новый ключ категории"), "note": P("STRING", "новый комментарий"), "date": DATE, "bucket": P("STRING", "card | cash", enum=["card", "cash"])},
    ("id",),
)
async def _update_entry(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    row = await db.get_finance_entry(ctx.uid, _str(a.get("id")) or "")
    if not row:
        return {"error": "entry not found"}
    fields: dict[str, Any] = {}
    amount = _num(a.get("amount"))
    if amount is not None and amount > 0:
        fields["amount"] = amount
    transfer = fin.transfer_from_note(row.get("note"))
    if _str(a.get("category")) and not transfer:
        kind = "income" if row.get("entry_type") == "income" else "expense"
        fields["category"] = cats.normalize(_str(a.get("category")), kind)
    note_new = a.get("note")
    bucket_new = _str(a.get("bucket"))
    if note_new is not None or bucket_new:
        note = _str(note_new) if note_new is not None else fin.clean_note(row.get("note"))
        if transfer:
            fields["note"] = fin.note_with_transfer(note, *transfer)
        else:
            fields["note"] = fin.note_with_bucket(note, bucket_new or fin.bucket_from_note(row.get("note")))
    day = parse_day(a.get("date"), ctx.profile.today)
    if day:
        fields["entry_date"] = day.isoformat()
    if not fields:
        return {"error": "nothing to change"}
    before = {k: row.get(k) for k in fields}
    await db.update_finance_entry(ctx.uid, row["id"], fields)
    cache.invalidate(ctx.uid, "fin_entries")
    undo.push(ctx.uid, {"type": "restore_fields", "entry_id": row["id"], "fields": before})
    ctx.mutated = True
    return {"before": entry_view(row), "after": entry_view({**row, **fields})}


MASS_DELETE = 10  # больше — сначала переспросить (голосом легко ошибиться: «удали всё за месяц»)


@tool("delete_finance_entries", "Удалить операции по id (можно несколько). Больше 10 сразу — вернёт confirm_needed: переспроси и повтори с confirm=true.",
      {"ids": IDS, "confirm": P("BOOLEAN", "он подтвердил массовое удаление")}, ("ids",))
async def _delete_entries(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    entries = await services.finance_entries(ctx.uid)
    rows = [r for r in entries if str(r.get("id")) in ids]
    if not rows:
        return {"error": "no matching entries"}
    if len(rows) > MASS_DELETE and not _bool(a.get("confirm")):
        total = sum(float(r.get("amount") or 0) for r in rows)
        return {"confirm_needed": len(rows), "total_amount": total, "preview": [entry_view(r) for r in rows[:5]],
                "hint": f"спроси: «Удалить {len(rows)} операций на {fin.fmt_money(total)}?» — «да» → повтори с confirm=true"}
    await db.delete_finance_entries(ctx.uid, [r["id"] for r in rows])
    cache.invalidate(ctx.uid, "fin_entries")
    undo.push(ctx.uid, {"type": "restore_entries", "rows": rows})
    ctx.mutated = True
    return {"deleted": [entry_view(r) for r in rows]}


@tool(
    "set_account_balance",
    "Задать фактический остаток счёта (карта/наличные) или общую сумму долгов — база подстраивается так, чтобы баланс стал равен указанному.",
    {"bucket": P("STRING", "card | cash | lent | debt", enum=["card", "cash", "lent", "debt"]), "amount": P("NUMBER", "фактический остаток")},
    ("bucket", "amount"),
)
async def _set_balance(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    bucket = fin.normalize_bucket(a.get("bucket"))
    amount = _num(a.get("amount"))
    if amount is None:
        return {"error": "amount required"}
    snap = await services.finance_snapshot(ctx.profile)
    live = fin.compute_balances(snap.entries)
    settings_ = dict(snap.settings)
    before = dict(settings_)
    settings_[f"{bucket}_base"] = amount - live[bucket]
    await services.save_finance_settings(ctx.uid, settings_)
    undo.push(ctx.uid, {"type": "restore_settings", "settings": before})
    ctx.mutated = True
    return {"bucket": bucket, "balance": amount, "was": round(snap.balances[bucket], 2)}


@tool(
    "clear_unnamed_debt",
    "Убрать из долгов сумму «без имени» (стартовая база + операции без комментария).",
    {"side": P("STRING", "lent — мне должны; debt — я должен", enum=["lent", "debt"])},
    ("side",),
)
async def _clear_unnamed(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    side = "debt" if (_str(a.get("side")) or "lent") == "debt" else "lent"
    settings_ = dict(await services.finance_settings(ctx.uid))
    entries = await services.finance_entries(ctx.uid)
    unnamed = [r for r in entries if (tr := fin.transfer_from_note(r.get("note"))) and side in tr and not fin.clean_note(r.get("note"))]
    before_base = float(settings_.get(f"{side}_base") or 0)
    settings_[f"{side}_base"] = 0.0
    await services.save_finance_settings(ctx.uid, settings_)
    if unnamed:
        await db.delete_finance_entries(ctx.uid, [r["id"] for r in unnamed])
        cache.invalidate(ctx.uid, "fin_entries")
    undo.push(ctx.uid, {"type": "restore_debt", "side": side, "base": before_base, "rows": unnamed})
    ctx.mutated = True
    return {"side": side, "removed_base": before_base, "removed_entries": len(unnamed)}


# ------------------------------------------------------------------ budgets
@tool("list_budgets", "Месячные лимиты по категориям и сколько уже потрачено в этом месяце.")
async def _list_budgets(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    statuses = await services.month_budget_statuses(ctx.profile)
    limits = await services.budgets(ctx.uid)
    by = {b.category: b for b in statuses}
    return {"budgets": [{"category": k, "limit": v, "spent": round(by[k].spent, 2) if k in by else 0, "ratio": round(by[k].ratio, 2) if k in by else 0} for k, v in limits.items()]}


@tool(
    "set_budget",
    "Установить месячный лимит на категорию расходов. amount=0 — убрать лимит. category='all' с amount=0 — убрать все.",
    {"category": P("STRING", "ключ категории или all"), "amount": P("NUMBER", "лимит на месяц, 0 = убрать")},
    ("category", "amount"),
)
async def _set_budget(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("budgets"):
        return {"error": "budgets table missing (migration 004)"}
    raw = _str(a.get("category")) or ""
    amount = _num(a.get("amount")) or 0.0
    limits = await services.budgets(ctx.uid)
    if raw.lower() == "all":
        if amount > 0:
            return {"error": "use a concrete category for a limit"}
        for k in list(limits):
            await services.set_budget(ctx.uid, k, 0)
        undo.push(ctx.uid, {"type": "restore_budgets", "limits": limits})
        ctx.mutated = True
        return {"removed": list(limits)}
    key = cats.normalize(raw, "expense")
    if key == "other" and raw.lower() not in {"other", "прочее", "boshqa"}:
        return {"error": f"unknown category '{raw}'", "categories": [c.key for c in cats.EXPENSE]}
    prev = limits.get(key, 0.0)
    await services.set_budget(ctx.uid, key, amount)
    undo.push(ctx.uid, {"type": "restore_budgets", "limits": {key: prev}})
    ctx.mutated = True
    return {"category": key, "limit": amount, "was": prev}


# ------------------------------------------------------------------ recurring
@tool("list_recurring", "Регулярные (ежемесячные) платежи: id, название, сумма, день месяца, включён ли.")
async def _list_recurring(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    items = await services.recurring(ctx.uid)
    return {"recurring": [recurring_view(r) for r in items]}


@tool(
    "add_recurring",
    "Добавить регулярный ежемесячный платёж (интернет, аренда, кредит…).",
    {"title": P("STRING", "название"), "amount": P("NUMBER", "сумма"), "day_of_month": P("INTEGER", "день месяца 1–31"), "category": P("STRING", "ключ категории (по умолчанию home)"), "bucket": P("STRING", "card | cash", enum=["card", "cash"])},
    ("title", "amount", "day_of_month"),
)
async def _add_recurring(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    if not await db.ensure_available("recurring_payments"):
        return {"error": "recurring table missing (migration 004)"}
    title, amount, day = _str(a.get("title")), _num(a.get("amount")), _int(a.get("day_of_month"))
    if not title or not amount or amount <= 0 or not day:
        return {"error": "title, amount, day_of_month required"}
    category = cats.normalize(_str(a.get("category")) or "home", "expense", note=title)
    row = await db.add_recurring(ctx.uid, title=title, amount=amount, category=category, bucket=fin.normalize_bucket(_str(a.get("bucket")) or "card"), day_of_month=max(1, min(31, day)))
    services.invalidate_recurring(ctx.uid)
    undo.push(ctx.uid, {"type": "delete_recurring", "ids": [row.get("id")]})
    ctx.mutated = True
    return {"added": recurring_view(row)}


@tool(
    "update_recurring",
    "Изменить регулярный платёж: название, сумму, день, категорию, счёт; enabled=false — поставить на паузу, true — включить.",
    {"id": ID, "title": P("STRING", "название"), "amount": P("NUMBER", "сумма"), "day_of_month": P("INTEGER", "день 1–31"), "category": P("STRING", "ключ категории"), "bucket": P("STRING", "card | cash", enum=["card", "cash"]), "enabled": P("BOOLEAN", "включён")},
    ("id",),
)
async def _update_recurring(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    row = await db.get_recurring(ctx.uid, _str(a.get("id")) or "")
    if not row:
        return {"error": "recurring not found"}
    fields: dict[str, Any] = {}
    if _str(a.get("title")):
        fields["title"] = _str(a.get("title"))
    if (amt := _num(a.get("amount"))) and amt > 0:
        fields["amount"] = amt
    if (day := _int(a.get("day_of_month"))):
        fields["day_of_month"] = max(1, min(31, day))
    if _str(a.get("category")):
        fields["category"] = cats.normalize(_str(a.get("category")), "expense")
    if _str(a.get("bucket")):
        fields["bucket"] = fin.normalize_bucket(_str(a.get("bucket")))
    if (en := _bool(a.get("enabled"))) is not None:
        fields["enabled"] = en
    if not fields:
        return {"error": "nothing to change"}
    before = {k: row.get(k) for k in fields}
    await db.update_recurring(ctx.uid, row["id"], fields)
    services.invalidate_recurring(ctx.uid)
    undo.push(ctx.uid, {"type": "recurring_fields", "rec_id": row["id"], "fields": before})
    ctx.mutated = True
    return {"before": recurring_view(row), "after": recurring_view({**row, **fields})}


@tool("delete_recurring", "Удалить регулярные платежи по id.", {"ids": IDS}, ("ids",))
async def _delete_recurring(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    items = [r for r in await services.recurring(ctx.uid) if str(r.get("id")) in ids]
    if not items:
        return {"error": "no matching recurring payments"}
    for r in items:
        await db.delete_recurring(ctx.uid, r["id"])
    services.invalidate_recurring(ctx.uid)
    undo.push(ctx.uid, {"type": "restore_recurring", "rows": items})
    ctx.mutated = True
    return {"deleted": [recurring_view(r) for r in items]}


# ------------------------------------------------------------------ reminders
@tool("list_reminders", "Напоминания пользователя: id, текст, время, дни, ссылки.")
async def _list_reminders(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    rows = await services.reminders(ctx.uid)
    return {"reminders": [reminder_view(r) for r in rows]}


_DAYS = P("STRING", "daily | weekdays | weekend | once (однократно, с date) | список дней недели через запятую 1..7 (1=Пн)")


def _days_arg(value: Any) -> tuple[list[int], bool]:
    s = _str(value) or "daily"
    if isinstance(value, list):
        return rem.days_from(value), False
    if "," in s or s.isdigit():
        return rem.days_from([x.strip() for x in s.split(",")]), False
    return rem.days_from(s), s.lower() == "once"


def _time_arg(value: Any) -> str | None:
    s = (_str(value) or "").replace(".", ":").replace(" ", "")
    if ":" not in s:
        return f"{int(s):02d}:00" if s.isdigit() and 0 <= int(s) <= 23 else None
    hh, _, mm = s.partition(":")
    if hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
        return f"{int(hh):02d}:{int(mm):02d}"
    return None


@tool(
    "add_reminder",
    "Создать напоминание: текст в указанное время, каждый день / по будням / однократно в дату. Можно приложить ссылки (видео-уроки) — будут присылаться по одной в день по кругу. «Через 2 часа» — посчитай время от текущего.",
    {"text": P("STRING", "текст напоминания (без ссылок)"), "time": P("STRING", "HH:MM"), "days": _DAYS, "date": P("STRING", "YYYY-MM-DD для однократного"), "links": ARR({"type": "STRING"}, "ссылки")},
    ("text", "time"),
)
async def _add_reminder(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    text = rem.URL_RE.sub("", _str(a.get("text")) or "").strip(" :—-")
    hhmm = _time_arg(a.get("time"))
    if not hhmm:
        return {"error": "time must be HH:MM"}
    links = [str(x) for x in (a.get("links") or []) if _str(x)] or rem.URL_RE.findall(ctx.text)
    days, once = _days_arg(a.get("days"))
    day = parse_day(a.get("date"), ctx.profile.today)
    if day and not once:
        once = True
    payload = {"text": text or ("Eslatma" if ctx.profile.lang == "uz" else "Напоминание"), "links": links, "idx": 0, "once": once, "date": day.isoformat() if day else None}
    row = await db.add_reminder(ctx.uid, text=rem.encode(payload), reminder_time=hhmm, days_of_week=days, tz_name=ctx.profile.tz_name)
    services.invalidate_reminders(ctx.uid)
    undo.push(ctx.uid, {"type": "delete_reminders", "ids": [row.get("id")]})
    ctx.mutated = True
    return {"added": reminder_view({**row, "reminder_text": rem.encode(payload), "reminder_time": hhmm, "days_of_week": days})}


@tool(
    "update_reminder",
    "Изменить напоминание: текст, время, дни, ссылки; enabled=false — выключить.",
    {"id": ID, "text": P("STRING", "текст"), "time": P("STRING", "HH:MM"), "days": _DAYS, "links": ARR({"type": "STRING"}, "ссылки"), "enabled": P("BOOLEAN", "включено")},
    ("id",),
)
async def _update_reminder(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    rows = await services.reminders(ctx.uid)
    row = next((r for r in rows if str(r.get("id")) == _str(a.get("id"))), None)
    if not row:
        return {"error": "reminder not found"}
    payload = rem.payload_of(row)
    fields: dict[str, Any] = {}
    changed_payload = False
    if _str(a.get("text")):
        payload["text"] = rem.URL_RE.sub("", _str(a.get("text")) or "").strip(" :—-")
        changed_payload = True
    if a.get("links") is not None:
        payload["links"] = [str(x) for x in (a.get("links") or []) if _str(x)]
        payload["idx"] = 0
        changed_payload = True
    if _str(a.get("time")):
        hhmm = _time_arg(a.get("time"))
        if not hhmm:
            return {"error": "time must be HH:MM"}
        fields["reminder_time"] = hhmm
    if _str(a.get("days")) or isinstance(a.get("days"), list):
        days, once = _days_arg(a.get("days"))
        fields["days_of_week"] = days
        payload["once"] = once
        changed_payload = True
    if (en := _bool(a.get("enabled"))) is not None:
        fields["enabled"] = en
    if changed_payload:
        fields["reminder_text"] = rem.encode(payload)
    if not fields:
        return {"error": "nothing to change"}
    before = {k: row.get(k) for k in fields}
    await db.update_reminder(ctx.uid, row["id"], fields)
    services.invalidate_reminders(ctx.uid)
    undo.push(ctx.uid, {"type": "reminder_fields", "reminder_id": row["id"], "fields": before})
    ctx.mutated = True
    return {"before": reminder_view(row), "after": reminder_view({**row, **fields})}


@tool("delete_reminders", "Удалить напоминания по id.", {"ids": IDS}, ("ids",))
async def _delete_reminders(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    rows = [r for r in await services.reminders(ctx.uid) if str(r.get("id")) in ids]
    if not rows:
        return {"error": "no matching reminders"}
    for r in rows:
        await db.delete_reminder(ctx.uid, r["id"])
    services.invalidate_reminders(ctx.uid)
    undo.push(ctx.uid, {"type": "restore_reminders", "rows": rows})
    ctx.mutated = True
    return {"deleted": [reminder_view(r) for r in rows]}


# ------------------------------------------------------------------ nutrition
async def _logs_between(ctx: ToolContext, start: date, end: date) -> list[dict[str, Any]]:
    days = max(1, (ctx.profile.today - start).days + 1)
    logs = await services.calorie_logs(ctx.profile, min(days, 366))
    out = []
    for r in logs:
        dt = _local_dt(r.get("created_at"), ctx.profile)
        if dt and start <= dt.date() <= end:
            out.append(r)
    return out


@tool(
    "list_calorie_logs",
    "Записи дневника питания за день/период с id (для удаления/правки). По умолчанию — сегодня.",
    {"date_from": DATE, "date_to": DATE, "limit": P("INTEGER", f"макс. записей (по умолчанию {DEFAULT_LIST})")},
)
async def _list_logs(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    today = ctx.profile.today
    start = parse_day(a.get("date_from"), today) or today
    end = parse_day(a.get("date_to"), today) or (start if not _str(a.get("date_to")) else today)
    rows = await _logs_between(ctx, start, end)
    tot = nutri.totals(rows)
    lim = _limit(a.get("limit"))
    return {"from": start.isoformat(), "to": end.isoformat(), "total": len(rows), "logs": [calorie_view(r, ctx.profile) for r in rows[:lim]],
            "sum": {k: int(v) for k, v in tot.items()}}


@tool(
    "get_nutrition_summary",
    "План КБЖУ (цель на день), съедено сегодня, остаток, калории по дням за период.",
    {"days": P("INTEGER", "сколько дней истории (по умолчанию 7)")},
)
async def _nutri_summary(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    days = max(1, min(90, _int(a.get("days")) or 7))
    plan, today_logs, logs = await services.nutrition_profile(ctx.uid), await services.today_calorie_logs(ctx.profile), await services.calorie_logs(ctx.profile, days)
    tot = nutri.totals(today_logs)
    by_day: dict[str, float] = {}
    for r in logs:
        dt = _local_dt(r.get("created_at"), ctx.profile)
        if dt:
            by_day[dt.date().isoformat()] = by_day.get(dt.date().isoformat(), 0.0) + float(r.get("calories") or 0)
    out: dict[str, Any] = {"today": {k: int(v) for k, v in tot.items()}, "today_meals": [str(r.get("meal_desc") or "") for r in today_logs[:15]],
                           "calories_by_day": {k: int(v) for k, v in sorted(by_day.items())}}
    if plan and plan.get("daily_calories"):
        out["plan"] = {k: plan.get(k) for k in ("title", "daily_calories", "protein", "fat", "carbs", "weight", "height", "age")}
        out["left_today"] = {"calories": int(float(plan.get("daily_calories") or 0) - tot["calories"]), "protein": int(float(plan.get("protein") or 0) - tot["protein"])}
    else:
        out["plan"] = None
    return out


_MEAL = {
    "type": "OBJECT",
    "properties": {"meal": P("STRING", "что съел"), "calories": P("INTEGER", "ккал"), "protein": P("NUMBER", "белки, г"), "fat": P("NUMBER", "жиры, г"), "carbs": P("NUMBER", "углеводы, г"), "date": DATE},
    "required": ["meal", "calories"],
}


@tool(
    "add_calorie_logs",
    "Записать еду в дневник напрямую с уже известными ккал/БЖУ (например задним числом: «вчера ещё съел…»). Для обычного «съел плов» используй hand_off(food).",
    {"items": ARR(_MEAL, "приёмы пищи")},
    ("items",),
)
async def _add_logs(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    items = []
    for it in a.get("items") or []:
        if not isinstance(it, dict) or not _str(it.get("meal")):
            continue
        day = parse_day(it.get("date"), ctx.profile.today)
        items.append({
            "meal_desc": _str(it.get("meal")), "calories": _int(it.get("calories")) or 0, "protein": _num(it.get("protein")), "fat": _num(it.get("fat")), "carbs": _num(it.get("carbs")),
            "confidence": 0.9, "created_at": _created_at_for(day, ctx.profile),
        })
    if not items:
        return {"error": "nothing to add"}
    inserted = await services.add_calorie_logs(ctx.uid, items)
    ids = [r.get("id") for r in inserted if r.get("id") is not None]
    undo.push(ctx.uid, {"type": "delete_calorie_logs", "ids": ids})
    ctx.mutated = True
    return {"added": [calorie_view(r, ctx.profile) for r in inserted]}


@tool(
    "update_calorie_log",
    "Исправить запись еды: название, ккал, БЖУ.",
    {"id": ID, "meal": P("STRING", "название"), "calories": P("INTEGER", "ккал"), "protein": P("NUMBER", "белки"), "fat": P("NUMBER", "жиры"), "carbs": P("NUMBER", "углеводы")},
    ("id",),
)
async def _update_log(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    row = await db.get_calorie_log(ctx.uid, _str(a.get("id")) or "")
    if not row:
        return {"error": "log not found"}
    fields: dict[str, Any] = {}
    if _str(a.get("meal")):
        fields["meal_desc"] = _str(a.get("meal"))
    if (c := _int(a.get("calories"))) is not None:
        fields["calories"] = c
    for k in ("protein", "fat", "carbs"):
        if (v := _num(a.get(k))) is not None:
            fields[k] = v
    if not fields:
        return {"error": "nothing to change"}
    before = {k: row.get(k) for k in fields}
    await db.update_calorie_log(ctx.uid, row["id"], fields)
    cache.invalidate(ctx.uid, "kcal_today")
    cache.invalidate(ctx.uid, "kcal_days")
    undo.push(ctx.uid, {"type": "restore_calorie_fields", "log_id": row["id"], "fields": before})
    ctx.mutated = True
    return {"before": calorie_view(row, ctx.profile), "after": calorie_view({**row, **fields}, ctx.profile)}


@tool("delete_calorie_logs", "Удалить записи еды по id.", {"ids": IDS}, ("ids",))
async def _delete_logs(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    ids = {str(x) for x in (a.get("ids") or []) if _str(x)}
    rows = []
    for x in ids:
        row = await db.get_calorie_log(ctx.uid, x)
        if row:
            rows.append(row)
    if not rows:
        return {"error": "no matching logs"}
    await db.delete_calorie_logs(ctx.uid, [r["id"] for r in rows])
    cache.invalidate(ctx.uid, "kcal_today")
    cache.invalidate(ctx.uid, "kcal_days")
    undo.push(ctx.uid, {"type": "restore_calorie_logs", "rows": [{k: r.get(k) for k in ("meal_desc", "calories", "protein", "fat", "carbs", "confidence", "advice", "photo_url", "created_at")} for r in rows]})
    ctx.mutated = True
    return {"deleted": [calorie_view(r, ctx.profile) for r in rows]}


@tool(
    "set_nutrition_plan",
    "Задать дневную норму: ккал и БЖУ (и/или название цели). Незаданные поля сохраняются.",
    {"daily_calories": P("INTEGER", "ккал в день"), "protein": P("NUMBER", "белки, г"), "fat": P("NUMBER", "жиры, г"), "carbs": P("NUMBER", "углеводы, г"), "title": P("STRING", "название цели (Похудение, Набор…)")},
)
async def _set_plan(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    current = await services.nutrition_profile(ctx.uid)
    new = dict(current or {})
    if (c := _int(a.get("daily_calories"))):
        new["daily_calories"] = c
    for k in ("protein", "fat", "carbs"):
        if (v := _num(a.get(k))) is not None:
            new[k] = v
    if _str(a.get("title")):
        new["title"] = _str(a.get("title"))
    if not new.get("daily_calories"):
        return {"error": "daily_calories required"}
    new.setdefault("mode", "custom")
    await services.save_nutrition_profile(ctx.uid, new)
    undo.push(ctx.uid, {"type": "restore_nutrition_profile", "profile": current})
    ctx.mutated = True
    return {"plan": {k: new.get(k) for k in ("title", "daily_calories", "protein", "fat", "carbs")}}


# ------------------------------------------------------------------ settings
@tool("get_settings", "Настройки: утренняя/вечерняя сводка (вкл/время), авто-отчёт (вкл/частота), язык.")
async def _get_settings(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    us = await services.user_settings(ctx.uid) if db.available("user_settings") else {}
    prefs = await db.get_report_preferences(ctx.uid)
    return {
        "brief_morning": us.get("brief_morning"), "brief_morning_time": us.get("brief_morning_time"),
        "brief_evening": us.get("brief_evening"), "brief_evening_time": us.get("brief_evening_time"),
        "report_enabled": prefs.get("enabled"), "report_frequency": prefs.get("frequency"),
        "proactive": us.get("proactive", True), "voice_reply": us.get("voice_reply", True),
        "language": ctx.profile.lang, "timezone": ctx.profile.tz_name, "currency": ctx.profile.currency,
    }


@tool(
    "update_settings",
    "Изменить настройки: сводки (вкл/выкл, время), авто-отчёт (вкл/выкл, weekly|monthly), проактивные подсказки, голосовые ответы, язык (ru|uz).",
    {
        "brief_morning": P("BOOLEAN", "утренняя сводка"), "brief_morning_time": P("STRING", "HH:MM"),
        "brief_evening": P("BOOLEAN", "вечерняя сводка"), "brief_evening_time": P("STRING", "HH:MM"),
        "proactive": P("BOOLEAN", "проактивные подсказки бота (долги, всплески трат, питание, цели)"),
        "voice_reply": P("BOOLEAN", "отвечать голосом на голосовые сообщения"),
        "report_enabled": P("BOOLEAN", "авто-отчёт"), "report_frequency": P("STRING", "weekly | monthly", enum=["weekly", "monthly"]),
        "language": P("STRING", "ru | uz", enum=["ru", "uz"]),
    },
)
async def _update_settings(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    us_fields: dict[str, Any] = {}
    for key in ("brief_morning", "brief_evening", "proactive", "voice_reply"):
        if (v := _bool(a.get(key))) is not None:
            us_fields[key] = v
    for key in ("brief_morning_time", "brief_evening_time"):
        if _str(a.get(key)):
            hhmm = _time_arg(a.get(key))
            if not hhmm:
                return {"error": f"{key} must be HH:MM"}
            us_fields[key] = hhmm
    if us_fields:
        if not await db.ensure_available("user_settings"):
            return {"error": "user_settings table missing (migration 004)"}
        before = await services.user_settings(ctx.uid)
        await services.save_user_settings(ctx.uid, us_fields)
        undo.push(ctx.uid, {"type": "restore_user_settings", "fields": {k: before.get(k) for k in us_fields}})
        changed.update(us_fields)
    rep_en, rep_fr = _bool(a.get("report_enabled")), _str(a.get("report_frequency"))
    if rep_en is not None or rep_fr:
        prefs = await db.get_report_preferences(ctx.uid)
        enabled = prefs["enabled"] if rep_en is None else rep_en
        frequency = (rep_fr or prefs["frequency"]).lower()
        await db.save_report_preferences(ctx.uid, enabled=enabled, frequency=frequency, last_sent_key=prefs.get("last_sent_key"))
        cache.invalidate(ctx.uid, "report_prefs")
        undo.push(ctx.uid, {"type": "restore_report_prefs", "enabled": prefs["enabled"], "frequency": prefs["frequency"]})
        changed.update({"report_enabled": enabled, "report_frequency": frequency})
    lang = (_str(a.get("language")) or "").lower()
    if lang in {"ru", "uz"} and lang != ctx.profile.lang:
        await db.update_user_language(ctx.uid, lang)
        ctx.profile.lang = lang
        cached = cache.get(ctx.uid, ("profile",))
        if cached is not None:
            cached.lang = lang
        changed["language"] = lang
    if not changed:
        return {"error": "nothing to change"}
    ctx.mutated = True
    return {"changed": changed}


# ------------------------------------------------------------------ deep analysis
@tool(
    "deep_analysis",
    "Полный разбор данных: тренды по месяцам (3 мес.), изменения по категориям, прогноз расходов и остатка до конца месяца, "
    "лимиты под угрозой, аномалии (крупные траты, дорогие дни, дубли), «где переплачиваю» (частые мелкие траты, повторы, кафе vs продукты, "
    "скрытые подписки), долги, питание (среднее, будни/выходные, перебор/недобор). Для «проанализируй», «сделай отчёт», «где я переплачиваю», «прогноз».",
    {"focus": P("STRING", "all | finance | nutrition", enum=["all", "finance", "nutrition"])},
)
async def _deep_analysis(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    focus = (_str(a.get("focus")) or "all").lower()
    snap = await services.finance_snapshot(ctx.profile)
    logs, plan, recurring, budgets = (
        await services.calorie_logs(ctx.profile, 30), await services.nutrition_profile(ctx.uid),
        await services.recurring(ctx.uid), await services.budgets(ctx.uid),
    )
    return analysis.full_analysis(
        entries=snap.entries, logs=logs, today=ctx.profile.today, tz=ctx.profile.tz, balances=snap.balances, settings=snap.settings,
        recurring=recurring, budgets=budgets, plan=plan, focus=focus if focus in {"all", "finance", "nutrition"} else "all",
    )


# ------------------------------------------------------------------ hand-off / navigation
@tool(
    "hand_off",
    "Передать сообщение специализированному разбору с подтверждением на экране. finance — пользователь сообщает трату/доход/перевод/долг сегодняшним днём («такси 25000», «дал Алишеру 200к»); "
    "food — сообщает, что съел (посчитать КБЖУ по описанию); vacancy — прислал текст вакансии для оформления поста. После вызова ничего не отвечай.",
    {"module": P("STRING", "finance | food | vacancy", enum=["finance", "food", "vacancy"]), "text": P("STRING", "текст для разбора (очищенный от лишнего)")},
    ("module",),
)
async def _hand_off(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    module = (_str(a.get("module")) or "").lower()
    if module not in {"finance", "food", "vacancy"}:
        return {"error": "module must be finance | food | vacancy"}
    ctx.handoff = (module, _str(a.get("text")) or ctx.text)
    return {"ok": True, "handed_off_to": module}


@tool(
    "open_screen",
    "Показать пользователю экран бота («покажи/открой …»): menu (главный), finance, nutrition, budgets (лимиты), recurring (регулярные), stats (статистика за месяц), tasks (задачи и заметки), goals (цели накоплений). Экран сам содержит данные — отдельно перечислять их не нужно. После изменения задач/целей открой соответствующий экран.",
    {"screen": P("STRING", "menu | finance | nutrition | budgets | recurring | stats | tasks | goals", enum=["menu", "finance", "nutrition", "budgets", "recurring", "stats", "tasks", "goals"])},
    ("screen",),
)
async def _open_screen(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    screen = (_str(a.get("screen")) or "menu").lower()
    ctx.open_screen = screen if screen in {"menu", "finance", "nutrition", "budgets", "recurring", "stats", "tasks", "goals"} else "menu"
    return {"ok": True, "screen": ctx.open_screen}


# ------------------------------------------------------------------ snapshot for the system prompt
async def snapshot(profile: Profile) -> str:
    """Короткий срез данных для системного промпта: балансы, сегодня, лимиты, регулярные, напоминания, питание, последние операции с id."""
    uid = profile.telegram_id
    # всё сразу: по очереди это десяток походов в Supabase (до 5 с на холодную)
    snap, limits, recurring, rems, plan, today_logs, extra_lines = await asyncio.gather(
        services.finance_snapshot(profile), services.budgets(uid), services.recurring(uid), services.reminders(uid),
        services.nutrition_profile(uid), services.today_calorie_logs(profile), _assistant_lines(profile),
    )
    m = fin.fmt_money
    parts = [
        f"Балансы: карта {m(snap.balances['card'])}, наличные {m(snap.balances['cash'])}, мне должны {m(snap.balances['lent'])}, я должен {m(snap.balances['debt'])}.",
        f"Сегодня: расход {m(snap.today_expense)}, доход {m(snap.today_income)}. Этот месяц: расход {m(snap.month.expense if snap.month else 0)}, доход {m(snap.month.income if snap.month else 0)}.",
    ]
    ledger = fin.debt_ledger(snap.entries, snap.settings)
    if ledger["lent"] or ledger["debt"]:
        lent_txt = ", ".join(f"{n or 'без имени'} {m(v)}" for n, v in ledger["lent"]) or "—"
        debt_txt = ", ".join(f"{n or 'без имени'} {m(v)}" for n, v in ledger["debt"]) or "—"
        parts.append(f"Долги по людям — мне должны: {lent_txt}. Я должен: {debt_txt}.")
        debt_rows = [r for r in snap.entries if (tr := fin.transfer_from_note(r.get("note"))) and ("lent" in tr or "debt" in tr)][:15]
        parts.append("Долговые операции (id, дата, сумма, счёт→счёт, имя): " + "; ".join(
            f"[{r.get('id')}] {str(r.get('entry_date') or '')[:10]} {m(float(r.get('amount') or 0))} {'→'.join(fin.transfer_from_note(r.get('note')))} «{fin.clean_note(r.get('note')) or ''}»" for r in debt_rows))
    if limits:
        parts.append("Лимиты/мес: " + ", ".join(f"{k}={m(v)}" for k, v in limits.items()))
    if recurring:
        parts.append("Регулярные: " + "; ".join(f"[{r.get('id')}] {r.get('title')} {m(float(r.get('amount') or 0))} {r.get('day_of_month')}-го{'' if r.get('enabled', True) else ' (пауза)'}" for r in recurring[:12]))
    if rems:
        parts.append("Напоминания: " + "; ".join(f"[{r.get('id')}] {rem.title(r)}" for r in rems[:12]))
    tot = nutri.totals(today_logs)
    if plan and plan.get("daily_calories"):
        parts.append(f"План КБЖУ: {plan.get('daily_calories')} ккал, Б{plan.get('protein')}/Ж{plan.get('fat')}/У{plan.get('carbs')} ({plan.get('title')}). Съедено сегодня: {int(tot['calories'])} ккал, Б{int(tot['protein'])}/Ж{int(tot['fat'])}/У{int(tot['carbs'])}.")
    else:
        parts.append(f"План КБЖУ не задан. Съедено сегодня: {int(tot['calories'])} ккал.")
    if today_logs:
        parts.append("Еда сегодня: " + "; ".join(f"[{r.get('id')}] {str(r.get('meal_desc') or '')[:40]} {int(float(r.get('calories') or 0))}ккал" for r in today_logs[:10]))
    recent = snap.entries[:12]
    if recent:
        lines = []
        for r in recent:
            v = entry_view(r)
            what = f"{v['from']}→{v['to']}" if v["kind"] == "transfer" else f"{'+' if v['kind'] == 'income' else '−'}{v.get('category')}"
            lines.append(f"[{v['id']}] {v['date']} {m(v['amount'])} {what}{(' «' + v['note'] + '»') if v.get('note') else ''}")
        parts.append("Последние операции (id, дата, сумма, категория): " + "; ".join(lines))
    parts.extend(extra_lines)
    return "\n".join(parts)


async def _assistant_lines(profile: Profile) -> list[str]:
    try:
        return await agent_tools_assistant.snapshot_lines(profile)
    except Exception:
        logger.debug("assistant snapshot failed", exc_info=True)
        return []


from . import agent_tools_assistant  # noqa: E402  — регистрирует инструменты заметок/задач/целей/сроков
from . import agent_tools_extra  # noqa: E402,F401  — ask_user, память, курсы валют, калькулятор, поиск, погода
from . import agent_tools_bulk  # noqa: E402,F401  — найти любую запись, массовые правки

__all__ = ["ToolContext", "Tool", "TOOLS", "declarations", "run", "snapshot", "filter_entries", "entry_view", "parse_day"]
