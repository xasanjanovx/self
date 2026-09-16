"""Финансы: чистая логика без БД и Telegram.

- кодирование счёта/перевода в поле note (совместимо со старыми записями):
  `[b:card] заметка`  — операция по счёту card|cash|lent|debt
  `[x:card>cash] ...` — внутренний перевод между счетами
- расчёт балансов, статистика по категориям, быстрый локальный парсер.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from . import categories as cats

BUCKETS = ("card", "cash", "lent", "debt")

_TRANSFER_RE = re.compile(r"^\[x:(card|cash|lent|debt)>(card|cash|lent|debt)\]\s*", re.IGNORECASE)
_BUCKET_RE = re.compile(r"^\[b:(card|cash|lent|debt)\]\s*", re.IGNORECASE)


def fmt_money(value: float) -> str:
    return f"{float(value):,.0f}".replace(",", " ")


# ------------------------------------------------------------ note encoding
def transfer_from_note(note: str | None) -> tuple[str, str] | None:
    match = _TRANSFER_RE.match(str(note or "").strip())
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower()


def bucket_from_note(note: str | None) -> str:
    raw = str(note or "").strip()
    transfer = _TRANSFER_RE.match(raw)
    if transfer:
        return transfer.group(1).lower()
    match = _BUCKET_RE.match(raw)
    return match.group(1).lower() if match else "card"


def clean_note(note: str | None) -> str | None:
    text = str(note or "").strip()
    while True:
        nxt = _TRANSFER_RE.sub("", text)
        nxt = _BUCKET_RE.sub("", nxt).strip()
        if nxt == text:
            break
        text = nxt
    return text or None


def note_with_bucket(note: str | None, bucket: str) -> str:
    return f"[b:{normalize_bucket(bucket)}] {clean_note(note) or ''}".strip()


def note_with_transfer(note: str | None, src: str, dst: str) -> str:
    return f"[x:{normalize_bucket(src)}>{normalize_bucket(dst)}] {clean_note(note) or ''}".strip()


def normalize_bucket(bucket: str | None) -> str:
    value = str(bucket or "").strip().lower()
    return value if value in BUCKETS else "card"


def is_transfer(entry: dict[str, Any]) -> bool:
    return transfer_from_note(entry.get("note")) is not None


def bucket_label(bucket: str, lang: str = "ru") -> str:
    ru = {"card": "💳 Карта", "cash": "💵 Наличные", "lent": "🤝 Дал в долг", "debt": "📌 Мои долги"}
    uz = {"card": "💳 Karta", "cash": "💵 Naqd", "lent": "🤝 Qarzga berilgan", "debt": "📌 Mening qarzim"}
    return (uz if lang == "uz" else ru).get(normalize_bucket(bucket), bucket)


def transfer_label(src: str, dst: str, lang: str = "ru") -> str:
    return f"{bucket_label(src, lang)} → {bucket_label(dst, lang)}"


def entry_category_key(entry: dict[str, Any]) -> str:
    """Ключ категории записи (старые свободные названия приводятся к ключу)."""
    if is_transfer(entry):
        return cats.TRANSFER_KEY
    kind = "income" if entry.get("entry_type") == "income" else "expense"
    return cats.normalize(entry.get("category"), kind, note=clean_note(entry.get("note")))


# ------------------------------------------------------------ balances
def apply_to_balances(balances: dict[str, float], *, kind: str, amount: float, bucket: str | None = None,
                      src: str | None = None, dst: str | None = None) -> None:
    amount = float(amount)
    if amount <= 0:
        return
    if kind == "transfer":
        # debt — пассив: возврат долга (to=debt) УМЕНЬШАЕТ его, заём (from=debt) — увеличивает.
        for b, sign in ((normalize_bucket(src), -1.0), (normalize_bucket(dst), 1.0)):
            if b == "debt":
                sign = -sign
            balances[b] = balances.get(b, 0.0) + sign * amount
        return
    b = normalize_bucket(bucket)
    if b in {"card", "cash"}:
        balances[b] += amount if kind == "income" else -amount
    elif b == "lent":
        balances[b] += amount if kind == "expense" else -amount
    elif b == "debt":
        balances[b] += amount if kind == "income" else -amount


def compute_balances(entries: list[dict[str, Any]], settings: dict[str, float] | None = None) -> dict[str, float]:
    balances = {b: 0.0 for b in BUCKETS}
    for row in entries:
        amount = float(row.get("amount") or 0)
        transfer = transfer_from_note(row.get("note"))
        if transfer:
            apply_to_balances(balances, kind="transfer", amount=amount, src=transfer[0], dst=transfer[1])
        else:
            apply_to_balances(
                balances,
                kind=str(row.get("entry_type") or "expense"),
                amount=amount,
                bucket=bucket_from_note(row.get("note")),
            )
    if settings:
        for b in BUCKETS:
            balances[b] += float(settings.get(f"{b}_base") or 0.0)
    return balances


def apply_pending(balances: dict[str, float], items: list[dict[str, Any]]) -> dict[str, float]:
    after = dict(balances)
    for item in items:
        if item.get("kind") == "transfer":
            apply_to_balances(after, kind="transfer", amount=float(item.get("amount") or 0),
                              src=item.get("from_bucket"), dst=item.get("to_bucket"))
        else:
            apply_to_balances(after, kind=str(item.get("kind") or "expense"), amount=float(item.get("amount") or 0),
                              bucket=item.get("bucket"))
    return after


# ------------------------------------------------------------ quick ops
def top_operations(entries: list[dict[str, Any]], *, limit: int = 8, since: date | None = None) -> list[dict[str, Any]]:
    """Самые частые операции (тип+категория+заметка+сумма), новые сверху при равенстве."""
    groups: dict[tuple, dict[str, Any]] = {}
    for idx, row in enumerate(entries):  # entries: новые сверху
        if is_transfer(row):
            continue
        if since is not None:
            try:
                if date.fromisoformat(str(row.get("entry_date"))[:10]) < since:
                    continue
            except Exception:
                pass
        amount = float(row.get("amount") or 0)
        if amount <= 0:
            continue
        entry_type = str(row.get("entry_type") or "expense")
        category = entry_category_key(row)
        note = clean_note(row.get("note")) or ""
        key = (entry_type, category, note.casefold(), round(amount, 2))
        existing = groups.get(key)
        if existing is None:
            groups[key] = {
                "entry_type": entry_type,
                "category": category,
                "note": note or None,
                "bucket": bucket_from_note(row.get("note")),
                "amount": amount,
                "count": 1,
                "first_idx": idx,
            }
        else:
            existing["count"] += 1
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["first_idx"]))
    top = ranked[:limit]
    # самая свежая операция — всегда первой кнопкой («повторить последнее»)
    latest = min(groups.values(), key=lambda g: g["first_idx"], default=None)
    if latest is not None and latest not in top:
        top = [latest] + top[: max(0, limit - 1)]
    elif latest is not None:
        top.remove(latest)
        top.insert(0, latest)
    return top


def quick_label(item: dict[str, Any], lang: str = "ru") -> str:
    sign = "➕" if item.get("entry_type") == "income" else "➖"
    note = str(item.get("note") or "").strip()
    desc = note or cats.label(item.get("category"), lang, with_emoji=False)
    return f"{sign}{fmt_money(float(item.get('amount') or 0))} {desc}"[:64]


# ------------------------------------------------------------ periods / stats
@dataclass
class Period:
    code: str
    start: date
    end: date
    prev_start: date
    prev_end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def period_for(code: str, today: date) -> Period:
    code = code if code in {"day", "week", "month", "prev_month", "year"} else "month"
    if code == "day":
        return Period("day", today, today, today - timedelta(days=1), today - timedelta(days=1))
    if code == "week":
        start = today - timedelta(days=6)
        return Period("week", start, today, start - timedelta(days=7), start - timedelta(days=1))
    if code == "month":
        start = today.replace(day=1)
        prev_end = start - timedelta(days=1)
        return Period("month", start, today, prev_end.replace(day=1), prev_end)
    if code == "prev_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
        prev_end = start - timedelta(days=1)
        return Period("prev_month", start, end, prev_end.replace(day=1), prev_end)
    start = today.replace(month=1, day=1)
    prev_end = start - timedelta(days=1)
    return Period("year", start, today, prev_end.replace(month=1, day=1), prev_end)


_MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
_MONTHS_UZ = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]


def period_title(period: Period, lang: str = "ru") -> str:
    months = _MONTHS_UZ if lang == "uz" else _MONTHS_RU
    if period.code == "day":
        return period.start.strftime("%d.%m.%Y")
    if period.code in {"month", "prev_month"}:
        return f"{months[period.start.month - 1].capitalize()} {period.start.year}"
    if period.code == "year":
        return f"{period.start.year}"
    return f"{period.start.strftime('%d.%m')} – {period.end.strftime('%d.%m')}"


def prev_period_title(period: Period, lang: str = "ru") -> str:
    months = _MONTHS_UZ if lang == "uz" else _MONTHS_RU
    if period.code == "day":
        return "kecha" if lang == "uz" else "вчера"
    if period.code in {"month", "prev_month"}:
        return f"{months[period.prev_start.month - 1]}"
    if period.code == "year":
        return f"{period.prev_start.year}"
    return "o'tgan 7 kun" if lang == "uz" else "прошлые 7 дней"


def _entry_date(row: dict[str, Any]) -> date | None:
    raw = str(row.get("entry_date") or str(row.get("created_at") or "")[:10])[:10]
    try:
        return date.fromisoformat(raw)
    except Exception:
        return None


def entries_between(entries: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    out = []
    for row in entries:
        d = _entry_date(row)
        if d is not None and start <= d <= end:
            out.append(row)
    return out


@dataclass
class Stats:
    period: Period
    expense: float = 0.0
    income: float = 0.0
    prev_expense: float = 0.0
    prev_income: float = 0.0
    transfers: int = 0
    ops: int = 0
    by_category: list[tuple[str, float, int]] = field(default_factory=list)  # (key, amount, count)
    income_by_category: list[tuple[str, float, int]] = field(default_factory=list)
    prev_by_category: dict[str, float] = field(default_factory=dict)
    top_day: tuple[date, float] | None = None
    days_with_expense: int = 0

    @property
    def net(self) -> float:
        return self.income - self.expense

    @property
    def avg_per_day(self) -> float:
        return self.expense / max(1, self.period.days)

    def expense_change_pct(self) -> float | None:
        if self.prev_expense <= 0:
            return None
        return (self.expense - self.prev_expense) / self.prev_expense * 100.0


def _aggregate(rows: list[dict[str, Any]]) -> tuple[float, float, dict[str, tuple[float, int]], dict[str, tuple[float, int]], int, dict[date, float]]:
    expense = income = 0.0
    by_cat: dict[str, tuple[float, int]] = {}
    by_cat_in: dict[str, tuple[float, int]] = {}
    transfers = 0
    by_day: dict[date, float] = {}
    for row in rows:
        amount = float(row.get("amount") or 0)
        if amount <= 0:
            continue
        if is_transfer(row):
            transfers += 1
            continue
        key = entry_category_key(row)
        if row.get("entry_type") == "income":
            income += amount
            prev = by_cat_in.get(key, (0.0, 0))
            by_cat_in[key] = (prev[0] + amount, prev[1] + 1)
        else:
            expense += amount
            prev = by_cat.get(key, (0.0, 0))
            by_cat[key] = (prev[0] + amount, prev[1] + 1)
            d = _entry_date(row)
            if d is not None:
                by_day[d] = by_day.get(d, 0.0) + amount
    return expense, income, by_cat, by_cat_in, transfers, by_day


def compute_stats(entries: list[dict[str, Any]], period: Period) -> Stats:
    current = entries_between(entries, period.start, period.end)
    previous = entries_between(entries, period.prev_start, period.prev_end)
    expense, income, by_cat, by_cat_in, transfers, by_day = _aggregate(current)
    prev_expense, prev_income, prev_by_cat, _, _, _ = _aggregate(previous)
    stats = Stats(period=period, expense=expense, income=income, prev_expense=prev_expense, prev_income=prev_income,
                  transfers=transfers, ops=len(current))
    stats.by_category = sorted(((k, v[0], v[1]) for k, v in by_cat.items()), key=lambda x: -x[1])
    stats.income_by_category = sorted(((k, v[0], v[1]) for k, v in by_cat_in.items()), key=lambda x: -x[1])
    stats.prev_by_category = {k: v[0] for k, v in prev_by_cat.items()}
    if by_day:
        top = max(by_day.items(), key=lambda kv: kv[1])
        stats.top_day = (top[0], top[1])
        stats.days_with_expense = len(by_day)
    return stats


def bar(ratio: float, width: int = 10) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    return "▰" * filled + "▱" * (width - filled)


# ------------------------------------------------------------ local parser
_AMOUNT_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[  ]\d{3})+|\d+(?:[.,]\d+)?)\s*(?P<mult>млн|mln|million|миллион|тыс\.?|тысяч|ming|k|к|m)?\b",
    re.IGNORECASE,
)
_INCOME_WORDS = ("доход", "кирим", "kirim", "daromad", "зарплат", "получил", "пришл", "oylik", "maosh", "oldim pul", "tushdi", "премия", "аванс", "avans")
_EXPENSE_WORDS = ("расход", "чиқим", "chiqim", "xarajat", "потратил", "sarfladim", "купил", "заплатил", "to'ladim", "toladim", "оплатил", "sotib oldim", "ketdi")
_COMPLEX_WORDS = (
    "долг", "qarz", "вернул", "qaytar", "снял", "положил", "перев", "o'tkaz", "otkaz", "yechdim", "soldim",
    "занял", "одолжил", "за друга", "кредит", "kredit", "погас", "обнал", "пополнил",
)
_CASH_WORDS = ("наличн", "naqd", "кэш", "cash", "налик", "nalichka")
_CARD_WORDS = ("картой", "на карту", "с карты", "karta", "kartadan", "kartaga", "по карте", "картa")
_BUCKET_HINT_RE = re.compile(
    r"\b(наличными|наличные|наличка|наликом|налик|кэшем|кэш|naqd(?:\s*pul)?(?:da|ga|dan)?|картой|на карту|с карты|по карте|karta(?:ga|dan|da)?)\b",
    re.IGNORECASE,
)


def bucket_hint(text: str) -> str:
    low = text.lower()
    if any(w in low for w in _CASH_WORDS):
        return "cash"
    return "card"
_CURRENCY_WORDS = re.compile(r"\b(сум|сўм|сумов|so'm|som|uzs|сумм)\b", re.IGNORECASE)


def parse_amount(text: str) -> tuple[float, str] | None:
    """Найти сумму в тексте. Возвращает (сумма, остаток текста без суммы)."""
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    num = match.group("num").replace(" ", "").replace(" ", "")
    mult = (match.group("mult") or "").lower()
    if "," in num and "." not in num:
        num = num.replace(",", ".")
    if "." in num:
        whole, frac = num.split(".", 1)
        # 25.000 — узбекский разделитель тысяч
        if len(frac) == 3 and not mult:
            num = whole + frac
    try:
        value = float(num)
    except ValueError:
        return None
    if mult in {"млн", "mln", "million", "миллион", "m"}:
        value *= 1_000_000
    elif mult in {"тыс", "тыс.", "тысяч", "ming", "k", "к"}:
        value *= 1_000
    rest = (text[: match.start()] + " " + text[match.end():]).strip()
    rest = _CURRENCY_WORDS.sub("", rest).strip(" ,.;:-–—")
    return value, rest


def _split_chunks(text: str) -> list[str]:
    parts = re.split(r"[\n;,]+|\s+(?:и|va|and|\+)\s+", text)
    return [p.strip(" .") for p in parts if p and p.strip(" .")]


_MIN_IMPLICIT_AMOUNT = 500  # меньше — это скорее «2 яйца», чем сумма в сумах


def _has_money_marker(low: str) -> bool:
    return any(w in low for w in _INCOME_WORDS + _EXPENSE_WORDS) or bool(_CURRENCY_WORDS.search(low))


def bare_amount(text: str) -> tuple[str, float] | None:
    """«25000» / «+300000» / «доход 5 млн» без описания → (kind, amount), иначе None."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 40:
        return None
    parsed = parse_amount(raw)
    if parsed is None:
        return None
    amount, rest = parsed
    low = raw.lower()
    kind = "income" if raw.startswith("+") or any(w in low for w in _INCOME_WORDS) else "expense"
    rest = re.sub(r"\b(расход|доход|chiqim|kirim|xarajat|daromad|потратил|sarfladim|получил|oldim)\b", "", rest, flags=re.IGNORECASE)
    rest = _BUCKET_HINT_RE.sub("", rest).strip(" +-,.;:")
    if rest or amount < 100:
        return None
    return kind, amount


