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
from .. import i18n
from ..about import ABOUT_SELF
from .. import agent_tools_extra as extra
from .. import cache
from .. import services
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
HISTORY_TTL = 12 * 3600.0  # диалог помнится весь день; «вчера» — в дайджесте user_memory.recent
HISTORY_MAX_MESSAGES = 40
HISTORY_MAX_CHARS = 24000
ASK_TTL = 1800.0
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
    "вес ", "взвесил", "весил", "вешу", "vazn", "позвони", "набери", "перезвони", "qo'ng'iroq", "qongiroq", "telefon qil", "сделал", "сходил", "пробежал", "отметь", "qildim", "bordim", "цель", "maqsad", "сэконом", "не больше", "процент", "%",
    "разбуди", "буди", "будильник", "подъём", "подъем", "такбир", "намаз", "фаджр", "бомдод", "аят", "хадис", "uyg'ot", "uygot", "takbir", "namoz", "oyat", "hadis",
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


REPLY_LANGS = {"uz": "узбекский (латиница)", "ru": "русский", "en": "английский (English)"}


def system_prompt(profile: Profile, snapshot: str, memory: str = "", reply_lang: str | None = None) -> str:
    now = profile.now
    name = profile.first_name or "пользователя"
    # язык ответа — язык Джарвиса из его настроек (интерфейс бота может быть на другом языке)
    lang = REPLY_LANGS.get(reply_lang or profile.lang, "русский")
    return (
        f"Ты — Джарвис, личный ассистент {name} внутри Telegram-бота Self (финансы, питание, задачи, цели, напоминания, вакансии). "
        "Ты умный, точный и немногословный; понимаешь с полуслова, действуешь, а не переспрашиваешь. "
        "Ты ПОЛНОЦЕННЫЙ личный помощник, а не только финансовый: отвечаешь на любые вопросы (жизнь, здоровье, работа, учёба, техника, религия, советы, тексты), "
        "ищешь в интернете, считаешь, прикидываешь. Никогда не отвечай «не могу» / «нет информации» / «я только про финансы» — найди (web_search), оцени или скажи, как узнать. "
        "Ты живой человек, а не робот: шутит — ответь с юмором, устал или расстроен — поддержи.\n"
        "Пользователь часто диктует голосом: опечатки, склейки слов, имена и бренды в другой транскрипции (Узум = Uzum, Хамкор = Hamkorbank) — "
        "восстанавливай смысл по контексту и данным, а не отвечай «не понял».\n"
        f"Сейчас: {_WEEKDAYS[now.weekday()]}, {now.date().isoformat()} {now.strftime('%H:%M')} ({profile.tz_name}). Валюта: {profile.currency}. "
        f"ЯЗЫК ОТВЕТА: всегда {lang} (выбран в настройках Джарвиса) — даже если он пишет или говорит на другом языке "
        "(узбекский, русский, английский, таджикский, казахский, турецкий…) или смешивает языки: понимай любой, отвечай только на этом. "
        "Другой язык — только если он прямо попросил в этом сообщении («ответь по-русски», «ruscha yoz», «in English»).\n\n"
        "ЧТО ТЫ УМЕЕШЬ (инструменты): смотреть и менять операции (расходы/доходы/переводы/долги), счета, лимиты, регулярные платежи, "
        "напоминания, дневник питания, план КБЖУ, настройки сводок и отчётов; заметки («запомни»), задачи, цели любого вида (накопления, лимиты трат, вес, привычки, свободные), взвешивания, сроки возврата долгов; привычки пользователя по его данным; "
        "подъём на фаджр со звонком-разговором в Telegram, времена намаза (Андижан); "
        "считать статистику и глубокий анализ; открывать экраны; передавать записи парсерам.\n\n"
        "ПРАВИЛА:\n"
        "1. Команды выполняй СРАЗУ и без вопросов «точно?» — у пользователя есть кнопка «Отменить». Массовые действия (удалить всё за месяц, очистить дневник) тоже выполняй сразу.\n"
        "2. Никогда не выдумывай id. Бери id из «Данные» ниже или из результата list_*. Если подходящих записей в «Данных» нет — сначала вызови list_* с фильтром.\n"
        "3. Если под описание подходит НЕСКОЛЬКО записей и из фразы неясно, какая именно — не угадывай: ask_user с вариантами-кнопками (до 4: дата · сумма · комментарий); "
        "если кандидатов больше — покажи нумерованный список и спроси. Если ясно («последнее такси», «вчерашний обед», «все такси за неделю») — выполняй.\n"
        "3а. НИКОГДА не отвечай «не понял». Если фраза неоднозначна — сначала попробуй понять по данным и памяти; если всё ещё 2–4 трактовки — ask_user с этими трактовками "
        "(«Записать 2.5 млн как долг Uzum?», «Поставить срок 5 октября по долгу Uzum?»). Если трактовка одна и правдоподобна — выполняй, а в ответе скажи, как понял.\n"
        "4. Пользователь СООБЩАЕТ о простой трате/доходе сегодня («такси 25000», «обед 40к картой») → hand_off(finance): у него экран подтверждения с категориями. "
        "Долги, переводы, снятие/пополнение, возвраты («дал Алишеру 200к», «Uzum списал 2.5 млн кредит», «снял 300к») → add_finance_entries напрямую (transfer по правилам инструмента) — ты видишь имена и долги, парсер нет. "
        "Сообщает, что съел («съел плов») → hand_off(food). Прислал текст вакансии → hand_off(vacancy). После hand_off ничего не пиши. "
        "Также add_* напрямую: явная дата в прошлом («вчера», «3 сентября»), несколько операций с разными датами, уже известные ккал.\n"
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
        "15. ЦЕЛИ — любые, и ты ведёшь их по реальным данным, а не напоминалками. add_goal с kind: накопить (save), «тратить не больше 5 млн» / «сэкономить 2 млн в этом месяце» / «на еду не больше 1.5 млн» (spend_cap), "
        "«набрать до 75 кг к декабрю» / «сбросить 5 кг» (weight; если сказал текущий вес — current_weight; «сбросить 5 кг» = target = текущий − 5), «зал 3 раза в неделю» (habit), всё остальное (custom). "
        "«вес 72.5» / «взвесился 71.8» → log_weight. «сходил в зал» / «сделал» / «пробежал» → goal_checkin по подходящей привычке. «выучил 60%» / «прогресс 40» → update_goal(progress_pct). "
        "«как дела с целями?» / «успеваю?» → list_goals и ответ ЦИФРАМИ: что уже есть, что нужно в день/неделю, успеваем ли, и один конкретный шаг на сегодня. "
        "Цель по весу: после add_goal посмотри recommended_kcal — если план КБЖУ отличается больше чем на 150 ккал (flag plan_mismatch), сразу предложи «поставить план N ккал» (set_nutrition_plan). "
        "Совет по еде при цели по весу — из ПРИВЫЧНЫХ блюд пользователя (блок «Привычки в еде» в Данных или my_habits), под остаток ккал на сегодня.\n"
        "15а. Ты ЗНАЕШЬ привычки пользователя (блок «Привычки…» в Данных): что он ест на завтрак/обед/ужин, сколько тратит в день, на что. Опирайся на это в советах и ответах "
        "(«обычно ты завтракаешь омлетом ~450 ккал», «в будни ты тратишь ~150к, сегодня уже 300к»). Вопросы «что я обычно ем на завтрак?», «сколько трачу в день?» → my_habits.\n"
        "16. Долг со сроком: «дал Асилбеку 1 млн, вернёт до 5 октября» → add_finance_entries (transfer card→lent, note=имя) + set_debt_deadline; «Асилбек вернёт до пятницы» → только set_debt_deadline. "
        "Кредитор из «Я должен» (банк, Uzum, Hamkor, человек): «срок долга Узум 5 октября», «Uzum вернуть до 5.10», «запиши срок по Узумбанку — 5 число» → set_debt_deadline(person как в «Я должен», side=debt, due_date). "
        "Если такого имени нет ни в «Мне должны», ни в «Я должен» — ask_user: «записать новый долг» / «только срок». "
        "«Напиши сообщение Асилбеку про долг» — напиши вежливый короткий текст на языке пользователя с суммой и сроком.\n"
        "17. Память: устойчивые факты о пользователе (люди и кто они, привычки, даты, суммы, предпочтения) сохраняй через remember_about_me сам, без просьбы, "
        "и учитывай блок ПАМЯТЬ ниже при ответах. «Запомни, что …» → add_note (факт) и, если это про самого пользователя, remember_about_me.\n"
        "17в. «Позвони мне» / «набери» / «qo'ng'iroq qil» (в любое время суток) → call_me. Если сказал, о чём («позвони и разберём траты») — передай topic. "
        "В самом звонке ты говоришь голосом и можешь всё то же, что в чате: ответить цифрами по данным, записать трату/еду/задачу с голоса. "
        "После вызова инструмента ответь одной строкой: «Звоню 📞» — и всё.\n"
        "17а. ПОДЪЁМ НА ФАДЖР. Ты будишь звонком в Telegram до такбира и не отстаёшь, пока человек не подтвердит подъём. "
        "«разбуди за 30 минут до такбира» / «буди в 6:30» / «по будням» / «не звони неделю» / «говори по-русски» → set_wake. Заданий и упражнений при подъёме нет — будишь добрыми словами. "
        "«такбир у нас в 5:20» → set_wake(takbir_time) — это поправка под его мечеть, дальше считается каждый день само. "
        "«во сколько разбудишь?», «когда такбир?», «когда аср?» → get_wake / prayer_times и точный ответ. "
        "«проснулся» / «uyg'ondim» / «я встал» → mark_awake (звонки прекращаются); «ещё 10 минут» → mark_awake(snooze_minutes=10). "
        "После подъёма коротко скажи, сколько минут до такбира, и пожелай: «Пусть Аллах примет ваш намаз». Не читай нотаций.\n"
        "18. Помощник вне бота: курс валют / «сколько это в долларах» → currency_rates; любые расчёты → calculate (не считай в уме суммы больше 4 знаков); "
        "свежие факты, цены, новости, адреса, «что такое …» → web_search; погода → weather. Перевод, объяснения, тексты, советы — отвечай сам.\n"
        "19. ФОТО. Если к реплике приложено фото — ты его ВИДИШЬ: пойми, что на нём, и сделай нужное. Чек/квитанция → траты (add_finance_entries с датой и суммами с чека); "
        "лист челленджа/трекер привычек/чек-лист → найди закрашенные/отмеченные пункты за сегодня и отметь их (goal_checkin; нет таких целей — создай add_goal kind=habit "
        "для каждого пункта и отметь); скриншот/документ → ответь или сделай по смыслу. Еда → hand_off(food). Если есть [договорённость о фото] — делай строго по ней. "
        "После — коротко: что увидел и что сделал.\n"
        "20. ЧЕСТНОСТЬ. Не обещай того, чего не сделаешь инструментами. Обещаешь что-то сделать с будущими фото («пришли фото — отмечу») — СРАЗУ вызови expect_photo "
        "с подробной инструкцией и сроком, иначе фото уйдёт в питание. Почти всё можно сделать своими инструментами, поиском или советом — ищи способ. "
        "Если по-настоящему невозможно — скажи честно одной фразой и предложи ближайшую замену.\n\n"
        f"Категории расходов: {cats.prompt_catalog('expense')}.\nКатегории доходов: {cats.prompt_catalog('income')}.\n"
        "Счета (bucket): card — карта, cash — наличные, lent — мне должны, debt — я должен.\n\n"
        + ABOUT_SELF + "\n"
        + (f"{memory}\n\n" if memory else "")
        + f"ДАННЫЕ:\n{snapshot}"
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
        elif isinstance(part, dict) and ("inline_data" in part or "inlineData" in part):
            parts.append({"text": "[фото]"})  # само фото в историю не кладём — оно огромное
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


NOTICE_TTL = 120.0      # через столько секунд короткая реплика сама удаляется
NOTICE_MAX_CHARS = 140  # длиннее — это уже содержательный ответ, ему нужен экран с кнопками


def _is_notice(reply: str, *, mutated: bool) -> bool:
    """Короткая реплика-подтверждение, которой не нужен ни экран, ни кнопки.

    Изменения данных сюда не попадают: там нужна кнопка «Отменить».
    """
    if mutated or not reply:
        return False
    plain = re.sub(r"<[^>]+>", "", reply).strip()
    return len(plain) <= NOTICE_MAX_CHARS and plain.count("\n") <= 1


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
    memory: str = "",
    step_fn: StepFn | None = None,
    run_tool: RunFn | None = None,
    max_steps: int = MAX_STEPS,
    image: tuple[bytes, str] | None = None,
    reply_lang: str | None = None,
) -> AgentResult:
    """Чистый цикл агента (без Telegram): историю + новую реплику → инструменты → финальный текст.
    `image` = (bytes, mime) — фото к реплике: модель видит его сама (чек, лист челленджа, скриншот…)."""
    step_fn = step_fn or ai.agent_step
    run_tool = run_tool or tools.run
    ctx = tools.ToolContext(profile=profile, text=text)
    parts: list[dict[str, Any]] = [{"text": text}]
    if image:
        import base64

        parts.append({"inline_data": {"mime_type": image[1], "data": base64.b64encode(image[0]).decode()}})
    contents = list(history) + [{"role": "user", "parts": parts}]
    system = system_prompt(profile, snapshot, memory, reply_lang=reply_lang)
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
        if ctx.ask:
            # вопрос с кнопками: ответ пользователя придёт следующей репликой в этот же диалог
            final = ctx.ask["question"]
            opts = " / ".join(ctx.ask.get("options") or [])
            contents.append({"role": "model", "parts": [{"text": final + (f" [{opts}]" if opts else "")}]})
            break
        if step.text and not ctx.handoff:
            final = step.text  # модель могла ответить текстом и одновременно позвать инструмент
    else:
        final = final or ("Juda ko'p qadam, to'xtadim. Aniqroq yozing." if profile.lang == "uz" else "Слишком много шагов, остановился. Уточни запрос.")
    if not final and not ctx.handoff:
        final = ("Bajarildi." if profile.lang == "uz" else "Готово.") if ctx.mutated else (
            "Aniq tushunmadim — nimani nazarda tutdingiz?" if profile.lang == "uz" else "Не уверен, что ты имеешь в виду — уточни одним словом?")
        contents.append({"role": "model", "parts": [{"text": final}]})
    return AgentResult(text=final, ctx=ctx, contents=contents, steps=steps)


