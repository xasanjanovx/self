"""Настройки бота (Уведомления · Напоминания · Язык) и отдельный экран «Джарвис»
(кнопка главного меню): звонок, будильник, голос и характер, звонок о важном, утро голосом.

Схема одна для всех разделов: сверху карточка «что сейчас», снизу кнопки. «Назад» из
разделов настроек ведёт в настройки, из будильника и характера — на экран «Джарвис».
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from .. import cache
from .. import emoji as pe
from .. import services
from .. import ui
from ..context import db
from ..keyboards import _btn, notify_keyboard, reminders_keyboard, settings_keyboard
from ..profile import Profile, h
from ..states import BotStates
from .common import answer_now, get_profile, safe_edit

router = Router(name="settings")
logger = logging.getLogger(__name__)

WAKE_MORNING = "wake"  # user_settings.brief_morning_time = «после подъёма»


async def report_prefs(uid: int) -> dict:
    return await cache.remember(uid, ("report_prefs",), 600, lambda: db.get_report_preferences(uid))


async def _show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Показать экран: из кнопки — правкой сообщения, из текста — на живом экране."""
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, kb)
        return
    from .. import screen as screen_mod

    await screen_mod.show_screen(target.bot, target.chat.id, text, kb)


# ------------------------------------------------------------------ краткие состояния разделов
async def alarm_summary(profile: Profile) -> str:
    """«к фаджру · завтра ~04:42 (такбир 05:07)» / «каждый день в 06:30» / «выключен»."""
    from .. import wake as wake_mod
    from .. import wake_runner

    uz = profile.lang == "uz"
    if not db.available("wake_settings"):
        return "—"
    s = wake_mod.WakeSettings.from_row(await services.wake_settings(profile.telegram_id))
    if not s.enabled:
        return "o'chirilgan" if uz else "выключен"
    _, plan = await wake_runner.plan_for(profile, profile.today + timedelta(days=1))
    if s.mode == "fixed":
        head = (f"har kuni {s.fixed_time}" if uz else f"каждый день в {s.fixed_time}") if s.fixed_time else "—"
    else:
        head = "bomdodga" if uz else "к фаджру"
        if plan.active and plan.wake_at:
            head += f" · {'ertaga' if uz else 'завтра'} ~{plan.wake_at:%H:%M}" + (f" (takbir {plan.takbir})" if uz and plan.takbir else f" (такбир {plan.takbir})" if plan.takbir else "")
    if set(s.days_of_week) == {1, 2, 3, 4, 5}:
        head += " · " + ("ish kunlari" if uz else "по будням")
    if s.skip_until and s.skip_until >= profile.today:
        head += " · " + (f"{s.skip_until:%d.%m} gacha uyg'otmayman" if uz else f"не бужу до {s.skip_until:%d.%m}")
    return head


def _morning_label(us: dict[str, Any], uz: bool) -> str:
    if not us.get("brief_morning", True):
        return "o'chiq" if uz else "выкл"
    raw = str(us.get("brief_morning_time") or "08:00")
    return ("turgandan keyin" if uz else "после подъёма") if raw == WAKE_MORNING else raw[:5]


def _evening_label(us: dict[str, Any], uz: bool) -> str:
    if not us.get("brief_evening", True):
        return "o'chiq" if uz else "выкл"
    return str(us.get("brief_evening_time") or "21:00")[:5]


async def _user_settings(uid: int) -> dict[str, Any]:
    if not await db.ensure_available("user_settings"):
        return {}
    return await services.user_settings(uid)


