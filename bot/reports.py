"""Сводка за период (дашборд «Аналитика» и авто-отчёт)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import categories as cats
from . import emoji as pe
from . import finance as fin
from .profile import Profile


@dataclass
class PeriodSummary:
    days: int
    start: date
    end: date
    stats: fin.Stats
    nutrition: dict[str, Any] | None
    text: str


def nutrition_summary(logs: list[dict[str, Any]], *, days: int, tz: Any, target: int | None) -> dict[str, Any] | None:
    by_day: dict[str, float] = {}
    for log in logs:
        created = log.get("created_at")
        kcal = log.get("calories")
        if not created or kcal is None:
            continue
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        key = dt.astimezone(tz).date().isoformat()
        by_day[key] = by_day.get(key, 0.0) + float(kcal)
    if not by_day:
        return None
    avg = sum(by_day.values()) / len(by_day)
    return {"days_logged": len(by_day), "avg_kcal": avg, "target": target or 0, "meals": len(logs)}


def build_summary(
    profile: Profile,
    *,
    days: int,
    entries: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    nutrition_profile: dict[str, Any] | None,
    title: str,
) -> PeriodSummary:
    lang, cur = profile.lang, profile.currency
    end = profile.today
    start = end - timedelta(days=days - 1)
    period = fin.Period("custom", start, end, start - timedelta(days=days), start - timedelta(days=1))
    stats = fin.compute_stats(entries, period)
    target = int((nutrition_profile or {}).get("daily_calories") or 0) or None
    nutrition = nutrition_summary(logs, days=days, tz=profile.tz, target=target)

    lines = [f"{title}", f"<i>{start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}</i>", ""]
    change = stats.expense_change_pct()
    change_text = f" ({'▲' if change > 0 else '▼'}{abs(change):.0f}%)" if change is not None else ""
    lines.append(f"{pe.WALLET} <b>{'Moliya' if lang == 'uz' else 'Финансы'}</b>")
    lines.append(f"{pe.EXPENSE} {'Chiqim' if lang == 'uz' else 'Расход'}: <b>{fin.fmt_money(stats.expense)} {cur}</b>{change_text}")
    lines.append(f"{pe.INCOME} {'Kirim' if lang == 'uz' else 'Доход'}: <b>{fin.fmt_money(stats.income)} {cur}</b>")
    net = stats.net
    lines.append(f"{'Natija' if lang == 'uz' else 'Итог'}: <b>{'+' if net >= 0 else '−'}{fin.fmt_money(abs(net))} {cur}</b> · {'kuniga' if lang == 'uz' else 'в день'} ~{fin.fmt_money(stats.avg_per_day)}")
    if stats.by_category:
        total = stats.expense or 1.0
        for key, amount, _ in stats.by_category[:5]:
            lines.append(f"  {cats.label(key, lang)} — {fin.fmt_money(amount)} · {amount / total * 100:.0f}%")
    lines.append("")
    lines.append(f"{pe.NUTRITION} <b>{'Oziqlanish' if lang == 'uz' else 'Питание'}</b>")
    if nutrition:
        avg = int(nutrition["avg_kcal"])
        tgt = f" / {target}" if target else ""
        lines.append(
            f"{'O`rtacha' if lang == 'uz' else 'В среднем'}: <b>{avg}{tgt} kkal</b> · "
            f"{'kunlar' if lang == 'uz' else 'дней с записями'}: {nutrition['days_logged']}/{days} · {'qabullar' if lang == 'uz' else 'приёмов'}: {nutrition['meals']}"
        )
        if target:
            diff = avg - target
            lines.append(("Maqsaddan " if lang == "uz" else "Отклонение от цели: ") + f"{'+' if diff >= 0 else '−'}{abs(diff)} kkal/{'kun' if lang == 'uz' else 'день'}")
    else:
        lines.append("<i>" + ("Yozuvlar yo'q." if lang == "uz" else "Записей нет.") + "</i>")
    return PeriodSummary(days=days, start=start, end=end, stats=stats, nutrition=nutrition, text="\n".join(lines))


__all__ = ["PeriodSummary", "build_summary", "nutrition_summary"]
