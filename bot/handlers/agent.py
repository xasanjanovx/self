"""«Джарвис»: свободная фраза → агент с инструментами (Gemini function calling).

Как работает:
- модель получает системный промпт (кто она, правила, срез данных с id) + историю
  последних реплик + инструменты из `bot/agent_tools.py`;
- в цикле (до MAX_STEPS) модель либо зовёт инструменты (мы выполняем и возвращаем
  результат), либо отвечает текстом — это и есть ответ пользователю;
- всё выполняется сразу, без «точно?»: любой ход можно откатить кнопкой «↩️ Отменить»
  (см. bot/undo.py); если кандидатов несколько — модель сама покажет список и спросит;
- «сообщил трату/еду/вакансию» — модель передаёт текст специализированному парсеру
  (hand_off), у которого свой экран подтверждения;
- история диалога хранится в памяти 30 минут, поэтому работают «удали её», «нет, вторую».
"""
from __future__ import annotations

import copy
import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import agent_tools as tools
from .. import cache
from .. import categories as cats
from .. import emoji as pe
from .. import screen as screen_mod
from .. import undo
from ..context import ai, settings
from ..keyboards import _btn, t
from ..profile import Profile
from .common import answer_now, get_profile, safe_delete, safe_edit, show_panel, show_progress

router = Router(name="agent")
logger = logging.getLogger(__name__)

MAX_STEPS = 8
HISTORY_TTL = 1800.0
HISTORY_MAX_MESSAGES = 24
HISTORY_MAX_CHARS = 16000
REPLY_MAX_CHARS = 3500

# Слова-триггеры: такие фразы идут к агенту раньше финансового/пищевого парсера
# (важно внутри экранов «Финансы»/«Питание», где любой текст иначе считается записью).
_COMMAND_HINTS = (
    "удали", "удалить", "убери", "убрать", "сотри", "отмени", "исправ", "поправ", "ошиб", "не правильно", "неправильно",
    "измени", "поменяй", "замени", "перенеси", "лимит", "бюджет", "больше нет", "больше нету", "нету больше", "нет больше",
    "напомни", "напоминай", "напоминание", "отправляй", "присылай", "каждый день", "каждое утро", "каждый вечер", "по будням",
    "что мне поесть", "что поесть", "что съесть", "посоветуй", "чем перекусить", "что приготовить",
    "покажи", "открой", "выключи", "включи", "поставь", "установи", "настрой", "сколько", "какой", "какие", "почему", "что ", "как ",
    "o'chir", "ochir", "tuzat", "xato", "limit", "eslat", "har kuni", "yubor", "nima yeyin", "nima yesam", "maslahat",
    "ko'rsat", "korsat", "och", "qancha", "qanday", "nega", "o'zgartir", "ozgartir",
    "не 1", "не 2", "не 3", "не 4", "не 5", "не 6", "не 7", "не 8", "не 9",
)
_NOT_A_COMMAND = ("должен", "qarz", "дал ", "взял", "вернул")
_STRONG = (
    "удали", "убери", "исправ", "поправ", "ошиб", "измени", "поменяй", "лимит", "напомин", "отправляй", "присылай", "больше нет",
    "покажи", "открой", "выключи", "включи", "o'chir", "tuzat", "eslat", "ko'rsat",
)


def looks_like_command(text: str) -> bool:
    low = f" {str(text or '').lower()} "
    if not any(k in low for k in _COMMAND_HINTS):
        return False
    # «дал Алишеру 200000, а он вернул» — это операции, а не команда; но «удали …» — команда всегда
    if any(k in low for k in _STRONG):
        return True
    return not any(k in low for k in _NOT_A_COMMAND)


