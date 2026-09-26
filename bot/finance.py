"""Финансы: чистая логика без БД и Telegram.

- кодирование счёта/перевода в поле note (совместимо со старыми записями):
  `[b:card] заметка`  — операция по счёту card|cash|lent|debt
  `[x:card>cash] ...` — внутренний перевод между счетами
  `[x:debt>card] [due:2026-10-26] UZUM BANK` — займ со своим сроком возврата (каждый займ — отдельный «транш»)
  `[x:card>debt] [t:19,65] UZUM BANK` — погашение конкретных займов (без тега — по ближайшему сроку)
- расчёт балансов, долги по займам (debt_book), статистика по категориям, быстрый локальный парсер.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from . import categories as cats

BUCKETS = ("card", "cash", "lent", "debt")
# "init" — виртуальный источник: долг уже существовал до начала учёта, деньги по счетам не двигались
INIT = "init"

_TRANSFER_RE = re.compile(r"^\[x:(card|cash|lent|debt|init)>(card|cash|lent|debt|init)\]\s*", re.IGNORECASE)
_BUCKET_RE = re.compile(r"^\[b:(card|cash|lent|debt)\]\s*", re.IGNORECASE)
_DUE_TAG_RE = re.compile(r"^\[due:(\d{4}-\d{2}-\d{2})\]\s*", re.IGNORECASE)
_TARGET_TAG_RE = re.compile(r"^\[t:([\w,-]+)\]\s*", re.IGNORECASE)


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


def _tags(note: str | None) -> tuple[str | None, list[str]]:
    """Служебные теги после префикса счёта: срок займа и какие займы гасит платёж."""
    text = str(note or "").strip()
    text = _TRANSFER_RE.sub("", text)
    text = _BUCKET_RE.sub("", text)
    due: str | None = None
    targets: list[str] = []
    while True:
        m = _DUE_TAG_RE.match(text)
        if m:
            due = m.group(1)
            text = text[m.end():]
            continue
        m = _TARGET_TAG_RE.match(text)
        if m:
            targets = [t for t in m.group(1).split(",") if t]
            text = text[m.end():]
            continue
        return due, targets


def due_from_note(note: str | None) -> date | None:
    raw = _tags(note)[0]
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def targets_from_note(note: str | None) -> list[str]:
    return _tags(note)[1]


def clean_note(note: str | None) -> str | None:
    text = str(note or "").strip()
    while True:
        nxt = _TRANSFER_RE.sub("", text)
        nxt = _BUCKET_RE.sub("", nxt)
        nxt = _DUE_TAG_RE.sub("", nxt)
        nxt = _TARGET_TAG_RE.sub("", nxt).strip()
        if nxt == text:
            break
        text = nxt
    return text or None


def note_with_bucket(note: str | None, bucket: str) -> str:
    return f"[b:{normalize_bucket(bucket)}] {clean_note(note) or ''}".strip()


def note_with_transfer(note: str | None, src: str, dst: str, *, due: str | date | None = None, targets: list[str] | None = None) -> str:
    tags = ""
    if due:
        tags += f"[due:{str(due)[:10]}] "
    if targets:
        tags += f"[t:{','.join(str(t) for t in targets)}] "
    return f"[x:{normalize_bucket(src)}>{normalize_bucket(dst)}] {tags}{clean_note(note) or ''}".strip()


def renote(old_note: str | None, text: str | None, *, due: str | date | None | bool = True) -> str:
    """Новый текст заметки с сохранением счёта/перевода и тегов. due=True — оставить срок как был, None/False — убрать."""
    transfer = transfer_from_note(old_note)
    old_due, targets = _tags(old_note)
    keep_due = old_due if due is True else (due or None)
    if transfer:
        return note_with_transfer(text, *transfer, due=keep_due, targets=targets)
    return note_with_bucket(text, bucket_from_note(old_note))


def normalize_bucket(bucket: str | None) -> str:
    value = str(bucket or "").strip().lower()
    return value if value in BUCKETS or value == INIT else "card"


def is_transfer(entry: dict[str, Any]) -> bool:
    return transfer_from_note(entry.get("note")) is not None


def bucket_label(bucket: str, lang: str = "ru") -> str:
    ru = {"card": "💳 Карта", "cash": "💵 Наличные", "lent": "🤝 Дал в долг", "debt": "📌 Мои долги", INIT: "🕘 Было раньше"}
    uz = {"card": "💳 Karta", "cash": "💵 Naqd", "lent": "🤝 Qarzga berilgan", "debt": "📌 Mening qarzim", INIT: "🕘 Avval bo'lgan"}
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
        # init — «уже было / списано»: старый долг (init→debt|lent) или прощённый (debt|lent→init), счета card/cash не трогаем.
        src_b, dst_b = normalize_bucket(src), normalize_bucket(dst)
        for b, sign in ((src_b, -1.0), (dst_b, 1.0)):
            if b == INIT:
                continue
            if b == "debt" and src_b != INIT and dst_b != INIT:
                sign = -sign
            elif b == "debt" and src_b == INIT:
                sign = 1.0  # старый долг: я должен
            elif b == "debt" and dst_b == INIT:
                sign = -1.0  # долг простили / списали
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
        result.append({"kind": kind, "amount": amount, "category": category, "note": tidy_note(rest_clean, category), "bucket": bucket})
    return result or None


_NOTE_PREP_RE = re.compile(r"^(за|на|для|с|в|по|uchun|ga)\s+", re.IGNORECASE)


def tidy_note(note: str, category: str) -> str | None:
    """«за транспорт» при категории «Транспорт» — лишний комментарий, убираем."""
    text = _NOTE_PREP_RE.sub("", str(note or "").strip()).strip(" ,.;:-")
    if not text:
        return None
    cat = cats.get(category)
    low = text.casefold()
    if cat and (low in {cat.ru.casefold(), cat.uz.casefold()} or low in {a.strip().casefold() for a in cat.aliases}):
        return None
    return text[:60]


def local_finance(text: str) -> bool:
    """Фраза, которую быстрые правила разбирают сами, без AI: «такси 25000», «25000», «мне должен Алишер 200к».
    Всё остальное с деньгами (долги, переводы, длинные фразы) решает агент — он видит контекст и может уточнить."""
    raw = str(text or "").strip()
    if not raw:
        return False
    return bare_amount(raw) is not None or parse_existing_debt(raw) is not None or parse_local(raw) is not None


def looks_like_finance(text: str) -> bool:
    low = str(text or "").lower()
    if not re.search(r"\d", low):
        return False
    parsed = parse_amount(low)
    if parsed is None:
        return False
    keywords = _INCOME_WORDS + _EXPENSE_WORDS + _COMPLEX_WORDS + ("сум", "so'm", "uzs", "трат", "плат", "pul", "должен", "должна", "должны", "qarzdor")
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


# ------------------------------------------------------------ debts: займы по кредиторам и должникам
# Каждый займ («взял у Uzum 1 050 000 до 26.10», «дал Асилбеку 1 млн») — отдельный транш со своей суммой, датой и сроком.
# Погашение распределяется на займы: сначала те, что указаны в теге [t:…], дальше — с ближайшим сроком
# (займы без срока — последними, по дате). Переплата копится у человека и уменьшает его следующий займ.
@dataclass
class Tranche:
    id: str
    side: str  # lent — мне должны, debt — я должен
    date: date | None
    amount: float
    left: float
    due: date | None = None
    account: str = "card"  # куда пришли / откуда ушли деньги: card | cash | init (долг был до учёта)

    @property
    def open(self) -> bool:
        return self.left >= 1


@dataclass
class Counterparty:
    side: str
    name: str
    tranches: list[Tranche] = field(default_factory=list)
    credit: float = 0.0  # переплата: вернул/мне вернули больше, чем было
    paid: float = 0.0

    @property
    def total(self) -> float:
        return sum(t.left for t in self.tranches) - self.credit

    def open_tranches(self) -> list[Tranche]:
        return sorted((t for t in self.tranches if t.open), key=_tranche_order)


def _tranche_order(t: Tranche) -> tuple:
    return (t.due is None, t.due or date.max, t.date or date.min, _id_num(t.id))


def _id_num(value: Any) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0


def _chrono_key(row: dict[str, Any]) -> tuple:
    return (str(row.get("entry_date") or "")[:10], str(row.get("created_at") or ""), _id_num(row.get("id")))


def _debt_effects(row: dict[str, Any]) -> list[tuple[str, str, str]]:
    """[(side, "up"|"down", счёт денег)] — как операция меняет долги. up — новый займ, down — погашение/списание."""
    transfer = transfer_from_note(row.get("note"))
    if transfer:
        src, dst = transfer
        out = []
        if dst == "lent":
            out.append(("lent", "up", src))
        if src == "lent":
            out.append(("lent", "down", dst))
        if src == "debt":
            out.append(("debt", "down" if dst == INIT else "up", dst))
        if dst == "debt":
            out.append(("debt", "up" if src == INIT else "down", src))
        return out
    bucket = bucket_from_note(row.get("note"))
    income = row.get("entry_type") == "income"
    if bucket == "lent":
        return [("lent", "down" if income else "up", "card")]
    if bucket == "debt":
        return [("debt", "up" if income else "down", "card")]
    return []


def person_key(name: str | None) -> str:
    return (str(name or "").strip(" .,;:—-")).casefold()


def _allocate(cp: Counterparty, amount: float, targets: list[str]) -> list[tuple[Tranche, float]]:
    """Погасить amount: сначала займы из targets (по порядку), потом — с ближайшим сроком. Остаток → переплата."""
    parts: list[tuple[Tranche, float]] = []
    left = amount
    by_id = {t.id: t for t in cp.tranches}
    queue = [by_id[t] for t in targets if t in by_id] + [t for t in cp.open_tranches() if t.id not in targets]
    for t in queue:
        if left <= 0:
            break
        if not t.open:
            continue
        use = min(t.left, left)
        t.left -= use
        left -= use
        parts.append((t, use))
    if left > 0:
        cp.credit += left
    return parts


def debt_book(entries: list[dict[str, Any]], settings: dict[str, float] | None = None,
              deadlines: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Counterparty]]:
    """{"lent": {ключ имени: Counterparty}, "debt": {...}} — все займы с остатками и сроками.
    deadlines — старые сроки «по человеку» (таблица debt_deadlines): действуют на займы без своего срока,
    взятые не позже дня, когда срок был поставлен."""
    book: dict[str, dict[str, Counterparty]] = {"lent": {}, "debt": {}}

    def _cp(side: str, name: str) -> Counterparty:
        key = person_key(name)
        cp = book[side].get(key)
        if cp is None:
            cp = book[side][key] = Counterparty(side=side, name=name.strip(" .,;:—-") if name else "")
        return cp

    person_due: dict[tuple[str, str], list[tuple[date, date | None]]] = {}
    for r in deadlines or []:
        try:
            due = date.fromisoformat(str(r.get("due_date"))[:10])
        except ValueError:
            continue
        try:
            set_on = date.fromisoformat(str(r.get("created_at") or "")[:10])
        except ValueError:
            set_on = None
        person_due.setdefault((str(r.get("side") or "lent"), person_key(r.get("person"))), []).append((due, set_on))

    def _inherited_due(side: str, key: str, day: date | None) -> date | None:
        rows = person_due.get((side, key))
        if rows is None and key and person_due:
            from . import names as names_mod  # «Асилбек» в сроках и «Асилбек ака» в операциях — один человек

            n = names_mod.norm(key)
            for (s, k), r in person_due.items():
                m = names_mod.norm(k)
                if s == side and n and m and (n == m or n.startswith(m) or m.startswith(n)):
                    rows = r
                    break
        for due, set_on in rows or []:
            if set_on is None or day is None or day <= set_on:
                return due
        return None

    for side in ("lent", "debt"):
        base = float((settings or {}).get(f"{side}_base") or 0)
        if base > 0:
            _cp(side, "").tranches.append(Tranche(id="base", side=side, date=None, amount=base, left=base, account=INIT))
        elif base < 0:
            _cp(side, "").credit += -base

    for row in sorted(entries, key=_chrono_key):
        amount = float(row.get("amount") or 0)
        if amount <= 0:
            continue
        for side, direction, account in _debt_effects(row):
            name = clean_note(row.get("note")) or ""
            cp = _cp(side, name)
            if direction == "up":
                day = _entry_date(row)
                left = amount
                if cp.credit > 0:  # переплата гасит новый займ
                    use = min(cp.credit, left)
                    cp.credit -= use
                    left -= use
                due = due_from_note(row.get("note")) or _inherited_due(side, person_key(name), day)
                cp.tranches.append(Tranche(id=str(row.get("id")), side=side, date=day, amount=amount, left=left, due=due, account=account))
            else:
                cp.paid += amount
                _allocate(cp, amount, targets_from_note(row.get("note")))
    return book


def debt_ledger(entries: list[dict[str, Any]], settings: dict[str, float] | None = None) -> dict[str, list[tuple[str, float]]]:
    """Кто мне должен и кому должен я — по именам из комментария операции.
    Возвращает {"lent": [(имя, сумма)], "debt": [(имя, сумма)]}, отсортировано по сумме."""
    book = debt_book(entries, settings)

    def _shape(side: str) -> list[tuple[str, float]]:
        items = [(cp.name, cp.total) for cp in book[side].values() if abs(cp.total) >= 1]
        return sorted(items, key=lambda x: -abs(x[1]))

    return {"lent": _shape("lent"), "debt": _shape("debt")}


def effective_deadlines(book: dict[str, dict[str, Counterparty]]) -> list[dict[str, Any]]:
    """Сроки по открытым займам: [{person, side, due_date, amount, loan_ids}] — одна строка на (человек, срок)."""
    rows: dict[tuple[str, str, date], dict[str, Any]] = {}
    for side, people in book.items():
        for cp in people.values():
            for t in cp.open_tranches():
                if t.due is None:
                    continue
                key = (side, person_key(cp.name), t.due)
                row = rows.setdefault(key, {"person": cp.name, "side": side, "due_date": t.due.isoformat(), "amount": 0.0, "loan_ids": []})
                row["amount"] += t.left
                row["loan_ids"].append(t.id)
    return sorted(rows.values(), key=lambda r: r["due_date"])


def find_counterparty(book: dict[str, dict[str, Counterparty]], side: str, name: str, *, min_score: float = 0.8) -> list[Counterparty]:
    """Кредитор/должник по имени из речи: «Узум» → «UZUM BANK», «Асельбек» → «Асилбек». Лучшие совпадения первыми;
    точное совпадение — единственным элементом."""
    from . import names as names_mod

    query = names_mod.norm(name)
    if not query:
        return [cp for k, cp in book.get(side, {}).items() if not k]
    scored = []
    for cp in book.get(side, {}).values():
        if not cp.name:
            continue
        cand = names_mod.norm(cp.name)
        if not cand:
            continue
        s = names_mod.score(query, cand)
        q_words, c_words = query.split(), cand.split()
        # «Узум» = «UZUM BANK», но «Uzum Nasiya» ≠ «UZUM BANK»: общее первое слово решает, только если второго нет у одного из них
        if cand.startswith(query) or query.startswith(cand) or (
                len(q_words[0]) >= 3 and q_words[0] == c_words[0] and (len(q_words) == 1 or len(c_words) == 1)):
            s = max(s, 0.9)
        elif len(q_words) > 1 and len(c_words) > 1 and q_words[0] == c_words[0] and q_words[1] != c_words[1]:
            s = min(s, 0.79)  # разные продукты одной компании — не сливать и не гасить молча (бухгалтер спросит)
        if s >= min_score:
            scored.append((s, cp))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= 0.99:
        return [scored[0][1]]
    return [cp for _, cp in scored]


_INSTITUTION_RE = re.compile(
    r"bank|банк|uzum|узум|\btez\b|\bтез\b|hamkor|хамкор|kapital|капитал|anor|анор|alif|алиф|payme|пейми|click|клик|paynet|пайнет|"
    r"ipak|ипак|asaka|асака|agrobank|агробанк|xalq|халк|\bnbu\b|\bsqb\b|trast|траст|infin|инфин|davr|давр|ziraat|octo|окто|"
    r"hayot|хаёт|tbc|тбс|apelsin|апельсин|zood|intend|интенд|nasiya|насия|рассрочк|кредит|kredit|микрозайм|mikroqarz|lombard|ломбард|"
    r"garant|гарант|turon|турон|universal|универсал|madad|мадад|poytaxt|пойтахт|smartbank|смартбанк|"
    r"\bооо\b|\bmchj\b|\bмчж\b|магазин|market|маркет",
    re.IGNORECASE,
)


def is_institution(name: str | None) -> bool:
    """Банк / приложение / магазин рассрочки — деньги от них всегда на карту, у займа всегда есть срок."""
    return bool(_INSTITUTION_RE.search(str(name or "")))


_DEBT_WORDS = (
    "долг", "qarz", "вернул", "вернула", "верну", "qaytar", "занял", "заняла", "одолжил", "за друга", "кредит", "kredit",
    "погас", "рассрочк", "насия", "nasiya", "займ", "заём", "простил", "должен", "должна", "должны",
)


def is_debt_phrase(text: str) -> bool:
    """Фраза про долги/займы/возвраты — её решает агент-бухгалтер (видит займы, сроки и балансы), а не простой парсер."""
    low = str(text or "").lower()
    return any(w in low for w in _DEBT_WORDS)


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
    if src == INIT and dst == "lent":
        return "Kim sizga qarz?" if uz else "Кто тебе должен?"
    if src == INIT and dst == "debt":
        return "Kimga qarzdorsiz? (odam yoki bank)" if uz else "Кому ты должен? (человек или банк)"
    if dst == "lent":
        return "Kimga qarz berdingiz?" if uz else "Кому дал в долг?"
    if src == "lent":
        return "Kim qaytardi?" if uz else "Кто вернул долг?"
    if src == "debt":
        return "Kimdan qarz oldingiz? (odam yoki bank)" if uz else "У кого взял в долг? (человек или банк)"
    if dst == "debt":
        return "Kimga qaytardingiz?" if uz else "Кому вернул долг?"
    return "Izoh?" if uz else "Комментарий?"


# ------------------------------------------------------------ existing (old) debts — без движения денег
_OLD_LENT_RE = re.compile(
    r"^(?:мне\s+(?:должен|должна|должны|дол[жг]?ны)|menga\s+qarz(?:dor)?)\s+(?P<who>.+)$|^(?P<who2>.+?)\s+(?:мне\s+)?(?:должен|должна|должны)\s+мне\s*(?P<rest2>.*)$|^(?P<who3>.+?)\s+menga\s+qarz(?:dor)?\s*(?P<rest3>.*)$",
    re.IGNORECASE,
)
_OLD_DEBT_RE = re.compile(
    r"^(?:я\s+(?:должен|должна)|мой\s+долг|у\s+меня\s+долг|men\s+qarzdorman|mening\s+qarzim)\s*(?P<who>.*)$|^men\s+(?P<who2>.+?)(?:ga|ga\s+)\s*qarzdorman\s*(?P<rest2>.*)$",
    re.IGNORECASE,
)
_OLD_HINT_RE = re.compile(r"\b(уже|давно|ещё|еще|с прошлого|раньше|старый долг|старые долги|avval|allaqachon|eski qarz)\b", re.IGNORECASE)


def _strip_amount(text: str) -> tuple[float, str] | None:
    parsed = parse_amount(text)
    if parsed is None:
        return None
    amount, rest = parsed
    rest = _OLD_HINT_RE.sub("", rest)
    rest = re.sub(r"\b(в долг|долг|qarz|sum|so'm)\b", "", rest, flags=re.IGNORECASE)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.;:—-")
    return amount, rest


def parse_existing_debt(text: str) -> dict[str, Any] | None:
    """«мне должен Абдулазиз 200000» / «Абдулазиз должен мне 200000» → init→lent;
    «я должен банку 3 млн» / «мой долг Хамкорбанк 3 млн» → init→debt. Деньги по счетам не двигаются."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw or len(raw) > 120:
        return None
    parsed = _strip_amount(raw)
    if parsed is None:
        return None
    amount, rest = parsed
    if amount <= 0:
        return None
    m = _OLD_LENT_RE.match(rest)
    if m:
        who = (m.group("who") or m.group("who2") or m.group("who3") or "").strip(" ,.;:—-")
        if _has_terms(who):
            return None
        return {"kind": "transfer", "amount": amount, "from_bucket": INIT, "to_bucket": "lent", "note": who or None}
    m = _OLD_DEBT_RE.match(rest)
    if m:
        who = (m.group("who") or m.group("who2") or "").strip(" ,.;:—-")
        who = re.sub(r"^(перед|у|banku|bankga)\s+", "", who, flags=re.IGNORECASE)
        if _has_terms(who) or is_institution(who):
            return None  # срок, дата, банк — к бухгалтеру: он спросит срок займа и не спутает дату с именем
        return {"kind": "transfer", "amount": amount, "from_bucket": INIT, "to_bucket": "debt", "note": who or None}
    return None


_TERMS_RE = re.compile(r"\d|\b(до|срок\w*|через|числ\w*|muddat\w*|gacha)\b", re.IGNORECASE)


def _has_terms(who: str) -> bool:
    return bool(_TERMS_RE.search(who or ""))
