"""Звонок на Gemini Live: живой голос, ответ за ~1 секунду, можно перебивать.

Схема:
  Telegram-звонок (pytgcalls)  ──входящий звук 24 кГц──▶  Gemini Live (websocket)
                               ◀──голос модели 24 кГц───
  Модель сама слышит паузы и перебивания (VAD на стороне Gemini), отвечает голосом
  и вызывает те же инструменты, что JES в чате: добавить цель, удалить операцию,
  записать калории, ответить по данным. Всё, что изменено в звонке, после разговора
  приходит в чат одним сообщением с кнопкой «Отменить».

Режимы:
  assistant — «позвони»: обычный разговор с доступом к данным;
  wake      — подъём на фаджр: мотивировать встать, по голосу убедиться, что встал (confirm_awake).

Трубку положили → разговор окончен, перезвона нет (кроме подъёма, где перезвон —
смысл функции, пока подъём не подтверждён).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import caller
from .about import ABOUT_SELF
from .persona import LANG_CODES, Persona, human_rules, lang_rule, style_rules
from .profile import Profile

logger = logging.getLogger(__name__)

WS_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.{ver}.GenerativeService.BidiGenerateContent"
MODELS = ("gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest")
UPLINK_BATCH_MS = 40         # шлём звук в Gemini пачками по 40 мс
MAX_SECONDS = 600            # 10 минут — страховка
DRAIN_SECONDS = 8.0          # после «до связи» даём договорить фразу
SILENCE_NUDGE_SECONDS = 7.0  # подъём: столько тишины — и Джарвис снова зовёт по имени
COMPRESS_ABOVE = 8000        # разговор (сверх инструкции и инструментов) вырос до ~8 тыс. токенов (~4 мин речи) — сжимаем…
COMPRESS_KEEP = 3000         # …до последних ~3 тыс. (минута-две разговора)
# телефон (он выбрал экономнее): каждый ответ Live заново оплачивает весь разговор, звук — вчетверо дороже текста;
# 155 с разговора стоили $0.16, из них $0.06 — его же голос в памяти. Держим последние ~20–30 с.
PHONE_COMPRESS_ABOVE = 2500
PHONE_COMPRESS_KEEP = 800
# «тихие» команды (behavior NON_BLOCKING + scheduling SILENT): после действия модель не тратит второй ответ —
# проверено на gemini-3.8-live 25.09: «позвони маме» $0.0021 → $0.0009. Ошибка/вопрос — модель говорит (WHEN_IDLE).
SILENT_TOOLS = {"phone_call", "set_alarm", "set_timer", "open_app", "media", "add_finance_entries", "add_calorie_logs", "add_task",
                "add_reminder", "confirm_send", "cancel_send", "phone_task", "send_to_chat", "end_call"}
# экономные настройки сессии, которые модель приняла: 2 — «размышления» minimal + сжатие, 1 — только сжатие, 0 — ничего
_extras_level: dict[str, int] = {}
# 26.09 он выбрал «голос как в звонке»: голос тот же (Sulafat), а манера на телефоне была сухой — теперь как по телефону,
# но команды по-прежнему молча (PHONE_RULES)
PHONE_VOICE = ("МАНЕРА РЕЧИ — как в звонке: тёплая, живая, разговорная, как близкий человек; реагируй на настроение, лёгкий юмор "
               "к месту. Это про то, КАК говоришь, когда отвечаешь словами; команды всё равно выполняешь молча.\n")
SPEECH_LANGS = ("ru-RU", "uz-UZ", "en-US")      # на чём он говорит — подсказка распознаванию речи
VOCABULARY = ("JES", "Джес", "Jes")            # имя ассистента — чтобы не слышалось как «жесть»/«Jeeves»

# инструменты чата, которые в голосе не нужны или мешают
_SKIP_TOOLS = {"hand_off", "open_screen", "ask_user", "call_me", "test_wake_call"}

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


class BillingExhausted(RuntimeError):
    """Gemini: «prepayment credits are depleted» — баланс AI Studio кончился."""


@dataclass
class LiveResult:
    answered: bool = False
    error: str | None = None
    confirmed: bool = False            # wake: подтвердил подъём
    snooze_minutes: int | None = None  # wake: попросил отложить
    transcript: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    mutated: bool = False
    model: str | None = None
    dialed: bool = False               # звонок дошёл до телефона (взяли или нет) — это не сбой Gemini


# ------------------------------------------------------------------ промпт и инструменты
def now_line(profile: Profile) -> str:
    now = profile.now
    return f"Сейчас {_WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y %H:%M}"


def facts_only(memory: str) -> str:
    """Память о нём без «недавних реплик» прошлых дней — для команд на телефоне они не нужны, а оплачиваются в каждом ответе."""
    return str(memory or "").split("\n\nНЕДАВНИЕ РЕПЛИКИ")[0].strip()


def system_instruction(profile: Profile, p: Persona, *, mode: str, snapshot: str = "", memory: str = "",
                       wake: dict[str, Any] | None = None, topic: str = "", with_time: bool = True, people: str = "") -> str:
    """with_time=False — без текущего времени: экономный режим телефона кладёт его в реплику, чтобы инструкция
    не менялась каждую минуту и Gemini брал её из кэша (10% цены)."""
    now = profile.now
    name = p.name_for(profile.first_name) or "пользователь"
    channel = ("Он позвал тебя голосом («JES») на своём Android-телефоне: ты его голосовой ассистент, как Siri, только умнее — "
               "говоришь через динамик телефона и управляешь телефоном своими инструментами. "
               if mode == "phone" else "Сейчас ты говоришь с ним ПО ТЕЛЕФОНУ (звонок в Telegram). ")
    where = f"{now_line(profile)}, Андижан, Узбекистан" if with_time else "Он живёт в Андижане, Узбекистан (время — в его репликах)"
    if mode == "phone":
        # он просил: работать, а не разговаривать, и экономно — без «характера» (шутки, «ого!», «хм») и прошлых реплик;
        # эта инструкция оплачивается в КАЖДОМ ответе Live
        from . import billing

        return (f"Ты — JES (читается «Джес»), голосовой помощник {name} на его Android-телефоне; работаешь на Gemini 3.8 Live (Google). "
                f"Голос женский — о себе в женском роде. {where}. Валюта — сум.\n{lang_rule(p)}\n{style_rules(p, spoken=True)}\n"
                + PHONE_VOICE + PHONE_RULES.replace("{year}", str(now.year)) + (PHONE_ECONOMY if billing.over_limit() else "")
                + (f"\n{people}\n" if people else "")
                + (f"\n{facts_only(memory)}\n" if facts_only(memory) else ""))
    base = (
        f"Ты — JES (читается «Джес»), личный помощник {name}. {channel}"
        "Голос у тебя женский — о себе говори в женском роде («поняла», «записала»). "
        f"{where}. Валюта — сум.\n\n"
        f"{lang_rule(p)}\n\n{human_rules(p)}\n{style_rules(p, spoken=True)}\n"
        "Речь: без списков, эмодзи и markdown; суммы словами («двадцать пять тысяч сум»), не называй id записей. "
        "Если перебили — сразу остановись и слушай.\n"
    )
    if mode == "wake":
        w = wake or {}
        title = {"shef": "Шеф", "ser": "Сэр", "boss": "Босс", "mix": "Шеф"}.get(p.honorific, name)
        quiz = w.get("quiz") or ""
        today = w.get("today") or ""
        return base + (
            "\nЗАДАЧА ЗВОНКА — УТРЕННИЙ ПОДЪЁМ. Ты живой умный помощник, а не будильник-автомат: слушай, что он говорит, "
            "и отвечай по существу. Никаких проповедей и повторов одного и того же.\n"
            f"Сейчас {now:%H:%M}. Такбир фаджра в {w.get('takbir') or 'скоро'}"
            + (f", до него {w.get('minutes_left')} мин." if w.get("minutes_left") is not None else ".") + "\n"
            + (f"ЕГО ДЕНЬ: {today}\n" if today else "")
            + "ХОД РАЗГОВОРА:\n"
            f"1) Сразу, в первую же секунду, бодро и тепло: «Доброе утро, {title}!» — и один живой вопрос («Как спалось?», «Проснулись?»). "
            "Можно одной фразой что-то полезное про его день (погода — инструмент weather, дела — из «ЕГО ДЕНЬ»).\n"
            "2) Как только он ответил связно (сказал, что проснулся/встал, или просто нормально заговорил) — ВЕРЬ ему и больше "
            "НЕ повторяй «вставайте». Сразу задай ВОПРОС ДНЯ (ниже) — один, коротко, как викторину.\n"
            "3) Он ответил: верно — коротко похвали; неверно или не знает — не спорь, спокойно скажи правильный ответ. "
            "Если ответ — дуа или аят, произнеси арабский текст ТОЧНО как написан ниже, слово в слово, а потом коротко смысл. "
            "Сразу после этого вызови confirm_awake (он проснулся — это главное, правильность ответа не важна).\n"
            f"4) В самом конце — одной фразой про намаз: сколько до такбира и «Пусть Аллах примет ваш намаз». Тепло попрощайся и вызови end_call.\n"
            "Молчит больше 7 секунд в начале — позови его по имени погромче одной фразой («Шеф, вы меня слышите?»), без нотаций. "
            "Сонное мычание вместо ответа — мягко попроси сказать пару слов нормально. Просит отложить — не больше 5 минут, "
            "только если настаивает: snooze(minutes). Сам говорит «встал, отключайся» и уже ответил на вопрос — confirm_awake и end_call.\n"
            + (f"\n{quiz}\n" if quiz else "")
        )
    rules = (
        "\nТЫ — ПОЛНОЦЕННЫЙ ПОМОЩНИК, А НЕ ТОЛЬКО ФИНАНСОВЫЙ. Отвечай на ЛЮБЫЕ вопросы, как умный знающий человек: "
        "жизнь, здоровье, спорт, учёба, работа, техника, религия, история, советы, перевод, посчитать, придумать, объяснить, просто поболтать. "
        "Свежие факты (новости, цены, курсы, погода, адреса, расписания, «что такое…», «кто такой…») — сначала web_search (ищет в Google), "
        "потом ответь своими словами; погода — weather, курс валют — currency_rates, сложный расчёт — calculate. "
        f"Твои знания устарели: всё, что могло измениться за последние два года (спорт, новости, цены, законы, версии, «последний/новый/текущий»), "
        f"ОБЯЗАТЕЛЬНО проверь поиском — сейчас {now.year} год. "
        "ЗАПРЕЩЕНО отвечать «не могу», «у меня нет информации», «я только финансовый помощник»: если точных данных нет — "
        "найди, прикинь, оцени или скажи, как узнать. Вопросы про тебя самого (что умеешь, как устроен, сколько стоишь) — из блока «О СЕБЕ».\n"
        "ДЕЙСТВИЯ: частое — своими инструментами (трата — add_finance_entries, еда — add_calorie_logs со своей оценкой калорий, "
        "напоминание, задача, заметка, «сколько потратил» — get_finance_stats, погода, поиск, курс). ВСЁ ОСТАЛЬНОЕ с его данными "
        "(исправить/удалить/найти старые записи, массовые правки, цели, долги, бюджеты, регулярные платежи, вес, настройки, отчёты, анализ, будильник) — "
        "bot_task: передай просьбу полностью своими словами с числами и датами и перескажи ответ. "
        "Просьбы выполняй сразу, без «точно?» («добавь цель…», «удали вчерашнее такси», «запиши обед сорок тысяч», «напомни завтра в девять»). "
        "После действия одной живой фразой скажи, что сделано. Вопросы по его данным — сначала инструмент, потом ответ цифрами. "
        "Узнал о нём что-то важное и надолго (люди, планы, предпочтения) — сохрани remember_about_me, не говоря об этом. "
        "Просит «скинь/отправь мне в чат» (список, рецепт, текст, ссылку, план) — send_to_chat с готовым текстом и скажи, что отправила.\n"
        "ЧЕСТНОСТЬ: не обещай того, чего не сделаешь инструментами. Договорились о фото («пришлю фото челленджа — отмечай», «буду слать чеки») — "
        "СРАЗУ вызови expect_photo с подробной инструкцией и сроком: тогда его фото в чате придут JES с этой инструкцией, а не в подсчёт калорий. "
        "Хочет что-то сложное — ищи способ своими инструментами; по-настоящему невозможное — честно одной фразой и ближайшая замена.\n"
        "Когда он прощается («всё», «пока», «rahmat», «xayr», «bo'ldi») — тепло и коротко попрощайся и вызови end_call.\n"
    )
    if topic:
        opening = f"Начни разговор с темы, которую он попросил: «{topic}». Поздоровайся одной фразой и сразу к делу.\n"
    else:
        opening = "Поздоровайся одной короткой живой фразой (по имени, учитывая время суток) и жди.\n"
    return (base + rules + opening + "\n" + ABOUT_SELF
            + (f"\n{memory}\n" if memory else "") + (f"\nДАННЫЕ:\n{snapshot}" if snapshot else ""))


# Телефон: Gemini Live заново оплачивает инструкцию и описания инструментов в КАЖДОМ ответе — поэтому здесь коротко
# (раньше ~40 тысяч знаков вместе с блоком «О себе» и данными бота; данные теперь — через инструменты и bot_task).
PHONE_RULES = (
    "\nГОЛОСОВОЙ АССИСТЕНТ НА ТЕЛЕФОНЕ. Ты РАБОТАЕШЬ, а не разговариваешь. Не здоровайся. Сказал только «JES» — молчи: "
    "телефон уже ответил «Да, сэр» твоим голосом.\n"
    "КОМАНДЫ — МОЛЧА: сразу вызови инструмент и НИЧЕГО не говори — ни «делаю», ни «сейчас», ни «открываю», ни «готово»: "
    "телефон сам покажет карточку. Говори, только если он спросил то, на что нужен ответ, инструмент вернул ошибку или "
    "ask_exactly (произнеси дословно).\n"
    "Ответ — одна короткая фраза, без «что-то ещё?». На любые вопросы отвечай по существу, без «не могу»; свежие факты — "
    "web_search, погода — weather; сейчас {year} год.\n"
    "ДАННЫЕ: трата — add_finance_entries, еда — add_calorie_logs, задача, напоминание, «сколько потратил» — get_finance_stats; "
    "прочее с его данными (исправить, удалить, цели, долги, бюджеты, отчёты, подъём, баланс Gemini) — bot_task, просьбой целиком.\n"
    "ПРОЧЕЕ НА ТЕЛЕФОНЕ — phone_task, просьбой целиком и с конкретикой: SMS, «что мне написали», фонарик, громкость, маршрут, "
    "такси, WhatsApp, YouTube, галерея, яркость, не беспокоить, звонки, настройки, курс, намаз, запомнить, отменить. "
    "Адрес или место — словами; если он показывает место на экране или камерой — прочитай его с кадра и передай.\n"
    "ЛЮДИ: не переспрашивай — инструмент сам выберет; в variants — другие написания (мама → ойи, ona, oyijon). "
    "Сообщения уходят после «да» — confirm_send, «нет» — cancel_send.\n"
    "ВИДЕТЬ: камера — look, экран — screen_look; кадр придёт с результатом — сразу ответь по нему. Нажимать в других "
    "приложениях не умеешь. need_unlock — одной фразой попроси разблокировать.\n"
    "Разговор НЕ закрывается сам: после ответа молча жди. Речь, обращённую не к тебе (кто-то рядом, телевизор), — не отвечай. "
    "Прощается («всё», «пока», «bo'ldi») — end_call молча. «Скинь в чат» — send_to_chat.\n"
)
PHONE_ECONOMY = (
    "\nЭКОНОМНЫЙ РЕЖИМ (дневной лимит расходов достигнут): отвечай ОДНОЙ короткой фразой, команды — строго молча, "
    "камеру и экран не включай без прямой просьбы.\n"
)
# инструменты, которые в голосе нужны редко — через bot_task (каждое описание оплачивается в каждом ответе)
PHONE_SKIP_TOOLS = {"complete_tasks", "get_wake", "expect_photo", "ai_status", "set_ai_balance", "calculate", "add_note"}
# облегчённый живой голос (он выбрал 25.09): в Live — только частое (~9 тыс. знаков вместо 18), остальное телефонное — phone_task
# (Flash-Lite выполняет его сам, bot/phone_live.py:phone_task), данные — bot_task
PHONE_LIVE_CORE = {"end_call", "phone_call", "telegram_send", "confirm_send", "cancel_send", "set_alarm", "set_timer", "open_app",
                   "media", "look", "screen_look", "weather", "web_search", "add_finance_entries", "get_finance_stats",
                   "add_reminder", "add_calorie_logs", "add_task", "bot_task", "send_to_chat"}
_PHONE_TASK = {"name": "phone_task",
               "description": "Всё остальное на телефоне: SMS, прочитать Telegram, фонарик, громкость, маршрут, такси, WhatsApp, YouTube, "
                              "галерея, яркость, не беспокоить, звонки: журнал/перезвонить, настройки, курс, намаз, запомнить, отменить последнее.",
               "parameters": {"type": "OBJECT", "properties": {"request": {"type": "STRING", "description": "просьба целиком, как он сказал, с именами и числами"}},
                              "required": ["request"]}}
_SHORT_PARAMS = {
    "variants": "другие написания и родственные слова (мама → ойи, онам, oyijon)",
    "who": "кому, как он сказал («мама», «Алишер», номер)",
}


def _short(text: Any, limit: int) -> str:
    """Первое предложение или начало до limit знаков — без обрыва на полуслове."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]

    def closed(i: int) -> bool:  # не резать внутри скобок и кавычек («моб. данные)»)
        head = cut[:i]
        return head.count("(") == head.count(")") and head.count("«") == head.count("»")

    for sep in (". ", "; ", "? ", " — ", ": ", ", "):
        i = len(cut)
        while (i := cut.rfind(sep, 0, i)) >= limit * 0.4:
            if closed(i):
                return cut[: i + (1 if sep[0] in ".;?" else 0)].rstrip(" ,:—")
    return cut[: cut.rfind(" ")].rstrip(" ,:—") + "…"


