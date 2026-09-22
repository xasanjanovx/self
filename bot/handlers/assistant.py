"""Экраны «Задачи» (+ заметки) и «Цели»: списки с кнопками, добавление текстом/голосом.

Задача разбирается локально (bot/tasks.py) — без похода в AI; команды и вопросы
(«удали…», «что у меня на завтра?») уходят Джарвису, суммы («такси 25000») — в общий
маршрутизатор. Цели создаются через Джарвиса (у него есть add_goal), пополнение —
кнопкой «Отложить» + число.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import agent_tools as tools
from .. import agent_tools_assistant as asst
from .. import analysis
from .. import cache
from .. import finance as fin
from .. import services
from .. import tasks as tasks_mod
from .. import ui
from .. import undo
from ..context import db
from ..keyboards import goal_detail_keyboard, goals_keyboard, notes_keyboard, tasks_keyboard
from ..profile import Profile, h
from ..states import BotStates
from .common import answer_now, get_profile, remember_panel, safe_delete, safe_edit, show_panel, show_progress, transcribe_audio

router = Router(name="assistant")
logger = logging.getLogger(__name__)

MIGRATION_HINT = ("Нужна миграция: выполни <code>sql/migrations/005_assistant.sql</code> в Supabase → SQL Editor.",
                  "Migratsiya kerak: Supabase → SQL Editor da <code>sql/migrations/005_assistant.sql</code> ni bajaring.")


# ------------------------------------------------------------------ tasks screen
def _task_line(t: dict[str, Any], today: Any, lang: str, *, time_only: bool = False) -> str:
    when = str(t.get("due_time") or "") if time_only else tasks_mod.when_label(t, today, lang)
    return f"• {h(t.get('text'))}" + (f" · {ui.muted(when)}" if when else "")


def tasks_text(profile: Profile, tasks: list[dict[str, Any]], *, done_view: bool = False) -> str:
    lang, today = profile.lang, profile.today
    uz = lang == "uz"
    if done_view:
        header = ui.title("✅", "Bajarilgan vazifalar" if uz else "Выполненные задачи")
        if not tasks:
            return ui.join(header, ui.muted("Hali yo'q." if uz else "Пока пусто."))
        lines = [f"• {h(t.get('text'))} · {ui.muted(str(t.get('done_at') or '')[:10])}" for t in tasks[:20]]
        return ui.join(header, ui.card(f"<b>{'Oxirgi' if uz else 'Последние'}</b>", lines), ui.muted("🗑 — o'chirish" if uz else "🗑 — удалить насовсем"))

    g = tasks_mod.group(tasks, today)
    header = ui.title("📝", "Vazifalar" if uz else "Задачи", f"{g.total} {'ta ochiq' if uz else 'открытых'}" if g.total else None)
    blocks: list[str | None] = [header]
    if g.overdue:
        blocks.append(ui.card(f"⚠️ <b>{'Kechikkan' if uz else 'Просрочено'}</b>", [_task_line(t, today, lang) for t in g.overdue]))
    if g.today:
        blocks.append(ui.card(f"📅 <b>{'Bugun' if uz else 'Сегодня'}</b>", [_task_line(t, today, lang, time_only=True) for t in g.today]))
    if g.upcoming:
        blocks.append(ui.card(f"🔜 <b>{'Yaqinda' if uz else 'Ближайшие'}</b>", [_task_line(t, today, lang) for t in g.upcoming[:10]]))
    if g.undated:
        blocks.append(ui.card(f"📌 <b>{'Sanasiz' if uz else 'Без даты'}</b>", [_task_line(t, today, lang) for t in g.undated[:10]]))
    if not g.total:
        blocks.append(ui.muted("Ochiq vazifalar yo'q." if uz else "Открытых задач нет."))
    blocks.append(ui.muted("✍️ «ertaga 18:00 onamga qo'ng'iroq» · «lampochka olish» · ovoz. ✅ — bajarildi." if uz
                           else "✍️ «позвонить маме завтра в 18:00» · «купить лампочку» · голос. ✅ — выполнено."))
    return ui.join(*blocks)


async def render_tasks(target: Message | CallbackQuery, state: FSMContext, profile: Profile, *, notice: str | None = None, done_view: bool = False) -> None:
    if not await db.ensure_available("tasks"):
        text, rows = "⚠️ " + profile.tr(*MIGRATION_HINT), []
    else:
        rows = await db.list_tasks(profile.telegram_id, include_done=True) if done_view else await services.tasks(profile.telegram_id)
        if done_view:
            rows = sorted([r for r in rows if r.get("done")], key=lambda r: str(r.get("done_at") or ""), reverse=True)
        else:
            g = tasks_mod.group(rows, profile.today)
            rows = g.overdue + g.today + g.upcoming + g.undated
        text = tasks_text(profile, rows, done_view=done_view)
    if notice:
        text += f"\n\n{notice}"
    await state.set_state(BotStates.waiting_task_input)
    await state.update_data(tasks_done_view=done_view)
    kb = tasks_keyboard(rows, profile.lang, done_view=done_view)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, text, kb)
    else:
        await show_panel(target, state, text, kb)


@router.callback_query(F.data == "menu:tasks")
async def cb_tasks(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await render_tasks(callback, state, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("task:view:"))
async def cb_tasks_view(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await render_tasks(callback, state, await get_profile(callback.from_user), done_view=callback.data.endswith(":done"))


@router.callback_query(F.data == "task:add")
async def cb_task_add(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    await state.set_state(BotStates.waiting_task_input)
    await remember_panel(callback, state)
    await safe_edit(
        callback,
        profile.tr(
            "Напиши задачу — можно с датой и временем:\n<code>позвонить маме завтра в 18:00</code> · <code>купить лампочку</code> · <code>3 ноября поздравить брата</code> · <code>в пятницу забрать посылку</code>",
            "Vazifani yozing — sana va vaqt bilan bo'lishi mumkin:\n<code>ertaga 18:00 onamga qo'ng'iroq</code> · <code>lampochka olish</code> · <code>juma kuni posilka olish</code>",
        ),
        tasks_keyboard([], profile.lang),
    )


@router.callback_query(F.data.startswith("task:done:"))
async def cb_task_done(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    task_id = callback.data.split(":")[-1]
    rows = [r for r in await services.tasks(profile.telegram_id) if str(r.get("id")) == task_id]
    if not rows:
        await answer_now(callback, profile.tr("Уже нет в списке", "Ro'yxatda yo'q"))
        await render_tasks(callback, state, profile)
        return
    await answer_now(callback, profile.tr("Выполнено ✅", "Bajarildi ✅"))
    await db.update_task(profile.telegram_id, rows[0]["id"], {"done": True, "done_at": services.utc_now().isoformat()})
    cache.invalidate(profile.telegram_id, "tasks")
    undo.remember(profile.telegram_id, {"type": "task_fields", "task_id": rows[0]["id"], "fields": {"done": False, "done_at": None}})
    await render_tasks(callback, state, profile, notice=profile.tr(f"✅ {h(rows[0].get('text'))} — сделано", f"✅ {h(rows[0].get('text'))} — bajarildi"))


@router.callback_query(F.data.startswith("task:del:"))
async def cb_task_delete(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    task_id = callback.data.split(":")[-1]
    rows = [r for r in await db.list_tasks(profile.telegram_id, include_done=True) if str(r.get("id")) == task_id]
    if rows:
        await db.delete_tasks(profile.telegram_id, [rows[0]["id"]])
        cache.invalidate(profile.telegram_id, "tasks")
        undo.remember(profile.telegram_id, {"type": "restore_tasks", "rows": rows})
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    await render_tasks(callback, state, profile, done_view=True)


# ------------------------------------------------------------------ notes
@router.callback_query(F.data == "task:notes")
async def cb_notes(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    uz = profile.lang == "uz"
    notes = await services.notes(profile.telegram_id)
    header = ui.title("🗒", "Eslatmalar" if uz else "Заметки", f"{len(notes)}" if notes else None)
    if notes:
        body = ui.card(f"<b>{'Oxirgi' if uz else 'Последние'}</b>", [f"• {h(n.get('text'))} · {ui.muted(str(n.get('created_at') or '')[:10])}" for n in notes[:20]])
    else:
        body = ui.muted("Hali yo'q." if uz else "Пока пусто.")
    hint = ui.muted("✍️ «eslab qol: akamning tug'ilgan kuni 3-noyabr»" if uz else "✍️ «запомни, что у брата день рождения 3 ноября»")
    await state.set_state(BotStates.waiting_task_input)
    await state.update_data(tasks_done_view=False)
    await remember_panel(callback, state)
    await safe_edit(callback, ui.join(header, body, hint), notes_keyboard(notes, profile.lang))


@router.callback_query(F.data.startswith("note:del:"))
async def cb_note_delete(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    note_id = callback.data.split(":")[-1]
    rows = [r for r in await services.notes(profile.telegram_id) if str(r.get("id")) == note_id]
    if rows:
        await db.delete_notes(profile.telegram_id, [rows[0]["id"]])
        cache.invalidate(profile.telegram_id, "notes")
        undo.remember(profile.telegram_id, {"type": "restore_notes", "rows": rows})
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    await cb_notes(callback, state)


# ------------------------------------------------------------------ text / voice on the tasks screen
_NOTE_PREFIXES = ("запомни", "заметка", "eslab qol", "eslatma")


async def handle_task_text(message: Message, state: FSMContext, profile: Profile, text: str, *, voice: bool = False) -> None:
    from . import agent
    from .inbox import route_text

    low = text.lower().strip()
    if low.startswith("/"):
        await safe_delete(message)
        return
    if low.startswith(_NOTE_PREFIXES) or agent.looks_like_command(text) or "?" in low:
        # команды («удали…», «перенеси на пятницу»), заметки и вопросы — Джарвису
        if await agent.handle_command(message, state, profile, text, voice=voice):
            return
    parsed = tasks_mod.parse_task(text, profile.today)
    if parsed.due_date is None and parsed.due_time is None and (fin.bare_amount(text) or fin.looks_like_finance(text)):
        # «такси 25000» на экране задач — это всё-таки трата
        if await route_text(message, state, profile, text, transcript=text if voice else None):
            return
    if not await db.ensure_available("tasks"):
        await safe_delete(message)
        await render_tasks(message, state, profile)
        return
    row = await db.add_task(profile.telegram_id, **parsed.as_row())
    cache.invalidate(profile.telegram_id, "tasks")
    undo.remember(profile.telegram_id, {"type": "delete_tasks", "ids": [row.get("id")]})
    await safe_delete(message)
    when = tasks_mod.when_label(row, profile.today, profile.lang)
    notice = f"✅ {profile.tr('Добавлено', 'Qo`shildi')}: {h(parsed.text)}" + (f" · {when}" if when else "")
    if parsed.due_time:
        notice += " · " + profile.tr("напомню в срок", "vaqtida eslataman")
    await render_tasks(message, state, profile, notice=notice)


@router.message(BotStates.waiting_task_input, F.text)
async def msg_task_text(message: Message, state: FSMContext) -> None:
    await handle_task_text(message, state, await get_profile(message.from_user), (message.text or "").strip())


@router.message(BotStates.waiting_task_input, F.voice | F.audio)
async def msg_task_voice(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await show_progress(message, profile.tr("⏳ Распознаю голос…", "⏳ Ovoz aniqlanmoqda…"))
    try:
        transcript = await transcribe_audio(message)
    except Exception:
        logger.exception("transcribe failed")
        transcript = ""
    if not transcript:
        await safe_delete(message)
        await render_tasks(message, state, profile, notice=profile.tr("Не расслышал, повтори.", "Eshitmadim, qaytaring."))
        return
    await handle_task_text(message, state, profile, transcript, voice=True)


@router.message(BotStates.waiting_task_input)
async def msg_task_other(message: Message, state: FSMContext) -> None:
    from .inbox import fallback

    await state.clear()
    await fallback(message, state)


# ------------------------------------------------------------------ goals screen
def _goal_lines(st: dict[str, Any], profile: Profile) -> list[str]:
    uz, cur = profile.lang == "uz", profile.currency
    lines = [
        f"{fin.bar(float(st.get('ratio') or 0), 12)} {ui.pct(float(st.get('ratio') or 0))}",
        f"<b>{fin.fmt_money(float(st.get('saved') or 0))}</b> / {fin.fmt_money(float(st.get('target') or 0))} {cur}",
        f"{'Qoldi' if uz else 'Осталось'}: {fin.fmt_money(float(st.get('remaining') or 0))}",
    ]
    if st.get("deadline"):
        try:
            from datetime import date

            dl = date.fromisoformat(st["deadline"])
            lines.append(f"{'Muddat' if uz else 'Срок'}: {dl:%d.%m.%Y}" + (f" · {st['months_left']} {'oy' if uz else 'мес.'}" if st.get("months_left") is not None else ""))
        except ValueError:
            pass
    if st.get("needed_per_month"):
        lines.append(f"{'Oyiga kerak' if uz else 'Нужно в месяц'}: <b>{fin.fmt_money(float(st['needed_per_month']))}</b>")
    if "on_track" in st:
        if st["on_track"]:
            lines.append("✅ " + ("Hozirgi sur'atda ulguramiz" if uz else "При текущем темпе успеваем"))
        else:
            pace = st.get("months_at_current_pace")
            tail = f" (~{pace} {'oy' if uz else 'мес.'})" if pace else ""
            lines.append("⚠️ " + ("Hozirgi sur'atda ulgurmaymiz" if uz else "При текущем темпе не успеваем") + tail)
    return lines


async def _goal_statuses(profile: Profile) -> list[dict[str, Any]]:
    try:
        return await asst.goals_with_status(tools.ToolContext(profile=profile, text=""))
    except Exception:
        logger.exception("goals_with_status failed")
        return [analysis.goal_status(g, profile.today) for g in await services.goals(profile.telegram_id)]


async def render_goals(target: Message | CallbackQuery, state: FSMContext, profile: Profile, *, notice: str | None = None) -> None:
    uz = profile.lang == "uz"
    if not await db.ensure_available("savings_goals"):
        text, goals = "⚠️ " + profile.tr(*MIGRATION_HINT), []
    else:
        goals = await services.goals(profile.telegram_id)
        statuses = await _goal_statuses(profile) if goals else []
        header = ui.title("🎯", "Maqsadlar" if uz else "Цели накоплений")
        blocks: list[str | None] = [header]
        for st in statuses:
            blocks.append(ui.card(f"🎯 <b>{h(st.get('title'))}</b>", _goal_lines(st, profile)))
        if not goals:
            blocks.append(ui.muted("Maqsadlar yo'q." if uz else "Целей пока нет."))
        blocks.append(ui.muted("✍️ «10 mln noutbukka yanvargacha» · «noutbukka 500k qo'shdim»" if uz
                               else "✍️ «накопить 10 млн на ноутбук к январю» · «отложил 500к на ноутбук»"))
        text = ui.join(*blocks)
    if notice:
        text += f"\n\n{notice}"
    await state.set_state(BotStates.waiting_goal_input)
    kb = goals_keyboard(goals, profile.lang)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, text, kb)
    else:
        await show_panel(target, state, text, kb)


@router.callback_query(F.data == "menu:goals")
async def cb_goals(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await render_goals(callback, state, await get_profile(callback.from_user))


@router.callback_query(F.data == "goal:add")
async def cb_goal_add(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    await state.set_state(BotStates.waiting_goal_input)
    await remember_panel(callback, state)
    await safe_edit(
        callback,
        profile.tr(
            "Напиши цель: <b>сумма, на что, к какому сроку</b>\n<code>накопить 10 млн на ноутбук к январю</code> · <code>5 млн на отпуск, уже есть 1 млн</code>",
            "Maqsadni yozing: <b>summa, nimaga, qachongacha</b>\n<code>yanvargacha noutbukka 10 mln</code> · <code>ta'tilga 5 mln, 1 mln bor</code>",
        ),
        goals_keyboard([], profile.lang),
    )


async def _find_goal(profile: Profile, goal_id: str) -> dict[str, Any] | None:
    rows = [r for r in await db.list_goals(profile.telegram_id, include_done=True) if str(r.get("id")) == goal_id]
    return rows[0] if rows else None


@router.callback_query(F.data.startswith("goal:view:"))
async def cb_goal_view(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    goal = await _find_goal(profile, callback.data.split(":")[-1])
    if goal is None:
        await render_goals(callback, state, profile)
        return
    st = next((s for s in await _goal_statuses(profile) if s.get("id") == str(goal.get("id"))), None) or analysis.goal_status(goal, profile.today)
    await state.set_state(BotStates.waiting_goal_input)
    await remember_panel(callback, state)
    await safe_edit(callback, ui.join(ui.title("🎯", h(goal.get("title"))), ui.card(f"<b>{'Holat' if profile.lang == 'uz' else 'Прогресс'}</b>", _goal_lines(st, profile))),
                    goal_detail_keyboard(goal["id"], profile.lang))


@router.callback_query(F.data.startswith("goal:deposit:"))
async def cb_goal_deposit(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    goal = await _find_goal(profile, callback.data.split(":")[-1])
    if goal is None:
        await render_goals(callback, state, profile)
        return
    await state.set_state(BotStates.waiting_goal_amount)
    await state.update_data(goal_id=str(goal["id"]))
    await remember_panel(callback, state)
    await safe_edit(
        callback,
        profile.tr(f"Сколько отложил на «{h(goal.get('title'))}»? Напиши сумму: <code>500000</code> · <code>1.5 млн</code> · <code>-200к</code> (снять)",
                   f"«{h(goal.get('title'))}» uchun qancha qo'shdingiz? Summa: <code>500000</code> · <code>1.5 mln</code> · <code>-200k</code>"),
        goal_detail_keyboard(goal["id"], profile.lang),
    )


@router.message(BotStates.waiting_goal_amount, F.text)
async def msg_goal_amount(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()
    await safe_delete(message)
    data = await state.get_data()
    goal = await _find_goal(profile, str(data.get("goal_id") or ""))
    parsed = fin.parse_amount(text)
    if goal is None or parsed is None:
        await render_goals(message, state, profile, notice=profile.tr("Не понял сумму.", "Summani tushunmadim.") if goal else None)
        return
    amount = -parsed[0] if text.lstrip().startswith("-") else parsed[0]
    before = float(goal.get("saved_amount") or 0)
    after = max(0.0, before + amount)
    await db.update_goal(profile.telegram_id, goal["id"], {"saved_amount": after})
    cache.invalidate(profile.telegram_id, "goals")
    undo.remember(profile.telegram_id, {"type": "goal_fields", "goal_id": goal["id"], "fields": {"saved_amount": before}})
    target = float(goal.get("target_amount") or 0)
    done_note = " 🎉" if target and after >= target else ""
    await render_goals(message, state, profile, notice=profile.tr(
        f"✅ {h(goal.get('title'))}: {fin.fmt_money(after)} / {fin.fmt_money(target)}{done_note}",
        f"✅ {h(goal.get('title'))}: {fin.fmt_money(after)} / {fin.fmt_money(target)}{done_note}",
    ))


@router.callback_query(F.data.startswith("goal:close:"))
async def cb_goal_close(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    goal = await _find_goal(profile, callback.data.split(":")[-1])
    if goal is not None:
        await db.update_goal(profile.telegram_id, goal["id"], {"done": True})
        cache.invalidate(profile.telegram_id, "goals")
        undo.remember(profile.telegram_id, {"type": "goal_fields", "goal_id": goal["id"], "fields": {"done": False}})
    await answer_now(callback, profile.tr("Цель закрыта 🎉", "Maqsad yopildi 🎉"))
    await render_goals(callback, state, profile)


@router.callback_query(F.data.startswith("goal:del:"))
async def cb_goal_delete(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    goal = await _find_goal(profile, callback.data.split(":")[-1])
    if goal is not None:
        await db.delete_goals(profile.telegram_id, [goal["id"]])
        cache.invalidate(profile.telegram_id, "goals")
        undo.remember(profile.telegram_id, {"type": "restore_goals", "rows": [goal]})
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    await render_goals(callback, state, profile)


@router.message(BotStates.waiting_goal_input, F.text)
async def msg_goal_text(message: Message, state: FSMContext) -> None:
    """Текст на экране целей — Джарвису (add_goal / update_goal), суммы — в общий маршрут."""
    from . import agent
    from .inbox import route_text

    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()
    if text.startswith("/"):
        await safe_delete(message)
        return
    if fin.bare_amount(text):
        await route_text(message, state, profile, text)
        return
    if not await agent.handle_command(message, state, profile, text):
        await safe_delete(message)
        await render_goals(message, state, profile, notice=profile.tr("Не получилось, попробуй ещё раз.", "Bo'lmadi, qayta urining."))


@router.message(BotStates.waiting_goal_input)
@router.message(BotStates.waiting_goal_amount)
async def msg_goal_other(message: Message, state: FSMContext) -> None:
    from .inbox import fallback

    await state.clear()
    await fallback(message, state)
