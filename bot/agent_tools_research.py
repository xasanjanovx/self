"""Подробная справка из интернета → в чат бота (05.10.2026): «найди всё про фильм/книгу/известного человека и скинь мне».

Инструмент `research` — один на всё: фильм или сериал, книга, ИЗВЕСТНЫЙ человек (для рассказа друзьям, розыгрышей, «кто это»),
место, компания, любая тема. Ищет Google (ai.research, умная модель), собирает карточку в нужном формате и отправляет в чат
отдельным сообщением (с источниками); голосом остаётся одна фраза с сутью. Формат он может задать сам (`format`).

Люди. Справка только о ПУБЛИЧНЫХ личностях (артисты, спортсмены, политики, писатели, предприниматели и блогеры со своей
известностью): биография, карьера, награды, новости, необычные факты. Никаких досье на частных лиц — адрес, телефон, почта,
документы, личные аккаунты, дети, здоровье — не ищем и не выдаём; если человек не публичный, так и скажем.

Регистрируется в общем реестре `agent_tools.TOOLS` (импорт в конце bot/agent_tools.py) — чат, голос и звонки.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

from . import deeds, session_memory
from .agent_tools import P, ToolContext, _str, tool
from .context import ai

logger = logging.getLogger(__name__)

CHUNK = 3800            # лимит Telegram — 4096 знаков; запас на разметку
MAX_QUERY = 200

KINDS = {"person": "человек", "movie": "фильм", "book": "книга", "place": "место", "company": "компания", "topic": "тема"}

# что нельзя искать о человеке, даже если он публичный: это не справка, а слежка
_PRIVATE = re.compile(
    r"(?:домашн\w*\s+адрес|где\s+(?:он|она|\w+)\s+(?:живёт|живет|прописан)|адрес\s+(?:проживания|прописки)|номер\s+телефона|телефон\w*\s+номер|"
    r"личн\w+\s+(?:номер|телефон|почт\w+|аккаунт)|паспорт\w*|прописк\w+|геолокаци\w+|где\s+(?:он|она)\s+(?:сейчас|находится)|"
    r"yashash\s+manzil|telefon\s+raqam|uy\s+manzil|home\s+address|phone\s+number|where\s+does\s+\w+\s+live)",
    re.IGNORECASE,
)

COMMON = (
    "Ты — редактор-исследователь. Найди в интернете и составь карточку для чтения в Telegram. Правила: только то, что подтверждено найденными "
    "источниками; ничего не выдумывай — нет данных, пропусти пункт; цифры и даты точные, с годом. Формат: обычный текст, заголовки разделов "
    "с эмодзи и **жирным**, пункты «• », без таблиц, без #, без ссылок в тексте. ПЕРВАЯ СТРОКА — «КРАТКО: …» (до 20 слов, главное; её "
    "прочитают вслух), дальше пустая строка и сама карточка. Язык — {language}. Сейчас {year} год: за последние два года проверяй свежее поиском."
)

PROMPTS = {
    "person": (
        "КАРТОЧКА О ПУБЛИЧНОМ ЧЕЛОВЕКЕ (артист, спортсмен, политик, писатель, учёный, предприниматель, блогер со своей аудиторией). "
        "Разделы по порядку: 👤 Кто это (полное имя и написание на языке оригинала, дата и место рождения, возраст, гражданство, чем известен — 2 строки); "
        "🎓 Образование и начало пути; 💼 Карьера (этапы с годами); 🏆 Достижения и награды; 🎬 Известные работы и проекты; "
        "💬 Публичная жизнь (брак и взрослые дети — только если он сам или СМИ публично это сообщали; официальные верифицированные соцсети с числом подписчиков; рост, интересы); "
        "💰 Состояние (только по Forbes и подобным публичным рейтингам); 📰 Новое за последний год; "
        "🤯 Необычные факты (5–7 — то, чем можно удивить друзей: малоизвестное, смешное, неожиданное, но проверенное); "
        "⚠️ Споры и скандалы — только подтверждённые и нейтрально. Известных людей с таким именем несколько — возьми самого известного, "
        "в разделе «Кто это» назови, кого выбрал, и перечисли остальных одной строкой.\n"
        "ЖЁСТКИЕ ПРАВИЛА. Это справка ТОЛЬКО о публичной личности. Если этого человека нет в надёжных открытых источниках как публичной персоны "
        "(обычный частный человек, знакомый, сосед, одноклассник) — выведи единственную строку «НЕ ПУБЛИЧНЫЙ: публичной информации об этом человеке нет» "
        "и больше НИЧЕГО; не пытайся его искать и собирать данные из соцсетей и баз. Никогда не ищи и не пиши: домашний адрес, телефон, личную почту, "
        "номера документов, текущее местоположение, непубличные аккаунты, данные несовершеннолетних детей, диагнозы без публичного признания."
    ),
    "movie": (
        "КАРТОЧКА ФИЛЬМА ИЛИ СЕРИАЛА. Разделы: 🎬 Название (русское и оригинальное), год, страна, жанр, длительность или число сезонов и серий; "
        "🎞 Режиссёр и сценарист; 🎭 В ролях (главные 5–6, «актёр — роль»); ⭐ Рейтинги (IMDb, Кинопоиск — если нашла, с числом голосов не нужно); "
        "📖 О чём (3–4 предложения БЕЗ спойлеров концовки); 🏆 Награды; 💵 Бюджет и сборы; 🤯 Интересные факты о съёмках (3–5); "
        "👍 Кому понравится и 👎 кому не зайдёт (по одной строке); 🔎 Похожее (3 названия). Если есть продолжения — что смотреть дальше и в каком порядке."
    ),
    "book": (
        "КАРТОЧКА КНИГИ. Разделы: 📚 Название (русское и оригинальное), автор, год, жанр, объём в страницах; 📖 О чём (3–4 предложения БЕЗ спойлеров концовки); "
        "👤 Об авторе (2 строки); ⭐ Оценки (Goodreads, Livelib — если нашла); 🏆 Награды и тиражи; 🧠 Главные идеи (3–5); 🤯 Интересные факты (3); "
        "💬 Яркая мысль из книги (без дословных цитат длиннее одной строки); 👍 Кому читать и 👎 кому пропустить; 🔎 Что почитать после (3 названия); "
        "🎬 Экранизации, если есть. Серия — порядок чтения."
    ),
    "place": (
        "КАРТОЧКА МЕСТА (страна, город, достопримечательность). Разделы: 📍 Где это и чем известно; 📜 История (кратко); 🧭 Что посмотреть (5 пунктов); "
        "🍽 Что попробовать; 🌤 Когда ехать; 🚗 Как добраться (из Андижана, Узбекистан, если уместно); 💵 Ориентир по ценам; 🤯 Факты (3–5)."
    ),
    "company": (
        "КАРТОЧКА КОМПАНИИ ИЛИ ПРОДУКТА. Разделы: 🏢 Что это (год основания, штаб-квартира, основатели, чем занимается); 💼 Продукты и услуги; "
        "📈 Цифры (выручка, сотрудники, оценка — с годом и источником); 📰 Новое за последний год; ⚖️ Плюсы и минусы для клиента; 🤯 Факты (3–5)."
    ),
    "topic": (
        "СПРАВКА ПО ТЕМЕ. Разделы: 💡 Суть (3–4 предложения простыми словами); 📜 Как возникло или как устроено; 🔑 Главное (5 пунктов); "
        "📰 Что нового за последний год; 🤯 Интересные факты (3–5); ❓ Частые вопросы (2–3 с короткими ответами)."
    ),
}


def _clean_text(text: str) -> tuple[str, str]:
    """(КРАТКО одной строкой, остальной текст карточки без служебной первой строки)."""
    brief = ""
    body = text.strip()
    m = re.match(r"\s*(?:\*\*)?КРАТКО:?(?:\*\*)?:?\s*(.+?)\s*(?:\n|$)", body, re.IGNORECASE)
    if m:
        brief = m.group(1).strip(" *")
        body = body[m.end():].strip()
    return brief, body


def to_html(text: str) -> str:
    """Разметка модели (**жирный**, «• ») → безопасный HTML Telegram: всё экранируется, остаётся только <b>."""
    out = html.escape(text, quote=False)
    out = re.sub(r"(?m)^\s*#{1,6}\s*", "", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^\s*[-*]\s+", "• ", out)
    return out.replace("*", "").strip()


def chunks(text: str, limit: int = CHUNK) -> list[str]:
    """Режем по пустым строкам (потом по строкам) — <b> не бывает длиннее строки, теги не рвутся."""
    parts: list[str] = []
    cur = ""
    for block in re.split(r"\n{2,}", text):
        for piece in ([block] if len(block) <= limit else block.split("\n")):
            if cur and len(cur) + len(piece) + 2 > limit:
                parts.append(cur)
                cur = ""
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return [p[:limit + 200] for p in parts if p.strip()]


def sources_line(sources: list[tuple[str, str]]) -> str:
    if not sources:
        return ""
    links = " · ".join(f'<a href="{html.escape(url, quote=True)}">{html.escape(title, quote=False)}</a>' for title, url in sources)
    return f"🔗 {links}"


async def _deliver(uid: int, card_html: str, extra: str) -> bool:
    from aiogram.types import LinkPreviewOptions

    from .context import bot_instance

    try:
        bot = bot_instance()
    except Exception:
        return False
    parts = chunks(card_html)
    if extra:
        parts.append(extra)
    for part in parts:
        try:
            await bot.send_message(uid, part, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception:
            try:  # разметка не прошла — отправим простым текстом, лучше без жирного, чем без ответа
                await bot.send_message(uid, re.sub(r"<[^>]+>", "", html.unescape(part)), parse_mode=None,
                                       link_preview_options=LinkPreviewOptions(is_disabled=True))
            except Exception:
                logger.warning("research: в чат не ушло", exc_info=True)
                return False
    return True


@tool(
    "research",
    "ПОДРОБНАЯ СПРАВКА ИЗ ИНТЕРНЕТА → в Telegram-чат отдельной карточкой: фильм или сериал, книга, ИЗВЕСТНЫЙ человек (биография, карьера, награды, "
    "новости, необычные факты — например чтобы рассказать друзьям), место, компания, любая тема. «Найди всё про Тома Хэнкса», «расскажи про фильм "
    "Интерстеллар и скинь», «что за книга Атомные привычки», «подробно про Дубай». Голосом скажи одной фразой суть и что карточка в Telegram. "
    "Только о ПУБЛИЧНЫХ людях: частных лиц не ищет. Короткий факт — web_search, а не это.",
    {"kind": P("STRING", "что ищем", enum=list(KINDS)),
     "query": P("STRING", "имя или название, КОНКРЕТНО: «Том Хэнкс», «Интерстеллар 2014», «Атомные привычки Джеймс Клир»"),
     "format": P("STRING", "если он сам сказал, как оформить («коротко», «по пунктам: год, режиссёр, сюжет», «только факты для друзей») — его слова; иначе не передавай"),
     "language": P("STRING", "язык карточки, если он попросил не русский («на узбекском», «in English»); по умолчанию русский")},
    ("kind", "query"),
)
async def _research(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    kind = _str(a.get("kind")) or "topic"
    kind = kind if kind in KINDS else "topic"
    query = (_str(a.get("query")) or "")[:MAX_QUERY]
    if not query:
        return {"error": "query required"}
    custom = (_str(a.get("format")) or "")[:300]
    if kind == "person" and _PRIVATE.search(f"{query} {custom}"):
        return {"error": "refused",
                "note": "Это не справка о публичной личности, а поиск личных данных (адрес, телефон, документы, местоположение) — такого не ищу. "
                        "Скажи ему это одной фразой, без нравоучений, и предложи рассказать о человеке публичные факты: биографию, карьеру, интересные факты."}
    language = _str(a.get("language")) or "русский"
    if deeds.source.get() in {"телефон", "звонок"}:
        # голосом поиск и карточка занимают 10–20 с: не держим разговор — собираем в фоне, карточка придёт в Telegram сама
        _spawn(_background_card(ctx, kind, query, custom, language))
        return {"ok": True, "started": True,
                "note": f"Карточка готовится и придёт в Telegram секунд через 20 — не жди её. Скажи ПО-РУССКИ одной короткой фразой, что собираешь всё про "
                        f"«{query}» и пришлёшь в Telegram. Слова про Telegram и чат — всегда по-русски, даже если вы говорите по-узбекски."}
    return await _build(ctx, kind, query, custom, language)


_background: set[asyncio.Task[Any]] = set()


def _spawn(coro) -> None:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _background_card(ctx: ToolContext, kind: str, query: str, custom: str, language: str) -> None:
    """Голосовая просьба: карточка собирается после ответа; не вышло — коротко пишем в чат, почему."""
    from .context import bot_instance

    try:
        res = await _build(ctx, kind, query, custom, language)
    except Exception:
        logger.warning("research (фон) упал", exc_info=True)
        res = {"error": "сбой поиска"}
    why = ""
    if res.get("public") is False and not res.get("ok"):
        why = f"🙅 «{html.escape(query, quote=False)}»: публичной информации нет — это не публичная личность, а досье на частных людей я не собираю."
    elif res.get("error"):
        why = f"⚠️ Не получилось собрать справку про «{html.escape(query, quote=False)}»: {html.escape(str(res['error'])[:100], quote=False)}. Попробуйте переформулировать."
    if why:
        try:
            await bot_instance().send_message(ctx.uid, why, parse_mode="HTML")
        except Exception:
            logger.warning("research: причина не ушла в чат", exc_info=True)


async def _build(ctx: ToolContext, kind: str, query: str, custom: str, language: str) -> dict[str, Any]:
    context = session_memory.block(ctx.uid, n=2, head=False)
    system = COMMON.format(language=language, year=ctx.profile.now.year) + "\n" + PROMPTS[kind]
    if custom:
        system += f"\nФОРМАТ ЗАДАЛ ОН САМ — следуй ему вместо разделов выше (первая строка «КРАТКО:» остаётся): «{custom}»."
    ask = f"{KINDS[kind].capitalize()}: {query}" + (f"\n\nКонтекст разговора (если запрос неполный — предмет оттуда):\n{context}" if context else "")
    try:
        text, sources = await ai.research(system, ask)
    except Exception as exc:
        logger.warning("research failed", exc_info=True)
        return {"error": f"поиск не удался: {str(exc)[:120]}"}
    brief, body = _clean_text(text)
    if not body and not brief:
        return {"error": "ничего не нашла", "note": "скажи, что по этому запросу ничего толком не нашла, и попроси уточнить название"}
    if kind == "person" and re.search(r"НЕ ПУБЛИЧНЫЙ", text, re.IGNORECASE):
        session_memory.set_topic(ctx.uid, f"человек: {query} (не публичный)")
        return {"ok": False, "public": False,
                "note": "Это не публичная личность — открытых данных нет, а досье на частных людей не собираю. Скажи это одной фразой "
                        "и предложи: если он расскажет, что знает сам, помогу придумать шутку или розыгрыш. Известного человека — по имени и фамилии."}
    sent = await _deliver(ctx.uid, to_html(body or brief), sources_line(sources))
    session_memory.set_topic(ctx.uid, f"{KINDS[kind]}: {query}")
    if not sent:
        return {"ok": True, "sent_to_chat": False, "brief": brief,
                "note": "в Telegram отправить не вышло — перескажи суть голосом коротко и скажи, что отправить в чат не получилось"}
    return {"ok": True, "sent_to_chat": True, "brief": brief, "chars": len(body),
            "note": "Подробная карточка уже в Telegram — не пересказывай её. Скажи ПО-РУССКИ одной-двумя фразами: суть (brief) и что всё подробно "
                    "отправила ему в Telegram. Слова про Telegram и чат — всегда по-русски, даже если вы говорите по-узбекски."}
