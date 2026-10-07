"""Сводка за период (дашборд «Аналитика» и авто-отчёт).

07.10: он сказал, что отчёты — простыня цифр, которую он не читает. Теперь сверху — выводы («расходы выросли на 18%, больше всего
выросло Такси»), а не только цифры; авто-отчёт (build_digest) — это 4–6 строк: итог, главный вывод, топ категорий, питание.
"""
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


def _riser(stats: fin.Stats) -> tuple[str, float, float] | None:
    """Категория, где расход вырос сильнее всего: (ключ, на сколько вырос в суммах, на сколько процентов). Мелочь не считаем."""
    best: tuple[str, float, float] | None = None
    for key, amount, _ in stats.by_category:
        prev = float(stats.prev_by_category.get(key) or 0)
        growth = amount - prev
        if prev <= 0 or growth <= 0 or amount < stats.expense * 0.08 or growth < prev * 0.15:
            continue
        if best is None or growth > best[1]:
            best = (key, growth, growth / prev * 100.0)
    return best


def insights(stats: fin.Stats, nutrition: dict[str, Any] | None, *, days: int, lang: str, currency: str) -> list[str]:
    """Выводы вместо голых цифр — самое важное первым, не больше трёх. Только то, что можно сказать по данным."""
    uz = lang == "uz"
    out: list[str] = []
    change = stats.expense_change_pct()
    if change is not None and abs(change) >= 15:
        riser = _riser(stats) if change > 0 else None
        word = ("oshdi" if change > 0 else "kamaydi") if uz else ("выросли" if change > 0 else "снизились")
        line = ("Xarajatlar " if uz else "Расходы ") + f"{word} {abs(change):.0f}%"
        if riser:
            # у маленькой прежней суммы проценты нелепы («+2400%») — тогда показываем, на сколько выросло в деньгах
            grew = f"+{riser[2]:.0f}%" if riser[2] < 200 else f"+{fin.fmt_money(riser[1])} {currency}"
            line += (f", ko'proq — «{cats.label(riser[0], lang, with_emoji=False)}» ({grew})" if uz
                     else f", больше всего — «{cats.label(riser[0], lang, with_emoji=False)}» ({grew})")
        out.append(("📈 " if change > 0 else "📉 ") + line)
    if stats.income and stats.net < 0:
        out.append("⚠️ " + (f"Xarajat daromaddan {fin.fmt_money(-stats.net)} {currency} ko'p" if uz
                           else f"Расходы превысили доходы на {fin.fmt_money(-stats.net)} {currency}"))
    if stats.by_category and stats.expense:
        key, amount, _ = stats.by_category[0]
        share = amount / stats.expense * 100
        if share >= 40:
            out.append(("🏷 " + (f"Eng ko'p — «{cats.label(key, lang, with_emoji=False)}»: {share:.0f}%" if uz
                                 else f"Больше всего ушло на «{cats.label(key, lang, with_emoji=False)}»: {share:.0f}%")))
    if stats.top_day and stats.top_day[1] >= max(stats.avg_per_day * 2.5, 1.0) and days >= 7:
        out.append(f"💥 {'Eng qimmat kun' if uz else 'Самый дорогой день'} — {stats.top_day[0]:%d.%m}: {fin.fmt_money(stats.top_day[1])} {currency}")
    if nutrition and nutrition.get("target"):
        diff = nutrition["avg_kcal"] - nutrition["target"]
        if abs(diff) >= nutrition["target"] * 0.15:
            out.append(f"🍽 {'Kuniga o`rtacha' if uz else 'В среднем'} {int(nutrition['avg_kcal'])} / {nutrition['target']} " + ("kkal — " if uz else "ккал — ")
                       + (("me'yordan ko'p" if diff > 0 else "me'yordan kam") if uz else ("выше нормы" if diff > 0 else "ниже нормы")))
    if stats.ops and stats.days_with_expense < days * 0.5:
        out.append("ℹ️ " + (f"Xarajatlar {days} kunning {stats.days_with_expense} tasida yozilgan — manzara to'liq emas" if uz
                            else f"Расходы записаны лишь в {stats.days_with_expense} из {days} дней — картина неполная"))
    return out[:3]


