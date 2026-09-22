"""Настройки: утренняя/вечерняя сводка, авто-отчёт, язык."""
from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import cache
from .. import emoji as pe
from .. import services
from .. import ui
from ..context import db
from ..keyboards import _btn, settings_keyboard
from ..states import BotStates
from ..profile import Profile, h
from .common import answer_now, get_profile, safe_edit

router = Router(name="settings")
logger = logging.getLogger(__name__)


async def report_prefs(uid: int) -> dict:
    return await cache.remember(uid, ("report_prefs",), 600, lambda: db.get_report_preferences(uid))


async def render_settings(callback: CallbackQuery, profile: Profile) -> None:
    lang = profile.lang
    if await db.ensure_available("user_settings"):
        us, prefs = await asyncio.gather(services.user_settings(profile.telegram_id), report_prefs(profile.telegram_id))
        hint = ""
    else:
        us = {"brief_morning": False, "brief_evening": False}
        prefs = await report_prefs(profile.telegram_id)
        hint = "\n\n⚠️ " + profile.tr(
            "Сводки недоступны: выполни <code>sql/migrations/004_v2_features.sql</code> в Supabase → SQL Editor.",
            "Xulosalar ishlamaydi: Supabase → SQL Editor da <code>sql/migrations/004_v2_features.sql</code> ni bajaring.",
        )
    text = ui.title(pe.SETTINGS, "Sozlamalar" if lang == "uz" else "Настройки")
    from .. import reminders as rem

    rems = await services.reminders(profile.telegram_id)
    if rems:
        text = ui.join(text, ui.card(f"<b>⏰ {'Eslatmalar' if lang == 'uz' else 'Напоминания'}</b>", [f"• {h(rem.title(r))}" for r in rems]))
    await safe_edit(
        callback,
        text + hint,
        settings_keyboard(
            lang,
            morning=bool(us.get("brief_morning", True)),
            evening=bool(us.get("brief_evening", True)),
            report_enabled=bool(prefs.get("enabled", True)),
            report_frequency=str(prefs.get("frequency") or "weekly"),
            reminders=[(str(r.get("id")), rem.title(r)) for r in rems],
            proactive=bool(us.get("proactive", True)),
            voice=bool(us.get("voice_reply", True)),
        ),
    )


