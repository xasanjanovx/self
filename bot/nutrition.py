"""Питание: расчёт плана КБЖУ и агрегаты по дневнику (без БД и Telegram)."""
from __future__ import annotations

import re
from typing import Any

from .ai import CalorieEstimate


def goal_title(mode: str, lang: str = "ru") -> str:
    ru = {"loss": "Снижение веса", "maintain": "Поддержание", "gain": "Набор веса", "muscle": "Набор мышц", "custom": "Свой план"}
    uz = {"loss": "Vazn kamaytirish", "maintain": "Vaznni ushlab turish", "gain": "Vazn yig'ish", "muscle": "Mushak yig'ish", "custom": "Shaxsiy reja"}
    labels = uz if lang == "uz" else ru
    return labels.get(mode, labels["maintain"])


def parse_profile(text: str) -> tuple[float, float, int] | None:
    raw = [part.strip() for part in re.split(r"[;,/ ]+", text.strip()) if part.strip()]
    if len(raw) != 3:
        return None
    try:
        weight = float(raw[0].replace(",", "."))
        height = float(raw[1].replace(",", "."))
        age = int(float(raw[2].replace(",", ".")))
    except Exception:
        return None
    if not (25 <= weight <= 350) or not (120 <= height <= 230) or not (12 <= age <= 90):
        return None
    return weight, height, age


def plan_from_profile(goal: str, weight: float, height: float, age: int, lang: str) -> dict[str, Any]:
    safe_goal = goal if goal in {"loss", "maintain", "gain", "muscle"} else "maintain"
    bmi = weight / ((height / 100.0) ** 2) if height > 0 else 0.0
    bmr = ((10 * weight + 6.25 * height - 5 * age + 5) + (10 * weight + 6.25 * height - 5 * age - 161)) / 2.0
    if age >= 45 or bmi >= 32:
        activity = 1.35
    elif age <= 30 and bmi <= 24:
        activity = 1.55
    else:
        activity = 1.45
    tdee = bmr * activity
    delta = {"loss": -450, "maintain": 0, "gain": 350, "muscle": 250}[safe_goal]
    target_kcal = max(1200, int(round(tdee + delta)))
    protein = int(round(weight * {"loss": 2.0, "maintain": 1.7, "gain": 1.8, "muscle": 1.9}[safe_goal]))
    fat = int(round(weight * {"loss": 0.8, "maintain": 0.9, "gain": 1.0, "muscle": 0.95}[safe_goal]))
    carbs = max(60, int(round((target_kcal - protein * 4 - fat * 9) / 4)))
    return {
        "mode": safe_goal,
        "title": goal_title(safe_goal, lang),
        "daily_calories": target_kcal,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
        "weight": round(weight, 1),
        "height": round(height, 1),
        "age": int(age),
        "bmi": round(bmi, 1),
        "tdee": int(round(tdee)),
    }


def parse_custom_plan(text: str) -> dict[str, int] | None:
    parts = [p.strip() for p in re.split(r"[;,/ ]+", text.strip()) if p.strip()]
    if len(parts) < 4:
        return None
    try:
        calories, protein, fat, carbs = (int(float(p.replace(",", "."))) for p in parts[:4])
    except Exception:
        return None
    if calories <= 0 or min(protein, fat, carbs) < 0:
        return None
    return {"daily_calories": calories, "protein": protein, "fat": fat, "carbs": carbs}


def totals(logs: list[dict[str, Any]]) -> dict[str, float]:
    out = {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "meals": float(len(logs))}
    for row in logs:
        for key in ("calories", "protein", "fat", "carbs"):
            if row.get(key) is not None:
                out[key] += float(row[key])
    return out


def pending_item(estimate: CalorieEstimate, *, photo_url: str | None = None) -> dict[str, Any]:
    return {
        "photo_url": photo_url,
        "meal_desc": estimate.meal_desc,
        "calories": estimate.calories,
        "protein": estimate.protein,
        "fat": estimate.fat,
        "carbs": estimate.carbs,
        "confidence": estimate.confidence,
        "advice": estimate.advice,
    }


def top_meals(logs: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(logs):  # новые сверху
        desc = str(row.get("meal_desc") or "").strip()
        if not desc:
            continue
        key = desc.casefold()
        existing = groups.get(key)
        if existing is None:
            groups[key] = {
                "meal_desc": desc,
                "calories": row.get("calories"),
                "protein": row.get("protein"),
                "fat": row.get("fat"),
                "carbs": row.get("carbs"),
                "count": 1,
                "first_idx": idx,
            }
        else:
            existing["count"] += 1
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["first_idx"]))
    return ranked[:limit]


_FOOD_WORDS = (
    "съел", "съела", "поел", "поела", "покушал", "перекус", "завтрак", "обед", "ужин", "калор", "ккал",
    "yedim", "yeb", "ovqat", "nonushta", "tushlik", "kechki", "kaloriya", "kkal",
    "омлет", "яйц", "каша", "плов", "суп", "салат", "хлеб", "курица", "мясо", "рыба", "рис", "гречк", "макарон",
    "пицц", "бургер", "шаурм", "самса", "лагман", "манты", "йогурт", "творог", "сыр", "молоко", "кофе", "чай",
    "банан", "яблок", "апельсин", "фрукт", "овощ", "картош", "сок", "кола", "печень", "шоколад", "торт", "мороженое",
    "osh", "somsa", "lag'mon", "lagmon", "manti", "shurva", "sho'rva", "non", "tuxum", "go'sht", "tovuq", "baliq",
    "guruch", "salat", "sut", "qatiq", "pishloq", "banan", "olma", "kartoshka", "sharbat", "shokolad", "tort",
)


def looks_like_food(text: str) -> bool:
    low = str(text or "").lower()
    if not low or len(low) > 400:
        return False
    return any(word in low for word in _FOOD_WORDS)
