from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from . import charts, insights
from .ai import AIService

logger = logging.getLogger(__name__)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode("ascii")


@dataclass
class ReportBundle:
    """Связка готового отчёта: текст для Telegram + опциональный PNG-график."""
    text: str
    chart: bytes | None = None
    insight: str | None = None


def build_report_bundle(
    payload: dict[str, Any],
    *,
    currency: str = "UZS",
    lang: str = "ru",
    ai: AIService | None = None,
    title_prefix: str | None = None,
    full_finance_history: list[dict[str, Any]] | None = None,
) -> ReportBundle:
    """Готовит расширенный отчёт: цифры + AI-инсайт + bar-chart расходов/доходов.

    `full_finance_history` — если передан, по нему считаются week-over-week изменения
    (т.е. история должна включать предыдущий период). Если не передан — берём только
    данные за текущий период из payload['finance_entries'].
    """
    summary_text = build_weekly_summary(payload, currency=currency, lang=lang)
    if title_prefix:
        summary_text = f"{title_prefix}\n\n{summary_text}"

    # AI-инсайт (опционально)
    insight: str | None = None
    if ai is not None:
        end_date = payload.get("end_date")
        start_date = payload.get("start_date")
        if isinstance(end_date, date) and isinstance(start_date, date):
            days = (end_date - start_date).days + 1
            trend = insights.compute_trend(
                full_finance_history or payload.get("finance_entries", []),
                payload.get("checkins", []),
                payload.get("habit_logs", []),
                end_date=end_date,
                days=days,
            )
            insight = insights.generate_insight(ai, trend, currency=currency, lang=lang)

    # График — bar расходов/доходов по дням
    chart_bytes: bytes | None = None
    try:
        start_date = payload.get("start_date")
        end_date = payload.get("end_date")
        if isinstance(start_date, date) and isinstance(end_date, date):
            chart_bytes = charts.finance_daily_chart(
                payload.get("finance_entries", []),
                start_date=start_date,
                end_date=end_date,
                currency=currency,
                lang=lang,
            )
    except Exception as exc:
        logger.warning("finance_daily_chart failed: %s", exc)

    return ReportBundle(text=summary_text, chart=chart_bytes, insight=insight)


def build_weekly_summary(payload: dict[str, Any], currency: str = "UZS", lang: str = "ru") -> str:
    finance_entries = payload.get("finance_entries", [])
    checkins = payload.get("checkins", [])
    habit_logs = payload.get("habit_logs", [])
    habits = payload.get("habits", [])
    calorie_logs = payload.get("calorie_logs", [])

    clean_finance_entries = [
        item
        for item in finance_entries
        if not str(item.get("note") or "").strip().lower().startswith("[x:")
    ]

    income = sum(_to_float(item.get("amount")) for item in clean_finance_entries if item.get("entry_type") == "income")
    expense = sum(_to_float(item.get("amount")) for item in clean_finance_entries if item.get("entry_type") == "expense")
    net = income - expense

    mood_values = [int(item["mood"]) for item in checkins if item.get("mood") is not None]
    energy_values = [int(item["energy"]) for item in checkins if item.get("energy") is not None]
    avg_mood = round(mean(mood_values), 1) if mood_values else None
    avg_energy = round(mean(energy_values), 1) if energy_values else None

    calories = [_to_float(item.get("calories")) for item in calorie_logs if item.get("calories") is not None]
    avg_calories = round(mean(calories), 0) if calories else None

    total_habits = len(habits)
    completed_logs = len(habit_logs)

    period = f"{payload['start_date']} - {payload['end_date']}"

    def _money(v: float) -> str:
        return f"{int(v):,}".replace(",", " ")

    if lang == "uz":
        lines = [
            f"Davr: {period}",
            f"Daromad: {_money(income)} {currency}",
            f"Xarajat: {_money(expense)} {currency}",
            f"Balans: {_money(net)} {currency}",
            f"Belgilangan kunlar: {len(checkins)}",
            f"Odatlar: {total_habits}, bajarilgan: {completed_logs}",
            f"Yozilgan ovqatlanish: {len(calorie_logs)}",
        ]
        if avg_mood is not None:
            lines.append(f"Oʻrtacha kayfiyat: {avg_mood}/10")
        if avg_energy is not None:
            lines.append(f"Oʻrtacha energiya: {avg_energy}/10")
        if avg_calories is not None:
            lines.append(f"Oʻrtacha kaloriya (foto): {avg_calories:.0f} kkal")
    else:
        lines = [
            f"Период: {period}",
            f"Доход: {_money(income)} {currency}",
            f"Расход: {_money(expense)} {currency}",
            f"Баланс: {_money(net)} {currency}",
            f"Дневных отметок: {len(checkins)}",
            f"Привычек: {total_habits}, выполнений: {completed_logs}",
            f"Приемов пищи в дневнике: {len(calorie_logs)}",
        ]
        if avg_mood is not None:
            lines.append(f"Среднее настроение: {avg_mood}/10")
        if avg_energy is not None:
            lines.append(f"Средняя энергия: {avg_energy}/10")
        if avg_calories is not None:
            lines.append(f"Средние калории по фото: {avg_calories:.0f} ккал")

    return "\n".join(lines)


def export_weekly_csv(payload: dict[str, Any], output_path: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["section", "date", "type", "amount", "category", "note", "extra"])

        for entry in payload.get("finance_entries", []):
            writer.writerow(
                [
                    "finance",
                    entry.get("entry_date", ""),
                    entry.get("entry_type", ""),
                    entry.get("amount", ""),
                    entry.get("category", ""),
                    entry.get("note", ""),
                    entry.get("source", ""),
                ]
            )

        for item in payload.get("checkins", []):
            writer.writerow(
                [
                    "checkin",
                    item.get("checkin_date", ""),
                    "",
                    "",
                    "",
                    item.get("note", ""),
                    f"mood={item.get('mood', '')};energy={item.get('energy', '')};weight={item.get('weight', '')}",
                ]
            )

        for item in payload.get("habit_logs", []):
            writer.writerow(
                [
                    "habit",
                    item.get("log_date", ""),
                    "done" if item.get("completed") else "skip",
                    "",
                    item.get("habit_id", ""),
                    item.get("note", ""),
                    "",
                ]
            )

        for item in payload.get("calorie_logs", []):
            writer.writerow(
                [
                    "calories",
                    str(item.get("created_at", ""))[:10],
                    "",
                    item.get("calories", ""),
                    "",
                    item.get("meal_desc", ""),
                    f"p={item.get('protein', '')};f={item.get('fat', '')};c={item.get('carbs', '')}",
                ]
            )

    return output


def export_weekly_pdf(
    payload: dict[str, Any],
    summary_text: str,
    output_path: str | Path,
    currency: str = "UZS",
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    pdf = canvas.Canvas(str(output), pagesize=A4)
    width, height = A4

    y = height - 40
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(40, y, "Weekly Report")
    y -= 20

    pdf.setFont("Helvetica", 10)
    lines = [
        f"Period: {payload['start_date']} - {payload['end_date']}",
        f"Currency: {currency}",
        "",
    ] + [_ascii(line) for line in summary_text.splitlines()]

    for line in lines:
        pdf.drawString(40, y, line[:110])
        y -= 14
        if y < 50:
            pdf.showPage()
            pdf.setFont("Helvetica", 10)
            y = height - 40

    pdf.save()
    return output