def _compact_schema(schema: Any, name: str = "") -> Any:
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if "description" in out and out.get("enum") and "|" in str(out["description"]):
        out.pop("description")  # «a | b | c» — это уже есть в enum
    elif "description" in out:
        out["description"] = _SHORT_PARAMS.get(name) or _short(out["description"], 80)
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _compact_schema(v, k) for k, v in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _compact_schema(out["items"])
    return out


def compact_declaration(decl: dict[str, Any]) -> dict[str, Any]:
    """Описание инструмента для голоса: суть в ~170 знаках, параметры — коротко."""
    out = dict(decl)
    out["description"] = _short(decl.get("description"), 170)
    if isinstance(decl.get("parameters"), dict):
        out["parameters"] = _compact_schema(decl["parameters"])
    return out


def _control_tools(mode: str) -> list[dict[str, Any]]:
    end = ("Закончить разговор (панель JES закроется) — когда он попрощался или сказал, что больше ничего не нужно."
           if mode == "phone" else "Положить трубку — когда разговор окончен или человек попрощался.")
    tools = [{"name": "end_call", "description": end,
              "parameters": {"type": "OBJECT", "properties": {}}}]
    if mode == "wake":
        tools += [
            {"name": "confirm_awake", "description": "Человек ясно и связно подтвердил, что проснулся и встаёт.",
             "parameters": {"type": "OBJECT", "properties": {}}},
            {"name": "snooze", "description": "Отложить звонок на несколько минут (не больше 10).",
             "parameters": {"type": "OBJECT", "properties": {"minutes": {"type": "INTEGER", "description": "на сколько минут"}}, "required": ["minutes"]}},
        ]
    return tools