def parse_local(text: str) -> list[dict[str, Any]] | None:
    """Быстрый разбор простых операций без AI: «такси 25000», «расход 40к обед, доход 5 млн зарплата».
    Возвращает None, если хоть один фрагмент непонятен или встречаются долги/переводы (это — к AI)."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 240:
        return None
    low = raw.lower()
    if any(word in low for word in _COMPLEX_WORDS):
        return None
    result: list[dict[str, Any]] = []
    for chunk in _split_chunks(raw):
        parsed = parse_amount(chunk)
        if parsed is None:
            return None
        amount, rest = parsed
        if amount <= 0 or amount > 10_000_000_000:
            return None
        chunk_low = chunk.lower()
        if amount < _MIN_IMPLICIT_AMOUNT and not _has_money_marker(chunk_low):
            return None
        kind = "income" if any(w in chunk_low for w in _INCOME_WORDS) else "expense"
        bucket = bucket_hint(chunk_low)
        rest_clean = re.sub(r"\b(расход|доход|chiqim|kirim|xarajat|daromad|потратил|sarfladim|получил|oldim)\b", "", rest, flags=re.IGNORECASE)
        rest_clean = _BUCKET_HINT_RE.sub("", rest_clean)
        rest_clean = re.sub(r"\s+", " ", rest_clean).strip(" ,.;:-–—")
        if len(rest_clean) < 2:
            return None
        category = cats.guess_from_text(rest_clean, kind)
        if category is None:
            return None
        result.append({"kind": kind, "amount": amount, "category": category, "note": rest_clean[:60], "bucket": bucket})
    return result or None


def looks_like_finance(text: str) -> bool:
    low = str(text or "").lower()
    if not re.search(r"\d", low):
        return False
    parsed = parse_amount(low)
    if parsed is None:
        return False
    keywords = _INCOME_WORDS + _EXPENSE_WORDS + _COMPLEX_WORDS + ("сум", "so'm", "uzs", "трат", "плат", "pul")
    if any(word in low for word in keywords):
        return True
    if parsed[0] < _MIN_IMPLICIT_AMOUNT:
        return False
    return cats.guess_from_text(low, "expense") is not None or cats.guess_from_text(low, "income") is not None


# ------------------------------------------------------------ budgets
@dataclass
class BudgetStatus:
    category: str
    limit: float
    spent: float

    @property
    def ratio(self) -> float:
        return self.spent / self.limit if self.limit > 0 else 0.0

    @property
    def left(self) -> float:
        return self.limit - self.spent


def budget_statuses(stats: Stats, budgets: dict[str, float]) -> list[BudgetStatus]:
    """Статус лимитов за период (обычно — текущий месяц), самые «горячие» сверху."""
    spent = {k: a for k, a, _ in stats.by_category}
    out = [BudgetStatus(cat, float(limit), float(spent.get(cat, 0.0))) for cat, limit in budgets.items() if limit > 0]
    out.sort(key=lambda b: -b.ratio)
    return out


def budget_warnings(statuses: list[BudgetStatus], keys: set[str] | None = None, *, lang: str = "ru") -> list[str]:
    """Строки-предупреждения для категорий, где потрачено ≥ 80% лимита."""
    lines = []
    for b in statuses:
        if keys is not None and b.category not in keys:
            continue
        if b.ratio >= 1.0:
            over = b.spent - b.limit
            lines.append(
                f"🚫 {cats.label(b.category, lang)}: {'limitdan oshdi' if lang == 'uz' else 'лимит превышен'} +{fmt_money(over)}"
                f" ({fmt_money(b.spent)} / {fmt_money(b.limit)})"
            )
        elif b.ratio >= 0.8:
            lines.append(
                f"⚠️ {cats.label(b.category, lang)}: {'qoldi' if lang == 'uz' else 'осталось'} {fmt_money(b.left)}"
                f" {'dan' if lang == 'uz' else 'из'} {fmt_money(b.limit)}"
            )
    return lines


# ------------------------------------------------------------ recurring payments
_DAY_RE = re.compile(
    r"(?:(?:каждое|каждого|every|har oyning|har oy)\s*)?(\d{1,2})\s*(?:-?(?:го|е|е\s*число|числа|число|sanasida|sana|kuni|chi|nchi|th)\b)",
    re.IGNORECASE,
)
_DAY_TAIL_RE = re.compile(r"(?:^|\s)(\d{1,2})\s*$")


def parse_recurring(text: str) -> dict[str, Any] | None:
    """«интернет 150000 5» / «аренда 2 млн 1 числа» / «kredit 1.2 mln har oyning 15» →
    {title, amount, day_of_month, category, bucket}."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return None
    day: int | None = None
    match = _DAY_RE.search(raw)
    if match:
        day = int(match.group(1))
        raw = (raw[: match.start()] + " " + raw[match.end():]).strip()
    parsed = parse_amount(raw)
    if parsed is None:
        return None
    amount, rest = parsed
    if day is None:
        tail = _DAY_TAIL_RE.search(rest)
        if tail and 1 <= int(tail.group(1)) <= 31:
            day = int(tail.group(1))
            rest = rest[: tail.start()].strip()
    if day is None or not (1 <= day <= 31) or amount <= 0:
        return None
    bucket = bucket_hint(rest.lower())
    title = _BUCKET_HINT_RE.sub("", rest)
    title = re.sub(r"\b(каждый месяц|ежемесячно|har oyning|har oy|oyiga|в месяц)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" ,.;:-–—")
    if len(title) < 2:
        return None
    category = cats.guess_from_text(title, "expense") or "home"
    return {"title": title[:60], "amount": amount, "day_of_month": day, "category": category, "bucket": bucket}


def recurring_due_day(day_of_month: int, year: int, month: int) -> int:
    """31-е в коротком месяце → последний день месяца."""
    import calendar

    return min(int(day_of_month), calendar.monthrange(year, month)[1])


def recurring_remaining(items: list[dict[str, Any]], today: date) -> tuple[float, list[dict[str, Any]]]:
    """Сумма ещё не оплаченных в этом месяце регулярных платежей и их список.
    Платежи, чей день уже прошёл и которые не отмечены — считаем оплаченными вне бота
    и не показываем как «обязательные»."""
    key = today.strftime("%Y-%m")
    pending = []
    total = 0.0
    for item in items:
        if not item.get("enabled", True):
            continue
        if str(item.get("last_done_key") or "") == key:
            continue
        if recurring_due_day(int(item.get("day_of_month") or 1), today.year, today.month) < today.day:
            continue
        pending.append(item)
        total += float(item.get("amount") or 0)
    pending.sort(key=lambda i: recurring_due_day(int(i.get("day_of_month") or 1), today.year, today.month))
    return total, pending


def recurring_due_today(items: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    key = today.strftime("%Y-%m")
    due = []
    for item in items:
        if not item.get("enabled", True):
            continue
        if str(item.get("last_done_key") or "") == key or str(item.get("last_asked_key") or "") == key:
            continue
        if recurring_due_day(int(item.get("day_of_month") or 1), today.year, today.month) <= today.day:
            due.append(item)
    return due


# ------------------------------------------------------------ debts by person
def debt_ledger(entries: list[dict[str, Any]], settings: dict[str, float] | None = None) -> dict[str, list[tuple[str, float]]]:
    """Кто мне должен и кому должен я — по именам из комментария операции.
    Возвращает {"lent": [(имя, сумма)], "debt": [(имя, сумма)]}, отсортировано по сумме."""
    lent: dict[str, float] = {}
    debt: dict[str, float] = {}
    names: dict[str, str] = {}

    def _key(note: str | None) -> str:
        name = (clean_note(note) or "").strip(" .,;:—-")
        if not name:
            return ""
        k = name.casefold()
        names.setdefault(k, name)
        return k

    for row in entries:
        transfer = transfer_from_note(row.get("note"))
        amount = float(row.get("amount") or 0)
        if amount <= 0:
            continue
        if transfer:
            src, dst = transfer
            k = _key(row.get("note"))
            if dst == "lent" and src in {"card", "cash"}:
                lent[k] = lent.get(k, 0.0) + amount
            elif src == "lent" and dst in {"card", "cash"}:
                lent[k] = lent.get(k, 0.0) - amount
            elif src == "debt" and dst in {"card", "cash"}:
                debt[k] = debt.get(k, 0.0) + amount
            elif dst == "debt" and src in {"card", "cash"}:
                debt[k] = debt.get(k, 0.0) - amount
            continue
        bucket = bucket_from_note(row.get("note"))
        if bucket == "lent":
            k = _key(row.get("note"))
            lent[k] = lent.get(k, 0.0) + (amount if row.get("entry_type") == "expense" else -amount)
        elif bucket == "debt":
            k = _key(row.get("note"))
            debt[k] = debt.get(k, 0.0) + (amount if row.get("entry_type") == "income" else -amount)

    if settings:
        if float(settings.get("lent_base") or 0):
            lent[""] = lent.get("", 0.0) + float(settings["lent_base"])
        if float(settings.get("debt_base") or 0):
            debt[""] = debt.get("", 0.0) + float(settings["debt_base"])

    def _shape(d: dict[str, float]) -> list[tuple[str, float]]:
        items = [(names.get(k, ""), v) for k, v in d.items() if abs(v) >= 1]
        return sorted(items, key=lambda x: -abs(x[1]))

    return {"lent": _shape(lent), "debt": _shape(debt)}


def needs_counterparty(item: dict[str, Any]) -> bool:
    """Операция с долгом без указания «кому/у кого»."""
    if item.get("kind") != "transfer":
        return False
    if "lent" not in (item.get("from_bucket"), item.get("to_bucket")) and "debt" not in (item.get("from_bucket"), item.get("to_bucket")):
        return False
    note = str(item.get("note") or "").strip().casefold()
    generic = {"", "долг", "qarz", "дал в долг", "взял в долг", "вернул долг", "возврат долга", "снял наличные", "qarzga berdim", "qarz oldim",
               "вернул", "qaytardi", "займ", "кредит", "kredit", "дал", "взял"}
    return note in generic or len(note) < 2


def debt_direction_label(item: dict[str, Any], lang: str = "ru") -> str:
    src, dst = item.get("from_bucket"), item.get("to_bucket")
    uz = lang == "uz"
    if dst == "lent":
        return "Kimga qarz berdingiz?" if uz else "Кому дал в долг?"
    if src == "lent":
        return "Kim qaytardi?" if uz else "Кто вернул долг?"
    if src == "debt":
        return "Kimdan qarz oldingiz? (odam yoki bank)" if uz else "У кого взял в долг? (человек или банк)"
    if dst == "debt":
        return "Kimga qaytardingiz?" if uz else "Кому вернул долг?"
    return "Izoh?" if uz else "Комментарий?"
