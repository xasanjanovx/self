"""Звонок по требованию: «позвони» → Nurai звонит и говорит с тобой голосом.

Отличие от подъёма (bot/wake_runner.py): здесь обычный разговор с полным доступом
к данным — тот же агент, что и в чате, со всеми инструментами. Можно спросить
(«сколько я потратил сегодня?») и записать («такси двадцать пять тысяч»), голосом.

Разговор заканчивается, когда человек прощается, молчит подряд или вышел лимит
времени/реплик — трубку кладём сами.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import call_dialog as cd
from . import caller
from . import voice
from .context import ai
from .profile import Profile

logger = logging.getLogger(__name__)

MAX_TURNS = 14
MAX_SECONDS = 420          # 7 минут — больше по телефону не нужно
REPLY_MAX_CHARS = 400      # длинные ответы в трубке не слушают
SILENT_ROUNDS_TO_STOP = 2  # столько раз промолчал подряд — прощаемся

_BYE = ("пока", "до свидания", "отбой", "хватит", "закончим", "кладу трубку", "всё спасибо", "все спасибо",
        "спасибо всё", "спасибо все", "xayr", "rahmat", "bo'ldi", "boldi", "yetar", "tugatdik")
_BYE_EXACT = ("всё", "все", "ок", "ok")


def is_goodbye(text: str) -> bool:
    """Человек прощается. Одиночное «всё» считаем прощанием, внутри фразы — нет."""
    low = " ".join(str(text or "").lower().replace(",", " ").replace("!", " ").replace(".", " ").split())
    if not low:
        return False
    if low in _BYE_EXACT:
        return True
    padded = f" {low} "
    return any(f" {w} " in padded for w in _BYE)


def greeting(profile: Profile, lang: str, topic: str = "") -> str:
    uz = lang != "ru"
    name = profile.first_name or ("do'st" if uz else "друг")
    if topic:
        return (f"Assalomu alaykum, {name}." if uz else f"Ассалому алайкум, {name}.")
    return (f"Assalomu alaykum, {name}. Tinglayapman." if uz else f"Ассалому алайкум, {name}. Слушаю тебя.")


def farewell(lang: str) -> str:
    return "Xayr, ishlaringizga omad." if lang != "ru" else "До связи, хорошего дня."


def voice_reply(text: str) -> str:
    """Ответ агента → пригодная для произношения фраза (без HTML, эмодзи и простыней)."""
    plain = voice.speakable(text)
    if len(plain) <= REPLY_MAX_CHARS:
        return plain
    cut = plain[:REPLY_MAX_CHARS]
    dot = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    return (cut[: dot + 1] if dot > 120 else cut).strip()


async def _agent_answer(profile: Profile, text: str, history: list[dict[str, Any]], snapshot: str) -> tuple[str, list[dict[str, Any]]]:
    """Ответ «Nurai» со всеми инструментами — как в чате, но короткий, для трубки."""
    from .handlers.agent import run_agent

    hint = ("[разговор по телефону: ответь ОДНИМ-ДВУМЯ короткими предложениями, без списков, "
            "эмодзи и markdown; числа называй словами там, где так естественнее]\n")
    result = await run_agent(profile, hint + text, history, snapshot=snapshot)
    return result.text or "", result.contents


async def call_now(profile: Profile, *, topic: str = "", lang: str | None = None) -> dict[str, Any]:
    """Позвонить прямо сейчас и поговорить. `topic` — с чего начать разговор."""
    from . import agent_tools

    if not caller.available():
        return {"ok": False, "error": "caller is not configured"}
    speak_lang = lang or ("uz" if profile.lang == "uz" else "ru")
    snapshot = await agent_tools.snapshot(profile)
    history: list[dict[str, Any]] = []

    opening = greeting(profile, speak_lang, topic)
    if topic:
        answer, history = await _agent_answer(profile, topic, history, snapshot)
        opening = f"{opening} {voice_reply(answer)}".strip()
    greeting_pcm = await ai.synthesize(voice.speakable(opening))
    if not greeting_pcm:
        return {"ok": False, "error": "tts unavailable"}

    transcript: list[str] = [f"я: {opening}"]
    turns = 0
    silent = 0

    async def on_utterance(pcm: bytes | None) -> dict[str, Any]:
        nonlocal turns, silent, history
        text = ""
        if pcm:
            path = await cd.pcm_to_ogg_file(pcm)
            if path:
                try:
                    text = (await ai.transcribe_voice(path)).strip()
                except Exception:
                    logger.warning("call transcribe failed", exc_info=True)
                finally:
                    cd.cleanup(path)
        if not text:
            silent += 1
            if silent >= SILENT_ROUNDS_TO_STOP:
                bye = farewell(speak_lang)
                transcript.append(f"я: {bye}")
                return {"pcm": await ai.synthesize(bye), "stop": True}
            again = "Eshitmadim, qaytaring." if speak_lang != "ru" else "Не расслышал, повтори."
            return {"pcm": await ai.synthesize(again), "stop": False}
        silent = 0
        turns += 1
        transcript.append(f"он: {text}")
        if is_goodbye(text):
            bye = farewell(speak_lang)
            transcript.append(f"я: {bye}")
            return {"pcm": await ai.synthesize(bye), "stop": True}
        try:
            answer, history = await _agent_answer(profile, text, history, snapshot)
        except Exception:
            logger.exception("call agent failed")
            answer = "Hozir javob bera olmadim." if speak_lang != "ru" else "Сейчас не смог ответить."
        say = voice_reply(answer) or ("Bajarildi." if speak_lang != "ru" else "Готово.")
        stop = turns >= MAX_TURNS
        if stop:
            say = f"{say} {farewell(speak_lang)}"
        transcript.append(f"я: {say}")
        return {"pcm": await ai.synthesize(say), "stop": stop}

    result = await caller.talk(profile.telegram_id, greeting_pcm=greeting_pcm, on_utterance=on_utterance,
                               ring_seconds=45, max_seconds=MAX_SECONDS, username=profile.username)
    return {"ok": bool(result.get("answered")), "error": result.get("error"), "turns": turns,
            "transcript": transcript}


# что сделать, если звонок не доходит (у приглашённых чаще всего: аккаунта Джарвиса нет в контактах)
_FIX_RU = ("Сделай один раз: 1) открой аккаунт Nurai (кнопка ниже или чат от него) → «Добавить в контакты»; "
           "2) Telegram → Настройки → Конфиденциальность → Звонки → «Все» (или «Мои контакты»).")
_FIX_UZ = ("Bir marta qiling: 1) Nurai akkauntini oching (pastdagi tugma yoki undan kelgan chat) → «Kontaktga qo'shish»; "
           "2) Telegram → Sozlamalar → Maxfiylik → Qo'ng'iroqlar → «Hamma» (yoki «Kontaktlarim»).")
_DEVICE_RU = ("Если телефон не звонил, а пришёл только «пропущенный»: добавь Nurai в контакты; на iPhone выключи "
              "Настройки → Телефон → «Заглушение неизвестных»; на Android разреши Telegram уведомления о звонках и работу в фоне.")
_DEVICE_UZ = ("Telefon jiringlamay, faqat «o'tkazib yuborilgan» kelgan bo'lsa: Nuraini kontaktlarga qo'shing; iPhone'da "
              "Sozlamalar → Telefon → «Noma'lumlarni o'chirish»ni o'chiring; Android'da Telegram'ga qo'ng'iroq bildirishnomalari va fon rejimiga ruxsat bering.")
HELPER_INTRO = {
    "ru": ("Ассалому алайкум! Я — Nurai, помощник из бота. С этого аккаунта я вам звоню (будильник и разговор). "
           "Чтобы звонки приходили, добавьте меня в контакты — кнопка «Добавить в контакты» вверху этого чата — "
           "и разрешите звонки: Настройки → Конфиденциальность → Звонки."),
    "uz": ("Assalomu alaykum! Men — Nurai, botdagi yordamchi. Shu akkauntdan sizga qo'ng'iroq qilaman (budilnik va suhbat). "
           "Qo'ng'iroqlar kelishi uchun meni kontaktlarga qo'shing — shu chat tepasidagi «Kontaktga qo'shish» tugmasi — "
           "va qo'ng'iroqlarga ruxsat bering: Sozlamalar → Maxfiylik → Qo'ng'iroqlar."),
    "en": ("Assalamu alaikum! I'm Nurai, the assistant from the bot. I call you from this account (alarm and conversations). "
           "For calls to come through, add me to your contacts — the “Add contact” button at the top of this chat — "
           "and allow calls: Settings → Privacy → Calls."),
}


async def helper_intro(profile: Profile) -> bool:
    """Аккаунт Nurai один раз пишет человеку: появляется чат с кнопкой Telegram «Добавить в контакты»
    (номер телефона никому давать не нужно). Только приглашённым, не чаще одного раза."""
    from . import access
    from .context import db

    uid = profile.telegram_id
    if access.is_owner(uid) or not caller.available():
        return False
    try:
        if db.available("alerts_log"):
            if await db.alert_was_sent(uid, "helper_intro"):
                return False
            await db.mark_alert_sent(uid, "helper_intro")
        lang = profile.lang if profile.lang in HELPER_INTRO else "ru"
        return await caller.send_message(uid, HELPER_INTRO[lang])
    except Exception:
        logger.warning("helper intro failed for %s", uid, exc_info=True)
        return False


async def _notify_failure(profile: Profile, error: str) -> None:
    """Звонок не состоялся — сказать, ЧТО сделать, а не просто «не удалось»."""
    reasons = {
        "tts unavailable": ("Не смог синтезировать голос — отвечаю текстом.", "Ovozni tayyorlay olmadim — matn bilan javob beraman."),
        "caller is not configured": ("Звонки пока не настроены.", "Qo'ng'iroqlar hali sozlanmagan."),
        "peer_unknown": ("Аккаунт Nurai тебя ещё «не знает». " + _FIX_RU, "Nurai akkaunti sizni hali tanimaydi. " + _FIX_UZ),
        "privacy": ("Telegram не пропустил звонок: твои настройки «Кто может мне звонить» запрещают аккаунту Nurai. " + _FIX_RU,
                    "Telegram qo'ng'iroqni o'tkazmadi: «Kim menga qo'ng'iroq qila oladi» sozlamasi Nurai akkauntiga ruxsat bermayapti. " + _FIX_UZ),
        "no_answer": ("Звонила, но трубку не взяли. " + _DEVICE_RU, "Qo'ng'iroq qildim, lekin javob bo'lmadi. " + _DEVICE_UZ),
        "daily_limit": ("Дневной лимит на живой голос исчерпан — позвоню завтра. Пишите здесь или зовите «Nurai» на телефоне (экономный режим).",
                        "Jonli ovoz uchun kunlik limit tugadi — ertaga qo'ng'iroq qilaman. Shu yerda yozing yoki telefonda «Nurai» deng."),
    }
    ru, uz = reasons.get(error, ("Дозвониться не получилось — возможно, звонок отклонён или закрыт настройками приватности.",
                                 "Qo'ng'iroq o'tmadi — rad etilgan yoki maxfiylik sozlamalari to'sib turgan bo'lishi mumkin."))
    fixable = error in {"privacy", "no_answer", "peer_unknown"}
    if fixable and caller.helper_username:
        ru += f" Аккаунт Nurai: @{caller.helper_username}"
        uz += f" Nurai akkaunti: @{caller.helper_username}"
    if fixable:
        await helper_intro(profile)
    await show_home(profile, f"📵 {profile.tr(ru, uz)}", helper_button=fixable)


async def show_home(profile: Profile, notice: str | None = None, *, undo: bool = False, helper_button: bool = False) -> None:
    """Главный экран (он в чате один) + строка про звонок — вместо отдельных сообщений,
    которые потом висят в чате. Заодно убирает экран Nurai, с которого звонили."""
    try:
        from aiogram.types import InlineKeyboardMarkup

        from . import screen as screen_mod
        from .context import bot_instance
        from .handlers.menu import build_dashboard
        from .keyboards import _btn, main_menu_keyboard

        text = await build_dashboard(profile)
        if notice:
            text += f"\n\n{notice}"
        kb = main_menu_keyboard(profile.lang, undo=undo)
        if helper_button and caller.helper_id:
            # открыть профиль аккаунта Джарвиса → «Добавить в контакты» (без номера телефона)
            button = _btn("👤 " + profile.tr("Аккаунт Nurai", "Nurai akkaunti"), url=f"tg://user?id={caller.helper_id}")
            try:
                await screen_mod.show_screen(bot_instance(), profile.telegram_id, text,
                                             InlineKeyboardMarkup(inline_keyboard=[[button], *kb.inline_keyboard]))
                return
            except Exception:
                logger.info("helper button rejected by Telegram — показываю без неё")
        await screen_mod.show_screen(bot_instance(), profile.telegram_id, text, kb)
    except Exception:
        logger.debug("call home screen failed", exc_info=True)


_running: set[int] = set()  # один звонок на человека одновременно


async def _remember_call(profile: Profile, transcript: list[str]) -> None:
    """Один «мозг»: разговор по телефону попадает в историю чата и в недавние реплики,
    чтобы в чате Nurai знал, о чём говорили («как я сказал по телефону…»)."""
    lines = [t for t in transcript if t.strip() and not t.rstrip().endswith(":")]
    if not lines:
        return
    try:
        from . import agent_tools_extra as extra
        from .handlers.agent import load_history, save_history

        text = "\n".join(lines[-30:])[:3000]
        history = load_history(profile.telegram_id)
        history.append({"role": "user", "parts": [{"text": f"(разговор по телефону с Nurai только что:\n{text})"}]})
        history.append({"role": "model", "parts": [{"text": "Помню наш разговор по телефону."}]})
        save_history(profile.telegram_id, history)
        asks = [t[4:] for t in lines if t.startswith("он: ")]
        if asks:
            await extra.remember_exchange(profile.telegram_id, "📞 " + " / ".join(asks[-3:]), "", when=profile.now.strftime("%d.%m %H:%M"))
    except Exception:
        logger.debug("remember call failed", exc_info=True)


async def _send_summary(profile: Profile, actions: list[str], mutated: bool) -> None:
    """После разговора — главный экран; если что-то изменено — одной строкой, с кнопкой «Отменить»."""
    names = {
        "add_finance_entries": ("операции", "operatsiya"), "update_finance_entry": ("правка операции", "operatsiya tahriri"),
        "delete_finance_entries": ("удаление операций", "operatsiyalarni o'chirish"), "add_calorie_logs": ("питание", "ovqat"), "delete_calorie_logs": ("удаление еды", "ovqatni o'chirish"),
        "add_goal": ("новая цель", "yangi maqsad"), "update_goal": ("цель", "maqsad"), "delete_goals": ("удаление цели", "maqsadni o'chirish"),
        "add_task": ("задача", "vazifa"), "complete_tasks": ("задачи выполнены", "vazifalar bajarildi"), "add_reminder": ("напоминание", "eslatma"),
        "log_weight": ("вес", "vazn"), "goal_checkin": ("отметка привычки", "odat belgisi"), "set_wake": ("будильник", "budilnik"),
    }
    done = []
    for a in actions:
        ru, uz = names.get(a, (None, None))
        label = profile.tr(ru, uz) if ru else None
        if label and label not in done:
            done.append(label)
    notice = ("📞 " + profile.tr("После звонка: ", "Qo'ng'iroqdan so'ng: ") + ", ".join(done)) if done else None
    await show_home(profile, notice, undo=mutated and bool(done))


def call_in_background(profile: Profile, *, topic: str = "", lang: str | None = None) -> asyncio.Task | None:
    """Звонок фоном: инструмент агента должен ответить сразу, а не ждать конца разговора.

    Разговор ведёт Gemini Live (bot/live_call.py); `lang` — только если явно попросили
    другой язык, иначе берётся из настроек Nurai.
    """
    uid = profile.telegram_id
    if uid in _running:
        return None

    async def runner() -> None:
        from . import live_call, services

        _running.add(uid)
        try:
            # язык — только на этот звонок: раз попросил по-узбекски — не переключать Джарвиса навсегда
            result = await live_call.run(profile, mode="assistant", topic=topic, lang=lang if lang in {"uz", "ru", "en"} else None)
            await services.log_agent(uid, text=f"call: {topic or 'разговор'}", kind="call",
                                     tools=",".join(result.actions), reply=" | ".join(result.transcript)[:900], ok=result.answered)
            if not result.answered:
                await _notify_failure(profile, str(result.error or ""))
            else:
                await _remember_call(profile, result.transcript)
                await _send_summary(profile, result.actions, result.mutated)
        except Exception:
            logger.exception("assistant call failed")
            await _notify_failure(profile, "internal error")
        finally:
            _running.discard(uid)

    return asyncio.create_task(runner(), name=f"call-{uid}")


def wake_test_in_background(profile: Profile) -> asyncio.Task | None:
    """«Проверить будильник»: звонок ровно как утром (режим подъёма, мотивация, проверка по голосу),
    но без записи в журнал подъёмов и без перезвонов."""
    uid = profile.telegram_id
    if uid in _running:
        return None

    async def runner() -> None:
        from datetime import datetime, timedelta, timezone

        from . import live_call, wake_runner

        _running.add(uid)
        try:
            s, plan = await wake_runner.plan_for(profile)
            if not plan.takbir_at or plan.takbir_at < datetime.now(timezone.utc):
                _, plan = await wake_runner.plan_for(profile, profile.today + timedelta(days=1))
            minutes_left = int((plan.takbir_at - datetime.now(timezone.utc)).total_seconds() // 60) if plan.takbir_at else None
            result = await live_call.run(profile, mode="wake", ring_seconds=45,
                                         wake={"takbir": plan.takbir, "minutes_left": minutes_left})
            if not result.answered:
                await _notify_failure(profile, str(result.error or ""))
                return
            verdict = (profile.tr("✅ подъём засчитан бы", "✅ turish qabul qilinardi") if result.confirmed
                       else profile.tr("⏰ подъём не засчитан — утром перезвонила бы", "⏰ turish qabul qilinmadi — ertalab qayta qo'ng'iroq qilardim"))
            await show_home(profile, "🧪 " + profile.tr("Проверка будильника: ", "Budilnik sinovi: ") + verdict)
        except Exception:
            logger.exception("wake test call failed")
            await _notify_failure(profile, "internal error")
        finally:
            _running.discard(uid)

    return asyncio.create_task(runner(), name=f"wake-test-{uid}")


async def on_incoming_call(user_id: int) -> bool:
    """Он сам позвонил аккаунту Nurai в Telegram — берём трубку и говорим (Gemini Live).
    Только владельцу: чужим звонкам caller отвечает сбросом. False — трубку не берём."""
    from .context import settings

    uid = int(user_id)
    if uid not in settings.allowed_telegram_ids:
        logger.info("call %s: входящий от постороннего — не беру", uid)
        return False
    if uid in _running:
        return False  # уже разговариваем (или звоним ему сами)
    from . import billing

    if not billing.live_allowed("assistant"):
        logger.info("call %s: входящий — дневной лимит живого голоса, не беру", uid)
        return False
    from .handlers.common import profile_by_id

    profile = await profile_by_id(uid)

    async def runner() -> None:
        from . import live_call, services

        _running.add(uid)
        try:
            result = await live_call.answer(profile)
            await services.log_agent(uid, text="call: входящий", kind="call", tools=",".join(result.actions),
                                     reply=" | ".join(result.transcript)[:900], ok=result.answered)
            if result.answered:
                await _remember_call(profile, result.transcript)
                await _send_summary(profile, result.actions, result.mutated)
        except Exception:
            logger.exception("incoming call failed")
        finally:
            _running.discard(uid)

    task = asyncio.create_task(runner(), name=f"incoming-call-{uid}")
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return True


_bg: set[asyncio.Task] = set()


async def start_listening() -> None:
    """При запуске бота: аккаунт Nurai в сети и берёт трубку, когда владелец звонит сам."""
    caller.set_incoming_handler(on_incoming_call)
    if caller.configured():
        if await caller.start():
            logger.info("caller: жду входящих звонков")


__all__ = ["call_now", "call_in_background", "wake_test_in_background", "on_incoming_call", "start_listening",
           "is_goodbye", "voice_reply", "greeting", "farewell", "MAX_TURNS"]
