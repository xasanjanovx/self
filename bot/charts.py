"""Генерация PNG-графиков через matplotlib (headless)."""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")  # headless для серверного окружения
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

logger = logging.getLogger(__name__)


# ---- Стилизация ----
_COLOR_INCOME = "#27ae60"      # зелёный
_COLOR_EXPENSE = "#e74c3c"     # красный
_COLOR_GRID = "#ecf0f1"
_COLOR_PRIMARY = "#3498db"
_COLOR_DONE = "#27ae60"
_COLOR_MISS = "#ecf0f1"
_COLOR_TEXT = "#2c3e50"


def _setup_fig(figsize: tuple[float, float] = (8, 3.2), dpi: int = 140):
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#bdc3c7")
    ax.spines["bottom"].set_color("#bdc3c7")
    ax.tick_params(colors=_COLOR_TEXT, labelsize=9)
    ax.yaxis.label.set_color(_COLOR_TEXT)
    ax.xaxis.label.set_color(_COLOR_TEXT)
    ax.title.set_color(_COLOR_TEXT)
    ax.grid(True, axis="y", color=_COLOR_GRID, linewidth=0.8, zorder=0)
    return fig, ax


def _fig_to_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ---- 1) Финансы: график расходов и доходов по дням ----
def finance_daily_chart(
    finance_entries: Iterable[dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
    currency: str = "UZS",
    title: str | None = None,
    lang: str = "ru",
) -> bytes | None:
    """Группирует доход/расход по дням и строит стилизованный bar chart."""
    days = (end_date - start_date).days + 1
    if days <= 0:
        return None

    dates = [start_date + timedelta(days=i) for i in range(days)]
    income = {d.isoformat(): Decimal("0") for d in dates}
    expense = {d.isoformat(): Decimal("0") for d in dates}

    has_data = False
    for entry in finance_entries:
        note = str(entry.get("note") or "").strip().lower()
        if note.startswith("[x:"):
            continue
        ed = entry.get("entry_date")
        if not ed:
            continue
        amount = Decimal(str(entry.get("amount") or 0))
        if entry.get("entry_type") == "income":
            if ed in income:
                income[ed] += amount
                has_data = True
        else:
            if ed in expense:
                expense[ed] += amount
                has_data = True

    if not has_data:
        return None

    income_vals = [float(income[d.isoformat()]) for d in dates]
    expense_vals = [float(expense[d.isoformat()]) for d in dates]

    if not title:
        title = {"uz": "Daromad va xarajatlar", "en": "Income and expenses"}.get(lang, "Доходы и расходы")

    fig, ax = _setup_fig(figsize=(max(7, days * 0.32), 3.4))
    x = np.arange(days)
    width = 0.4
    ax.bar(x - width / 2, income_vals, width=width, color=_COLOR_INCOME, label="+", zorder=2)
    ax.bar(x + width / 2, [-v for v in expense_vals], width=width, color=_COLOR_EXPENSE, label="-", zorder=2)
    ax.axhline(0, color="#bdc3c7", linewidth=0.7, zorder=1)

    # Подписи дат — каждые ~7 шагов
    step = max(1, days // 8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([d.strftime("%d.%m") for d in dates[::step]], rotation=0)

    # Y-формат с пробелами вместо разделителей
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(abs(v)):,}".replace(",", " ")))
    ax.set_title(f"{title} ({currency})", fontsize=11, pad=10)
    ax.legend(loc="upper left", frameon=False, fontsize=9)

    fig.tight_layout()
    return _fig_to_bytes(fig)


# ---- 2) Калории по дням ----
def calorie_trend_chart(
    calorie_logs: Iterable[dict[str, Any]],
    *,
    end_date: date,
    days: int = 14,
    target: int | None = None,
    tz: Any = None,
    lang: str = "ru",
) -> bytes | None:
    """Линейный график калорий по дням (по локальной дате юзера)."""
    by_day: dict[str, float] = {}
    for log in calorie_logs:
        created = log.get("created_at")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(tz) if tz is not None else dt
        key = local.date().isoformat()
        kcal = log.get("calories")
        if kcal is None:
            continue
        by_day[key] = by_day.get(key, 0.0) + float(kcal)

    dates = [end_date - timedelta(days=days - 1 - i) for i in range(days)]
    values = [by_day.get(d.isoformat(), 0.0) for d in dates]
    if not any(v > 0 for v in values):
        return None

    fig, ax = _setup_fig(figsize=(max(7, days * 0.38), 3.0))
    ax.plot(dates, values, marker="o", linewidth=2.0, color=_COLOR_PRIMARY, markersize=4, zorder=3)
    ax.fill_between(dates, values, color=_COLOR_PRIMARY, alpha=0.12, zorder=2)
    if target and target > 0:
        ax.axhline(target, color="#f39c12", linestyle="--", linewidth=1.2,
                   label={"uz": "Maqsad", "en": "Goal"}.get(lang, "Цель"))
        ax.legend(loc="upper right", frameon=False, fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 7)))
    ax.set_title({"uz": "Kunlik kaloriya", "en": "Calories per day"}.get(lang, "Калории по дням"), fontsize=11, pad=10)
    fig.tight_layout()
    return _fig_to_bytes(fig)


# ---- 3) Расходы по категориям ----
def expense_categories_chart(
    items: list[tuple[str, float]],
    *,
    top_n: int = 10,
    currency: str = "UZS",
    lang: str = "ru",
    title: str | None = None,
) -> bytes | None:
    """Горизонтальный bar chart: [(название категории, сумма)] — уже агрегировано."""
    items = [(str(n), float(v)) for n, v in items if float(v) > 0][:top_n]
    if not items:
        return None
    names = [n for n, _ in items]
    values = [v for _, v in items]
    total = sum(values) or 1.0

    fig, ax = plt.subplots(figsize=(7, max(2.4, 0.5 * len(items) + 1)), dpi=140)
    bars = ax.barh(range(len(items)), values, color=_COLOR_EXPENSE, alpha=0.85, zorder=2)
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels(names, fontsize=9, color=_COLOR_TEXT)
    ax.invert_yaxis()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(colors=_COLOR_TEXT, labelsize=9)
    ax.grid(True, axis="x", color=_COLOR_GRID, linewidth=0.8, zorder=0)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}".replace(",", " ")))
    for bar, val in zip(bars, values):
        ax.text(val, bar.get_y() + bar.get_height() / 2,
                f"  {int(val):,} ({val / total * 100:.0f}%)".replace(",", " "),
                va="center", ha="left", fontsize=8, color=_COLOR_TEXT)
    ax.set_xlim(0, max(values) * 1.35)
    title = title or {"uz": "Xarajatlar toifalar bo'yicha", "en": "Expenses by category"}.get(lang, "Расходы по категориям")
    ax.set_title(f"{title} ({currency})", fontsize=11, pad=10, color=_COLOR_TEXT)
    fig.tight_layout()
    return _fig_to_bytes(fig)


__all__ = ["finance_daily_chart", "calorie_trend_chart", "expense_categories_chart"]