def _period_numbers(profile: Profile, *, days: int, entries: list[dict[str, Any]], logs: list[dict[str, Any]],
                    nutrition_profile: dict[str, Any] | None) -> tuple[date, date, fin.Stats, int | None, dict[str, Any] | None]:
    end = profile.today
    start = end - timedelta(days=days - 1)
    period = fin.Period("custom", start, end, start - timedelta(days=days), start - timedelta(days=1))
    stats = fin.compute_stats(entries, period)
    target = int((nutrition_profile or {}).get("daily_calories") or 0) or None
    return start, end, stats, target, nutrition_summary(logs, days=days, tz=profile.tz, target=target)


def build_summary(
    profile: Profile,
    *,
    days: int,
    entries: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    nutrition_profile: dict[str, Any] | None,
    title: str,
) -> PeriodSummary:
    """Полный экран «Аналитика»: выводы сверху, потом цифры."""
    lang, cur = profile.lang, profile.currency
    start, end, stats, target, nutrition = _period_numbers(profile, days=days, entries=entries, logs=logs, nutrition_profile=nutrition_profile)

    lines = [f"{title}", f"<i>{start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}</i>", ""]
    notes = insights(stats, nutrition, days=days, lang=lang, currency=cur)
    if notes:
        lines.append(f"💡 <b>{'Asosiysi' if lang == 'uz' else 'Главное'}</b>")
        lines.extend(notes)
        lines.append("")
    change = stats.expense_change_pct()
    change_text = f" ({'▲' if change > 0 else '▼'}{abs(change):.0f}%)" if change is not None else ""
    lines.append(f"{pe.WALLET} <b>{'Moliya' if lang == 'uz' else 'Финансы'}</b>")
    lines.append(f"{pe.EXPENSE} {'Chiqim' if lang == 'uz' else 'Расход'}: <b>{fin.fmt_money(stats.expense)} {cur}</b>{change_text} · {'kuniga' if lang == 'uz' else 'в день'} ~{fin.fmt_money(stats.avg_per_day)}")
    if stats.income:
        net = stats.net
        lines.append(f"{pe.INCOME} {'Kirim' if lang == 'uz' else 'Доход'}: <b>{fin.fmt_money(stats.income)} {cur}</b> · "
                     f"{'Natija' if lang == 'uz' else 'Итог'}: <b>{'+' if net >= 0 else '−'}{fin.fmt_money(abs(net))}</b>")
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
            f"{'kunlar' if lang == 'uz' else 'дней с записями'}: {nutrition['days_logged']}/{days}"
        )
    else:
        lines.append("<i>" + ("Yozuvlar yo'q." if lang == "uz" else "Записей нет.") + "</i>")
    return PeriodSummary(days=days, start=start, end=end, stats=stats, nutrition=nutrition, text="\n".join(lines))


def build_digest(
    profile: Profile,
    *,
    days: int,
    entries: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    nutrition_profile: dict[str, Any] | None,
    title: str,
) -> str | None:
    """Авто-отчёт (воскресенье / 1-е число) в 4–6 строк: итог, главные выводы, топ категорий, питание. Нет данных — None."""
    lang, cur = profile.lang, profile.currency
    uz = lang == "uz"
    start, end, stats, target, nutrition = _period_numbers(profile, days=days, entries=entries, logs=logs, nutrition_profile=nutrition_profile)
    if not stats.ops and not nutrition:
        return None
    change = stats.expense_change_pct()
    trend = f" ({'▲' if change > 0 else '▼'}{abs(change):.0f}%)" if change is not None else ""
    lines = [f"{title} · <i>{start:%d.%m}–{end:%d.%m}</i>",
             f"💸 <b>{fin.fmt_money(stats.expense)} {cur}</b>{trend} · {'kuniga' if uz else 'в день'} ~{fin.fmt_money(stats.avg_per_day)}"
             + (f" · 💰 {fin.fmt_money(stats.income)}" if stats.income else "")]
    notes = insights(stats, nutrition, days=days, lang=lang, currency=cur)[:2]
    lines.extend(notes)
    if stats.by_category and stats.expense and not any(n.startswith("🏷") for n in notes):
        lines.append(" · ".join(f"{cats.label(k, lang)} {a / stats.expense * 100:.0f}%" for k, a, _ in stats.by_category[:3]))
    if nutrition and not any(line.startswith("🍽") for line in lines):
        avg = int(nutrition["avg_kcal"])
        lines.append(f"🍽 {'O`rtacha' if uz else 'В среднем'} {avg}" + (f" / {target}" if target else "") + f" {'kkal' if uz else 'ккал'} · {nutrition['days_logged']}/{days}")
    return "\n".join(lines)


__all__ = ["PeriodSummary", "build_summary", "build_digest", "insights", "nutrition_summary"]
