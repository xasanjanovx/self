"""Бухгалтер долгов: займы, погашения, перекредитование — чистая логика без БД и Telegram.

Агент передаёт смысл («взял у Uzum 1 050 000», «погасил TEZ»), а суммы, остатки, сроки и счета считает этот модуль:
- каждый займ — отдельный транш со своим сроком (у одного кредитора их может быть несколько);
- «погасил TEZ» без суммы = весь остаток; погашение идёт на займ с ближайшим сроком (или на указанный loan_id);
- заплатил больше долга — спрашиваем: проценты/комиссия (расход) или переплата;
- банк/приложение → деньги на карту без вопроса и у займа спрашиваем срок; человек → «на карту или наличными?»;
- взял у одного, чтобы закрыть другого, — считаем, сколько осталось на счёте;
- покупка в рассрочку = расход по категории + долг, деньги с карты не уходят.

plan() ничего не пишет: возвращает строки для finance_entries, итоги для ответа или ОДИН вопрос с кнопками.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from . import categories as cats
from . import finance as fin

# action → (сторона, направление): debt — я должен, lent — мне должны; up — новый займ, down — погашение/списание
ACTIONS: dict[str, tuple[str, str]] = {
    "borrow": ("debt", "up"),             # взял в долг / кредит / займ — деньги пришли на счёт
    "repay": ("debt", "down"),            # вернул свой долг / погасил кредит — деньги ушли со счёта
    "lend": ("lent", "up"),               # дал в долг / оплатил за друга
    "collect": ("lent", "down"),          # мне вернули долг
    "owe_existing": ("debt", "up"),       # я уже был должен до начала учёта — деньги сейчас не двигаются
    "owed_existing": ("lent", "up"),      # мне уже были должны до начала учёта
    "creditor_forgave": ("debt", "down"),  # мне простили долг / банк списал
    "i_forgave": ("lent", "down"),        # я простил долг / точно не вернут — списываю
    "buy_on_credit": ("debt", "up"),      # купил в рассрочку / в кредит: расход по категории + долг
}
MONEY_ACTIONS = {"borrow", "repay", "lend", "collect"}

_CARD_HINT = re.compile(r"карт|karta|kartaga|kartadan|card|💳|перевод|click|payme|клик|пейми", re.IGNORECASE)
_CASH_HINT = re.compile(r"налич|налик|нал\b|кэш|cash|naqd|💵", re.IGNORECASE)
# срок назван в самой фразе (модель любит сама «дописать» срок через месяц)
_DUE_HINT = re.compile(
    r"\d{1,2}[./-]\d{1,2}|\bдо\b|через|срок|числ|недел|месяц|\bгод|\bлет\b|завтра|послезавтра|понедельник|вторник|сред[уы]|четверг|пятниц|суббот|воскрес|"
    r"январ|феврал|март|апрел|ма[йя]\b|июн|июл|август|сентябр|октябр|ноябр|декабр|muddat|gacha|keyin|hafta|oy\b|oyga|yil|ertaga|"
    r"yanvar|fevral|mart|aprel|iyun|iyul|avgust|sentabr|oktabr|noyabr|dekabr|\bdue\b|until|month|week",
    re.IGNORECASE,
)
_NO_NAME = {"без имени", "неизвестно", "не указано", "нет", "—", "-", "unknown", "nomsiz", "noma'lum", "kimdir", "кто-то", "someone", "null", "none"}


@dataclass
class Plan:
    rows: list[dict[str, Any]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    ask: dict[str, Any] | None = None
    error: str | None = None
    before: dict[str, float] = field(default_factory=dict)
    after: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    people: list[fin.Counterparty] = field(default_factory=list)  # затронутые кредиторы/должники (после операции)


def _m(v: float) -> str:
    return fin.fmt_money(v)


def _acc_word(acc: str, uz: bool, *, case: str = "to") -> str:
    if acc == "cash":
        return "naqd" if uz else ("наличными" if case == "by" else "наличные")
    return ("kartaga" if case == "to" else "kartadan") if uz else ("на карту" if case == "to" else "с карты")


def _due_label(due: date | None, uz: bool) -> str:
    if due is None:
        return "muddatsiz" if uz else "без срока"
    return f"{due:%d.%m} gacha" if uz else f"до {due:%d.%m}"


def parse_due(value: Any, today: date) -> date | None:
    """2026-10-26 / 26.10 / 26.10.2026 / 26/10 → дата (без года — ближайшая будущая)."""
    s = str(value or "").strip().lower()
    if not s or s in {"null", "none"}:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.search(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", s)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else today.year
    if year < 100:
        year += 2000
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if not m.group(3) and d < today:
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return None
    return d


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s.lower() in {"null", "none"}:
        return None
    parsed = fin.parse_amount(s)
    return parsed[0] if parsed else None


def _flag(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"true", "1", "yes", "да", "ha"}


def account_said(text: str) -> str | None:
    """Сказал ли он сам, куда/откуда деньги: «на карту», «наличными», кнопка «💵 Наличные»."""
    cash, card = bool(_CASH_HINT.search(text or "")), bool(_CARD_HINT.search(text or ""))
    if cash and not card:
        return "cash"
    if card and not cash:
        return "card"
    return "both" if card and cash else None


def _loan_said(t: fin.Tranche, text: str) -> bool:
    """Он сам назвал этот займ: его сумму («тот, что 1 050 000») или срок («до 26.10», кнопка «1 050 000 · до 26.10»)."""
    if not text:
        return True
    digits = {re.sub(r"\D", "", m) for m in re.findall(r"\d[\d  ]*", text)}
    if {str(int(round(t.left))), str(int(round(t.amount)))} & digits:
        return True
    if t.due and (f"{t.due:%d.%m}" in text or f"{t.due.day}.{t.due.month:02d}" in text):
        return True
    return bool(t.due is None and re.search(r"без срока|muddatsiz", text, re.IGNORECASE))


def _ask(question: str, options: list[str], hint: str) -> dict[str, Any]:
    return {"question": question, "options": [o[:40] for o in options[:4]], "hint": hint}


def _tranche_option(t: fin.Tranche, uz: bool) -> str:
    return f"{_m(t.left)} · {_due_label(t.due, uz)}"


def describe(cp: fin.Counterparty, uz: bool = False) -> str:
    """«UZUM BANK: 3 255 000 — 2 205 000 до 01.10, 1 050 000 без срока» / «TEZ: долг закрыт»."""
    name = cp.name or ("nomsiz" if uz else "без имени")
    total = cp.total
    if total < 1:
        if total <= -1:
            extra = ("ortiqcha to'landi " if uz else "переплата ") if cp.side == "debt" else ("ortiqcha qaytardi " if uz else "вернул больше на ")
            return f"{name}: {extra}{_m(-total)}"
        return f"{name}: {'qarz yopildi' if uz else 'долг закрыт'}"
    parts = cp.open_tranches()
    if len(parts) <= 1:
        due = parts[0].due if parts else None
        return f"{name}: {_m(total)}" + (f" ({_due_label(due, uz)})" if due else "")
    return f"{name}: {_m(total)} — " + ", ".join(f"{_m(t.left)} {_due_label(t.due, uz)}" for t in parts[:4])


def plan(items: list[dict[str, Any]], *, book: dict[str, dict[str, fin.Counterparty]], balances: dict[str, float],
         today: date, text: str = "", lang: str = "ru") -> Plan:
    uz = lang == "uz"
    book = copy.deepcopy(book)
    after = dict(balances)
    out = Plan(before=dict(balances))
    said = account_said(text)
    moved_in: dict[str, float] = {}   # счёт → сколько пришло займами в этом сообщении
    moved_out: dict[str, float] = {}  # счёт → сколько ушло на погашение
    touched: list[fin.Counterparty] = []
    new_seq = 0

    def add_row(row: dict[str, Any]) -> None:
        out.rows.append(row)
        transfer = fin.transfer_from_note(row["note"])
        if transfer:
            fin.apply_to_balances(after, kind="transfer", amount=row["amount"], src=transfer[0], dst=transfer[1])
        else:
            fin.apply_to_balances(after, kind=row["entry_type"], amount=row["amount"], bucket=fin.bucket_from_note(row["note"]))

    def cp_for(side: str, name: str) -> fin.Counterparty:
        key = fin.person_key(name)
        cp = book[side].get(key)
        if cp is None:
            cp = book[side][key] = fin.Counterparty(side=side, name=name.strip(" .,;:—-"))
        return cp

    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        action = str(it.get("action") or "").strip().lower()
        if action not in ACTIONS:
            out.error = f"unknown action {action!r}; allowed: {', '.join(ACTIONS)}"
            return out
        side, direction = ACTIONS[action]
        person_raw = str(it.get("person") or "").strip(" .,;:—-")
        if person_raw.casefold() in _NO_NAME:
            person_raw = ""
        amount = _num(it.get("amount"))
        day = parse_due(it.get("date"), today) if it.get("date") else today
        if day is None or day > today + timedelta(days=1):
            day = today
        again = f"Когда ответит — вызови record_debt ещё раз с теми же items, дополнив операцию №{idx + 1}"

        # ---- кто
        if not person_raw:
            known = [cp.name for cp in sorted(book[side].values(), key=lambda c: -c.total) if cp.name and cp.total >= 1][:3]
            q = {"borrow": ("Kimdan qarz oldingiz? (odam yoki bank)", "У кого взяли в долг? (человек или банк)"),
                 "repay": ("Kimga qaytardingiz?", "Кому вернули долг?"),
                 "lend": ("Kimga qarz berdingiz?", "Кому дали в долг?"),
                 "collect": ("Kim qaytardi?", "Кто вернул долг?"),
                 "buy_on_credit": ("Qayerdan nasiyaga oldingiz? (bank/do'kon)", "Где взяли в рассрочку? (банк/магазин)")}.get(
                action, ("Kim?", "Кто?"))
            out.ask = _ask(q[0] if uz else q[1], known, f"{again}: person = ответ (имя или банк).")
            return out
        if direction == "up":
            # новый займ: к существующему человеку — только при уверенном совпадении («Азиз» ≠ «Абдулазиз»)
            cands = fin.find_counterparty(book, side, person_raw, min_score=0.86)
            if len(cands) > 1:
                out.ask = _ask("Aynan kim?" if uz else "Кто именно?", [c.name for c in cands[:3]] + [f"{'Yangi' if uz else 'Новый'}: {person_raw}"],
                               f"{again}: person = выбранное имя (для «Новый: …» — это имя как есть).")
                return out
            name = cands[0].name if cands else re.sub(r"^(?:Новый|Yangi):\s*", "", person_raw)
            cp = cp_for(side, name)
        else:
            cands = fin.find_counterparty(book, side, person_raw)
            open_cands = [c for c in cands if c.total >= 1]
            closed = cands[0] if cands and not open_cands else None
            if not open_cands and action not in {"creditor_forgave", "i_forgave"}:
                if _flag(it.get("existed_before")):
                    cp = cp_for(side, closed.name if closed else person_raw)
                else:
                    # долга нет (не было или уже закрыт) — не угадываем: перепутал сторону, повтор, старый долг или просто расход
                    shown = closed.name if closed else person_raw
                    other_side = "lent" if side == "debt" else "debt"
                    flipped = next((c for c in fin.find_counterparty(book, other_side, person_raw) if c.total >= 1), None)
                    similar = [c.name for c in fin.find_counterparty(book, side, person_raw, min_score=0.6) if c.total >= 1 and c is not closed][:1]
                    status = (f" ({describe(closed, uz).split(': ', 1)[1]})" if closed else "")
                    opts = []
                    if flipped is not None:  # «вернул Асилбеку», а это Асилбек должен мне
                        opts.append((f"Aksincha: {flipped.name} menga qaytardi" if uz else f"Наоборот: {flipped.name} вернул мне") if side == "debt"
                                    else (f"Aksincha: men {flipped.name}ga qaytardim" if uz else f"Наоборот: я вернул {flipped.name}"))
                    opts += [(f"Bu {n}" if uz else f"Это {n}") for n in similar]
                    opts.append("Qarz hisobdan oldin bo'lgan" if uz else "Долг был до учёта")
                    if side == "debt":
                        q = (f"«{shown}» oldida ochiq qarz yo'q{status}. Bu nima?" if uz else f"Открытого долга перед «{shown}» нет{status}. Что это?")
                        opts.append("Oddiy xarajat" if uz else "Обычный расход")
                    else:
                        q = (f"«{shown}»ning ochiq qarzi yo'q{status}. Bu nima?" if uz else f"Открытого долга «{shown}» передо мной нет{status}. Что это?")
                        opts.append("Oddiy kirim" if uz else "Обычный доход")
                    if closed:
                        opts.append("Allaqachon yozilgan" if uz else "Уже записано")
                    flip_action = {"repay": "collect", "collect": "repay"}.get(action, action)
                    out.ask = _ask(q, opts, f"{again}: «наоборот» → action={flip_action}, person={flipped.name if flipped else shown}; "
                                            "«был до учёта» → existed_before=true; «это X» → person=X; "
                                            "«обычный расход/доход» → НЕ record_debt, а add_finance_entries (expense/income); "
                                            "«уже записано» → ничего не записывай и не удаляй.")
                    return out
            elif not open_cands:
                out.error = f"по «{closed.name if closed else person_raw}» открытого долга нет — списывать нечего"
                return out
            elif len(open_cands) > 1:
                out.ask = _ask("Aynan kim?" if uz else "Кто именно?", [c.name for c in open_cands[:4]], f"{again}: person = выбранное имя.")
                return out
            else:
                cp = open_cands[0]
            name = cp.name

        # ---- сколько
        loan_id = str(it.get("loan_id") or "").strip() or None
        loan = next((t for t in cp.tranches if t.id == loan_id), None) if loan_id else None
        if loan_id and loan is None:
            out.error = f"loan_id {loan_id} не найден у «{name}»; займы: " + ", ".join(f"{t.id}={_m(t.left)}" for t in cp.open_tranches())
            return out
        if loan is not None and amount is None and len(cp.open_tranches()) > 1 and not _loan_said(loan, text):
            loan = None  # займ выбрала модель, а он не называл ни суммы, ни срока займа — спросим, какой
        if direction == "down":
            open_parts = cp.open_tranches()
            if amount is None:
                if loan is not None:
                    amount = loan.left
                elif len(open_parts) <= 1 or action in {"creditor_forgave", "i_forgave"}:
                    amount = max(cp.total, 0.0)
                else:
                    opts = [(f"Hammasi {_m(cp.total)}" if uz else f"Весь долг {_m(cp.total)}")] + [_tranche_option(t, uz) for t in open_parts[:3]]
                    out.ask = _ask(f"{name}ga qancha qaytarildi?" if uz else f"Сколько погашено по {name}?", opts,
                                   f"{again}: «весь долг» → amount = вся сумма; выбран займ → amount = его остаток и loan_id этого займа "
                                   f"(займы: {', '.join(f'{t.id} = {_m(t.left)} {_due_label(t.due, False)}' for t in open_parts)}).")
                    return out
                if amount < 1 and not _flag(it.get("existed_before")):
                    out.error = f"по «{name}» долга нет ({describe(cp, uz)})"
                    return out
        if amount is None or amount <= 0:
            q = {"borrow": f"Сколько взяли у {name}?", "lend": f"Сколько дали {name}?", "owe_existing": f"Сколько вы должны {name}?",
                 "owed_existing": f"Сколько {name} должен вам?", "buy_on_credit": "На какую сумму покупка?"}.get(action, f"Какая сумма ({name})?")
            out.ask = _ask(q, [], f"{again}: amount = ответ.")
            return out

        # ---- проценты / переплата
        interest = _num(it.get("interest")) or 0.0
        if interest >= amount:
            out.error = ("interest должен быть меньше amount (amount — весь платёж, interest — его часть). Ничего не записано; "
                         "ничего не удаляй и не переписывай — спроси пользователя, сколько из платежа проценты")
            return out
        principal = amount - interest
        if direction == "down" and action in {"creditor_forgave", "i_forgave"}:
            principal = min(principal, max(cp.total, 0.0))
        elif direction == "down" and not _flag(it.get("existed_before")):
            owed = max(cp.total, 0.0) if loan is None else loan.left
            diff = principal - owed
            if diff >= 1 and not _flag(it.get("keep_overpay")):
                if side == "debt":
                    q = (f"{name}: qarz {_m(owed)}, siz {_m(amount)} to'ladingiz. Farq {_m(diff)} — bu:" if uz
                         else f"Долг {name} — {_m(owed)}, а заплачено {_m(amount)}. Разница {_m(diff)} — это:")
                    opts = (["Foiz / komissiya", "Ortiqcha to'lov", "Summa boshqa"] if uz
                            else ["Проценты / комиссия", "Переплата", "Сумма другая"])
                else:
                    q = (f"{name} {_m(owed)} qarz edi, {_m(amount)} qaytardi. Farq {_m(diff)} — bu:" if uz
                         else f"{name} был должен {_m(owed)}, а вернул {_m(amount)}. Разница {_m(diff)} — это:")
                    opts = (["Foiz / sovg'a (kirim)", "Endi men unga qarzman", "Summa boshqa"] if uz
                            else ["Проценты / подарок (доход)", "Теперь я ему должен", "Сумма другая"])
                out.ask = _ask(q, opts, f"{again}: проценты → interest={diff:.0f} (amount тот же); переплата / «теперь я должен» → "
                                        "keep_overpay=true; «сумма другая» → спроси сумму.")
                return out

        # ---- счёт
        acc: str | None = None
        if action in MONEY_ACTIONS:
            given = str(it.get("account") or "").strip().lower()
            given = given if given in {"card", "cash"} else None
            if given and (said in {given, "both"} or fin.is_institution(name)):
                acc = given
            elif said in {"card", "cash"}:
                acc = said
            elif fin.is_institution(name):
                acc = "card"
            else:
                q = {"borrow": (f"{name} {_m(amount)} berdi — pul qayerga tushdi?", f"{name} дал {_m(amount)} — куда пришли деньги?"),
                     "repay": (f"{name}ga {_m(amount)} qaytarish — kartadanmi yoki naqdmi?", f"Возврат {name} {_m(amount)} — с карты или наличными?"),
                     "lend": (f"{name}ga {_m(amount)} berdingiz — kartadanmi yoki naqdmi?", f"Дали {name} {_m(amount)} — с карты или наличными?"),
                     "collect": (f"{name} {_m(amount)} qaytardi — kartagami yoki naqdmi?", f"{name} вернул {_m(amount)} — на карту или наличными?")}[action]
                out.ask = _ask(q[0] if uz else q[1], ["💳 Karta", "💵 Naqd"] if uz else ["💳 Карта", "💵 Наличные"],
                               f"{again}: account = card или cash по ответу.")
                return out
        elif action == "buy_on_credit":
            acc = "card"

        # ---- срок (займы у банков — всегда со сроком; у людей — если назвал)
        due: date | None = None
        if direction == "up":
            due = parse_due(it.get("due_date"), today) if (not text or _DUE_HINT.search(text)) else None
            if due is None and action in {"borrow", "buy_on_credit"} and fin.is_institution(name) and not _flag(it.get("no_due")):
                month = _plus_month(today)
                out.ask = _ask(f"{name} {_m(amount)} — qachongacha qaytarish kerak?" if uz else f"{name} {_m(amount)} — до какого числа вернуть?",
                               [f"{'Bir oydan keyin' if uz else 'Через месяц'} — {month:%d.%m}", "Muddatsiz" if uz else "Без срока"],
                               f"{again}: due_date = дата из ответа (YYYY-MM-DD, «26.10» тоже можно); «без срока» → no_due=true.")
                return out

        # ---- записываем
        if direction == "up":
            new_seq += 1
            left = principal
            if cp.credit > 0:
                use = min(cp.credit, left)
                cp.credit -= use
                left -= use
            src = {"borrow": "debt", "buy_on_credit": "debt", "lend": acc, "owe_existing": fin.INIT, "owed_existing": fin.INIT}[action]
            dst = {"borrow": acc, "buy_on_credit": acc, "lend": "lent", "owe_existing": "debt", "owed_existing": "lent"}[action]
            add_row({"entry_type": "expense", "amount": principal, "category": cats.TRANSFER_KEY,
                     "note": fin.note_with_transfer(name, src, dst, due=due), "entry_date": day.isoformat()})
            cp.tranches.append(fin.Tranche(id=f"new{new_seq}", side=side, date=day, amount=principal, left=left, due=due, account=acc or fin.INIT))
            if action == "borrow":
                moved_in[acc] = moved_in.get(acc, 0.0) + principal
                out.lines.append(f"Займ: {name} дал {_m(principal)} → {_acc_word(acc, False)}, {_due_label(due, False)}")
            elif action == "lend":
                out.lines.append(f"Дал в долг {name} {_m(principal)} {_acc_word(acc, False, case='from')}" + (f", вернуть {_due_label(due, False)}" if due else ""))
            elif action == "buy_on_credit":
                what = str(it.get("note") or "").strip() or name
                category = cats.normalize(str(it.get("category") or ""), "expense", note=what)
                add_row({"entry_type": "expense", "amount": principal, "category": category,
                         "note": fin.note_with_bucket(what, "card"), "entry_date": day.isoformat()})
                out.lines.append(f"Покупка в рассрочку: {what} {_m(principal)} — расход «{cats.label(category, 'ru', with_emoji=False)}», "
                                 f"долг {name} +{_m(principal)} ({_due_label(due, False)}); с карты деньги не ушли")
            else:
                out.lines.append(("Старый долг: я должен " if action == "owe_existing" else "Старый долг: мне должен ")
                                 + f"{name} {_m(principal)}" + (f", {_due_label(due, False)}" if due else ""))
        else:
            if _flag(it.get("existed_before")) and principal > max(cp.total, 0.0):
                missing = principal - max(cp.total, 0.0)
                add_row({"entry_type": "expense", "amount": missing, "category": cats.TRANSFER_KEY,
                         "note": fin.note_with_transfer(name, fin.INIT, side), "entry_date": day.isoformat()})
                new_seq += 1
                cp.tranches.append(fin.Tranche(id=f"new{new_seq}", side=side, date=day, amount=missing, left=missing, account=fin.INIT))
            targets = [loan.id] if loan is not None else []
            parts = fin._allocate(cp, principal, targets)
            real = [t.id for t, _ in parts if not t.id.startswith("new") and t.id != "base"]
            if action == "repay":
                src, dst = acc, "debt"
            elif action == "collect":
                src, dst = "lent", acc
            elif action == "creditor_forgave":
                src, dst = "debt", fin.INIT
            else:
                src, dst = "lent", fin.INIT
            if principal >= 1:
                add_row({"entry_type": "expense", "amount": principal, "category": cats.TRANSFER_KEY,
                         "note": fin.note_with_transfer(name, src, dst, targets=real), "entry_date": day.isoformat()})
            closed_loans = [t for t, _ in parts if not t.open]
            partial = [t for t, _ in parts if t.open]
            if action == "repay":
                moved_out[acc] = moved_out.get(acc, 0.0) + principal
                line = f"Погашено {name} {_m(principal)} {_acc_word(acc, False, case='from')}"
            elif action == "collect":
                line = f"{name} вернул {_m(principal)} → {_acc_word(acc, False)}"
            elif action == "creditor_forgave":
                line = f"{name} простил/списал долг {_m(principal)}"
            else:
                line = f"Списал долг {name} {_m(principal)} (не вернёт)"
            if len(parts) > 1 or (parts and len(cp.tranches) > 1):
                if closed_loans:
                    line += "; закрыт займ " + ", ".join(f"{_m(t.amount)} ({_due_label(t.due, False)})" for t in closed_loans)
                if partial:
                    line += "; по займу " + ", ".join(f"{_due_label(t.due, False)} осталось {_m(t.left)}" for t in partial)
            out.lines.append(line)
            if interest >= 1:
                if side == "debt":
                    add_row({"entry_type": "expense", "amount": interest, "category": "debt",
                             "note": fin.note_with_bucket(f"Проценты {name}", acc or "card"), "entry_date": day.isoformat()})
                    moved_out[acc] = moved_out.get(acc, 0.0) + interest
                    out.lines.append(f"Проценты/комиссия {name} {_m(interest)} — расход «Долги/кредит»")
                else:
                    add_row({"entry_type": "income", "amount": interest, "category": "other_in",
                             "note": fin.note_with_bucket(f"Проценты от {name}", acc or "card"), "entry_date": day.isoformat()})
                    out.lines.append(f"Сверх долга {name} {_m(interest)} — доход")
        if cp not in touched:
            touched.append(cp)

    # ---- перекредитование: взял у одного, отдал другому — что осталось на счёте
    for acc in ("card", "cash"):
        got, gave = moved_in.get(acc, 0.0), moved_out.get(acc, 0.0)
        if got >= 1 and gave >= 1:
            rest = got - gave
            where = "на карте" if acc == "card" else "в наличных"
            if rest >= 1:
                out.lines.append(f"Из займов {_m(got)} на погашение ушло {_m(gave)} — {where} осталось {_m(rest)}")
            elif rest <= -1:
                out.lines.append(f"На погашение добавлено {_m(-rest)} своих денег ({where.replace('на карте', 'с карты').replace('в наличных', 'из наличных')})")
            else:
                out.lines.append("Займ целиком ушёл на погашение — на счёте ничего не осталось")
    for acc in ("card", "cash"):
        if after.get(acc, 0.0) < 0 <= out.before.get(acc, 0.0) or (after.get(acc, 0.0) < 0 and after[acc] < out.before.get(acc, 0.0)):
            word = "карте" if acc == "card" else "наличных"
            out.warnings.append(f"По учёту на {word} получается {_m(after[acc])} — возможно, часть была с другого счёта "
                                f"или баланс устарел: напишите «на {'карте' if acc == 'card' else 'руках'} сейчас …»")
    out.after = after
    out.people = touched
    return out


def _plus_month(d: date) -> date:
    import calendar

    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))