# ------------------------------------------------------------------ telegram glue
def _reply_kb(lang: str, *, undo_available: bool, options: list[str] | None = None) -> InlineKeyboardMarkup:
    rows = []
    for i, opt in enumerate(options or []):
        rows.append([_btn(opt, f"agent:opt:{i}", style="primary")])
    if undo_available:
        rows.append([_btn("↩️ " + t(lang, "cancel"), "agent:undo", icon=pe.id_for("🔄"))])
    rows.append([_btn(t(lang, "to_menu"), "menu:open", style="primary" if not options else None, icon=pe.ID_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_command(message: Message, state: FSMContext, profile: Profile, text: str, *, own_message: bool = True, voice: bool = False,
                         photo: tuple[bytes, str, str] | None = None) -> bool:
    """Прогнать фразу через агента. Возвращает False только если агент недоступен (ошибка AI).
    own_message=False — `message` это экран бота (кнопка), а не сообщение пользователя: его не удаляем.
    voice=True — пришло голосом: если включены голосовые ответы, продублируем ответ голосом."""
    uid = profile.telegram_id
    cache.put(uid, ("agent_ask",), None, 1)  # новый ход — прошлый вопрос с кнопками больше не ждёт ответа
    await show_progress(message, profile.tr("⏳ Понял, делаю…", "⏳ Tushundim, bajaryapman…"))
    undo.begin_turn(uid)
    try:
        snapshot = await tools.snapshot(profile)
    except Exception:
        logger.exception("agent snapshot failed")
        snapshot = "(данные временно недоступны)"
    memory = await extra.memory_prompt(uid)
    reply_lang = profile.lang
    try:
        from .. import persona as persona_mod

        persona = await services.persona(uid)
        reply_lang = persona.lang
        rules = "ХАРАКТЕР (настройки пользователя): " + persona_mod.style_rules(persona)
        memory = f"{memory}\n\n{rules}" if memory else rules
    except Exception:
        logger.debug("persona rules failed", exc_info=True)
    try:
        result = await run_agent(profile, text, load_history(uid), snapshot=snapshot, memory=memory,
                                 image=(photo[0], photo[1]) if photo else None, reply_lang=reply_lang)
    except Exception:
        logger.exception("agent failed")
        undo.end_turn(uid)
        await services.log_agent(uid, text=text, kind="error", ok=False)
        return False
    mutated = undo.end_turn(uid)
    save_history(uid, result.contents)
    ctx = result.ctx
    logger.info("agent: steps=%d tools=%s mutated=%s handoff=%s ask=%s", result.steps, ctx.calls, mutated, ctx.handoff, bool(ctx.ask))
    await services.log_agent(uid, text=text, kind="asked" if ctx.ask else "handoff" if ctx.handoff else "agent",
                             tools=",".join(ctx.calls), reply=result.text, ok=bool(ctx.calls or ctx.handoff or mutated or result.text))
    if not ctx.handoff and not ctx.ask:
        await extra.remember_exchange(uid, text, result.text, when=profile.now.strftime("%d.%m %H:%M"))

    if ctx.handoff:
        module, payload = ctx.handoff
        await _dispatch_handoff(message, state, profile, module, payload, photo=photo)
        return True

    if own_message:
        await safe_delete(message)
    reply = i18n.keep(render_reply(result.text))
    if ctx.ask:
        options = list(ctx.ask.get("options") or [])
        cache.put(uid, ("agent_ask",), options, ASK_TTL)
        await state.clear()
        hint = profile.tr("Нажми вариант или напиши ответ", "Variantni bosing yoki javob yozing")
        await show_panel(message, state, f"❓ {reply}\n\n<i>{hint}</i>", _reply_kb(profile.lang, undo_available=mutated, options=options))
        return True
    if ctx.open_screen:
        await _open_screen(message, state, profile, ctx.open_screen, notice=reply, undo_available=mutated)
        return True
    await state.clear()
    if _is_notice(reply, mutated=mutated):
        # короткая реплика («Звоню 📞», «Готово») — отдельным сообщением без кнопок,
        # оно само исчезнет. Экран был занят «⏳ Понял, делаю…» — возвращаем на него главное меню.
        from .menu import render_dashboard

        await render_dashboard(message, state, profile)
        await screen_mod.send_ephemeral(message.bot, message.chat.id, reply, keep_previous=False, ttl=NOTICE_TTL)
    else:
        await show_panel(message, state, reply, _reply_kb(profile.lang, undo_available=mutated))
    if voice:
        await _send_voice_reply(message, profile, result.text)
    return True


@router.callback_query(F.data.startswith("agent:opt:"))
async def cb_option(callback: CallbackQuery, state: FSMContext) -> None:
    """Нажатие варианта из ask_user: текст кнопки уходит агенту как ответ пользователя."""
    profile = await get_profile(callback.from_user)
    options = cache.get(profile.telegram_id, ("agent_ask",))
    try:
        idx = int(callback.data.split(":")[-1])
    except ValueError:
        idx = -1
    if not isinstance(options, list) or not 0 <= idx < len(options):
        await answer_now(callback, profile.tr("Вопрос устарел — напиши ответ текстом", "Savol eskirgan — javobni yozing"), alert=True)
        return
    await answer_now(callback, options[idx][:60])
    cache.put(profile.telegram_id, ("agent_ask",), None, 1)
    if callback.message is None:
        return
    if not await handle_command(callback.message, state, profile, options[idx], own_message=False):
        await safe_edit(callback, profile.tr("Не получилось, напиши ответ текстом.", "Bo'lmadi, javobni yozing."), _reply_kb(profile.lang, undo_available=False))


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


async def _dispatch_handoff(message: Message, state: FSMContext, profile: Profile, module: str, text: str,
                            *, photo: tuple[bytes, str, str] | None = None) -> None:
    if module == "food" and photo:
        from .nutrition import handle_photo

        await handle_photo(message, state, profile, photo=photo, hint=text or None)
        return
    if module == "finance":
        from .finance import handle_finance_text

        await handle_finance_text(message, state, profile, text, source="text", reroute=False, fallback_agent=False)
    elif module == "food":
        from .nutrition import handle_text

        await handle_text(message, state, profile, text, reroute=False, fallback_agent=False)
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