@router.callback_query(F.data.startswith("settings:toggle:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    """Переключатели: проактивные подсказки, голосовые ответы (колонки миграции 005)."""
    profile = await get_profile(callback.from_user)
    field = callback.data.split(":")[-1]
    if field not in {"proactive", "voice_reply"} or not await db.ensure_available("user_settings"):
        await answer_now(callback)
        return
    us = await services.user_settings(profile.telegram_id)
    new_value = not bool(us.get(field, True))
    try:
        await services.save_user_settings(profile.telegram_id, {field: new_value})
    except Exception:
        logger.warning("toggle %s failed (migration 005?)", field, exc_info=True)
        await answer_now(callback, profile.tr("Нужна миграция 005_assistant.sql", "005_assistant.sql migratsiyasi kerak"), alert=True)
        return
    await answer_now(callback, "✅" if new_value else "⛔")
    await render_settings(callback, profile)


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_settings(callback, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("settings:brief:"))
async def cb_brief_toggle(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    which = callback.data.split(":")[-1]
    if not await db.ensure_available("user_settings"):
        await answer_now(callback, profile.tr("Сначала выполни миграцию 004", "Avval 004 migratsiyasini bajaring"), alert=True)
        return
    us = await services.user_settings(profile.telegram_id)
    field = "brief_morning" if which == "morning" else "brief_evening"
    new_value = not bool(us.get(field, True))
    await services.save_user_settings(profile.telegram_id, {field: new_value})
    label = profile.tr("Утро", "Ertalab") if which == "morning" else profile.tr("Вечер", "Kechqurun")
    await answer_now(callback, f"{label}: {'✅' if new_value else '⛔'}")
    await render_settings(callback, profile)


@router.callback_query(F.data.startswith("report:set:"))
async def cb_report_set(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    mode = callback.data.split(":")[-1]
    prefs = await report_prefs(profile.telegram_id)
    if mode == "off":
        enabled, frequency = False, str(prefs.get("frequency") or "weekly")
        note = profile.tr("Авто-отчёт выключен", "Avto-hisobot o'chirildi")
    else:
        enabled, frequency = True, ("monthly" if mode == "monthly" else "weekly")
        note = profile.tr("Раз в месяц ✅" if frequency == "monthly" else "Раз в неделю ✅", "Oyda bir ✅" if frequency == "monthly" else "Haftada bir ✅")
    await answer_now(callback, note)
    await db.save_report_preferences(profile.telegram_id, enabled=enabled, frequency=frequency, last_sent_key=prefs.get("last_sent_key"))
    cache.invalidate(profile.telegram_id, "report_prefs")
    await render_settings(callback, profile)


@router.callback_query(F.data.startswith("settings:rem_del:"))
async def cb_reminder_delete(callback: CallbackQuery) -> None:
    profile = await get_profile(callback.from_user)
    rem_id = callback.data.split(":")[-1]
    await answer_now(callback, profile.tr("Удалено", "O'chirildi"))
    await db.delete_reminder(profile.telegram_id, rem_id)
    services.invalidate_reminders(profile.telegram_id)
    await render_settings(callback, profile)


# ------------------------------------------------------------------ подъём и звонки
async def render_wake(callback: CallbackQuery, profile: Profile, *, notice: str | None = None) -> None:
    """Экран «Подъём и звонки»: что сейчас настроено и во сколько разбудим завтра."""
    from datetime import timedelta

    from .. import caller
    from .. import prayer
    from .. import wake as wake_mod
    from .. import wake_runner
    from ..keyboards import wake_settings_keyboard

    uz = profile.lang == "uz"
    if not await db.ensure_available("wake_settings"):
        await safe_edit(callback, "⚠️ " + profile.tr(
            "Нужна миграция: выполни <code>sql/migrations/008_wake.sql</code> в Supabase → SQL Editor.",
            "Migratsiya kerak: Supabase → SQL Editor da <code>sql/migrations/008_wake.sql</code> ni bajaring."),
            settings_keyboard(profile.lang, morning=True, evening=True, report_enabled=True, report_frequency="weekly"))
        return
    s = wake_mod.WakeSettings.from_row(await services.wake_settings(profile.telegram_id))
    tomorrow = profile.today + timedelta(days=1)
    _, plan = await wake_runner.plan_for(profile, tomorrow)
    rows = await prayer.timings(tomorrow, latitude=s.latitude, longitude=s.longitude, method=s.calc_method)

    when = plan.wake_at.strftime("%H:%M") if plan.wake_at else "—"
    lines = [
        f"{'Ertaga uyg`otaman' if uz else 'Завтра разбужу'}: <b>{when}</b>" if plan.active
        else ("Ertaga uyg'otmayman" if uz else "Завтра не бужу") + f" ({plan.reason})",
        f"{'Bomdod azoni' if uz else 'Азан фаджра'}: {plan.fajr or '—'} · {'takbir' if uz else 'такбир'}: <b>{plan.takbir or '—'}</b> "
        f"({'azondan' if uz else 'после азана'} +{s.takbir_offset_min} {'daq' if uz else 'мин'})",
        f"{'Takbirdan oldin' if uz else 'До такбира'}: {s.offset_min} {'daqiqa' if uz else 'минут'}",
    ]
    if s.mode == "fixed":
        lines.append(f"{'Aniq vaqt' if uz else 'Точное время'}: {s.fixed_time or '—'}")
    if s.skip_until:
        lines.append(("Uyg'otmayman" if uz else "Не бужу") + f" {'gacha' if uz else 'до'} {s.skip_until:%d.%m}")
    call_state = ("sozlangan ✅" if uz else "настроены ✅") if caller.available() else ("sozlanmagan ⚠️" if uz else "не настроены ⚠️")
    lines.append(f"{'Qo`ng`iroqlar' if uz else 'Звонки'}: {call_state}")

    history = await services.wake_history(profile.telegram_id, days=14)
    stats = wake_mod.stats_line(history, profile.lang) if history else None

    blocks = [
        ui.title("⏰", "Uyg'otish va qo'ng'iroq" if uz else "Подъём и звонки"),
        ui.card(f"<b>{'Reja' if uz else 'План'}</b>", lines),
        ui.card(f"<b>{'Namoz vaqtlari (ertaga)' if uz else 'Намаз завтра'}</b>", [prayer.summary(rows, profile.lang, takbir=plan.takbir)]) if rows else None,
        ui.card(f"<b>{'Statistika' if uz else 'Статистика'}</b>", [stats]) if stats else None,
    ]
    text = ui.join(*blocks)
    if notice:
        text += f"\n\n{notice}"
    await safe_edit(callback, text, wake_settings_keyboard(
        profile.lang, enabled=s.enabled, call_enabled=s.call_enabled, talk=s.talk, voice_lang=s.voice_lang,
        mode=s.mode, days=list(s.days_of_week), hardness=s.hardness, tasks=list(s.confirm_tasks)))


@router.callback_query(F.data == "settings:wake")
async def cb_wake_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_now(callback)
    await state.clear()
    await render_wake(callback, await get_profile(callback.from_user))


@router.callback_query(F.data.startswith("wakeset:"))
async def cb_wake_change(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопки экрана подъёма: переключатели, язык, режим, сдвиг минут, дни, задания, тестовый звонок."""
    from .. import call_assistant
    from .. import caller
    from .. import wake as wake_mod

    profile = await get_profile(callback.from_user)
    parts = callback.data.split(":")
    action, value = parts[1], (parts[2] if len(parts) > 2 else "")
    if not await db.ensure_available("wake_settings"):
        await answer_now(callback, profile.tr("Нужна миграция 008_wake.sql", "008_wake.sql migratsiyasi kerak"), alert=True)
        return
    s = wake_mod.WakeSettings.from_row(await services.wake_settings(profile.telegram_id))
    fields: dict = {}
    notice = None

    if action == "calltest":
        if not caller.available():
            await answer_now(callback, profile.tr("Звонки не настроены", "Qo'ng'iroq sozlanmagan"), alert=True)
            return
        if call_assistant.call_in_background(profile) is None:
            await answer_now(callback, profile.tr("Уже звоню", "Allaqachon qo'ng'iroq qilyapman"))
            return
        await answer_now(callback, profile.tr("Звоню 📞", "Qo'ng'iroq qilyapman 📞"))
        return
    if action == "toggle" and value in {"enabled", "call_enabled", "talk"}:
        fields[value] = not getattr(s, value)
    elif action == "toggle" and value == "hardness":
        fields["hardness"] = "normal" if s.hardness == "hard" else "hard"
    elif action == "lang" and value in {"uz", "ru"}:
        fields["voice_lang"] = value
    elif action == "mode" and value in {"fajr", "fixed"}:
        fields["mode"] = value
        if value == "fixed" and not s.fixed_time:
            fields["fixed_time"] = "06:30"
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
    await render_wake(callback, profile, notice=None)



# ------------------------------------------------------------------ джарвис: голос, язык, характер, будильник
async def render_jarvis(target, profile: Profile, *, notice: str | None = None) -> None:  # noqa: ANN001
    """Раздел «Джарвис»: голос, язык звонков, обращение, тон, длина ответов, будильник."""
    from datetime import timedelta

    from .. import caller
    from .. import persona as persona_mod
    from .. import wake_runner
    from ..keyboards import jarvis_settings_keyboard

    uz = profile.lang == "uz"
    p = await services.persona(profile.telegram_id)
    s, plan = await wake_runner.plan_for(profile, profile.today + timedelta(days=1))
    voice_ru, voice_uz = persona_mod.VOICES.get(p.voice, ("", ""))
    tone = {"friendly": ("Дружелюбный", "Do'stona"), "calm": ("Спокойный", "Xotirjam"), "strict": ("Строгий", "Qat'iy")}[p.tone]
    length = {"short": ("коротко", "qisqa"), "normal": ("обычно", "o'rtacha"), "detailed": ("подробно", "batafsil")}[p.verbosity]
    if s.mode == "fixed":
        alarm = (f"har kuni {s.fixed_time}" if uz else f"каждый день в {s.fixed_time}")
    else:
        alarm = (f"takbirdan {s.offset_min} daq oldin" if uz else f"за {s.offset_min} мин до такбира")
    if plan.active and plan.wake_at:
        alarm += (f" · ertaga {plan.wake_at:%H:%M}" if uz else f" · завтра в {plan.wake_at:%H:%M}")
    if not s.enabled:
        alarm = "o'chirilgan" if uz else "выключен"
    lines = [
        f"🔊 {'Ovoz' if uz else 'Голос'}: <b>{voice_uz if uz else voice_ru}</b>",
        f"🗣 {'Qo`ng`iroq tili' if uz else 'Язык звонков'}: <b>{'o`zbekcha' if p.lang == 'uz' else 'русский'}</b>",
        f"🤝 {'Murojaat' if uz else 'Обращение'}: <b>{'siz' if p.address == 'siz' else 'sen'}</b> · {'ohang' if uz else 'тон'}: <b>{tone[1] if uz else tone[0]}</b> · {'javoblar' if uz else 'ответы'}: <b>{length[1] if uz else length[0]}</b>",
        f"⏰ {'Budilnik' if uz else 'Будильник'}: <b>{alarm}</b>",
        f"📞 {'Qo`ng`iroqlar' if uz else 'Звонки'}: {'✅' if caller.available() else '⚠️'}",
    ]
    text = ui.join(ui.title("🤖", "Jarvis" if uz else "Джарвис"), ui.card(f"<b>{'Hozir' if uz else 'Сейчас'}</b>", lines))
    if notice:
        text += f"\n\n{notice}"
    kb = jarvis_settings_keyboard(profile.lang, voice=p.voice, call_lang=p.lang, address=p.address, tone=p.tone,
                                  verbosity=p.verbosity, alarm_mode=s.mode)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, kb)
    else:
        from .. import screen as screen_mod

        await screen_mod.show_screen(target.bot, target.chat.id, text, kb)


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
    if not await db.ensure_available("assistant_settings"):
        await answer_now(callback, profile.tr("Нужна миграция 009", "009 migratsiyasi kerak"), alert=True)
        return
    if action == "sample":
        await answer_now(callback, profile.tr("Записываю образец…", "Namuna tayyorlanmoqda…"))
        await _send_voice_sample(callback, profile)
        return
    if action == "alarm" and value == "time":
        await answer_now(callback)
        await state.set_state(BotStates.waiting_alarm_time)
        await safe_edit(callback, profile.tr("⏰ Во сколько будить? Напиши время, например <code>6:30</code>",
                                             "⏰ Soat nechada uyg'otay? Vaqtni yozing, masalan <code>6:30</code>"),
                        InlineKeyboardMarkup(inline_keyboard=[[_btn(profile.tr("Отмена", "Bekor"), "settings:jarvis")]]))
        return
    if action == "alarm" and value == "fajr":
        if await db.ensure_available("wake_settings"):
            await services.save_wake_settings(profile.telegram_id, {"mode": "fajr", "enabled": True})
        await answer_now(callback, "🕌 ✅")
        await render_jarvis(callback, profile)
        return
    fields = {}
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
    if fields:
        await services.save_persona(profile.telegram_id, fields)
    await answer_now(callback, "✅")
    await render_jarvis(callback, profile)
    if "voice" in fields:
        await _send_voice_sample(callback, profile)


async def _send_voice_sample(callback: CallbackQuery, profile: Profile) -> None:
    """Короткое голосовое выбранным голосом — выбрать на слух. Само исчезает через минуту."""
    from aiogram.types import BufferedInputFile

    from .. import screen as screen_mod
    from .. import voice as voice_mod
    from ..context import ai

    p = await services.persona(profile.telegram_id)
    name = p.name_for(profile.first_name)
    phrase = (f"Assalomu alaykum, {name}. Men Jarvisman, sizga yordam berishga tayyorman." if p.lang == "uz"
              else f"Ассалому алайкум, {name}. Я Джарвис, готова помочь.")
    try:
        pcm = await ai.synthesize(phrase, voice=p.voice)
        ogg = await voice_mod.pcm_to_ogg(pcm) if pcm else None
        if not ogg:
            return
        msg = await callback.bot.send_voice(profile.telegram_id, BufferedInputFile(ogg, "jarvis.ogg"))
        screen_mod._ephemerals[profile.telegram_id].append(msg.message_id)
        import asyncio as _asyncio

        _asyncio.create_task(screen_mod._delete_later(callback.bot, profile.telegram_id, msg.message_id, 60))
    except Exception:
        logger.warning("voice sample failed", exc_info=True)


@router.message(BotStates.waiting_alarm_time, F.text)
async def msg_alarm_time(message: Message, state: FSMContext) -> None:
    from ..agent_tools import _time_arg

    profile = await get_profile(message.from_user)
    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    hhmm = _time_arg(raw)
    if not hhmm:
        await render_jarvis(message, profile, notice=profile.tr("Не понял время — напиши, например, 6:30", "Vaqtni tushunmadim — masalan 6:30"))
        return
    await state.clear()
    if await db.ensure_available("wake_settings"):
        await services.save_wake_settings(profile.telegram_id, {"mode": "fixed", "fixed_time": hhmm, "enabled": True})
    await render_jarvis(message, profile, notice=profile.tr(f"⏰ Будильник: каждый день в {hhmm}", f"⏰ Budilnik: har kuni {hhmm}"))