# В голосе — только частые инструменты: описания всех 60+ инструментов оплачиваются в КАЖДОЙ реплике Gemini Live
# (было ~13 тыс. токенов на реплику). Остальное делает «помощник из чата» — дешёвая текстовая модель со всеми инструментами.
VOICE_CORE = {"add_finance_entries", "get_finance_stats", "add_reminder", "add_task", "complete_tasks", "add_calorie_logs", "add_note",
              "weather", "web_search", "currency_rates", "calculate", "prayer_times", "get_wake", "remember_about_me",
              "ai_status", "set_ai_balance", "expect_photo"}
_DELEGATE = {"name": "bot_task",
             "description": "Помощник из чата со ВСЕМИ инструментами бота: любая работа с его данными, для которой у тебя нет своего инструмента — "
                            "исправить, удалить, найти, перенести записи (операции, задачи, заметки, еда, вес), массовые правки, цели, долги, бюджеты, "
                            "регулярные платежи, настройки, будильник, отчёты и анализ. Вернёт готовый ответ — перескажи его коротко.",
             "parameters": {"type": "OBJECT", "properties": {"request": {"type": "STRING", "description": "просьба целиком, своими словами, со всеми числами, датами и именами"}},
                            "required": ["request"]}}
_DELEGATE_SKIP = {"hand_off", "open_screen", "ask_user", "expect_photo", "call_me", "test_wake_call"}