# ------------------------------------------------------------------ system prompt
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def system_prompt(profile: Profile, snapshot: str) -> str:
    now = profile.now
    name = profile.first_name or "пользователя"
    lang = "узбекский (латиница)" if profile.lang == "uz" else "русский"
    return (
        f"Ты — Джарвис, личный ассистент {name} внутри Telegram-бота Self (финансы, питание, напоминания, вакансии). "
        "Ты умный, точный и немногословный; действуешь, а не переспрашиваешь.\n"
        f"Сейчас: {_WEEKDAYS[now.weekday()]}, {now.date().isoformat()} {now.strftime('%H:%M')} ({profile.tz_name}). Валюта: {profile.currency}. "
        f"Язык пользователя по умолчанию: {lang} — отвечай на том языке, на котором он пишет.\n\n"
        "ЧТО ТЫ УМЕЕШЬ (инструменты): смотреть и менять операции (расходы/доходы/переводы/долги), счета, лимиты, регулярные платежи, "
        "напоминания, дневник питания, план КБЖУ, настройки сводок и отчётов; заметки («запомни»), задачи, цели накоплений, сроки возврата долгов; "
        "считать статистику и глубокий анализ; открывать экраны; передавать записи парсерам.\n\n"
        "ПРАВИЛА:\n"
        "1. Команды выполняй СРАЗУ и без вопросов «точно?» — у пользователя есть кнопка «Отменить». Массовые действия (удалить всё за месяц, очистить дневник) тоже выполняй сразу.\n"
        "2. Никогда не выдумывай id. Бери id из «Данные» ниже или из результата list_*. Если подходящих записей в «Данных» нет — сначала вызови list_* с фильтром.\n"
        "3. Если под описание подходит НЕСКОЛЬКО записей и из фразы неясно, какая именно — не угадывай: покажи нумерованный список (дата · сумма · категория · комментарий) и спроси, какую. "
        "Если ясно («последнее такси», «вчерашний обед», «все такси за неделю») — выполняй.\n"
        "4. Пользователь СООБЩАЕТ о трате/доходе/долге сегодня («такси 25000», «дал Алишеру 200к») → hand_off(finance). Сообщает, что съел («съел плов») → hand_off(food). "
        "Прислал текст вакансии → hand_off(vacancy). После hand_off ничего не пиши. Исключения — add_* напрямую: явная дата в прошлом («вчера», «3 сентября»), несколько операций с разными датами, или уже известные ккал.\n"
        "5. Вопросы по данным («сколько потратил на еду в этом месяце», «что я ел вчера», «кто мне должен») — возьми цифры инструментом и ответь конкретно.\n"
        "6. Совет по питанию («что поесть») — посмотри get_nutrition_summary и предложи 2–4 варианта с граммами и ккал под остаток дня, простые продукты, доступные в Узбекистане; ночью — лёгкое и белковое.\n"
        "7. Всё остальное (общие вопросы, перевод, объяснения, болтовня) — отвечай как умный дружелюбный ассистент, зная контекст данных пользователя.\n"
        "8. Формат ответа: коротко (1–8 строк), БЕЗ markdown (никаких **, #, таблиц, code-блоков), можно эмодзи и списки через «•». "
        "Суммы — с пробелами между тысячами (1 250 000) без валюты или с «сум». После действия — что именно сделано (что удалено/изменено, сколько).\n"
        "9. Суммы в речи: «10 тыс»=10000, «700к»=700000, «1.5 млн»=1500000, «40к»=40000. Категории — только ключи из списка ниже. "
        "«Без имени» в долгах → clear_unnamed_debt. «На карте сейчас 500к» → set_account_balance. «Напомни через 2 часа» → add_reminder с временем = сейчас + 2 ч, days=once, date=сегодня.\n"
        "10. Ошибка инструмента — объясни по-человечески, не показывай JSON. Никогда не придумывай цифры и записи: если инструмент их не вернул — скажи, что данных нет.\n"
        "11. «Покажи/открой лимиты / финансы / питание / статистику / регулярные» → open_screen: экран сам покажет данные, перечислять их не нужно.\n"
        "12. Имена людей в долгах могут быть записаны иначе, чем сказал пользователь («Асельбек» vs «Асилбек» — голосовой ввод): смотри «Долги по людям» ниже и подбирай похожее имя; note_contains ищет нечётко. "
        "«Дал Асилбеку не 2 млн, а 1 млн» = итог по человеку должен стать 1 000 000: посмотри его долговые операции и исправь сумму / удали лишнюю, чтобы итог сошёлся.\n"
        "13. «Проанализируй мои данные / где переплачиваю / прогноз до конца месяца / сделай отчёт» → deep_analysis, затем разбор: главный тренд; 3–5 конкретных находок с цифрами "
        "(что выросло и на сколько, аномалии, прогноз остатка, лимиты под угрозой); 2–3 совета с конкретными суммами. Тут можно длиннее — до 15 строк; смысловые блоки разделяй пустой строкой, блок начинай с эмодзи. "
        "Названия категорий бери из category_labels, не показывай ключи (food → Еда (кафе)).\n"
        "14. «Запомни, что …» → add_note (факт коротко). Если это событие с датой (день рождения, встреча, срок) — ещё и add_task с due_date (год — ближайший будущий). "
        "«Купить лампочку», «позвонить маме завтра в 18:00», «мои дела на сегодня», «сделал/готово/купил» → задачи (add_task / list_tasks / complete_tasks). "
        "Вопрос о фактах из прошлого («когда у брата день рождения?», «какой у меня размер?») — смотри «Заметки» ниже или list_notes.\n"
        "15. «Хочу накопить 10 млн на ноутбук к январю» → add_goal; «отложил 500к на ноутбук» → update_goal(add_amount); «как дела с целью?» → list_goals и ответ: накоплено, осталось, нужно в месяц, успеваем ли.\n"
        "16. Долг со сроком: «дал Асилбеку 1 млн, вернёт до 5 октября» → add_finance_entries (transfer card→lent, note=имя) + set_debt_deadline; «Асилбек вернёт до пятницы» → только set_debt_deadline. "
        "«Напиши сообщение Асилбеку про долг» — напиши вежливый короткий текст на языке пользователя с суммой и сроком.\n\n"
        f"Категории расходов: {cats.prompt_catalog('expense')}.\nКатегории доходов: {cats.prompt_catalog('income')}.\n"
        "Счета (bucket): card — карта, cash — наличные, lent — мне должны, debt — я должен.\n\n"
        f"ДАННЫЕ:\n{snapshot}"
    )


