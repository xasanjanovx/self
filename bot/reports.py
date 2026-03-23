from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode("ascii")


def build_weekly_summary(payload: dict[str, Any], currency: str = "UZS") -> str:
    finance_entries = payload.get("finance_entries", [])
    checkins = payload.get("checkins", [])
    habit_logs = payload.get("habit_logs", [])
    habits = payload.get("habits", [])
    calorie_logs = payload.get("calorie_logs", [])

    income = sum(_to_float(item.get("amount")) for item in finance_entries if item.get("entry_type") == "income")
    expense = sum(_to_float(item.get("amount")) for item in finance_entries if item.get("entry_type") == "expense")
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

    lines = [
        f"Период: {period}",
        f"Доход: {income:,.0f} {currency}",
        f"Расход: {expense:,.0f} {currency}",
        f"Баланс: {net:,.0f} {currency}",
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
