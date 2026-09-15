"""AI-инсайт: короткий человеческий комментарий к цифрам периода."""
from __future__ import annotations

import logging
from typing import Any

from . import categories as cats
from . import finance as fin
from .ai import AIService

logger = logging.getLogger(__name__)


def build_prompt(stats: fin.Stats, nutrition: dict[str, Any] | None, *, currency: str, lang: str) -> str:
    def money(v: float) -> str:
        return fin.fmt_money(v)

    pieces = [f"Период: {stats.period.days} дн. Валюта: {currency}."]
    pieces.append(f"Расход: {money(stats.expense)} (пред. период {money(stats.prev_expense)})")
    pieces.append(f"Доход: {money(stats.income)} (пред. период {money(stats.prev_income)})")
    if stats.by_category:
        pieces.append("Топ расходов: " + ", ".join(f"{cats.label(k, 'ru', with_emoji=False)} {money(a)}" for k, a, _ in stats.by_category[:4]))
    jumps = []
    for key, amount, _ in stats.by_category[:8]:
        prev = stats.prev_by_category.get(key, 0.0)
        if prev > 0 and abs(amount - prev) / prev >= 0.3:
            jumps.append(f"{cats.label(key, 'ru', with_emoji=False)} {((amount - prev) / prev * 100):+.0f}%")
    if jumps:
        pieces.append("Скачки: " + ", ".join(jumps[:3]))
    if stats.top_day:
        pieces.append(f"Самый затратный день: {stats.top_day[0].strftime('%d.%m')} {money(stats.top_day[1])}")
    if nutrition:
        pieces.append(
            f"Питание: дней с записями {nutrition.get('days_logged', 0)} из {stats.period.days}, "
            f"среднее {int(nutrition.get('avg_kcal') or 0)} ккал/день, цель {int(nutrition.get('target') or 0)}"
        )
    lang_name = "узбекском (латиница)" if lang == "uz" else "русском"
    return (
        "Ты — личный финансовый и фитнес-аналитик пользователя. По данным ниже дай короткий инсайт:\n"
        "1) одно предложение — главный тренд (рост/падение, категория-лидер, скачок);\n"
        "2) одно предложение — конкретный совет на следующий период.\n"
        f"Отвечай на {lang_name} языке, дружелюбно, без воды, без хэштегов и markdown. Максимум 2–3 предложения.\n\n"
        f"Данные: {' | '.join(pieces)}"
    )


async def generate_insight(ai: AIService, stats: fin.Stats, nutrition: dict[str, Any] | None, *, currency: str, lang: str) -> str | None:
    if stats.expense == 0 and stats.income == 0 and not nutrition:
        return None
    try:
        text = await ai.generate_text(build_prompt(stats, nutrition, currency=currency, lang=lang), temperature=0.4, max_tokens=300)
        return text.strip() or None
    except Exception as exc:
        logger.warning("AI insight failed: %s", exc)
        return None


__all__ = ["build_prompt", "generate_insight"]
