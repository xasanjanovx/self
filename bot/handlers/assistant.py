"""Экраны «Задачи» (+ заметки) и «Цели»: списки с кнопками, добавление текстом/голосом.

Задача разбирается локально (bot/tasks.py) — без похода в AI; команды и вопросы
(«удали…», «что у меня на завтра?») уходят JES, суммы («такси 25000») — в общий
маршрутизатор. Цели создаются через JES (у него есть add_goal), пополнение —
кнопкой «Отложить» + число.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import analysis
from .. import cache
from .. import finance as fin
from .. import goals as goals_mod
from .. import habits
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
        return ui.join(header, ui.card(f"<b>{'Oxirgi' if uz else 'Последние'}</b>", lines))

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
    await state.set_state(BotStates.waiting_task_input)
    await state.update_data(tasks_done_view=False)
    await remember_panel(callback, state)
    await safe_edit(callback, ui.join(header, body), notes_keyboard(notes, profile.lang))


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
_GOAL_ICON = {"save": "🎯", "spend_cap": "💸", "weight": "⚖️", "habit": "🔁", "custom": "🏁"}


def _goal_lines(st: dict[str, Any], profile: Profile, *, meals: dict[str, Any] | None = None) -> list[str]:
    return goals_mod.lines(st, profile.lang, meals=meals)


async def _goal_statuses(profile: Profile, goals: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        statuses, data = await goals_mod.statuses_for(profile, goals=goals)
        return statuses, data.meals
    except Exception:
        logger.exception("goal statuses failed")
        rows = goals if goals is not None else await services.goals(profile.telegram_id)
        return [analysis.goal_status(g, profile.today) | {"kind": "save", "flags": []} for g in rows], {}


async def _habits_block(profile: Profile) -> str | None:
    """Карточка «Что я о тебе знаю» — привычки из данных (еда по слотам, средние траты)."""
    try:
        logs, entries = await services.calorie_logs(profile, 30), await services.finance_entries(profile.telegram_id)
        lines = habits.habits_card(habits.meal_patterns(logs, tz=profile.tz, today=profile.today), habits.spending_patterns(entries, today=profile.today), profile.lang)
    except Exception:
        logger.debug("habits card failed", exc_info=True)
        return None
    if not lines:
        return None
    return ui.card(f"🧠 <b>{'Odatlaringiz' if profile.lang == 'uz' else 'Что я о тебе знаю'}</b>", lines)


async def render_goals(target: Message | CallbackQuery, state: FSMContext, profile: Profile, *, notice: str | None = None) -> None:
    uz = profile.lang == "uz"
    if not await db.ensure_available("savings_goals"):
        text, goals = "⚠️ " + profile.tr(*MIGRATION_HINT), []
    else:
        goals = await services.goals(profile.telegram_id)
        statuses, meals = await _goal_statuses(profile, goals) if goals else ([], {})
        header = ui.title("🎯", "Maqsadlar" if uz else "Цели")
        blocks: list[str | None] = [header]
        for st in statuses:
            icon = _GOAL_ICON.get(str(st.get("kind") or "save"), "🎯")
            blocks.append(ui.card(f"{icon} <b>{h(st.get('title'))}</b>", _goal_lines(st, profile, meals=meals)))
        if not goals:
            blocks.append(ui.muted("Maqsadlar yo'q." if uz else "Целей пока нет."))
        blocks.append(await _habits_block(profile))
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
            "Напиши цель своими словами — любую:\n<code>накопить 10 млн на ноутбук к январю</code>\n<code>сэкономить 2 млн в этом месяце</code>\n"
            "<code>набрать до 75 кг к декабрю</code> · <code>сбросить до 80</code>\n<code>зал 3 раза в неделю</code>\n<code>выучить 500 слов к марту</code>",
            "Maqsadni o'z so'zingiz bilan yozing:\n<code>yanvargacha noutbukka 10 mln</code>\n<code>shu oyda 2 mln tejash</code>\n"
            "<code>dekabrgacha 75 kg</code> · <code>80 kg gacha tushish</code>\n<code>zal haftasiga 3 marta</code>\n<code>martgacha 500 so'z</code>",
        ),
        goals_keyboard([], profile.lang),
    )


async def _find_goal(profile: Profile, goal_id: str) -> dict[str, Any] | None:
    rows = [r for r in await db.list_goals(profile.telegram_id, include_done=True) if str(r.get("id")) == goal_id]
    return rows[0] if rows else None


async def _render_goal_detail(callback: CallbackQuery, state: FSMContext, profile: Profile, goal: dict[str, Any], *, notice: str | None = None) -> None:
    statuses, meals = await _goal_statuses(profile, [goal])
    st = statuses[0] if statuses else analysis.goal_status(goal, profile.today) | {"kind": "save", "flags": []}
    kind = goals_mod.kind_of(goal)
    await state.set_state(BotStates.waiting_goal_input)
    await remember_panel(callback, state)
    text = ui.join(ui.title(_GOAL_ICON.get(kind, "🎯"), h(goal.get("title"))), ui.card(f"<b>{'Holat' if profile.lang == 'uz' else 'Прогресс'}</b>", _goal_lines(st, profile, meals=meals)))
    if notice:
        text += f"\n\n{notice}"
    await safe_edit(callback, text, goal_detail_keyboard(goal["id"], profile.lang, kind=kind, today_checked=bool(st.get("today_checked"))))


@router.callback_query(F.data.startswith("goal:view:"))
async def cb_goal_view(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    goal = await _find_goal(profile, callback.data.split(":")[-1])
    if goal is None:
        await render_goals(callback, state, profile)
        return
    await _render_goal_detail(callback, state, profile, goal)


@router.callback_query(F.data.startswith("goal:check:"))
@router.callback_query(F.data.startswith("goal:uncheck:"))
async def cb_goal_check(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    goal = await _find_goal(profile, callback.data.split(":")[-1])
    if goal is None:
        await answer_now(callback)
        await render_goals(callback, state, profile)
        return
    if not await db.ensure_available("goal_checkins"):
        await answer_now(callback, profile.tr("Нужна миграция 007_goals.sql", "007_goals.sql migratsiyasi kerak"))
        return
    if callback.data.startswith("goal:uncheck:"):
        await services.uncheck(profile.telegram_id, goal_id=goal["id"], day=profile.today)
        undo.remember(profile.telegram_id, {"type": "restore_checkin", "goal_id": goal["id"], "day": profile.today.isoformat()})
        await answer_now(callback, profile.tr("Отметка снята", "Belgi olib tashlandi"))
    else:
        await services.checkin(profile.telegram_id, goal_id=goal["id"], day=profile.today)
        undo.remember(profile.telegram_id, {"type": "delete_checkin", "goal_id": goal["id"], "day": profile.today.isoformat()})
        await answer_now(callback, profile.tr("Отмечено ✅", "Belgilandi ✅"))
    await _render_goal_detail(callback, state, profile, goal)


@router.callback_query(F.data.startswith("goal:deposit:"))
@router.callback_query(F.data.startswith("goal:weight:"))
@router.callback_query(F.data.startswith("goal:progress:"))
@router.callback_query(F.data.startswith("goal:limit:"))
async def cb_goal_deposit(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    mode = callback.data.split(":")[1]
    goal = await _find_goal(profile, callback.data.split(":")[-1])
    if goal is None:
        await render_goals(callback, state, profile)
        return
    await state.set_state(BotStates.waiting_goal_amount)
    await state.update_data(goal_id=str(goal["id"]), goal_mode=mode)
    await remember_panel(callback, state)
    title = h(goal.get("title"))
    prompts = {
        "deposit": (f"Сколько отложил на «{title}»? Напиши сумму: <code>500000</code> · <code>1.5 млн</code> · <code>-200к</code> (снять)",
                    f"«{title}» uchun qancha qo'shdingiz? Summa: <code>500000</code> · <code>1.5 mln</code> · <code>-200k</code>"),
        "weight": ("Текущий вес, кг: <code>72.5</code>", "Hozirgi vazn, kg: <code>72.5</code>"),
        "progress": (f"«{title}» — на сколько процентов готово? <code>60</code>", f"«{title}» — necha foiz bajarildi? <code>60</code>"),
        "limit": (f"Новый лимит в месяц для «{title}»: <code>4 млн</code>", f"«{title}» uchun yangi oylik limit: <code>4 mln</code>"),
    }
    await safe_edit(callback, profile.tr(*prompts.get(mode, prompts["deposit"])), goal_detail_keyboard(goal["id"], profile.lang, kind=goals_mod.kind_of(goal)))


@router.message(BotStates.waiting_goal_amount, F.text)
async def msg_goal_amount(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()
    await safe_delete(message)
    data = await state.get_data()
    mode = str(data.get("goal_mode") or "deposit")
    goal = await _find_goal(profile, str(data.get("goal_id") or ""))
    parsed = fin.parse_amount(text)
    if goal is None or parsed is None:
        await render_goals(message, state, profile, notice=profile.tr("Не понял число.", "Raqamni tushunmadim.") if goal else None)
        return
    value = parsed[0]
    uid = profile.telegram_id
    if mode == "weight":
        if not (25 <= value <= 350) or not await db.ensure_available("weight_logs"):
            await render_goals(message, state, profile, notice=profile.tr("Вес должен быть 25–350 кг (и нужна миграция 007).", "Vazn 25–350 kg bo'lishi kerak."))
            return
        prev = next((r for r in await services.weight_logs(uid) if str(r.get("day"))[:10] == profile.today.isoformat()), None)
        await services.log_weight(uid, weight=value, day=profile.today)
        undo.remember(uid, {"type": "restore_weight", "day": profile.today.isoformat(), "row": prev})
        notice = f"⚖️ {value:g} {'kg' if profile.lang == 'uz' else 'кг'} ✅"
    elif mode == "progress":
        pct = max(0.0, min(100.0, value))
        before = float(goal.get("saved_amount") or 0)
        fields: dict[str, Any] = {"saved_amount": pct}
        if pct >= 100:
            fields["done"] = True
        await db.update_goal(uid, goal["id"], fields)
        undo.remember(uid, {"type": "goal_fields", "goal_id": goal["id"], "fields": {"saved_amount": before, "done": False}})
        notice = f"📈 {h(goal.get('title'))}: {int(pct)}%" + (" 🎉" if pct >= 100 else "")
    elif mode == "limit":
        before = float(goal.get("target_amount") or 0)
        await db.update_goal(uid, goal["id"], {"target_amount": value})
        undo.remember(uid, {"type": "goal_fields", "goal_id": goal["id"], "fields": {"target_amount": before}})
        notice = f"💸 {h(goal.get('title'))}: {fin.fmt_money(value)}/{'oy' if profile.lang == 'uz' else 'мес'}"
    else:
        amount = -value if text.lstrip().startswith("-") else value
        before = float(goal.get("saved_amount") or 0)
        after = max(0.0, before + amount)
        await db.update_goal(uid, goal["id"], {"saved_amount": after})
        undo.remember(uid, {"type": "goal_fields", "goal_id": goal["id"], "fields": {"saved_amount": before}})
        target = float(goal.get("target_amount") or 0)
        notice = f"✅ {h(goal.get('title'))}: {fin.fmt_money(after)} / {fin.fmt_money(target)}" + (" 🎉" if target and after >= target else "")
    cache.invalidate(uid, "goals")
    await render_goals(message, state, profile, notice=notice)


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
    """Текст на экране целей — JES (add_goal / update_goal), суммы — в общий маршрут."""
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
