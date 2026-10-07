"""Охрана ленты канала (07.10): платный пост наверху ≥ 3 часов и удаление рекламы на запрещённые темы.

Его слова: «мне дали заказ на размещение, я сам разместил — эта вакансия должна быть на топе минимум 3 часа; после этого найти вакансию и
разместить» и «я включил Яндекс-рекламу, фильтруй её: кредиты, ставки, страхование — сразу удалять с канала». Реклама у него — посты с
хештегом #reklama.

Как работает: аккаунт JES (Telethon) каждые пару минут читает публичный канал, бот сверяет посты со своими (id записаны при публикации):
  • свой пост — пропускаем;
  • пост в нашем шаблоне (подвал «Tez va oson ish toping», дисклеймер) — это он разместил сам: включаем защиту ленты на protect_hours (≥ 3);
  • пост с #reklama не в нашем шаблоне — реклама: тема из включённых (кредиты/займы, банки, ставки/крипта, страхование) → удаляем и присылаем
    ему текст удалённого; другая тема — не трогаем;
  • любой другой чужой пост — тоже ручной: защита ленты.
Реклама никогда не «держит» ленту. Свои вакансии в шаблоне канала (даже про банк: «Kredit menejer kerak») не удаляются — шаблон их защищает.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from . import vacancy_feed as feed

logger = logging.getLogger(__name__)

POLL_LIMIT = 25
SEED_LIMIT = 60                 # сколько последних постов канала один раз прочитать ради телефонов (антидубли)
FIRST_RUN_LOOKBACK_H = 6       # при первом запуске смотрим только последние часы — историю не трогаем, ничего не удаляем

_REKLAMA = re.compile(r"#\s*(?:reklama|реклама)|\berid\b\s*[:=]?\s*\w", re.IGNORECASE)
_OUR_TEMPLATE = ("tez va oson ish toping", "ma'muriyati javobgar emas", "ish beruvchi pul so'rasa", "ogoh bo'ling")

_TOPICS: dict[str, re.Pattern[str]] = {
    "credit": re.compile(
        r"kredit|qarz\b|qarz\s+ber|nasiya(?!siz)|zayom|zaym|mikroqarz|mikrokredit|ssuda|foizsiz|ipoteka|"
        r"кредит|займ|заём|заем\b|микрозайм|рассрочк|ипотек|ссуд|в долг|без процентов|"
        r"\bloan\b|installment"),
    "bank": re.compile(r"\bbank\b|banki|bankda|mikromoliya|omonat|depozit|\bбанк|микрофинанс|\bмфо\b|депозит|вклад|deposit"),
    "bets": re.compile(
        r"stavka|tikish|bukmeker|kazino|casino|lotoreya|1xbet|melbet|mostbet|linebet|1win|pin-?up|slot\b|forex|trading|kripto|crypto|"
        r"bitcoin|usdt|binance|airdrop|"
        r"ставк|букмекер|казино|лотере|форекс|трейдинг|крипт|биткоин|бинанс"),
    "insure": re.compile(
        r"sug'urta|sugurta|investitsiya|dividend|passiv daromad|"
        r"страхов|инвестиц|дивиденд|пассивный доход|\bпиф\b|insurance|invest\b"),
}


def _norm(text: str) -> str:
    return feed._norm(text)


def is_reklama(text: str) -> bool:
    return bool(_REKLAMA.search(text or ""))


def is_our_template(text: str) -> bool:
    low = _norm(text)
    return any(marker in low for marker in _OUR_TEMPLATE)


def ad_topics(text: str, enabled: dict[str, bool] | None = None) -> list[str]:
    """Темы рекламного текста по ключевым словам; enabled — какие темы он включил в настройках (None — все)."""
    low = _norm(text)
    return [name for name, rx in _TOPICS.items() if (enabled is None or enabled.get(name)) and rx.search(low)]


def classify(text: str, mid: int) -> str:
    """own | manual (его пост — защита ленты) | ad (реклама #reklama)."""
    if feed.is_own(mid):
        return "own"
    if is_our_template(text or ""):
        return "manual"
    return "ad" if is_reklama(text or "") else "manual"


async def llm_topics(text: str, ai: Any, enabled: dict[str, bool]) -> list[str]:
    """Второе мнение для рекламы, где слов-признаков нет (перефраз): про что она. Только через Vertex, строго и коротко."""
    from .ai import extract_json, vertex_only

    prompt = (
        "Это рекламный пост в Telegram-канале Узбекистана. Определи, про что реклама. Ответь ТОЛЬКО JSON: "
        '{"credit":false,"bank":false,"bets":false,"insure":false}. credit — кредиты, займы, микрозаймы, рассрочка, ипотека, деньги в долг; '
        "bank — банк, МФО, вклады, банковские карты; bets — ставки, букмекеры, казино, лотереи, криптовалюта, форекс, трейдинг; "
        "insure — страхование, инвестиции, пассивный доход. true только если реклама ПРЯМО об этом, сомневаешься — false.\n\n"
        f"ТЕКСТ:\n{text[:2500]}"
    )
    try:
        with vertex_only():
            data = extract_json(await ai.generate([{"text": prompt}], temperature=0.0, json_mode=True, thinking_budget=0, max_tokens=120))
    except Exception as exc:
        logger.info("channel_guard: второе мнение не вышло: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    return [name for name in _TOPICS if data.get(name) is True and enabled.get(name)]


def _channel() -> str:
    from .context import settings

    return str(settings.vacancy_channel or "")


def _owner() -> int | None:
    from .context import settings

    ids = sorted(settings.allowed_telegram_ids)
    return ids[0] if ids else None


async def _tell(bot: Any, text: str) -> None:
    owner = _owner()
    if owner is None:
        return
    from . import screen as screen_mod

    try:
        await screen_mod.send_note(bot, owner, text, ttl=86400, sticky=True)
    except Exception:
        logger.warning("channel_guard: не смог написать владельцу", exc_info=True)


async def handle_post(bot: Any, post: dict[str, Any], *, ai: Any | None = None, history: bool = False) -> str:
    """Один пост канала: {id, text, ts}. → own | seen | hold | deleted | notified | ad_ok.
    history — старый пост (первый запуск): защиту ленты можем включить, но ничего не удаляем."""
    st = feed.load()
    mid = int(post["id"])
    done = st.setdefault("guard_done", [])
    if mid in done:
        return "seen"
    done.append(mid)
    del done[:-300]
    text = post.get("text") or ""
    kind = classify(text, mid)
    if kind != "ad":
        feed.note_channel_phones(text, float(post["ts"]))      # такую же вакансию из чужих каналов потом не предложим
    if kind == "own":
        feed.save()
        return "own"
    if kind == "manual":
        feed.note_manual_post(float(post["ts"]), mid)
        feed.save()
        return "hold"
    # реклама
    if not feed.cfg("ads_on") or history:
        feed.save()
        return "ad_ok"
    enabled = dict(feed.cfg("ads_cats"))
    topics = ad_topics(text, enabled)
    if not topics and ai is not None:
        topics = await llm_topics(text, ai, enabled)
    feed.save()
    if not topics:
        return "ad_ok"
    labels = ", ".join(feed.AD_CATEGORIES[t] for t in topics)
    entry = {"id": mid, "ts": float(post["ts"]), "topics": topics, "text": text[:700], "deleted": False}
    if feed.cfg("ads_mode") != "delete":
        feed.log_ad(entry)
        await _tell(bot, f"🛡 В канале реклама на запрещённую тему ({labels}). Режим «только сообщать» — не удалял.\n\n{text[:700]}")
        return "notified"
    try:
        await bot.delete_message(_channel(), mid)
        entry["deleted"] = True
    except Exception as exc:
        feed.log_ad(entry)
        await _tell(bot, f"🛡 Нашёл рекламу ({labels}), но удалить не смог: {str(exc)[:120]}. "
                         "Проверь, что у бота в канале есть право удалять сообщения.\n\n" + text[:700])
        return "notified"
    feed.log_ad(entry)
    await _tell(bot, f"🛡 Удалил из канала рекламу ({labels}). Её текст, если нужен:\n\n{text[:900]}")
    return "deleted"


async def poll(bot: Any, *, ai: Any | None = None) -> dict[str, Any]:
    """Прочитать канал аккаунтом JES и разобрать новые посты. → счётчики или {"error": ...}."""
    from . import caller

    client = caller.user_client()
    if client is None:
        return {"error": "аккаунт JES не в сети"}
    channel = _channel().lstrip("@")
    if not channel or channel.lstrip("-").isdigit():
        return {"error": "канал указан не @именем — читать публичную ленту нечем"}
    st = feed.load()
    cursor = int(st["guard_cursor"])
    first = cursor == 0
    try:
        entity = await client.get_entity(channel)
        posts = []
        async for message in client.iter_messages(entity, limit=POLL_LIMIT, min_id=cursor):
            date = getattr(message, "date", None)
            posts.append({"id": int(message.id), "ts": date.timestamp() if date else time.time(),
                          "text": (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()})
    except Exception as exc:
        if type(exc).__name__ == "FloodWaitError":
            try:
                feed._flood(exc)
            except feed.FeedUnavailable:
                pass
        logger.warning("channel_guard: канал не прочитался: %s: %s", type(exc).__name__, exc)
        return {"error": type(exc).__name__}
    counts = {"posts": len(posts), "hold": 0, "deleted": 0, "notified": 0, "ad_ok": 0, "own": 0}
    horizon = time.time() - FIRST_RUN_LOOKBACK_H * 3600
    for post in sorted(posts, key=lambda p: p["id"]):
        if first and post["ts"] < horizon:
            st.setdefault("guard_done", []).append(post["id"])
            if classify(post["text"], post["id"]) != "ad":
                feed.note_channel_phones(post["text"], post["ts"])
            continue
        result = await handle_post(bot, post, ai=ai, history=first)
        if result in counts:
            counts[result] += 1
    if not st.get("phones_seeded"):
        # один раз: телефоны вакансий, которые уже стоят в канале (до нас), — чтобы не предлагать те же из чужих каналов
        st["phones_seeded"] = True
        try:
            async for message in client.iter_messages(entity, limit=SEED_LIMIT):
                body = (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()
                date = getattr(message, "date", None)
                if body and classify(body, int(message.id)) != "ad":
                    feed.note_channel_phones(body, date.timestamp() if date else None)
        except Exception as exc:
            st["phones_seeded"] = False
            logger.info("channel_guard: телефоны канала не собрались: %s", type(exc).__name__)
        feed.save()
    if posts:
        st["guard_cursor"] = max(int(st["guard_cursor"]), max(p["id"] for p in posts))
        feed.save()
    return counts


__all__ = ["poll", "handle_post", "classify", "ad_topics", "is_reklama", "is_our_template", "llm_topics"]