# ------------------------------------------------------------------ history
def _compact(value: Any, depth: int = 0) -> Any:
    """Сжать результат инструмента для хранения в истории: длинные списки → первые 10 элементов."""
    if isinstance(value, list):
        items = value[:10]
        out = [_compact(v, depth + 1) for v in items]
        if len(value) > 10:
            out.append(f"… ещё {len(value) - 10}")
        return out
    if isinstance(value, dict):
        return {k: _compact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


def compact_message(msg: dict[str, Any]) -> dict[str, Any]:
    parts = []
    for part in msg.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("functionResponse"), dict):
            fr = dict(part["functionResponse"])
            fr["response"] = _compact(fr.get("response"))
            parts.append({"functionResponse": fr})
        else:
            parts.append(part)
    return {"role": msg.get("role"), "parts": parts}


def _size(msg: dict[str, Any]) -> int:
    try:
        return len(json.dumps(msg, ensure_ascii=False))
    except Exception:
        return 0


def trim_history(contents: list[dict[str, Any]], *, max_messages: int = HISTORY_MAX_MESSAGES, max_chars: int = HISTORY_MAX_CHARS) -> list[dict[str, Any]]:
    """Оставить хвост истории: не длиннее max_messages/max_chars и начинающийся с текстовой реплики пользователя
    (чтобы не оборвать пару functionCall/functionResponse)."""
    msgs = [compact_message(m) for m in contents]
    while msgs and (len(msgs) > max_messages or sum(_size(m) for m in msgs) > max_chars):
        msgs.pop(0)
    while msgs and not _is_user_text(msgs[0]):
        msgs.pop(0)
    return msgs


def _is_user_text(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "user" and any(isinstance(p, dict) and p.get("text") for p in (msg.get("parts") or []))


def load_history(uid: int) -> list[dict[str, Any]]:
    value = cache.get(uid, ("agent_history",))
    return copy.deepcopy(value) if isinstance(value, list) else []


def save_history(uid: int, contents: list[dict[str, Any]]) -> None:
    cache.put(uid, ("agent_history",), trim_history(contents), HISTORY_TTL)


def forget_history(uid: int) -> None:
    cache.put(uid, ("agent_history",), None, 1)


# ------------------------------------------------------------------ reply rendering
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BULLET_RE = re.compile(r"^\s*[\*\-–]\s+", re.MULTILINE)
_HEADER_RE = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)


