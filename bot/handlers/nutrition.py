"""Питание: панель, настройка профиля, ввод еды (фото/текст/голос), дневник."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import emoji as pe
from .. import finance as fin
from .. import nutrition as nutri
from .. import services
from .. import ui
from .. import vacancy as vac
from ..context import ai, db
from ..keyboards import (
    back_to_menu_keyboard,
    calorie_confirm_keyboard,
    calorie_delete_confirm_keyboard,
    calorie_detail_keyboard,
    calorie_meals_keyboard,
    calorie_panel_keyboard,
    nutrition_goal_keyboard,
)
from ..profile import Profile, h
from ..states import BotStates
from .common import (
    answer_now,
    get_photo_bytes,
    get_profile,
    message_text,
    remember_panel,
    safe_delete,
    safe_edit,
    show_panel,
    show_progress,
    transcribe_audio,
)

router = Router(name="nutrition")
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ texts
def setup_text(lang: str) -> str:
    return (
        "🍽️ <b>Oziqlanish / Profil sozlamasi</b>\n\n"
        "1) Maqsadni tanlang.\n2) Profilni kiriting: <b>vazn;bo'y;yosh</b>.\n"
        "3) Bot kunlik BJU rejani hisoblaydi.\n\n<i>Misol: <code>82;178;27</code></i>"
        if lang == "uz"
        else "🍽️ <b>Питание / Настройка профиля</b>\n\n"
        "1) Выбери цель.\n2) Введи профиль: <b>вес;рост;возраст</b>.\n"
        "3) Бот рассчитает персональный дневной план КБЖУ.\n\n<i>Пример: <code>82;178;27</code></i>"
    )


def _quick_labels(items: list[dict[str, Any]], lang: str) -> list[str]:
    unit = "kkal" if lang == "uz" else "ккал"
    labels = []
    for item in items:
        desc = str(item.get("meal_desc") or "").strip()
        if not desc:
            continue
        kcal = item.get("calories")
        labels.append(f"🍽 {desc[:22]} · {int(round(float(kcal)))} {unit}" if kcal is not None else f"🍽 {desc[:30]}")
    return labels


async def build_panel(profile: Profile) -> tuple[str, list[str], list[dict[str, Any]]]:
    nutrition_profile, logs, quick = await asyncio.gather(
        services.nutrition_profile(profile.telegram_id),
        services.today_calorie_logs(profile),
        services.top_meals(profile),
    )
    lang = profile.lang
    uz = lang == "uz"
    labels = _quick_labels(quick, lang)
    header = ui.title(pe.NUTRITION, "Oziqlanish" if uz else "Питание", ui.human_date(profile.today, lang))
    if not nutrition_profile:
        text = ui.join(header, ui.muted("Avval maqsad va profilni sozlang — «Maqsad va profil»." if uz else "Сначала настрой цель и профиль — кнопка «Цель и профиль»."))
        return text, labels, quick

    totals = nutri.totals(logs)
    target = float(nutrition_profile.get("daily_calories") or 0)
    eaten = float(totals["calories"])
    left = max(0.0, target - eaten)
    ratio = (eaten / target) if target > 0 else 0.0
    p, f_, c = (float(nutrition_profile.get(k) or 0) for k in ("protein", "fat", "carbs"))
    unit = "kkal" if uz else "ккал"

    goal_line = f"🎯 {h(nutrition_profile.get('title') or '-')}"
    if nutrition_profile.get("weight") and nutrition_profile.get("height") and nutrition_profile.get("age"):
        w, ht, age = float(nutrition_profile["weight"]), int(float(nutrition_profile["height"])), int(nutrition_profile["age"])
        goal_line += ui.muted(f" · {w:.1f} kg / {ht} sm / {age} yosh" if uz else f" · {w:.1f} кг / {ht} см / {age} лет")
    day_lines = [
        goal_line,
        f"{fin.bar(ratio, 12)} {ui.pct(ratio)}",
        f"<b>{int(eaten)}</b> / {int(target)} {unit} · {'qoldi' if uz else 'осталось'} <b>{int(left)}</b>",
        f"🥩 {int(totals['protein'])}/{int(p)}   🧈 {int(totals['fat'])}/{int(f_)}   🍞 {int(totals['carbs'])}/{int(c)} g",
    ]
    day_card = ui.card(f"<b>{'Bugun' if uz else 'Сегодня'}</b>", day_lines)

    meals_card = None
    if logs:
        meal_lines = []
        for row in logs[:8]:
            kcal = row.get("calories")
            meal_lines.append(f"• {h(str(row.get('meal_desc') or '')[:40])} — <b>{int(float(kcal)) if kcal is not None else '—'}</b>")
        meals_card = ui.card(f"<b>{'Qabullar' if uz else 'Приёмы'}</b> · {int(totals['meals'])}", meal_lines)

    hint = ui.muted("📷 rasm · «osh yedim» · ovozli xabar" if uz else "📷 фото блюда · «съел плов и салат» · голос")
    return ui.join(header, day_card, meals_card, hint), labels, quick


async def render_panel(target: Message | CallbackQuery, state: FSMContext, profile: Profile, *, notice: str | None = None) -> None:
    text, labels, quick = await build_panel(profile)
    if notice:
        text += f"\n\n{notice}"
    await state.set_state(BotStates.waiting_calorie_input)
    await state.update_data(pending_calorie_items=None, pending_nutri_goal=None, quick_meals=quick)
    kb = calorie_panel_keyboard(labels, profile.lang)
    if isinstance(target, CallbackQuery):
        await remember_panel(target, state)
        await safe_edit(target, text, kb)
    else:
        await show_panel(target, state, text, kb)


# ------------------------------------------------------------------ panel / goals
@router.callback_query(F.data.in_({"menu:calorie", "calorie:panel"}))
async def cb_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    nutrition_profile = await services.nutrition_profile(profile.telegram_id)
    if not nutrition_profile:
        await state.set_state(BotStates.waiting_nutrition_goal)
        await remember_panel(callback, state)
        await safe_edit(callback, setup_text(profile.lang), nutrition_goal_keyboard(profile.lang))
        return
    await render_panel(callback, state, profile)


@router.callback_query(F.data == "calorie:goals")
async def cb_goals(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    await state.set_state(BotStates.waiting_nutrition_goal)
    await remember_panel(callback, state)
    await safe_edit(callback, setup_text(profile.lang), nutrition_goal_keyboard(profile.lang))


@router.callback_query(F.data.startswith("nutri:set:"))
async def cb_nutri_set(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    mode = callback.data.split(":")[-1]
    await remember_panel(callback, state)
    if mode == "custom":
        await state.set_state(BotStates.waiting_nutrition_custom)
        await safe_edit(
            callback,
            profile.tr(
                "Ручной план: <b>калории;белки;жиры;углеводы</b>\nПример: <code>2400;160;70;260</code>",
                "Qo'lda reja: <b>kaloriya;oqsil;yog';uglevod</b>\nMisol: <code>2400;160;70;260</code>",
            ),
            back_to_menu_keyboard(profile.lang),
        )
        return
    if mode not in {"loss", "maintain", "gain", "muscle"}:
        mode = "maintain"
    await state.set_state(BotStates.waiting_nutrition_profile)
    await state.update_data(pending_nutri_goal=mode)
    title = nutri.goal_title(mode, profile.lang)
    await safe_edit(
        callback,
        profile.tr(
            f"🎯 <b>Цель: {title}</b>\n\nВведи профиль: <b>вес;рост;возраст</b>\nПример: <code>82;178;27</code>",
            f"🎯 <b>Maqsad: {title}</b>\n\nProfilni kiriting: <b>vazn;bo'y;yosh</b>\nMisol: <code>82;178;27</code>",
        ),
        back_to_menu_keyboard(profile.lang),
    )


@router.message(BotStates.waiting_nutrition_goal)
async def msg_goal_invalid(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    await show_panel(message, state, setup_text(profile.lang), nutrition_goal_keyboard(profile.lang))


@router.message(BotStates.waiting_nutrition_profile, F.text)
async def msg_profile(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    parsed = nutri.parse_profile(message.text or "")
    if parsed is None:
        await show_panel(
            message,
            state,
            profile.tr(
                "Неверный формат.\nНужно: <b>вес;рост;возраст</b>\nПример: <code>82;178;27</code>",
                "Format noto'g'ri.\nKerak: <b>vazn;bo'y;yosh</b>\nMisol: <code>82;178;27</code>",
            ),
            back_to_menu_keyboard(profile.lang),
        )
        return
    data = await state.get_data()
    mode = str(data.get("pending_nutri_goal") or "maintain")
    plan = nutri.plan_from_profile(mode, *parsed, profile.lang)
    await services.save_nutrition_profile(profile.telegram_id, plan)
    await render_panel(message, state, profile, notice=profile.tr("✅ План рассчитан и сохранён.", "✅ Reja hisoblandi va saqlandi."))


@router.message(BotStates.waiting_nutrition_profile)
async def msg_profile_invalid(message: Message) -> None:
    await safe_delete(message)


@router.message(BotStates.waiting_nutrition_custom, F.text)
async def msg_custom(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    plan = nutri.parse_custom_plan(message.text or "")
    if plan is None:
        await show_panel(
            message,
            state,
            profile.tr("Формат: калории;белки;жиры;углеводы. Пример: <code>2400;160;70;260</code>", "Format: kaloriya;oqsil;yog';uglevod. Misol: <code>2400;160;70;260</code>"),
            back_to_menu_keyboard(profile.lang),
        )
        return
    await services.save_nutrition_profile(profile.telegram_id, {"mode": "custom", "title": nutri.goal_title("custom", profile.lang), **plan})
    await render_panel(message, state, profile, notice=profile.tr("✅ План сохранён.", "✅ Reja saqlandi."))


@router.message(BotStates.waiting_nutrition_custom)
async def msg_custom_invalid(message: Message) -> None:
    await safe_delete(message)


# ------------------------------------------------------------------ input
def _format_pending(items: list[dict[str, Any]], lang: str, transcript: str | None = None) -> str:
    if not items:
        return "Saqlash uchun ma'lumot yo'q." if lang == "uz" else "Нет данных для сохранения."
    title = "🍽️ <b>Oziqlanish / Tekshiruv</b>" if lang == "uz" else "🍽️ <b>Питание / Проверка</b>"
    lines = [title, ""]
    total = {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
    for idx, item in enumerate(items[:8], start=1):
        kcal = item.get("calories")
        prefix = f"{idx}. " if len(items) > 1 else ""
        lines.append(f"{prefix}<b>{h(item.get('meal_desc') or '-')}</b> — {int(float(kcal)) if kcal is not None else '—'} {'kkal' if lang == 'uz' else 'ккал'}")
        for key in total:
            if item.get(key) is not None:
                total[key] += float(item[key])
    conf = items[0].get("confidence") if len(items) == 1 else None
    lines.append("")
    if lang == "uz":
        lines.append(f"🥩 Oqsil {int(total['protein'])} g · 🧈 Yog' {int(total['fat'])} g · 🍞 Uglevod {int(total['carbs'])} g")
        if len(items) > 1:
            lines.append(f"<b>Jami: {int(total['calories'])} kkal</b>")
        if conf is not None:
            lines.append(f"<i>AI ishonchliligi: {round(float(conf) * 100)}%</i>")
        lines += ["", "Kunlik jurnalga saqlaymizmi?"]
    else:
        lines.append(f"🥩 Белки {int(total['protein'])} г · 🧈 Жиры {int(total['fat'])} г · 🍞 Углеводы {int(total['carbs'])} г")
        if len(items) > 1:
            lines.append(f"<b>Итого: {int(total['calories'])} ккал</b>")
        if conf is not None:
            lines.append(f"<i>Уверенность AI: {round(float(conf) * 100)}%</i>")
        lines += ["", "Сохранить в дневник?"]
    if transcript:
        lines.append("")
        lines.append(("<b>Aniqlandi:</b> " if lang == "uz" else "<b>Распознано:</b> ") + f"<code>{h(transcript[:280])}</code>")
    return "\n".join(lines)


AUTO_SAVE_CONFIDENCE = 0.9


async def capture_origin(state: FSMContext) -> None:
    """Откуда пришла еда: панель «Питание» (panel) или чат/главный экран (menu)."""
    current = await state.get_state()
    data = await state.get_data()
    if current == BotStates.waiting_calorie_input.state:
        origin = "panel"
    elif current == BotStates.waiting_calorie_confirm.state and data.get("pending_origin"):
        origin = str(data["pending_origin"])
    else:
        origin = "menu"
    await state.update_data(pending_origin=origin)


async def commit_items(target: Message | CallbackQuery, state: FSMContext, profile: Profile, items: list[dict[str, Any]]) -> None:
    """Записать в дневник, показать «✅ Записано …» с отменой и вернуться на исходный экран."""
    from .. import cache

    try:
        inserted = await services.add_calorie_logs(profile.telegram_id, items)
    except Exception as exc:
        logger.exception("Calorie save failed")
        text = f"{pe.CROSS} {profile.tr('Ошибка сохранения', 'Saqlash xatosi')}: {h(str(exc)[:120])}"
        if isinstance(target, CallbackQuery):
            await safe_edit(target, text, back_to_menu_keyboard(profile.lang))
        else:
            await show_panel(target, state, text, back_to_menu_keyboard(profile.lang))
        return
    uz = profile.lang == "uz"
    parts = [f"{h(i.get('meal_desc') or '-')} — {int(float(i.get('calories') or 0))} {'kkal' if uz else 'ккал'}" for i in items]
    notice = f"{pe.CHECK} <b>{'Yozildi' if uz else 'Записано'}:</b> " + " · ".join(parts)
    ids = [r.get("id") for r in (inserted or []) if r.get("id") is not None]
    if ids:
        cache.put(profile.telegram_id, ("undo",), {"type": "delete_calorie_logs", "ids": ids}, 1800)
    origin = str((await state.get_data()).get("pending_origin") or "menu")
    if origin == "panel":
        await render_panel(target, state, profile, notice=notice)
        return
    from .menu import render_dashboard

    await render_dashboard(target, state, profile, notice=notice, undo=bool(ids))


async def _ask_confirm(message: Message, state: FSMContext, profile: Profile, items: list[dict[str, Any]], *, transcript: str | None = None) -> None:
    if items and all(float(i.get("confidence") or 0) >= AUTO_SAVE_CONFIDENCE for i in items):
        await commit_items(message, state, profile, items)
        return
    await state.set_state(BotStates.waiting_calorie_confirm)
    await state.update_data(pending_calorie_items=items)
    await show_panel(message, state, _format_pending(items, profile.lang, transcript), calorie_confirm_keyboard(profile.lang))


async def handle_photo(message: Message, state: FSMContext, profile: Profile) -> None:
    await capture_origin(state)
    await show_progress(message, profile.tr("⏳ Анализирую фото…", "⏳ Rasm tahlil qilinmoqda…"))
    hint = message_text(message) or None
    try:
        image_bytes, mime_type, file_id = await get_photo_bytes(message)
        await safe_delete(message)
        estimate = await ai.estimate_calories_by_photo(image_bytes, mime_type, hint=hint)
    except Exception as exc:
        logger.exception("Calorie photo analyze failed")
        await render_panel(message, state, profile, notice=f"{pe.CROSS} {profile.tr('Ошибка анализа фото', 'Rasm tahlili xatosi')}: {h(str(exc)[:120])}")
        return
    await _ask_confirm(message, state, profile, [nutri.pending_item(estimate, photo_url=f"tg_file:{file_id}")])


async def handle_text(
    message: Message, state: FSMContext, profile: Profile, raw_text: str, *, transcript: str | None = None, reroute: bool = True
) -> None:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        await safe_delete(message)
        await render_panel(message, state, profile, notice=profile.tr("Нужен текст блюда или фото.", "Taom matni yoki rasmi kerak."))
        return
    await capture_origin(state)
    if reroute:
        from .agent import handle_command, looks_like_command

        if looks_like_command(raw_text) and await handle_command(message, state, profile, raw_text):
            return
    if reroute and (fin.looks_like_finance(raw_text) or vac.looks_like_vacancy(raw_text)):
        from .inbox import route_text  # локальный импорт: избегаем цикла

        if await route_text(message, state, profile, raw_text, transcript=transcript, skip_food=True):
            return
    await safe_delete(message)
    await show_progress(message, profile.tr("⏳ Считаю КБЖУ…", "⏳ BJU hisoblanmoqda…"))
    try:
        estimates = await ai.parse_nutrition_items(raw_text)
    except Exception as exc:
        logger.exception("Calorie text analyze failed")
        await render_panel(message, state, profile, notice=f"{pe.CROSS} {profile.tr('Ошибка анализа', 'Tahlil xatosi')}: {h(str(exc)[:120])}")
        return
    if not estimates:
        await render_panel(message, state, profile, notice=profile.tr("Не смог распознать еду в сообщении.", "Xabarda taom aniqlanmadi."))
        return
    await _ask_confirm(message, state, profile, [nutri.pending_item(e) for e in estimates], transcript=transcript)


async def handle_voice(message: Message, state: FSMContext, profile: Profile, transcript: str | None = None) -> None:
    if transcript is None:
        await show_progress(message, profile.tr("⏳ Распознаю голос…", "⏳ Ovoz aniqlanmoqda…"))
        try:
            transcript = await transcribe_audio(message)
        except Exception as exc:
            logger.exception("Calorie voice transcribe failed")
            await safe_delete(message)
            await render_panel(message, state, profile, notice=f"{pe.CROSS} {profile.tr('Ошибка распознавания', 'Ovozni aniqlash xatosi')}: {h(str(exc)[:120])}")
            return
    await handle_text(message, state, profile, transcript or "", transcript=transcript)


@router.message(BotStates.waiting_calorie_input, F.photo)
@router.message(BotStates.waiting_calorie_confirm, F.photo)
async def msg_input_photo(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    caption = message_text(message)
    if caption and vac.looks_like_vacancy(caption):
        from .vacancy import process_vacancy

        await process_vacancy(message, state, profile, caption)
        return
    await handle_photo(message, state, profile)


@router.message(BotStates.waiting_calorie_input, F.voice | F.audio)
@router.message(BotStates.waiting_calorie_confirm, F.voice | F.audio)
async def msg_input_voice(message: Message, state: FSMContext) -> None:
    await handle_voice(message, state, await get_profile(message.from_user))


@router.message(BotStates.waiting_calorie_input, F.text)
@router.message(BotStates.waiting_calorie_confirm, F.text)
async def msg_input_text(message: Message, state: FSMContext) -> None:
    # в состоянии подтверждения новый текст = новая запись (старая отменяется)
    profile = await get_profile(message.from_user)
    text = (message.text or "").strip()
    if text.startswith("/"):
        await safe_delete(message)
        return
    await handle_text(message, state, profile, text)


@router.message(BotStates.waiting_calorie_input)
@router.message(BotStates.waiting_calorie_confirm)
async def msg_input_invalid(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    await safe_delete(message)
    await render_panel(message, state, profile, notice=profile.tr("Отправь фото, текст или голос.", "Rasm, matn yoki ovoz yuboring."))


# ------------------------------------------------------------------ confirm / quick / diary
@router.callback_query(F.data == "calorie:confirm")
async def cb_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    items = (await state.get_data()).get("pending_calorie_items") or []
    if not items:
        await answer_now(callback, profile.tr("Нет данных для сохранения", "Saqlash uchun ma'lumot yo'q"), alert=True)
        return
    await answer_now(callback, profile.tr("Сохранено ✅", "Saqlandi ✅"))
    await commit_items(callback, state, profile, items)


@router.callback_query(F.data == "calorie:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    if str((await state.get_data()).get("pending_origin") or "menu") == "panel":
        await render_panel(callback, state, profile)
        return
    from .menu import render_dashboard

    await render_dashboard(callback, state, profile)


@router.callback_query(F.data.startswith("calorie:quick:"))
async def cb_quick(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        await answer_now(callback)
        return
    quick = (await state.get_data()).get("quick_meals") or await services.top_meals(profile)
    if idx < 0 or idx >= len(quick):
        await answer_now(callback, profile.tr("Не найдено, обнови панель", "Topilmadi, panelni yangilang"), alert=True)
        return
    item = quick[idx]
    await answer_now(callback, profile.tr("Добавлено ✅", "Qo'shildi ✅"))
    await services.add_calorie_logs(
        profile.telegram_id,
        [{"meal_desc": item.get("meal_desc"), "calories": item.get("calories"), "protein": item.get("protein"), "fat": item.get("fat"), "carbs": item.get("carbs")}],
    )
    await render_panel(callback, state, profile)


def _period_days(code: str) -> int:
    return {"day": 1, "week": 7, "month": 30}.get(code, 1)


@router.callback_query(F.data.startswith("calorie:meals:"))
async def cb_meals(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    period = callback.data.split(":")[-1]
    period = period if period in {"day", "week", "month"} else "day"
    logs = await services.today_calorie_logs(profile) if period == "day" else await services.calorie_logs(profile, _period_days(period))
    lang = profile.lang
    labels = {"day": ("День", "Kun"), "week": ("Неделя", "Hafta"), "month": ("Месяц", "Oy")}[period]
    lines = [
        "🍽️ <b>Oziqlanish / Qabullar</b>" if lang == "uz" else "🍽️ <b>Питание / Приёмы</b>",
        f"<i>{'Davr' if lang == 'uz' else 'Период'}: {labels[1] if lang == 'uz' else labels[0]}</i>",
        "",
    ]
    if not logs:
        lines.append("Qabullar topilmadi." if lang == "uz" else "Приёмы не найдены.")
    else:
        last_day = None
        total = 0.0
        for row in logs[:40]:
            day = str(row.get("created_at") or "")[:10]
            if day != last_day:
                if last_day is not None:
                    lines.append("")
                try:
                    from datetime import date as _d
                    lines.append(f"<b>{_d.fromisoformat(day).strftime('%d.%m.%Y')}</b>")
                except Exception:
                    lines.append(f"<b>{day}</b>")
                last_day = day
            kcal = row.get("calories")
            total += float(kcal or 0)
            lines.append(f"• {h(str(row.get('meal_desc') or '')[:48])} ({int(float(kcal)) if kcal is not None else '—'} {'kkal' if lang == 'uz' else 'ккал'})")
        lines += ["", f"<b>{'Jami' if lang == 'uz' else 'Итого'}: {int(total)} {'kkal' if lang == 'uz' else 'ккал'}</b>"]
    await state.set_state(BotStates.waiting_calorie_input)
    await remember_panel(callback, state)
    await safe_edit(callback, "\n".join(lines), calorie_meals_keyboard(logs, period, lang))


@router.callback_query(F.data.startswith("calorie:view:"))
async def cb_view(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    log_id = callback.data.split(":")[-1]
    log = await db.get_calorie_log(profile.telegram_id, log_id)
    if not log:
        await answer_now(callback, profile.tr("Запись не найдена", "Yozuv topilmadi"), alert=True)
        return
    lang = profile.lang

    def v(key: str) -> str:
        val = log.get(key)
        return "-" if val is None else str(int(float(val))) if key != "confidence" else f"{round(float(val) * 100)}%"

    text = (
        f"🍽️ <b>Taom / Tafsilot</b>\n\nNomi: <b>{h(log.get('meal_desc') or '-')}</b>\nKkal: {v('calories')}\nOqsil: {v('protein')} g\nYog': {v('fat')} g\nUglevod: {v('carbs')} g\nAI ishonchliligi: <i>{v('confidence')}</i>"
        if lang == "uz"
        else f"🍽️ <b>Питание / Детали блюда</b>\n\nНазвание: <b>{h(log.get('meal_desc') or '-')}</b>\nКалории: {v('calories')}\nБелки: {v('protein')} г\nЖиры: {v('fat')} г\nУглеводы: {v('carbs')} г\nУверенность AI: <i>{v('confidence')}</i>"
    )
    await state.set_state(BotStates.waiting_calorie_input)
    await remember_panel(callback, state)
    await safe_edit(callback, text, calorie_detail_keyboard(log_id, lang))


@router.callback_query(F.data.startswith("calorie:ask_del:"))
async def cb_ask_delete(callback: CallbackQuery) -> None:
    await answer_now(callback)
    profile = await get_profile(callback.from_user)
    log_id = callback.data.split(":")[-1]
    await safe_edit(
        callback,
        profile.tr("Удалить запись о блюде?", "Taom yozuvini o'chiraymi?"),
        calorie_delete_confirm_keyboard(log_id, profile.lang),
    )


@router.callback_query(F.data.startswith("calorie:del:"))
async def cb_delete(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    log_id = callback.data.split(":")[-1]
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    try:
        await services.delete_calorie_log(profile.telegram_id, log_id)
    except Exception:
        logger.exception("Calorie delete failed")
    await render_panel(callback, state, profile)
