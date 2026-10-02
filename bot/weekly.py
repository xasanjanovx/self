"""Итоги недели голосом (29.09, его выбор: воскресенье 20:00, голосовое + текст): деньги, задачи, уроки, подъёмы и что JES
сделала за неделю (bot/deeds.py).

Цифры считает код; Gemini только пересказывает их живым голосом (как утренняя сводка, bot/voice_brief.py). Стоит ~$0.01 в
неделю: пересказ Flash-Lite + ~1 минута озвучки.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from . import categories as cats
from . import daily_tasks, deeds, lessons, services
from . import finance as fin
from . import voice as voice_mod
from . import wake as wake_mod
from .context import ai, db
from .persona import Persona, honorific_rule
from .profile import Profile

logger = logging.getLogger(__name__)

CAPTION_MAX = 1000
_MUSIC = re.compile(r"\b(audio|klip\w*|clip\w*|popuri|taronalar\w*|music|song\w*|mp3|official video|песн\w*|музык\w*|клип\w*)\b",
                    re.IGNORECASE)


async def facts(profile: Profile) -> dict[str, Any]:
    """Всё, что было за 7 дней (сегодня включительно), числами."""
    uid = profile.telegram_id
    today = profile.now.date()
    period = fin.period_for("week", today)
    out: dict[str, Any] = {"start": period.start, "end": today}
    try:
        stats = fin.compute_stats(await services.finance_entries(uid), period)
        out["money"] = {"spent": stats.expense, "income": stats.income, "prev_spent": stats.prev_expense,
                        "change_pct": stats.expense_change_pct(), "ops": stats.ops,
                        "top": [(cats.label(k, profile.lang, with_emoji=False), v) for k, v, _ in stats.by_category[:3]]}
    except Exception:
        logger.warning("weekly: деньги", exc_info=True)
    if db.available("tasks"):
        try:
            rows = await db.list_tasks(uid, include_done=True)
            done = [r for r in rows if r.get("done") and str(r.get("done_at") or "")[:10] >= period.start.isoformat()]
            open_ = [r for r in rows if not r.get("done")]
            overdue = [r for r in open_ if r.get("due_date") and str(r["due_date"])[:10] < today.isoformat()]
            out["tasks"] = {"done": len(done), "open": len(open_), "overdue": len(overdue),
                            "done_titles": [str(r.get("title") or "")[:60] for r in done[:4]]}
        except Exception:
            logger.warning("weekly: задачи", exc_info=True)
    habits = daily_tasks.all_items(uid)
    if habits:
        out["daily"] = [daily_tasks.describe(h, today) for h in habits[:6]]
    since = time.time() - 7 * 86400
    # уроки — не музыка и не ролики, открытые на секунду (MediaWatcher видит весь YouTube)
    watched = [r for r in lessons.items(uid) if float(r.get("at") or 0) >= since and int(r.get("position") or 0) >= 60
               and int(r.get("duration") or 0) >= 8 * 60 and not _MUSIC.search(str(r.get("title") or ""))]
    if watched:
        out["lessons"] = [{"title": r["title"][:70], "at": lessons.fmt(int(r.get("position") or 0)),
                           "of": lessons.fmt(int(r.get("duration") or 0)), "finished": lessons.finished(r)} for r in watched[:5]]
    try:
        history = await services.wake_history(uid, days=7)
        if history:
            out["wake"] = wake_mod.stats_line(history, profile.lang)
    except Exception:
        logger.warning("weekly: подъёмы", exc_info=True)
    counts = deeds.week_counts(uid, period.start, today)
    if counts.get("всего"):
        out["jes"] = counts
    return out


def text(profile: Profile, f: dict[str, Any]) -> str:
    """Короткий текст под голосовым — цифры, чтобы посмотреть глазами."""
    tr = profile.tr
    lines = [f"🗓 <b>{tr('Итоги недели', 'Hafta yakuni')}</b> · {f['start']:%d.%m}–{f['end']:%d.%m}"]
    m = f.get("money")
    if m:
        change = m.get("change_pct")
        trend = "" if change is None else f" ({'+' if change >= 0 else ''}{change:.0f}% {tr('к прошлой', 'o`tgan haftaga')})"
        top = ", ".join(f"{name} {fin.fmt_money(v)}" for name, v in m["top"])
        lines.append(f"💸 {tr('Потрачено', 'Sarflandi')}: <b>{fin.fmt_money(m['spent'])}</b> {profile.currency}{trend}"
                     + (f"\n   {top}" if top else ""))
        if m.get("income"):
            lines.append(f"💰 {tr('Доход', 'Daromad')}: {fin.fmt_money(m['income'])} {profile.currency}")
    t = f.get("tasks")
    if t and (t["done"] or t["open"]):
        lines.append(f"✅ {tr('Задачи', 'Vazifalar')}: {tr('сделано', 'bajarildi')} {t['done']}, {tr('открыто', 'ochiq')} {t['open']}"
                     + (f", {tr('просрочено', 'muddati o`tgan')} {t['overdue']}" if t["overdue"] else ""))
    for d in f.get("daily") or []:
        if d.get("progress"):
            lines.append(f"🔁 {d['title']}: {d['progress']}")
    for les in f.get("lessons") or []:
        lines.append(f"🎓 {les['title']} — " + (tr("досмотрено", "tugadi") if les["finished"] else f"{les['at']} / {les['of']}"))
    if f.get("wake"):
        lines.append(str(f["wake"]))
    j = f.get("jes")
    if j:
        parts = [f"{k} {v}" for k, v in j.items() if k != "всего" and v]
        lines.append(f"🤖 JES: {tr('дел', 'ish')} {j['всего']}" + (f" ({', '.join(parts)})" if parts else ""))
    out = "\n".join(lines)
    return out if len(out) <= CAPTION_MAX else out[: CAPTION_MAX - 1] + "…"


def script_prompt(profile: Profile, p: Persona, caption: str) -> str:
    plain = re.sub(r"<[^>]+>", "", caption)
    lang = {"uz": "узбекском (литературный, живой, латиницей)", "en": "английском"}.get(p.lang, "русском")
    return (
        f"Ты — JES (читается «Джес»), личный помощник {p.name_for(profile.first_name) or ''}. Сейчас воскресный вечер: расскажи "
        f"ГОЛОСОМ итоги его недели на {lang} языке, на «вы». Голос женский — о себе в женском роде. {honorific_rule(p)}\n"
        "Как живой человек: одна тёплая фраза, затем главное — деньги (сколько и на что, лучше или хуже прошлой недели), задачи, "
        "уроки, подъёмы на намаз — только то, что есть ниже; похвали за хорошее, мягко одна подсказка на следующую неделю. "
        "80–120 слов, без списков, эмодзи и скобок; суммы и время — словами. Ничего не выдумывай. Верни только текст для озвучки.\n\n"
        f"ИТОГИ:\n{plain}"
    )


async def send(bot: Bot, profile: Profile, p: Persona) -> bool:
    """Голосовое + текст. Голос не вышел — только текст. True — что-то отправлено."""
    f = await facts(profile)
    caption = text(profile, f)
    if len(caption.splitlines()) < 2:
        return False  # за неделю пусто — не беспокоим
    ogg = None
    if voice_mod.available():
        try:
            script = (await ai.generate_text(script_prompt(profile, p, caption), temperature=0.6, max_tokens=600)).strip()
            pcm = await ai.synthesize(script, voice=p.voice) if script else None
            ogg = await voice_mod.pcm_to_ogg(pcm) if pcm else None
        except Exception:
            logger.warning("weekly: голос не вышел", exc_info=True)
    if ogg:
        sent = await bot.send_voice(profile.telegram_id, BufferedInputFile(ogg, "jes-week.ogg"), caption=caption, parse_mode="HTML")
    else:
        sent = await bot.send_message(profile.telegram_id, caption, parse_mode="HTML")
    if getattr(sent, "message_id", None):  # 02.10: итоги недели не копятся в чате — исчезают при следующем действии, как остальное
        from . import screen as screen_mod

        screen_mod.track_ephemeral(profile.telegram_id, int(sent.message_id))
    logger.info("weekly: итоги недели отправлены %s (%s)", profile.telegram_id, "голосом" if ogg else "текстом")
    return True


async def maybe_send(bot: Bot, profile: Profile, due_key: str) -> bool:
    """Один раз за неделю (due_key — «2026-W40»), только владельцу JES."""
    uid = profile.telegram_id
    us = await services.user_settings(uid)
    if us.get("weekly_voice_key") == due_key or us.get("weekly_voice") is False:
        return False
    await services.save_user_settings(uid, {"weekly_voice_key": due_key})
    return await send(bot, profile, await services.persona(uid))


__all__ = ["facts", "text", "send", "maybe_send", "script_prompt"]