async def delegate(profile: Profile, request: str) -> dict[str, Any]:
    """bot_task: просьбу из голоса выполняет агент чата (flash-lite, все инструменты); шаги отката — в тот же ход разговора."""
    from . import agent_tools
    from . import agent_tools_extra as extra
    from .handlers.agent import run_agent

    request = request.strip()
    if not request:
        return {"error": "пустая просьба"}
    try:
        snapshot, memory = await asyncio.gather(agent_tools.snapshot(profile), extra.memory_prompt(profile.telegram_id))
    except Exception:
        snapshot, memory = "", ""
    hint = "[голосовая просьба через JES: выполни инструментами и ответь одной-двумя короткими фразами, без списков, эмодзи и id]\n"
    decls = [d for d in agent_tools.declarations() if d["name"] not in _DELEGATE_SKIP]
    res = await run_agent(profile, hint + request, [], snapshot=snapshot, memory=memory, decls=decls)
    return {"ok": True, "reply": res.text, "done": res.ctx.calls}


_SEND_TO_CHAT = {"name": "send_to_chat",
                 "description": "Отправить ему в Telegram-чат текст (список, рецепт, план, ссылку, адрес, черновик сообщения) — когда просит «скинь/отправь в чат» или это удобнее прочитать, чем слушать.",
                 "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "готовый текст сообщения, можно с переносами строк"}}, "required": ["text"]}}


def tool_declarations(mode: str, *, full: bool = False) -> list[dict[str, Any]]:
    """full — телефон со всеми инструментами (экономный режим и phone_task), иначе в Live — облегчённый набор."""
    from . import agent_tools

    decls = _control_tools(mode)
    if mode in {"assistant", "phone"}:
        decls += [d for d in agent_tools.declarations() if d["name"] in VOICE_CORE]
        decls.append(_DELEGATE)
        decls.append(_SEND_TO_CHAT)
        if mode == "phone":
            from . import phone_live

            decls += phone_live.phone_declarations()
            decls = [d for d in decls if d["name"] not in PHONE_SKIP_TOOLS]
            if not full:
                decls = [d for d in decls if d["name"] in PHONE_LIVE_CORE] + [_PHONE_TASK]
                decls = [{**d, "behavior": "NON_BLOCKING"} if d["name"] in SILENT_TOOLS else d for d in decls]
        # голос: описания короткие — они оплачиваются в каждом ответе Gemini Live
        decls = [compact_declaration(d) for d in decls]
    else:
        decls += [d for d in agent_tools.declarations() if d["name"] in {"prayer_times", "get_wake", "weather"}]
    return decls


def _extras_rejected(error: str) -> bool:
    """Отказ похож на «не знаю такую настройку» (или безликое invalid argument) — стоит попробовать без экономных настроек."""
    low = str(error or "").lower()
    return any(k in low for k in ("thinking", "compression", "context_window", "contextwindow", "sliding", "invalid argument",
                                  "unknown name", "cannot find field", "behavior", "non_blocking", "language_codes", "languagecodes",
                                  "custom_vocabulary", "customvocabulary"))


def phone_task_declarations() -> list[dict[str, Any]]:
    """Что умеет phone_task: телефонные и голосовые инструменты, которых нет в облегчённом Live."""
    return [d for d in tool_declarations("phone", full=True) if d["name"] not in PHONE_LIVE_CORE]


# ------------------------------------------------------------------ сессия
class _Session:
    def __init__(self, profile: Profile, persona: Persona, *, mode: str, system: str) -> None:
        from . import agent_tools

        self.profile = profile
        self.persona = persona
        self.mode = mode
        self.system = system
        self.uid = profile.telegram_id
        self.result = LiveResult()
        self.out = bytearray()                 # голос модели, ждущий отправки в звонок
        self.stop = asyncio.Event()
        self.hangup_after_speech = False
        self.ctx = agent_tools.ToolContext(profile=profile, text="(звонок)")
        self._in_text: list[str] = []
        self._out_text: list[str] = []
        self.last_activity = 0.0               # когда последний раз кто-то говорил (для «толкача» при подъёме)
        self._tool_tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()       # в один websocket пишут микрофон, инструменты и «толкач» — по очереди
        self.pre_answer = False                # идут гудки: модель уже готовит приветствие, закрытие сессии — не конец разговора
        self.greeting = ""                     # что модель сказала, пока шли гудки (для переподключения)
        self.heard_user = False                # он уже что-то сказал (подъём: не толкать каждые 7 секунд)
        self.nonblocking = False               # модель приняла «тихие» инструменты (SILENT_TOOLS) — ставит setup_payload

    async def send(self, ws, payload: dict[str, Any]) -> bool:  # noqa: ANN001
        try:
            async with self._send_lock:
                await ws.send_str(json.dumps(payload, ensure_ascii=False, default=str))
            return True
        except Exception:
            return False

    def track(self, coro) -> asyncio.Task:  # noqa: ANN001
        task = asyncio.create_task(coro)
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)
        return task

    # --- websocket
    async def connect(self, session):  # noqa: ANN001 — aiohttp.ClientSession
        from .context import settings

        last_error = None
        # в настройках выбран Qwen (Alibaba, в 3–6 раз дешевле) — сначала он; не вышло — как обычно Gemini.
        # Подъём на фаджр — всегда Gemini: там арабские дуа, которые должны звучать точно.
        if self.persona.voice_model == "qwen" and self.mode in {"phone", "assistant"}:
            from . import qwen_live

            if qwen_live.available():
                try:
                    ws = await qwen_live.connect(self.setup_payload(qwen_live.MODEL, rich=False), voice=self.persona.qwen_voice)
                    self.result.model = qwen_live.MODEL
                    logger.info("live: модель %s, голос %s, язык %s", qwen_live.MODEL, self.persona.qwen_voice, self.persona.lang)
                    return ws
                except Exception as exc:
                    logger.warning("live: Qwen недоступен (%s) — Gemini", str(exc)[:200])
                    qwen_live.report_failure(str(exc))
        # сначала с жёстким языком речи (languageCode); модель не приняла — та же модель без него.
        # Не приняла экономные настройки (_extras_level) — та же попытка проще (запоминаем до перезапуска).
        attempts = [(m, r) for m in MODELS for r in (True, False)]
        i = 0
        while i < len(attempts):
            model, rich = attempts[i]
            try:
                ws = await session.ws_connect(WS_URL.format(ver="v1beta") + f"?key={settings.gemini_api_key}",
                                              heartbeat=20, max_msg_size=0)
            except Exception as exc:
                last_error = f"connect: {exc}"
                i += 1
                continue
            await ws.send_str(json.dumps(self.setup_payload(model, rich=rich)))
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=15)
            except asyncio.TimeoutError:
                await ws.close()
                last_error = f"{model}: setup timeout"
                i += 1
                continue
            data = _decode(msg)
            if data is not None and "setupComplete" in data:
                self.result.model = model
                logger.info("live: модель %s, голос %s, язык %s, расширенный режим %s, экономия %s", model, self.persona.voice,
                            self.persona.lang, rich, _extras_level.get(model, 2))
                return ws
            last_error = f"{model}: {getattr(msg, 'extra', None) or getattr(msg, 'data', '')!s}"[:300]
            logger.warning("live setup failed: %s", last_error)
            await ws.close()
            from . import billing

            if billing.is_billing_error(None, last_error):
                # предоплата кончилась — другие модели тоже откажут, не перебираем все 6 вариантов
                billing.exhausted(last_error)
                raise BillingExhausted(last_error)
            level = _extras_level.get(model, 2)
            if level > 0 and _extras_rejected(last_error):
                _extras_level[model] = level - 1
                logger.warning("live: %s не принял экономные настройки (уровень %s) — пробую проще", model, level)
                continue
            i += 1
        raise RuntimeError(last_error or "live setup failed")

    def setup_payload(self, model: str, *, rich: bool) -> dict[str, Any]:
        """rich — жёсткий язык речи (languageCode из настроек). Не включаем: enableAffectiveDialog
        (модель отвечает «invalid argument» на первую же реплику) и встроенный googleSearch
        (модель им не пользуется — поиск идёт через обычный инструмент web_search).

        Экономия (_extras_level, модель не приняла — без них): без «размышлений» (thinkingBudget 0: gemini-3.8-live думает
        перед каждым ответом ~50–400 токенов, они оплачиваются как текст на выходе; thinkingLevel он не принимает)
        и сжатие памяти разговора: Live в КАЖДОМ ответе заново оплачивает весь
        разговор, поэтому, когда он вырос, старое начало отбрасывается (инструкция и инструменты остаются всегда)."""
        speech: dict[str, Any] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.persona.voice}}}
        gen: dict[str, Any] = {"responseModalities": ["AUDIO"], "speechConfig": speech}
        decls = self.declarations()
        tools: list[dict[str, Any]] = [{"functionDeclarations": decls}] if decls else []
        if rich and not self.persona.mirror:  # отвечает на языке вопроса — язык речи не фиксируем
            speech["languageCode"] = LANG_CODES.get(self.persona.lang, "uz-UZ")
        setup: dict[str, Any] = {
            "model": f"models/{model}",
            "generationConfig": gen,
            "systemInstruction": {"parts": [{"text": self.system}]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }
        if tools:
            setup["tools"] = tools
        level = 0 if model.startswith("qwen") else _extras_level.get(model, 2)
        if level >= 2:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        if level >= 1:
            # ~3 знака на токен: инструкция + описания инструментов — постоянная часть; разговор сверху — до ~8 тыс. токенов
            base = (len(self.system) + len(json.dumps(decls, ensure_ascii=False))) // 3
            above, keep = (PHONE_COMPRESS_ABOVE, PHONE_COMPRESS_KEEP) if self.mode == "phone" else (COMPRESS_ABOVE, COMPRESS_KEEP)
            setup["contextWindowCompression"] = {"triggerTokens": base + above, "slidingWindow": {"targetTokens": base + keep}}
            # он говорит только по-русски, по-узбекски и по-английски: без подсказки тихую речь распознавание писало
            # испанским («hermana») и латиницей («Dasshif»), а имя JES — чем попало (проверено: 3.8-live принимает)
            vocab = list(VOCABULARY) + [w for w in getattr(self, "extra_vocab", []) if w not in VOCABULARY]
            setup["inputAudioTranscription"] = {"languageCodes": list(SPEECH_LANGS), "customVocabulary": vocab[:30]}
        self.nonblocking = level >= 1 and self.mode == "phone"
        if not self.nonblocking and tools:
            # модель не приняла «тихие» инструменты — объявляем их обычными
            tools[0]["functionDeclarations"] = [{k: v for k, v in d.items() if k != "behavior"} for d in decls]
        return {"setup": setup}

    def declarations(self) -> list[dict[str, Any]]:
        return tool_declarations(self.mode)

    # --- задачи
    async def uplink(self, ws, incoming: asyncio.Queue) -> None:  # noqa: ANN001
        """Звук собеседника → Gemini (пачками по 40 мс)."""
        batch = bytearray()
        need = caller.LIVE_RATE // 1000 * UPLINK_BATCH_MS * 2
        while not self.stop.is_set():
            try:
                chunk = await asyncio.wait_for(incoming.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            batch.extend(chunk)
            if len(batch) >= need:
                payload = {"realtimeInput": {"audio": {"data": base64.b64encode(bytes(batch)).decode(), "mimeType": f"audio/pcm;rate={caller.LIVE_RATE}"}}}
                batch.clear()
                if not await self.send(ws, payload):
                    self.stop.set()
                    return

    async def downlink(self, ws) -> None:  # noqa: ANN001
        """Ответы Gemini: голос, перебивания, расшифровки, вызовы инструментов.
        Инструменты выполняются фоном: пока ищется погода, модель слышит перебивания и может говорить."""
        from . import billing

        while not self.stop.is_set():
            msg = await ws.receive()
            data = _decode(msg)
            if data is None:
                if msg.type.name in {"CLOSE", "CLOSED", "CLOSING", "ERROR"}:
                    extra = str(getattr(msg, "extra", "") or "")
                    logger.info("live: соединение с моделью закрыто (%s)", extra)
                    if billing.is_billing_error(None, extra):
                        billing.exhausted(extra)
                    if not self.pre_answer:  # пока гудки — переподключимся, когда возьмут трубку
                        self.stop.set()
                    return
                continue
            if "usageMetadata" in data:
                billing.record(self.result.model or MODELS[0], data["usageMetadata"], kind="live")
            sc = data.get("serverContent") or {}
            if sc.get("interrupted"):
                self.out.clear()  # перебили — замолкаем сразу
            for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                blob = part.get("inlineData") or {}
                if blob.get("data"):
                    self.out.extend(base64.b64decode(blob["data"]))
            if (t := (sc.get("inputTranscription") or {}).get("text")):
                self.last_activity = asyncio.get_running_loop().time()
                self._in_text.append(t)
                if t.strip():
                    self.heard_user = True
            if (t := (sc.get("outputTranscription") or {}).get("text")):
                self._out_text.append(t)
                if self.pre_answer:
                    self.greeting += t
            if sc.get("turnComplete"):
                self._flush_transcript()
            if "toolCall" in data:
                self.track(self._run_tools(ws, data["toolCall"].get("functionCalls") or []))
            if "goAway" in data:
                logger.info("live: сервер просит завершить сессию")
                self.hangup_after_speech = True

    async def playout(self) -> None:
        """Голос модели → звонок, ровно в реальном времени (кадры по 10 мс)."""
        loop = asyncio.get_running_loop()
        frame = caller.FRAME_BYTES
        silence = bytes(frame)
        tick = caller.FRAME_MS / 1000
        next_at = loop.time()
        drained_at = None
        while not self.stop.is_set():
            if len(self.out) >= frame:
                self.last_activity = loop.time()
                chunk = bytes(self.out[:frame])
                del self.out[:frame]
            else:
                chunk = silence
                if self.hangup_after_speech:
                    drained_at = drained_at or loop.time()
                    if loop.time() - drained_at > 0.6:  # договорил прощание — кладём трубку
                        self.stop.set()
                        return
            if not await caller.send_audio(self.uid, chunk):
                self.stop.set()
                return
            next_at += tick
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:  # отстали (сеть/CPU) — не пытаемся догнать рывком
                next_at = loop.time()

    async def nudger(self, ws) -> None:  # noqa: ANN001
        """Подъём: если он замолчал (мог снова заснуть), модель сама не заговорит — толкаем её."""
        loop = asyncio.get_running_loop()
        self.last_activity = loop.time()
        while not self.stop.is_set():
            await asyncio.sleep(1.0)
            if self.out or self.hangup_after_speech or self.result.confirmed:
                continue
            # уже разговаривали — не дёргаем через 7 с (он мог просто думать над вопросом): ждём дольше
            limit = SILENCE_NUDGE_SECONDS * (3 if self.heard_user else 1)
            if loop.time() - self.last_activity < limit:
                continue
            self.last_activity = loop.time()
            logger.info("live: тишина %.0f с — зову снова", limit)
            nudge = ("[Он молчит. Позови его по имени бодро ОДНОЙ короткой фразой и спроси, слышит ли он. Без нотаций и мотивации.]"
                     if not self.heard_user else "[Он давно молчит. Коротко спроси, всё ли в порядке и слышит ли он тебя.]")
            if not await self.send(ws, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": nudge}]}], "turnComplete": True}}):
                return

    # --- инструменты
    async def _run_one(self, call: dict[str, Any]) -> dict[str, Any]:
        from . import agent_tools

        name, args, cid = call.get("name"), call.get("args") or {}, call.get("id")
        logger.info("live tool %s %s", name, json.dumps(args, ensure_ascii=False)[:200])
        try:
            if name == "end_call":
                self.hangup_after_speech = True
                result: dict[str, Any] = {"ok": True}
            elif name == "confirm_awake":
                self.result.confirmed = True
                result = {"ok": True}
            elif name == "send_to_chat":
                result = await _send_to_chat(self.uid, str(args.get("text") or ""))
                if result.get("ok"):
                    self.result.actions.append("send_to_chat")
            elif name == "snooze":
                self.result.snooze_minutes = max(1, min(10, int(args.get("minutes") or 5)))
                self.hangup_after_speech = True
                result = {"ok": True, "minutes": self.result.snooze_minutes}
            elif name == "bot_task":
                result = await delegate(self.profile, str(args.get("request") or ""))
                self.result.actions.extend(result.get("done") or [])
            else:
                result = await agent_tools.run(str(name), args, self.ctx)
                if not result.get("error"):
                    self.result.actions.append(str(name))
        except Exception as exc:
            logger.exception("live tool %s failed", name)
            result = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        return {"id": cid, "name": name, "response": _jsonable(result)}

    async def _run_tools(self, ws, calls: list[dict[str, Any]]) -> None:  # noqa: ANN001
        """Несколько инструментов в одном ходе — параллельно (погода + курс не ждут друг друга)."""
        responses = await asyncio.gather(*(self._run_one(c) for c in calls))
        if not await self.send(ws, {"toolResponse": {"functionResponses": list(responses)}}):
            self.stop.set()

    def _flush_transcript(self) -> None:
        if self._in_text:
            self.result.transcript.append("он: " + "".join(self._in_text).strip())
            self._in_text.clear()
        if self._out_text:
            self.result.transcript.append("я: " + "".join(self._out_text).strip())
            self._out_text.clear()


async def _send_to_chat(uid: int, text: str) -> dict[str, Any]:
    """Текст из звонка/с телефона в чат (он сам попросил прислать). Висит, пока он не нажмёт любую кнопку
    в боте, — дальше чат снова чистый."""
    text = text.strip()
    if not text:
        return {"error": "empty text"}
    try:
        from . import screen
        from .context import bot_instance

        sent = await bot_instance().send_message(uid, text[:4000], parse_mode=None)
        screen.track_sent(uid, sent.message_id)
        return {"ok": True}
    except Exception as exc:
        logger.warning("send_to_chat failed", exc_info=True)
        return {"error": str(exc)[:120]}


def _decode(msg) -> dict[str, Any] | None:  # noqa: ANN001
    try:
        if msg.type.name == "TEXT":
            return json.loads(msg.data)
        if msg.type.name == "BINARY":
            return json.loads(msg.data.decode("utf-8"))
    except Exception:
        logger.debug("live: не разобрал сообщение", exc_info=True)
    return None


def _jsonable(value: Any) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"result": str(value)[:2000]}


# ------------------------------------------------------------------ точка входа
KICK = "[Звонок соединён. Начинай.]"
KICK_INCOMING = "[Он сам позвонил тебе в Telegram. Коротко поздоровайся (по имени, по времени суток) и спроси, чем помочь.]"
ANSWER_PAUSE = 0.5   # трубку взяли — полсекунды, чтобы поднести телефон к уху, и сразу голос


async def _pregreet(sess: "_Session", http, kick: str):  # noqa: ANN001
    """Пока идут гудки: подключаемся к Gemini и просим поздороваться. Голос копится в sess.out
    и зазвучит в ту же секунду, как возьмут трубку (раньше — ещё ~1.5 с тишины после ответа)."""
    ws = await sess.connect(http)
    sess.pre_answer = True
    await sess.send(ws, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": kick}]}], "turnComplete": True}})
    down = asyncio.create_task(sess.downlink(ws), name="live-down")
    return ws, down


async def _not_prepared():  # noqa: ANN202
    """Приветствие заранее не готовили — _live_ready подключится, когда возьмут трубку."""
    raise LookupError("приветствие заранее не готовили")


async def _live_ready(sess: "_Session", http, early: asyncio.Task, kick: str):  # noqa: ANN001
    """Живая сессия к моменту «трубку взяли». Приветствие уже готово — просто продолжаем.
    Gemini закрыл сессию, пока шли гудки (~30 с простоя), или не подключился — подключаемся заново:
    если приветствие уже накоплено, модели только сообщаем, что поздоровалась, иначе просим начать."""
    ws = down = None
    try:
        ws, down = await early
    except Exception as exc:
        logger.info("live: заранее не подключились (%s) — подключаюсь сейчас", exc)
    sess.pre_answer = False
    if ws is not None and not ws.closed and down is not None and not down.done():
        return ws, down
    if ws is not None:
        logger.info("live: сессия закрылась, пока шли гудки — переподключаюсь")
    if down is not None and not down.done():
        down.cancel()
    for attempt in (1, 2):
        ws = await sess.connect(http)
        if sess.out and sess.greeting.strip():
            note = f"[Звонок соединён. Ты уже поздоровалась: «{sess.greeting.strip()[:300]}». Дальше слушай его и отвечай.]"
            ok = await sess.send(ws, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": note}]}], "turnComplete": False}})
        else:
            sess.out.clear()
            ok = await sess.send(ws, {"clientContent": {"turns": [{"role": "user", "parts": [{"text": kick}]}], "turnComplete": True}})
        if ok:
            return ws, asyncio.create_task(sess.downlink(ws), name="live-down")
        if attempt == 2:
            raise RuntimeError("live: сессия обрывается на старте")
        logger.info("live: сессия оборвалась на старте — переподключаюсь")
        try:
            await ws.close()
        except Exception:
            pass
    raise RuntimeError("unreachable")


async def _prompt_parts(profile: Profile, mode: str) -> tuple[Persona, str, str]:
    """Голос/характер и память — параллельно. Срез данных бота в промпт звонка больше не кладём: Live оплачивает
    инструкцию заново в КАЖДОМ ответе (срез — десятки тысяч знаков), а данные есть в инструментах и bot_task."""
    from . import services

    uid = profile.telegram_id
    if mode != "assistant":
        return await services.persona(uid), "", ""
    from . import agent_tools_extra as extra

    persona, memory = await asyncio.gather(services.persona(uid), extra.memory_prompt(uid))
    return persona, "", memory


async def _converse(sess: "_Session", http, call: dict[str, Any], early: asyncio.Task, kick: str) -> None:
    """Трубку взяли: голос ⇄ Gemini до конца разговора, потом кладём трубку."""
    from . import undo

    uid = sess.uid
    sess.result.answered = True
    undo.begin_turn(uid)
    ws = None
    tasks: list[asyncio.Task] = []
    watcher = stopper = None
    try:
        ws, down = await _live_ready(sess, http, early, kick)
        await asyncio.sleep(ANSWER_PAUSE)
        tasks = [
            down,
            asyncio.create_task(sess.uplink(ws, call["incoming"]), name="live-up"),
            asyncio.create_task(sess.playout(), name="live-play"),
        ]
        if sess.mode == "wake":
            tasks.append(asyncio.create_task(sess.nudger(ws), name="live-nudge"))
        ended: asyncio.Event = call["ended"]
        watcher = asyncio.create_task(ended.wait(), name="live-ended")
        stopper = asyncio.create_task(sess.stop.wait(), name="live-stop")
        await asyncio.wait({watcher, stopper}, timeout=MAX_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        if ended.is_set():
            logger.info("call %s: собеседник положил трубку", uid)
    except Exception as exc:
        sess.result.error = f"live: {type(exc).__name__}: {exc}"[:300]
        logger.exception("call %s: разговор сорвался", uid)
    finally:
        # что бы ни случилось после «трубку взяли» — кладём трубку, иначе звонок висит,
        # а следующая попытка «соединяется» с этим мёртвым звонком
        sess.stop.set()
        extra = [t for t in (watcher, stopper) if t is not None]
        for task in (*tasks, *extra, *sess._tool_tasks):
            task.cancel()
        await asyncio.gather(*tasks, *extra, *sess._tool_tasks, return_exceptions=True)
        sess._flush_transcript()
        await caller.hang_up(uid)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        sess.result.mutated = bool(undo.end_turn(uid))


async def run(profile: Profile, *, mode: str = "assistant", topic: str = "", wake: dict[str, Any] | None = None,
              ring_seconds: int = 45, lang: str | None = None, pregreet: bool = True) -> LiveResult:
    """Позвонить и провести разговор целиком. Возвращает итог (что сказано, что сделано).

    Всё, что можно, — параллельно с гудками: набор номера стартует сразу, промпт собирается и
    Gemini готовит приветствие, пока телефон звонит. Взяли трубку — голос через полсекунды.
    pregreet=False — приветствие не готовим заранее (повторные звонки будильника: 26.09 он не взял 20 звонков подряд,
    и каждое заготовленное приветствие стоило ~$0.003–0.009); Gemini подключается, когда взяли трубку (~1 с).
    После дневного лимита — не звоним (кроме подъёма на фаджр)."""
    from . import billing

    uid = profile.telegram_id
    if not billing.live_allowed(mode):
        logger.info("call %s: дневной лимит живого голоса — не звоню (%s)", uid, mode)
        return LiveResult(error="daily_limit")
    meter = billing.start_session("wake" if mode == "wake" else "call")
    try:
        return await _run(profile, mode=mode, topic=topic, wake=wake, ring_seconds=ring_seconds, lang=lang, pregreet=pregreet)
    finally:
        billing.end_session(meter)


async def _run(profile: Profile, *, mode: str, topic: str, wake: dict[str, Any] | None, ring_seconds: int, lang: str | None,
               pregreet: bool = True) -> LiveResult:
    import aiohttp

    from . import billing

    uid = profile.telegram_id
    dial = asyncio.create_task(caller.open_stream_call(uid, username=profile.username, ring_seconds=ring_seconds), name="live-dial")
    persona, snapshot, memory = await _prompt_parts(profile, mode)
    if lang:
        from dataclasses import replace

        persona = replace(persona, lang=lang)
    system = system_instruction(profile, persona, mode=mode, snapshot=snapshot, memory=memory, wake=wake, topic=topic)
    if mode == "assistant":
        system += billing.voice_note()
    sess = _Session(profile, persona, mode=mode, system=system)

    async with aiohttp.ClientSession() as http:
        early = asyncio.create_task(_pregreet(sess, http, KICK) if pregreet else _not_prepared(), name="live-connect")
        try:
            call = await dial
        except BaseException:
            await _close_pre(early)
            raise
        sess.result.dialed = True
        if not call.get("answered"):
            sess.result.error = call.get("error")
            await _close_pre(early)
            return sess.result
        await _converse(sess, http, call, early, KICK)
    logger.info("call %s: итог — модель %s, реплик %s, действия %s", uid, sess.result.model, len(sess.result.transcript), sess.result.actions)
    return sess.result


async def answer(profile: Profile) -> LiveResult:
    """Он сам позвонил JES в Telegram: сначала Gemini (приветствие готовится ~1 с), потом берём
    трубку — он слышит голос сразу, а не тишину после ответа."""
    from . import billing

    meter = billing.start_session("incoming")
    try:
        return await _answer(profile)
    finally:
        billing.end_session(meter)


async def _answer(profile: Profile) -> LiveResult:
    import aiohttp

    from . import billing

    persona, snapshot, memory = await _prompt_parts(profile, "assistant")
    system = system_instruction(profile, persona, mode="assistant", snapshot=snapshot, memory=memory) + billing.voice_note()
    sess = _Session(profile, persona, mode="assistant", system=system)
    async with aiohttp.ClientSession() as http:
        early = asyncio.create_task(_pregreet(sess, http, KICK_INCOMING), name="live-connect")
        try:
            await asyncio.wait_for(asyncio.shield(early), timeout=4)
        except Exception:
            pass  # не успели — возьмём трубку всё равно, а сессию догоним в _live_ready
        call = await caller.accept_stream_call(profile.telegram_id)
        if not call.get("answered"):
            sess.result.error = call.get("error")
            await _close_pre(early)
            return sess.result
        await _converse(sess, http, call, early, KICK_INCOMING)
    logger.info("call %s: входящий — итог: модель %s, реплик %s, действия %s", profile.telegram_id, sess.result.model,
                len(sess.result.transcript), sess.result.actions)
    return sess.result


async def _close_pre(early: asyncio.Task) -> None:
    """Звонок не состоялся — закрыть заранее открытую сессию Gemini (и её приём)."""
    if not early.done():
        early.cancel()
    try:
        ws, down = await early
    except BaseException:
        return
    down.cancel()
    try:
        await ws.close()
    except Exception:
        pass


__all__ = ["run", "answer", "LiveResult", "system_instruction", "tool_declarations", "MODELS"]
