"""AI-инсайты: считаем числовые тренды и просим Gemini объяснить их человеческим языком."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

from .ai import AIService

logger = logging.getLogger(__name__)


def _sum_finance(entries: Iterable[dict[str, Any]], *, kind: str) -> Decimal:
    total = Decimal("0")
    for e in entries:
        if e.get("entry_type") != kind:
            continue
        note = str(e.get("note") or "").strip().lower()
        if note.startswith("[x:"):
            continue
        total += Decimal(str(e.get("amount") or 0))
    return total


def _category_totals(entries: Iterable[dict[str, Any]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for e in entries:
        if e.get("entry_type") != "expense":
            continue
        note = str(e.get("note") or "").strip().lower()
        if note.startswith("[x:"):
            continue
        cat = str(e.get("category") or "—").strip() or "—"
        totals[cat] = totals.get(cat, Decimal("0")) + Decimal(str(e.get("amount") or 0))
    return totals


def _filter_by_range(entries: Iterable[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        d = e.get("entry_date")
        if not d:
            continue
        try:
            dd = date.fromisoformat(str(d))
        except Exception:
            continue
        if start <= dd <= end:
            out.append(e)
    return out


def compute_trend(
    finance_entries: list[dict[str, Any]],
    checkins: list[dict[str, Any]],
    habit_logs: list[dict[str, Any]],
    *,
    end_date: date,
    days: int,
) -> dict[str, Any]:
    """Считает базовые метрики и сравнивает с предыдущим окном такой же длины."""
    cur_start = end_date - timedelta(days=days - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    cur_fin = _filter_by_range(finance_entries, cur_start, end_date)
    prev_fin = _filter_by_range(finance_entries, prev_start, prev_end)

    cur_inc = _sum_finance(cur_fin, kind="income")
    cur_exp = _sum_finance(cur_fin, kind="expense")
    prev_inc = _sum_finance(prev_fin, kind="income")
    prev_exp = _sum_finance(prev_fin, kind="expense")

    def _pct(now: Decimal, prev: Decimal) -> float | None:
        if prev <= 0:
            return None
        return float((now - prev) / prev * 100)

    cur_cats = _category_totals(cur_fin)
    prev_cats = _category_totals(prev_fin)
    cat_jumps: list[tuple[str, float, float, float]] = []  # (name, prev, now, pct)
    for cat, now_val in cur_cats.items():
        prev_val = prev_cats.get(cat, Decimal("0"))
        if prev_val > 0:
            pct = _pct(now_val, prev_val)
            if pct is not None and abs(pct) >= 25:
                cat_jumps.append((cat, float(prev_val), float(now_val), pct))
        elif now_val > 0:
            cat_jumps.append((cat, 0.0, float(now_val), 100.0))
    cat_jumps.sort(key=lambda x: abs(x[3]), reverse=True)

    cur_moods = [c.get("mood") for c in checkins if c.get("mood") is not None]
    cur_energy = [c.get("energy") for c in checkins if c.get("energy") is not None]
    avg_mood = sum(map(float, cur_moods)) / len(cur_moods) if cur_moods else None
    avg_energy = sum(map(float, cur_energy)) / len(cur_energy) if cur_energy else None

    completed_logs = sum(1 for lg in habit_logs if lg.get("completed"))

    return {
        "period_days": days,
        "income_now": float(cur_inc),
        "expense_now": float(cur_exp),
        "income_prev": float(prev_inc),
        "expense_prev": float(prev_exp),
        "income_pct": _pct(cur_inc, prev_inc),
        "expense_pct": _pct(cur_exp, prev_exp),
        "category_jumps": cat_jumps[:3],
        "top_categories": sorted(((k, float(v)) for k, v in cur_cats.items()), key=lambda x: x[1], reverse=True)[:3],
        "avg_mood": avg_mood,
        "avg_energy": avg_energy,
        "checkin_count": len(checkins),
        "habit_completions": completed_logs,
    }


def build_insight_prompt(trend: dict[str, Any], *, currency: str, lang: str) -> str:
    """Готовим компактный JSON-context для модели и инструкцию."""
    days = trend["period_days"]

    def _money(v: float) -> str:
        return f"{int(v):,}".replace(",", " ")

    pieces: list[str] = []
    pieces.append(f"Период: {days} дней. Валюта: {currency}.")
    pieces.append(f"Доход: {_money(trend['income_now'])} (было {_money(trend['income_prev'])})")
    pieces.append(f"Расход: {_money(trend['expense_now'])} (было {_money(trend['expense_prev'])})")
    if trend.get("income_pct") is not None:
        pieces.append(f"Изм. дохода: {trend['income_pct']:+.0f}%")
    if trend.get("expense_pct") is not None:
        pieces.append(f"Изм. расхода: {trend['expense_pct']:+.0f}%")
    if trend["category_jumps"]:
        jumps_str = "; ".join(f"{c} {p:+.0f}% (с {_money(o)} до {_money(n)})" for c, o, n, p in trend["category_jumps"])
        pieces.append(f"Скачки по категориям: {jumps_str}")
    if trend["top_categories"]:
        top_str = ", ".join(f"{c}: {_money(v)}" for c, v in trend["top_categories"])
        pieces.append(f"Топ расходов: {top_str}")
    if trend.get("avg_mood") is not None:
        pieces.append(f"Среднее настроение: {trend['avg_mood']:.1f}/10")
    if trend.get("avg_energy") is not None:
        pieces.append(f"Средняя энергия: {trend['avg_energy']:.1f}/10")
    pieces.append(f"Чек-инов за период: {trend['checkin_count']}")
    pieces.append(f"Привычек выполнено: {trend['habit_completions']}")
    context = " | ".join(pieces)

    lang_name = "узбекском (латиница)" if lang == "uz" else "русском"
    return (
        "Ты — личный аналитик пользователя в боте по саморазвитию.\n"
        "Дай короткий конкретный инсайт по данным за период.\n"
        "Формат:\n"
        "1) 1 предложение про главный тренд (рост/падение, скачок категории).\n"
        "2) 1 предложение — конкретный совет на следующую неделю.\n"
        f"Отвечай на {lang_name} языке. Будь дружелюбен, без воды, без хэштегов, без эмодзи в начале строк. Максимум 3 предложения всего.\n\n"
        f"Данные: {context}"
    )


def generate_insight(
    ai: AIService,
    trend: dict[str, Any],
    *,
    currency: str,
    lang: str,
) -> str | None:
    """Возвращает 2-3 предложения от Gemini или None если AI недоступен."""
    if trend["income_now"] == 0 and trend["expense_now"] == 0 and trend["checkin_count"] == 0 and trend["habit_completions"] == 0:
        return None
    prompt = build_insight_prompt(trend, currency=currency, lang=lang)
    try:
        text = ai._generate_content(
            model=ai.text_model,
            parts=[{"text": prompt}],
            temperature=0.4,
        )
        return text.strip() or None
    except Exception as exc:
        logger.warning("AI insight generation failed: %s", exc)
        return None


__all__ = ["compute_trend", "build_insight_prompt", "generate_insight"]