def render_reply(text: str) -> str:
    """Текст модели → безопасный HTML: экранируем, **жирный** оставляем, маркеры списков → «•»."""
    clean = html.escape(str(text or "").strip())
    clean = _HEADER_RE.sub("", clean)
    clean = _BULLET_RE.sub("• ", clean)
    clean = _BOLD_RE.sub(r"<b>\1</b>", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    if len(clean) > REPLY_MAX_CHARS:
        clean = clean[: REPLY_MAX_CHARS - 1].rstrip() + "…"
    return clean


# ------------------------------------------------------------------ agent loop
@dataclass
class AgentResult:
    text: str
    ctx: tools.ToolContext
    contents: list[dict[str, Any]]
    steps: int


StepFn = Callable[..., Awaitable[Any]]
RunFn = Callable[[str, dict[str, Any], tools.ToolContext], Awaitable[dict[str, Any]]]


async def run_agent(
    profile: Profile,
    text: str,
    history: list[dict[str, Any]],
    *,
    snapshot: str,
    step_fn: StepFn | None = None,
    run_tool: RunFn | None = None,
    max_steps: int = MAX_STEPS,
) -> AgentResult:
    """Чистый цикл агента (без Telegram): историю + новую реплику → инструменты → финальный текст."""
    step_fn = step_fn or ai.agent_step
    run_tool = run_tool or tools.run
    ctx = tools.ToolContext(profile=profile, text=text)
    contents = list(history) + [{"role": "user", "parts": [{"text": text}]}]
    system = system_prompt(profile, snapshot)
    decls = tools.declarations()
    final = ""
    steps = 0
    for steps in range(1, max_steps + 1):
        step = await step_fn(contents, system=system, tools=decls, thinking_budget=settings.agent_thinking_budget)
        contents.append({"role": "model", "parts": step.parts or [{"text": step.text or "…"}]})
        if not step.calls:
            final = step.text or final
            break
        responses = []
        for name, args in step.calls:
            logger.info("agent tool %s %s", name, json.dumps(args, ensure_ascii=False)[:300])
            result = await run_tool(name, args, ctx)
            responses.append({"functionResponse": {"name": name, "response": result}})
        contents.append({"role": "user", "parts": responses})
        if ctx.handoff:
            # парсер покажет свой экран; в историю кладём отметку, чтобы пары call/response были закрыты
            final = ""
            contents.append({"role": "model", "parts": [{"text": f"(передано в {ctx.handoff[0]})"}]})
            break
        if step.text and not ctx.handoff:
            final = step.text  # модель могла ответить текстом и одновременно позвать инструмент
    else:
        final = final or ("Juda ko'p qadam, to'xtadim. Aniqroq yozing." if profile.lang == "uz" else "Слишком много шагов, остановился. Уточни запрос.")
    if not final and not ctx.handoff:
        final = ("Bajarildi." if profile.lang == "uz" else "Готово.") if ctx.mutated else ("Tushunmadim, boshqacha yozing." if profile.lang == "uz" else "Не понял, напиши иначе.")
        contents.append({"role": "model", "parts": [{"text": final}]})
    return AgentResult(text=final, ctx=ctx, contents=contents, steps=steps)


# ------------------------------------------------------------------ telegram glue
def _reply_kb(lang: str, *, undo_available: bool) -> InlineKeyboardMarkup:
    rows = []
    if undo_available:
        rows.append([_btn("↩️ " + t(lang, "cancel"), "agent:undo", icon=pe.id_for("🔄"))])
    rows.append([_btn(t(lang, "to_menu"), "menu:open", style="primary", icon=pe.ID_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_command(message: Message, state: FSMContext, profile: Profile, text: str, *, own_message: bool = True, voice: bool = False) -> bool:
    """Прогнать фразу через агента. Возвращает False только если агент недоступен (ошибка AI).
    own_message=False — `message` это экран бота (кнопка), а не сообщение пользователя: его не удаляем.
    voice=True — пришло голосом: если включены голосовые ответы, продублируем ответ голосом."""
    uid = profile.telegram_id
    await show_progress(message, profile.tr("⏳ Понял, делаю…", "⏳ Tushundim, bajaryapman…"))
    undo.begin_turn(uid)
    try:
        snapshot = await tools.snapshot(profile)
    except Exception:
        logger.exception("agent snapshot failed")
        snapshot = "(данные временно недоступны)"
    try:
        result = await run_agent(profile, text, load_history(uid), snapshot=snapshot)
    except Exception:
        logger.exception("agent failed")
        undo.end_turn(uid)
        return False
    mutated = undo.end_turn(uid)
    save_history(uid, result.contents)
    ctx = result.ctx
    logger.info("agent: steps=%d tools=%s mutated=%s handoff=%s", result.steps, ctx.calls, mutated, ctx.handoff)

    if ctx.handoff:
        module, payload = ctx.handoff
        await _dispatch_handoff(message, state, profile, module, payload)
        return True

    if own_message:
        await safe_delete(message)
    reply = render_reply(result.text)
    if ctx.open_screen:
        await _open_screen(message, state, profile, ctx.open_screen, notice=reply, undo_available=mutated)
        return True
    await state.clear()
    await show_panel(message, state, reply, _reply_kb(profile.lang, undo_available=mutated))
    if voice:
        await _send_voice_reply(message, profile, result.text)
    return True


async def _send_voice_reply(message: Message, profile: Profile, text: str) -> None:
    """Голосовой ответ на голосовой вопрос (настройка voice_reply, нужны TTS-модель и ffmpeg)."""
    from .. import services
    from .. import voice as voice_mod
    from aiogram.types import BufferedInputFile

    try:
        if not voice_mod.available():
            return
        us = await services.user_settings(profile.telegram_id)
        if not us.get("voice_reply", True):
            return
        data = await voice_mod.make_voice(text)
        if not data:
            return
        sent = await message.bot.send_voice(message.chat.id, BufferedInputFile(data, filename="jarvis.ogg"))
        screen_mod.track_ephemeral(message.chat.id, sent.message_id)
    except Exception:
        logger.warning("voice reply failed", exc_info=True)


async def _dispatch_handoff(message: Message, state: FSMContext, profile: Profile, module: str, text: str) -> None:
    if module == "finance":
        from .finance import handle_finance_text

        await handle_finance_text(message, state, profile, text, source="text", reroute=False)
    elif module == "food":
        from .nutrition import handle_text

        await handle_text(message, state, profile, text, reroute=False)
    else:
        from .vacancy import process_vacancy

        await process_vacancy(message, state, profile, text)


async def _open_screen(message: Message, state: FSMContext, profile: Profile, screen: str, *, notice: str, undo_available: bool) -> None:
    if screen == "finance":
        from .finance import render_panel

        await render_panel(message, state, profile, notice=notice)
    elif screen == "nutrition":
        from .nutrition import render_panel as render_nutrition

        await render_nutrition(message, state, profile, notice=notice)
    elif screen == "budgets":
        from .finance_extra import render_budgets

        await render_budgets(message, state, profile, notice=notice)
    elif screen == "recurring":
        from .finance_extra import render_recurring

        await render_recurring(message, state, profile, notice=notice)
    elif screen == "tasks":
        from .assistant import render_tasks

        await render_tasks(message, state, profile, notice=notice)
    elif screen == "goals":
        from .assistant import render_goals

        await render_goals(message, state, profile, notice=notice)
    elif screen == "stats":
        from .. import finance as fin
        from .. import services
        from ..keyboards import finance_stats_keyboard
        from .finance import build_stats_text

        entries, limits = await services.finance_entries(profile.telegram_id), await services.budgets(profile.telegram_id)
        stats = fin.compute_stats(entries, fin.period_for("month", profile.today))
        await state.clear()
        await show_panel(message, state, build_stats_text(stats, profile, limits) + (f"\n\n{notice}" if notice else ""), finance_stats_keyboard("month", profile.lang))
    else:
        from .menu import render_dashboard

        await render_dashboard(message, state, profile, notice=notice, undo=undo_available)


@router.callback_query(F.data == "agent:undo")
async def cb_undo(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    uid = profile.telegram_id
    if not undo.peek(uid):
        await answer_now(callback, profile.tr("Отменять нечего", "Bekor qiladigan narsa yo'q"), alert=True)
        return
    await answer_now(callback, profile.tr("Отменено ↩️", "Bekor qilindi ↩️"))
    try:
        await undo.apply(uid, tz_name=profile.tz_name)
    except Exception:
        logger.exception("undo failed")
        await safe_edit(callback, profile.tr("Не удалось отменить.", "Bekor qilib bo'lmadi."), _reply_kb(profile.lang, undo_available=False))
        return
    # агент должен знать, что действие откатили
    history = load_history(uid)
    if history:
        history.append({"role": "user", "parts": [{"text": "(нажал «Отменить»: последнее действие откачено)"}]})
        history.append({"role": "model", "parts": [{"text": "Понял, откатил."}]})
        save_history(uid, history)
    from ..keyboards import main_menu_keyboard
    from .menu import build_dashboard

    await state.clear()
    if callback.message is not None:
        await screen_mod.drop_chart(callback.bot, callback.message.chat.id)
    await safe_edit(callback, await build_dashboard(profile), main_menu_keyboard(profile.lang))


__all__ = ["router", "looks_like_command", "handle_command", "run_agent", "render_reply", "trim_history", "system_prompt", "AgentResult"]
