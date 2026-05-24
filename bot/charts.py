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
from matplotlib.colors import ListedColormap

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
        title = "Доходы и расходы" if lang != "uz" else "Daromad va xarajatlar"

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


# ---- 2) Привычки: heatmap последних N дней ----
def habits_heatmap(
    habit_logs: Iterable[dict[str, Any]],
    habits: Iterable[dict[str, Any]],
    *,
    end_date: date,
    days: int = 30,
    lang: str = "ru",
) -> bytes | None:
    """Матрица привычки × день. Зелёная клетка = выполнено."""
    habits_list = [h for h in habits if h.get("active", True) is not False]
    if not habits_list:
        return None

    habits_list.sort(key=lambda h: str(h.get("created_at") or ""))
    habit_id_to_idx = {str(h["id"]): i for i, h in enumerate(habits_list)}
    dates = [end_date - timedelta(days=days - 1 - i) for i in range(days)]
    date_to_idx = {d.isoformat(): i for i, d in enumerate(dates)}

    matrix = np.zeros((len(habits_list), days), dtype=int)
    any_log = False
    for log in habit_logs:
        h_idx = habit_id_to_idx.get(str(log.get("habit_id")))
        d_idx = date_to_idx.get(str(log.get("log_date")))
        if h_idx is None or d_idx is None:
            continue
        if log.get("completed"):
            matrix[h_idx, d_idx] = 1
            any_log = True

    if not any_log:
        return None

    fig, ax = plt.subplots(figsize=(max(7, days * 0.28), max(2, len(habits_list) * 0.42 + 1)), dpi=140)
    cmap = ListedColormap([_COLOR_MISS, _COLOR_DONE])
    ax.imshow(matrix, aspect="auto", cmap=cmap, vmin=0, vmax=1)

    # Подписи строк (имена привычек, обрезка)
    row_labels = []
    for h in habits_list:
        name = str(h.get("name") or "")
        if len(name) > 22:
            name = name[:20] + "…"
        row_labels.append(name)
    ax.set_yticks(range(len(habits_list)))
    ax.set_yticklabels(row_labels, fontsize=9, color=_COLOR_TEXT)

    step = max(1, days // 7)
    ax.set_xticks(list(range(0, days, step)))
    ax.set_xticklabels([dates[i].strftime("%d.%m") for i in range(0, days, step)], fontsize=8, color=_COLOR_TEXT)

    # Сетка между клетками
    ax.set_xticks(np.arange(-0.5, days, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(habits_list), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", length=0)
    for s in ax.spines.values():
        s.set_visible(False)

    title = "Привычки: последние 30 дней" if lang != "uz" else "Odatlar: soʻnggi 30 kun"
    ax.set_title(title, fontsize=11, pad=8, color=_COLOR_TEXT)

    fig.tight_layout()
    return _fig_to_bytes(fig)


# ---- 3) Калории: тренд по дням ----
def calorie_trend_chart(
    calorie_logs: Iterable[dict[str, Any]],
    *,
    end_date: date,
    days: int = 14,
    target: int | None = None,
    tz_offset_hours: float = 5.0,  # Asia/Tashkent default
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
        local = dt + timedelta(hours=tz_offset_hours)
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
                   label=("Цель" if lang != "uz" else "Maqsad"))
        ax.legend(loc="upper right", frameon=False, fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 7)))
    ax.set_title(("Калории по дням" if lang != "uz" else "Kunlik kaloriya"), fontsize=11, pad=10)
    fig.tight_layout()
    return _fig_to_bytes(fig)


# ---- 4) Mood / Energy ----
def mood_energy_chart(
    checkins: Iterable[dict[str, Any]],
    *,
    end_date: date,
    days: int = 14,
    lang: str = "ru",
) -> bytes | None:
    by_day: dict[str, tuple[float | None, float | None]] = {}
    for c in checkins:
        d = c.get("checkin_date")
        if not d:
            continue
        m = c.get("mood")
        e = c.get("energy")
        by_day[str(d)] = (
            float(m) if m is not None else None,
            float(e) if e is not None else None,
        )

    dates = [end_date - timedelta(days=days - 1 - i) for i in range(days)]
    mood_vals = [by_day.get(d.isoformat(), (None, None))[0] for d in dates]
    energy_vals = [by_day.get(d.isoformat(), (None, None))[1] for d in dates]

    if not any(v is not None for v in mood_vals + energy_vals):
        return None

    fig, ax = _setup_fig(figsize=(max(7, days * 0.38), 3.0))
    mood_label = "Настроение" if lang != "uz" else "Kayfiyat"
    energy_label = "Энергия" if lang != "uz" else "Energiya"
    # matplotlib умеет показывать None как разрывы, если использовать masked array
    mood_masked = np.array([v if v is not None else np.nan for v in mood_vals], dtype=float)
    energy_masked = np.array([v if v is not None else np.nan for v in energy_vals], dtype=float)
    ax.plot(dates, mood_masked, marker="o", linewidth=2.0, color="#9b59b6", markersize=4, label=mood_label, zorder=3)
    ax.plot(dates, energy_masked, marker="s", linewidth=2.0, color="#f39c12", markersize=4, label=energy_label, zorder=3)
    ax.set_ylim(0, 10.5)
    ax.set_yticks(range(0, 11, 2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 7)))
    ax.set_title(("Самочувствие" if lang != "uz" else "Kayfiyat va energiya"), fontsize=11, pad=10)
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    fig.tight_layout()
    return _fig_to_bytes(fig)


# ---- 5) Категории расходов: горизонтальный bar ----
def expense_categories_chart(
    finance_entries: Iterable[dict[str, Any]],
    *,
    top_n: int = 7,
    currency: str = "UZS",
    lang: str = "ru",
) -> bytes | None:
    totals: dict[str, Decimal] = {}
    for entry in finance_entries:
        if entry.get("entry_type") != "expense":
            continue
        note = str(entry.get("note") or "").strip().lower()
        if note.startswith("[x:"):
            continue
        category = str(entry.get("category") or "—").strip() or "—"
        amount = Decimal(str(entry.get("amount") or 0))
        totals[category] = totals.get(category, Decimal("0")) + amount

    if not totals:
        return None

    items = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:top_n]
    names = [n for n, _ in items]
    values = [float(v) for _, v in items]

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
                f"  {int(val):,}".replace(",", " "),
                va="center", ha="left", fontsize=8, color=_COLOR_TEXT)
    title = ("Топ расходов по категориям" if lang != "uz" else "Asosiy xarajat toifalari")
    ax.set_title(f"{title} ({currency})", fontsize=11, pad=10, color=_COLOR_TEXT)
    fig.tight_layout()
    return _fig_to_bytes(fig)


__all__ = [
    "finance_daily_chart",
    "habits_heatmap",
    "calorie_trend_chart",
    "mood_energy_chart",
    "expense_categories_chart",
]