# ------------------------------------------------------------------ главный экран
async def render_settings(target: Message | CallbackQuery, profile: Profile) -> None:
    uz = profile.lang == "uz"
    uid = profile.telegram_id
    us, prefs, rems = await asyncio.gather(_user_settings(uid), report_prefs(uid), services.reminders(uid))
    report = ("yo'q" if uz else "без отчёта") if not prefs.get("enabled", True) else (
        ("oylik hisobot" if uz else "отчёт раз в месяц") if prefs.get("frequency") == "monthly" else ("haftalik hisobot" if uz else "отчёт раз в неделю"))
    lines = [
        f"🔔 <b>{'Bildirishnomalar' if uz else 'Уведомления'}</b> — {'ertalab' if uz else 'утро'} {_morning_label(us, uz)} · "
        f"{'kechqurun' if uz else 'вечер'} {_evening_label(us, uz)} · {report}",
        f"🗒 <b>{'Eslatmalar' if uz else 'Напоминания'}</b> — {len(rems) if rems else ('yo`q' if uz else 'нет')}",
        f"🌐 <b>{'Til' if uz else 'Язык'}</b> — {'O`zbekcha' if uz else 'Русский'}",
    ]
    text = ui.join(ui.title(pe.SETTINGS, "Sozlamalar" if uz else "Настройки"), ui.card(f"<b>{'Hozir' if uz else 'Сейчас'}</b>", lines))
    await _show(target, text, settings_keyboard(profile.lang))


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_settings(callback, await get_profile(callback.from_user))


# ------------------------------------------------------------------ уведомления
async def render_notify(target: Message | CallbackQuery, profile: Profile, *, notice: str | None = None) -> None:
    uz = profile.lang == "uz"
    us, prefs = await asyncio.gather(_user_settings(profile.telegram_id), report_prefs(profile.telegram_id))
    lines = [
        f"🌅 {'Ertalabki xulosa' if uz else 'Утренняя сводка'}: <b>{_morning_label(us, uz)}</b>",
        f"🌙 {'Kechki xulosa' if uz else 'Вечерняя сводка'}: <b>{_evening_label(us, uz)}</b>",
        f"💡 {'Jarvis maslahatlari' if uz else 'Подсказки Джарвиса'}: <b>{'✅' if us.get('proactive', True) else '⛔'}</b>",
        f"🎙 {'Ovozli javoblar' if uz else 'Голосовые ответы'}: <b>{'✅' if us.get('voice_reply', True) else '⛔'}</b>",
    ]
    text = ui.join(ui.title("🔔", "Bildirishnomalar" if uz else "Уведомления"), ui.card(f"<b>{'Hozir' if uz else 'Сейчас'}</b>", lines))
    if not us:
        text += "\n\n⚠️ " + profile.tr("Нужна миграция 004_v2_features.sql", "004_v2_features.sql migratsiyasi kerak")
    if notice:
        text += f"\n\n{notice}"
    kb = notify_keyboard(
        profile.lang, morning=bool(us.get("brief_morning", True)), morning_wake=str(us.get("brief_morning_time") or "") == WAKE_MORNING,
        evening=bool(us.get("brief_evening", True)), proactive=bool(us.get("proactive", True)), voice=bool(us.get("voice_reply", True)),
        report_enabled=bool(prefs.get("enabled", True)), report_frequency=str(prefs.get("frequency") or "weekly"),
    )
    await _show(target, text, kb)


@router.callback_query(F.data == "settings:notify")
async def cb_notify(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_notify(callback, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("settings:toggle:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    """Подсказки Джарвиса и голосовые ответы."""
    profile = await get_profile(callback.from_user)
    field = callback.data.split(":")[-1]
    if field not in {"proactive", "voice_reply"} or not await db.ensure_available("user_settings"):
        await answer_now(callback)
        return
    us = await services.user_settings(profile.telegram_id)
    new_value = not bool(us.get(field, True))
    await services.save_user_settings(profile.telegram_id, {field: new_value})
    await answer_now(callback, "✅" if new_value else "⛔")
    await render_notify(callback, profile)


@router.callback_query(F.data.startswith("settings:brief:"))
async def cb_brief_toggle(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    which = callback.data.split(":")[-1]
    if not await db.ensure_available("user_settings"):
        await answer_now(callback, profile.tr("Нужна миграция 004", "004 migratsiyasi kerak"), alert=True)
        return
    us = await services.user_settings(profile.telegram_id)
    field = "brief_morning" if which == "morning" else "brief_evening"
    new_value = not bool(us.get(field, True))
    await services.save_user_settings(profile.telegram_id, {field: new_value})
    await answer_now(callback, "✅" if new_value else "⛔")
    await render_notify(callback, profile)


@router.callback_query(F.data.startswith("notify:"))
async def cb_notify_change(callback: CallbackQuery, state: FSMContext) -> None:
    """«После подъёма» для утренней сводки и ввод времени сводок."""
    profile = await get_profile(callback.from_user)
    _, action, which = callback.data.split(":")
    if not await db.ensure_available("user_settings"):
        await answer_now(callback, profile.tr("Нужна миграция 004", "004 migratsiyasi kerak"), alert=True)
        return
    if action == "morning" and which == "wake":
        us = await services.user_settings(profile.telegram_id)
        now_wake = str(us.get("brief_morning_time") or "") == WAKE_MORNING
        await services.save_user_settings(profile.telegram_id, {"brief_morning_time": "08:00" if now_wake else WAKE_MORNING, "brief_morning": True})
        await answer_now(callback, "✅")
        await render_notify(callback, profile)
        return
    if action == "time" and which in {"morning", "evening"}:
        await answer_now(callback)
        await state.set_state(BotStates.waiting_brief_time)
        await state.update_data(brief_which=which)
        label = profile.tr("утренней", "ertalabki") if which == "morning" else profile.tr("вечерней", "kechki")
        await safe_edit(callback, profile.tr(f"⌨️ Время {label} сводки? Например <code>7:30</code>", f"⌨️ {label.capitalize()} xulosa vaqti? Masalan <code>7:30</code>"),
                        InlineKeyboardMarkup(inline_keyboard=[[_btn(profile.tr("Отмена", "Bekor"), "settings:notify")]]))
        return
    await answer_now(callback)


@router.message(BotStates.waiting_brief_time, F.text)
async def msg_brief_time(message: Message, state: FSMContext) -> None:
    from ..agent_tools import _time_arg

    profile = await get_profile(message.from_user)
    data = await state.get_data()
    which = str(data.get("brief_which") or "morning")
    hhmm = _time_arg((message.text or "").strip())
    try:
        await message.delete()
    except Exception:
        pass
    if not hhmm:
        await render_notify(message, profile, notice=profile.tr("Не понял время — например, 7:30", "Vaqtni tushunmadim — masalan 7:30"))
        return
    await state.clear()
    field = "brief_morning_time" if which == "morning" else "brief_evening_time"
    enable = "brief_morning" if which == "morning" else "brief_evening"
    await services.save_user_settings(profile.telegram_id, {field: hhmm, enable: True})
    await render_notify(message, profile, notice=f"✅ {hhmm}")


@router.callback_query(F.data.startswith("report:set:"))
async def cb_report_set(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    mode = callback.data.split(":")[-1]
    prefs = await report_prefs(profile.telegram_id)
    if mode == "off":
        enabled, frequency = False, str(prefs.get("frequency") or "weekly")
    else:
        enabled, frequency = True, ("monthly" if mode == "monthly" else "weekly")
    await answer_now(callback, "✅")
    await db.save_report_preferences(profile.telegram_id, enabled=enabled, frequency=frequency, last_sent_key=prefs.get("last_sent_key"))
    cache.invalidate(profile.telegram_id, "report_prefs")
    await render_notify(callback, profile)


# ------------------------------------------------------------------ напоминания
async def render_reminders(callback: CallbackQuery, profile: Profile) -> None:
    from .. import reminders as rem

    uz = profile.lang == "uz"
    rems = await services.reminders(profile.telegram_id)
    body = ui.card(f"<b>{'Faol' if uz else 'Активные'}</b>", [f"• {h(rem.title(r))}" for r in rems]) if rems else ui.muted("Yo'q." if uz else "Пока нет.")
    await safe_edit(callback, ui.join(ui.title("🗒", "Eslatmalar" if uz else "Напоминания"), body),
                    reminders_keyboard(profile.lang, [(str(r.get("id")), rem.title(r)) for r in rems]))


@router.callback_query(F.data == "settings:reminders")
async def cb_reminders(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_reminders(callback, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("settings:rem_del:"))
async def cb_reminder_delete(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    rem_id = callback.data.split(":")[-1]
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    await db.delete_reminder(profile.telegram_id, rem_id)
    services.invalidate_reminders(profile.telegram_id)
    await render_reminders(callback, profile)


# ------------------------------------------------------------------ будильник
async def render_wake(target: Message | CallbackQuery, profile: Profile, *, notice: str | None = None) -> None:
    """Будильник: во сколько разбудит завтра, такбир, намаз, статистика + все переключатели."""
    from .. import caller
    from .. import prayer
    from .. import wake as wake_mod
    from .. import wake_runner
    from ..keyboards import wake_settings_keyboard

    uz = profile.lang == "uz"
    if not await db.ensure_available("wake_settings"):
        await _show(target, "⚠️ " + profile.tr("Нужна миграция 008_wake.sql", "008_wake.sql migratsiyasi kerak"), settings_keyboard(profile.lang))
        return
    s = wake_mod.WakeSettings.from_row(await services.wake_settings(profile.telegram_id))
    tomorrow = profile.today + timedelta(days=1)
    _, plan = await wake_runner.plan_for(profile, tomorrow)
    rows = await prayer.timings(tomorrow, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)

    if plan.active and plan.wake_at:
        head = f"{'Ertaga uyg`otaman' if uz else 'Завтра разбужу'}: <b>~{plan.wake_at:%H:%M}</b>"
    else:
        reason = {"off": ("o'chirilgan", "выключен"), "day_off": ("dam olish kuni", "выходной"), "skip": ("to'xtatilgan", "пауза"),
                  "no_times": ("namoz vaqti yo'q", "нет времени намаза")}.get(plan.reason, ("", ""))
        head = ("Ertaga uyg'otmayman" if uz else "Завтра не бужу") + (f" — {reason[0] if uz else reason[1]}" if reason[0] else "")
    lines = [head]
    if s.mode == "fajr":
        lines.append(f"{'Takbirdan' if uz else 'За'} {s.offset_min} {'daqiqa oldin' if uz else 'мин до такбира'} · "
                     f"{'takbir' if uz else 'такбир'} <b>{plan.takbir or '—'}</b> ({'azon' if uz else 'азан'} {plan.fajr or '—'} +{s.takbir_offset_min})")
    else:
        lines.append(f"{'Aniq vaqt' if uz else 'Точное время'}: <b>{s.fixed_time or '—'}</b>")
    lines.append(f"{'Qo`ng`iroqlar' if uz else 'Звонки'}: {'✅' if caller.available() else '⚠️'}")

    history = await services.wake_history(profile.telegram_id, days=14)
    stats = wake_mod.stats_line(history, profile.lang) if history else None
    text = ui.join(
        ui.title("⏰", "Budilnik" if uz else "Будильник"),
        ui.card(f"<b>{'Hozir' if uz else 'Сейчас'}</b>", lines),
        ui.card(f"<b>{'Namoz (ertaga)' if uz else 'Намаз завтра'}</b>", [prayer.summary(rows, profile.lang, takbir=plan.takbir)]) if rows else None,
        ui.card(f"<b>{'Statistika' if uz else 'Статистика'}</b>", [stats]) if stats else None,
    )
    if notice:
        text += f"\n\n{notice}"
    await _show(target, text, wake_settings_keyboard(
        profile.lang, enabled=s.enabled, call_enabled=s.call_enabled, talk=s.talk, voice_lang=s.voice_lang,
        mode=s.mode, days=list(s.days_of_week), hardness=s.hardness, tasks=list(s.confirm_tasks)))


@router.callback_query(F.data == "settings:wake")
async def cb_wake_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_wake(callback, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("wakeset:"))
async def cb_wake_change(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопки будильника + «Позвонить» (он живёт здесь, чтобы звать из любого раздела)."""
    from .. import call_assistant
    from .. import caller
    from .. import wake as wake_mod

    profile = await get_profile(callback.from_user)
    parts = callback.data.split(":")
    action, value = parts[1], (parts[2] if len(parts) > 2 else "")
    if action == "calltest":
        if not caller.available():
            await answer_now(callback, profile.tr("Звонки не настроены", "Qo'ng'iroq sozlanmagan"), alert=True)
            return
        if call_assistant.call_in_background(profile) is None:
            await answer_now(callback, profile.tr("Уже звоню", "Allaqachon qo'ng'iroq qilyapman"))
            return
        await answer_now(callback, profile.tr("Звоню 📞", "Qo'ng'iroq qilyapman 📞"))
        # экран Джарвиса не оставляем — возвращаем главное меню с пометкой; итог звонка придёт туда же
        from .menu import render_dashboard

        await render_dashboard(callback, state, profile, notice=profile.tr("📞 Звоню… возьми трубку", "📞 Qo'ng'iroq qilyapman… trubkani oling"))
        return
    if not await db.ensure_available("wake_settings"):
        await answer_now(callback, profile.tr("Нужна миграция 008_wake.sql", "008_wake.sql migratsiyasi kerak"), alert=True)
        return
    s = wake_mod.WakeSettings.from_row(await services.wake_settings(profile.telegram_id))
    fields: dict[str, Any] = {}
    notice = None
    if action == "toggle" and value in {"enabled", "call_enabled", "talk"}:
        fields[value] = not getattr(s, value)
    elif action == "toggle" and value == "hardness":
        fields["hardness"] = "normal" if s.hardness == "hard" else "hard"
    elif action == "mode" and value == "fajr":
        fields.update({"mode": "fajr", "enabled": True})
    elif action == "offset":
        fields["offset_min"] = max(0, min(180, s.offset_min + int(value)))
    elif action == "takbir":
        fields["takbir_offset_min"] = max(0, min(120, s.takbir_offset_min + int(value)))
    elif action == "days":
        fields["days_of_week"] = [1, 2, 3, 4, 5] if value == "work" else [1, 2, 3, 4, 5, 6, 7]
    elif action == "task" and value in wake_mod.TASKS:
        tasks = list(s.confirm_tasks)
        if value in tasks and len(tasks) > 1:
            tasks.remove(value)
        elif value not in tasks:
            tasks.append(value)
        else:
            notice = profile.tr("Хотя бы одно задание нужно оставить", "Kamida bitta vazifa qolishi kerak")
        fields["confirm_tasks"] = tasks
    if fields:
        await services.save_wake_settings(profile.telegram_id, fields)
    await answer_now(callback, notice or "✅")
    await render_wake(callback, profile)


@router.message(BotStates.waiting_alarm_time, F.text)
async def msg_alarm_time(message: Message, state: FSMContext) -> None:
    from ..agent_tools import _time_arg

    profile = await get_profile(message.from_user)
    hhmm = _time_arg((message.text or "").strip())
    try:
        await message.delete()
    except Exception:
        pass
    if not hhmm:
        await render_wake(message, profile, notice=profile.tr("Не понял время — например, 6:30", "Vaqtni tushunmadim — masalan 6:30"))
        return
    await state.clear()
    if await db.ensure_available("wake_settings"):
        await services.save_wake_settings(profile.telegram_id, {"mode": "fixed", "fixed_time": hhmm, "enabled": True})
    await render_wake(message, profile, notice=profile.tr(f"⏰ Буду будить в {hhmm}", f"⏰ {hhmm} da uyg'otaman"))


# ------------------------------------------------------------------ джарвис: голос, язык, характер
async def render_jarvis(target: Message | CallbackQuery, profile: Profile, *, notice: str | None = None) -> None:
    from .. import caller
    from .. import persona as persona_mod
    from ..keyboards import jarvis_settings_keyboard

    uz = profile.lang == "uz"
    p = await services.persona(profile.telegram_id)
    voice_ru, voice_uz = persona_mod.VOICES.get(p.voice, ("", ""))
    tone = {"friendly": ("Дружелюбный", "Do'stona"), "calm": ("Спокойный", "Xotirjam"), "strict": ("Строгий", "Qat'iy")}[p.tone]
    length = {"short": ("коротко", "qisqa"), "normal": ("обычно", "o'rtacha"), "detailed": ("подробно", "batafsil")}[p.verbosity]
    lines = [
        f"🔊 {'Ovoz' if uz else 'Голос'}: <b>{voice_uz if uz else voice_ru}</b>",
        f"🗣 {'Qo`ng`iroq tili' if uz else 'Язык звонков'}: <b>{'o`zbekcha' if p.lang == 'uz' else 'русский'}</b>",
        f"🤝 {'Murojaat' if uz else 'Обращение'}: <b>{('siz' if p.address == 'siz' else 'sen') if uz else ('на «вы»' if p.address == 'siz' else 'на «ты»')}</b>"
        f" · <b>{persona_mod.HONORIFICS[p.honorific][1] if uz else persona_mod.HONORIFICS[p.honorific][0]}</b>",
        f"🎭 {'Ohang' if uz else 'Тон'}: <b>{tone[1] if uz else tone[0]}</b> · {'javoblar' if uz else 'ответы'}: <b>{length[1] if uz else length[0]}</b>",
        f"📞 {'Qo`ng`iroqlar' if uz else 'Звонки'}: {'✅' if caller.available() else '⚠️'}",
    ]
    text = ui.join(ui.title("🎭", "Ovoz va xarakter" if uz else "Голос и характер"), ui.card(f"<b>{'Hozir' if uz else 'Сейчас'}</b>", lines))
    if notice:
        text += f"\n\n{notice}"
    kb = jarvis_settings_keyboard(profile.lang, voice=p.voice, call_lang=p.lang, address=p.address, tone=p.tone,
                                  verbosity=p.verbosity, honorific=p.honorific)
    await _show(target, text, kb)


@router.callback_query(F.data == "settings:jarvis")
async def cb_jarvis(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_jarvis(callback, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("jarvis:"))
async def cb_jarvis_change(callback: CallbackQuery, state: FSMContext) -> None:
    from .. import persona as persona_mod

    profile = await get_profile(callback.from_user)
    _, action, *rest = callback.data.split(":")
    value = rest[0] if rest else ""
    if action == "alarm" and value == "time":  # «точное время» будильника — спрашиваем HH:MM
        await answer_now(callback)
        await state.set_state(BotStates.waiting_alarm_time)
        await safe_edit(callback, profile.tr("⏰ Во сколько будить? Например <code>6:30</code>", "⏰ Soat nechada uyg'otay? Masalan <code>6:30</code>"),
                        InlineKeyboardMarkup(inline_keyboard=[[_btn(profile.tr("Отмена", "Bekor"), "settings:wake")]]))
        return
    if not await db.ensure_available("assistant_settings"):
        await answer_now(callback, profile.tr("Нужна миграция 009", "009 migratsiyasi kerak"), alert=True)
        return
    if action == "sample":
        await answer_now(callback, profile.tr("Записываю образец…", "Namuna tayyorlanmoqda…"))
        await _send_voice_sample(callback, profile)
        return
    if action == "toggle" and value in {"alert_calls", "morning_voice"}:  # переключатели на экране «Джарвис»
        p = await services.persona(profile.telegram_id)
        await services.save_persona(profile.telegram_id, {value: not getattr(p, value)})
        await answer_now(callback, "✅")
        await render_jarvis_hub(callback, profile)
        return
    if action == "photo" and value == "off":
        await services.save_persona(profile.telegram_id, {"photo_intent": None, "photo_intent_until": None})
        await answer_now(callback, "✅")
        await render_jarvis_hub(callback, profile)
        return
    fields: dict[str, Any] = {}
    if action == "voice" and value in persona_mod.VOICES:
        fields["voice"] = value
    elif action == "lang" and value in {"uz", "ru"}:
        fields["lang"] = value
    elif action == "address" and value in {"sen", "siz"}:
        fields["address"] = value
    elif action == "tone" and value in persona_mod.TONES:
        fields["tone"] = value
    elif action == "verbosity" and value in persona_mod.VERBOSITY:
        fields["verbosity"] = value
    elif action == "honorific" and value in persona_mod.HONORIFICS:
        fields["honorific"] = value
    if fields:
        await services.save_persona(profile.telegram_id, fields)
    await answer_now(callback, "✅")
    await render_jarvis(callback, profile)
    if "voice" in fields:
        await _send_voice_sample(callback, profile)


async def _send_voice_sample(callback: CallbackQuery, profile: Profile) -> None:
    """Короткое голосовое выбранным голосом — выбрать на слух. Само исчезает через минуту."""
    from .. import screen as screen_mod
    from .. import voice as voice_mod
    from ..context import ai

    p = await services.persona(profile.telegram_id)
    name = p.name_for(profile.first_name)
    phrase = (f"Assalomu alaykum, {name}. Men Jarvisman, yordam berishga tayyorman." if p.lang == "uz"
              else f"Ассалому алайкум, {name}. Я Джарвис, готова помочь.")
    try:
        pcm = await ai.synthesize(phrase, voice=p.voice)
        ogg = await voice_mod.pcm_to_ogg(pcm) if pcm else None
        if not ogg:
            return
        msg = await callback.bot.send_voice(profile.telegram_id, BufferedInputFile(ogg, "jarvis.ogg"))
        screen_mod.track_ephemeral(profile.telegram_id, msg.message_id, ttl=60)
    except Exception:
        logger.warning("voice sample failed", exc_info=True)


# ------------------------------------------------------------------ кнопка «Джарвис» в главном меню
async def _call_stats(profile: Profile, uz: bool) -> str | None:
    """«последний сегодня 01:50 · за неделю 4» — по журналу agent_log (kind=call)."""
    if not db.available("agent_log"):
        return None
    try:
        rows = [r for r in await db.list_agent_log(profile.telegram_id, days=7, limit=200) if r.get("kind") == "call"]
    except Exception:
        return None
    if not rows:
        return "hali yo'q" if uz else "пока не было"
    from datetime import datetime

    last = datetime.fromisoformat(str(rows[0]["created_at"]).replace("Z", "+00:00")).astimezone(profile.tz)
    return (f"oxirgisi {last:%d.%m %H:%M} · haftada {len(rows)}" if uz else f"последний {last:%d.%m %H:%M} · за неделю {len(rows)}")


async def _important_now(profile: Profile) -> list[str]:
    """Что сейчас важно (то, из-за чего Джарвис позвонил бы сам)."""
    import re

    from .. import proactive
    from ..workers import important_alerts

    try:
        alerts = await asyncio.wait_for(proactive.collect(profile), timeout=6)
    except Exception:
        return []
    return [re.sub(r"<[^>]+>", "", a.text)[:120] for a in important_alerts(alerts)[:3]]


async def render_jarvis_hub(target: Message | CallbackQuery, profile: Profile, *, notice: str | None = None) -> None:
    """Экран «Джарвис»: что он сейчас делает для тебя и что помнит + все его настройки."""
    from .. import agent_tools_extra as extra
    from .. import caller
    from .. import persona as persona_mod
    from .. import wake as wake_mod
    from ..keyboards import jarvis_hub_keyboard

    uz = profile.lang == "uz"
    uid = profile.telegram_id
    p, intent, mem = await asyncio.gather(services.persona(uid), services.photo_intent(uid), services.user_memory(uid))
    voice_ru, voice_uz = persona_mod.VOICES.get(p.voice, ("", ""))
    on, off = ("yoniq", "o'chiq") if uz else ("вкл", "выкл")

    now_lines = [f"⏰ {'Budilnik' if uz else 'Будильник'}: <b>{await alarm_summary(profile)}</b>"]
    if db.available("wake_log"):
        history = await services.wake_history(uid, days=14)
        if history and (stats := wake_mod.stats_line(history, profile.lang)):
            now_lines.append(f"🔥 {stats}")
    if (calls := await _call_stats(profile, uz)):
        now_lines.append(f"📞 {'Qo`ng`iroqlar' if uz else 'Звонки'}: {calls}" + ("" if caller.available() else " · ⚠️"))
    if intent:
        now_lines.append(f"📷 {'Rasm kutyapman' if uz else 'Жду фото'}: {h(intent[:90])}")

    important = await _important_now(profile)
    facts = extra.parse_facts((mem or {}).get("facts") or "")
    char_lines = [
        f"🔊 {voice_uz if uz else voice_ru} · {'o`zbekcha' if p.lang == 'uz' else 'русский'} · "
        f"{('siz' if p.address == 'siz' else 'sen') if uz else ('на «вы»' if p.address == 'siz' else 'на «ты»')} · "
        f"{persona_mod.HONORIFICS[p.honorific][1] if uz else persona_mod.HONORIFICS[p.honorific][0]}",
        f"📞 {'Muhim bo`lsa qo`ng`iroq' if uz else 'Звонок о важном'}: <b>{on if p.alert_calls else off}</b> · "
        f"🎙 {'Ertalab ovozli' if uz else 'Утро голосом'}: <b>{on if p.morning_voice else off}</b>",
    ]
    text = ui.join(
        ui.title("🤖", "Jarvis" if uz else "Джарвис"),
        ui.card(f"<b>{'Hozir' if uz else 'Сейчас'}</b>", now_lines),
        ui.card(f"<b>{'Muhim' if uz else 'Важно сейчас'}</b>", [f"• {h(x)}" for x in important]) if important else None,
        ui.card(f"<b>{'Sen haqingda eslayman' if uz else 'Помню о тебе'}</b> · {len(facts)}", [f"• {h(f[:80])}" for f in facts[-3:]]) if facts else None,
        ui.card(f"<b>{'Xarakter' if uz else 'Характер'}</b>", char_lines),
    )
    if notice:
        text += f"\n\n{notice}"
    await _show(target, text, jarvis_hub_keyboard(profile.lang, alert_calls=p.alert_calls, morning_voice=p.morning_voice, photo_intent=bool(intent)))


@router.callback_query(F.data == "menu:jarvis")
async def cb_jarvis_hub(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_jarvis_hub(callback, await get_profile(callback.from_user))


@router.callback_query(F.data == "brief:text:morning")
async def cb_brief_text(callback: CallbackQuery) -> None:
    """«📄 Текстом» под голосовой утренней сводкой."""
    from .. import briefs
    from .. import screen as screen_mod

    profile = await get_profile(callback.from_user)
    await answer_now(callback)
    try:
        text = await briefs.morning_brief(profile)
    except Exception:
        logger.exception("morning brief text failed")
        return
    await screen_mod.send_ephemeral(callback.bot, profile.telegram_id, text, keep_previous=True)
