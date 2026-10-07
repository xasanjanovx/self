"""Автоподбор вакансий: экран, карточки каналов и вакансий, фоновый круг (07.10). Только владелец.

JES читает одобренные каналы (bot/vacancy_feed.py), к каждой прошедшей проверку вакансии рисуется картинка (Nano Banana 2.1 +
логотип) и приходит карточка: «Опубликовать» / «Пропустить» / «Другая картинка». В канал — только по его кнопке (каждый пост
спрашивается у него обязательно), всегда фото + текст одним сообщением. Премиум-эмодзи: бот ставит их только в личку — в канал Telegram
их от бота не принимает (проверено), поэтому у карточки вторая кнопка «📤»: готовый пост приходит владельцу, и он пересылает его сам.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup

from .. import access, image_gen
from .. import vacancy as vac
from .. import vacancy_feed as feed
from ..context import settings
from ..keyboards import _btn, vacancy_channel_keyboard
from ..profile import h
from .common import answer_now, safe_edit

router = Router(name="vacancy_feed")
logger = logging.getLogger(__name__)

DISCOVERY_EVERY_S = 86400
MAX_SOURCES_AUTO = 12            # столько каналов в работе — автопоиск новых больше не тревожит
IMAGE_TRIES = 3                  # столько кругов пробуем нарисовать картинку, прежде чем показать карточку без неё (она без кнопки «Опубликовать»)
_tasks: set[asyncio.Task] = set()
_busy = asyncio.Lock()           # один круг за раз (кнопка «проверить сейчас» и воркер не наступают друг на друга)



def owner_filter(callback: CallbackQuery) -> bool:
    """Все кнопки автоподбора («vf:…») — только у владельца; остальные нажатия сюда не попадают."""
    return bool(callback.data) and callback.data.startswith("vf:") and access.is_owner(callback.from_user.id)


router.callback_query.filter(owner_filter)


def owner_id() -> int | None:
    ids = sorted(settings.allowed_telegram_ids)
    return ids[0] if ids else None


def _spawn(coro: Any) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------ экран
def gap_label(minutes: int) -> str:
    return f"{minutes // 60} ч" if minutes % 60 == 0 else f"{minutes} мин"


def guard_line() -> str:
    left = feed.hold_left()
    if left > 0:
        return f"🛡 Платный пост наверху ленты: новые карточки не шлю ещё {feed.human_wait(left)}"
    return "🛡 Лента свободна: платного поста наверху нет"


def panel_text() -> str:
    s = feed.status()
    start, end = feed.window()
    ads = feed.load()["ads_log"]
    lines = [
        "🤖 <b>Автоподбор вакансий</b>",
        "",
        f"Статус: {'включён ✅' if s['enabled'] else 'выключен ⏸'} · каждую вакансию решаешь ты (карточка), по одной, "
        f"не чаще раза в {gap_label(int(feed.cfg('card_gap_min')))}",
        f"Каналы: {s['approved']} в работе · {s['pending']} ждут твоего решения",
        f"Сегодня {s['today']} из {s['cap']} · окно {start:02d}:00–{end:02d}:00 · в очереди {s['new']} · ждут ответа {s['carded']}"
        + (f" · отложено {len(feed.candidates('scheduled'))}" if feed.candidates("scheduled") else ""),
        guard_line(),
        f"🚫 Реклама: фильтр {'включён' if feed.cfg('ads_on') else 'выключен'} · удалено {sum(1 for a in ads if a.get('deleted'))}",
        "",
        "JES читает каналы-источники и отбрасывает ненадёжное: нет контакта работодателя, деньги вперёд, работа за границей, "
        "нет зарплаты или условий. Остальное оформляет по шаблону канала и рисует баннер (Nano Banana 2.1 только "
        "через Vertex, дизайн каждый раз другой, логотип снизу слева). Фото и текст — одним постом. В канал — только после твоего «Опубликовать». "
        "Премиум-эмодзи бот в канал поставить не может (Telegram не принимает от бота) — для них кнопка «📤»: пришлю готовый пост, перешлёшь сам.",
    ]
    if float(s["flood_until"] or 0) > time.time():
        lines += ["", f"⏳ Telegram просит JES подождать до {datetime.fromtimestamp(float(s['flood_until']), feed.TZ):%H:%M}."]
    return "\n".join(lines)


def panel_keyboard() -> InlineKeyboardMarkup:
    s = feed.status()
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("▶️ Следующая вакансия", "vf:now", style="success")],
        [_btn("🔍 Найти каналы", "vf:find", style="primary"), _btn(f"📡 Каналы ({s['approved']}+{s['pending']})", "vf:srcs")],
        [_btn("💰 Платный пост размещён", "vf:paid")],
        [_btn("⏸ Выключить" if s["enabled"] else "▶️ Включить", "vf:toggle"), _btn("⚙️ Настройки", "vf:cfg", style="primary")],
        [_btn("🛡 Защита ленты и реклама", "vf:ads")],
        [_btn("⬅️ Назад", "menu:vacancy")],
    ])


async def _show_panel(callback: CallbackQuery, note: str = "") -> None:
    await safe_edit(callback, panel_text() + (f"\n\n{note}" if note else ""), panel_keyboard())


@router.callback_query(F.data == "vf:panel")
async def cb_panel(callback: CallbackQuery) -> None:
    await answer_now(callback)
    await _show_panel(callback)


@router.callback_query(F.data == "vf:toggle")
async def cb_toggle(callback: CallbackQuery) -> None:
    await answer_now(callback)
    feed.set_enabled(not feed.load()["enabled"])
    await _show_panel(callback)


@router.callback_query(F.data == "vf:cap")
async def cb_cap(callback: CallbackQuery) -> None:
    await answer_now(callback)
    cap = int(feed.load()["cap"])
    nxt = next((c for c in feed.CAPS if c > cap), feed.CAPS[0])
    feed.set_cap(nxt)
    await _show_panel(callback)


# ------------------------------------------------------------------ каналы
def _source_line(name: str, info: dict[str, Any]) -> str:
    st = info.get("stats") or {}
    base = f"@{name}" + (" ⭐" if info.get("favorite") else "")
    if info.get("status") == "pending":
        return f"{base} · {int(info.get('members') or 0):,} подписчиков · {int(info.get('passing') or 0)} из {int(info.get('vacancies') or 0)} вакансий проходят"
    reasons = sorted((st.get("reasons") or {}).items(), key=lambda kv: -kv[1])[:2]
    why = ", ".join(f"{r} ×{n}" for r, n in reasons)
    line = f"{base} · принято {int(st.get('queued') or 0)}, отброшено {int(st.get('rejected') or 0)}" + (f" ({why})" if why else "")
    return line + (" · ⚠️ не читается" if info.get("error") else "")


@router.callback_query(F.data == "vf:srcs")
async def cb_sources(callback: CallbackQuery) -> None:
    await answer_now(callback)
    feed.ensure_favorites()
    approved, pending = feed.sources("approved"), feed.sources("pending")
    lines = ["📡 <b>Каналы-источники</b>", ""]
    rows = []
    if approved:
        lines.append("<b>В работе</b>")
        for name, info in approved.items():
            lines.append("• " + h(_source_line(name, info)))
            rows.append([_btn(f"🗑 Убрать @{name}", f"vf:rm:{name}")])
    if pending:
        lines += ["", "<b>Ждут решения</b>"]
        for name, info in list(pending.items())[:10]:
            lines.append("• " + h(_source_line(name, info)))
            rows.append([_btn(f"✅ @{name}", f"vf:src:ok:{name}"), _btn("❌", f"vf:src:no:{name}")])
    if not approved and not pending:
        lines.append("Пока пусто. Нажми «Найти каналы» — JES поищет сам, а ты выберешь надёжные.")
    rows.append([_btn("⬅️ Назад", "vf:panel")])
    await safe_edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("vf:rm:"))
async def cb_remove(callback: CallbackQuery) -> None:
    await answer_now(callback, "Убрала из работы")
    feed.set_source_status(callback.data.split(":", 2)[2], "rejected")
    await cb_sources(callback)


@router.callback_query(F.data.startswith("vf:src:"))
async def cb_source_decision(callback: CallbackQuery) -> None:
    _, _, verdict, name = callback.data.split(":", 3)
    ok = verdict == "ok"
    if not feed.set_source_status(name, "approved" if ok else "rejected"):
        await answer_now(callback, "Этого канала уже нет в списке", alert=True)
        return
    await answer_now(callback, "Беру в работу" if ok else "Не трогаю")
    text = f"✅ @{name} — в работе. Первые вакансии придут в течение получаса (или нажми «Проверить сейчас»)." if ok else f"❌ @{name} — не берём."
    try:
        await callback.message.edit_text(text, reply_markup=None)
    except Exception:
        pass


def _source_card(item: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    name = item["name"]
    text = (f"📡 <b>{h(str(item.get('title') or name))}</b> · @{name}\n"
            f"👥 {int(item.get('members') or 0):,} подписчиков\n"
            f"📊 Из {int(item.get('posts') or 0)} последних постов: {int(item.get('vacancies') or 0)} вакансий, "
            f"{int(item.get('passing') or 0)} проходят проверку ({round(float(item.get('ratio') or 0) * 100)}%)\n"
            f"Пример: «{h(str(item.get('sample') or ''))}…»\n\nБрать вакансии из этого канала?")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Брать", f"vf:src:ok:{name}", style="success"), _btn("❌ Не надо", f"vf:src:no:{name}")],
        [_btn("🔗 Открыть канал", url=f"https://t.me/{name}")],
    ])
    return text, kb


async def _discover_and_report(bot: Bot, chat_id: int) -> None:
    try:
        added = await feed.discover(own_channel=settings.vacancy_channel)
    except feed.FeedUnavailable as exc:
        await bot.send_message(chat_id, f"🔍 Поиск каналов не вышел: {h(str(exc))}.")
        return
    except Exception:
        logger.exception("vacancy_feed: поиск каналов упал")
        await bot.send_message(chat_id, "🔍 Поиск каналов сломался — подробности в логе. Попробую в следующий раз.")
        return
    if not added:
        await bot.send_message(chat_id, "🔍 Новых подходящих каналов не нашла: все найденные мелкие, неактивные или с малым числом нормальных вакансий.")
        return
    await bot.send_message(chat_id, f"🔍 Нашла каналов: {len(added)}. Выбери надёжные — читать буду только их.")
    for item in added:
        text, kb = _source_card(item)
        await bot.send_message(chat_id, text, reply_markup=kb)


@router.callback_query(F.data == "vf:find")
async def cb_find(callback: CallbackQuery) -> None:
    await answer_now(callback, "Ищу каналы — пришлю список")
    _spawn(_discover_and_report(callback.bot, callback.from_user.id))
    await _show_panel(callback, "🔍 Ищу публичные каналы с вакансиями… Это пара минут, карточки придут сообщениями ниже.")


# ------------------------------------------------------------------ карточки вакансий
async def _send_post(bot: Bot, chat_id: int | str, post: str, image: bytes | str | None, markup: InlineKeyboardMarkup | None,
                     *, head: str | None = None) -> tuple[list[int], str | None, int]:
    """Фото + текст ОДНИМ сообщением (подпись к фото). Пост заранее сокращается до лимита (vac.fit_post); если всё же не влез
    (почти невозможно) — фото, а следом текст. Без картинки — только текст (так уходит лишь карточка без картинки владельцу, в канал — никогда).
    → (id сообщений, file_id, id поста)."""
    if image is None:
        msg = await bot.send_message(chat_id, post, reply_markup=markup)
        return [msg.message_id], None, msg.message_id
    photo = BufferedInputFile(image, filename="vacancy.jpg") if isinstance(image, bytes) else image
    if vac.visible_len(post) <= vac.CAPTION_LIMIT:
        msg = await bot.send_photo(chat_id, photo, caption=post, reply_markup=markup)
        return [msg.message_id], msg.photo[-1].file_id, msg.message_id
    first = await bot.send_photo(chat_id, photo, caption=head)
    second = await bot.send_message(chat_id, post, reply_markup=markup)
    return [first.message_id, second.message_id], first.photo[-1].file_id, second.message_id


def channel_markup(contact_url: str | None) -> InlineKeyboardMarkup | None:
    """Кнопки под постом в канале: «Bog'lanish» (работодателю) и «E'lon joylash» (админу канала), обе синие."""
    return vacancy_channel_keyboard("uz", contact_url, ad_url=vac.build_ad_url(settings.vacancy_footer_url))


def _views_label(views: Any) -> str:
    """« · 👁 12.4к» — сколько просмотров у исходного поста (в подписи места нет, поэтому в кнопке «Источник»)."""
    try:
        count = int(views or 0)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    return f" · 👁 {count}" if count < 1000 else f" · 👁 {count / 1000:.1f}к".replace(".0к", "к")


def _card_markup(cand: dict[str, Any], contact_url: str | None, *, can_publish: bool = True) -> InlineKeyboardMarkup:
    """Без картинки «Опубликовать» нет: фото обязательно в одном посте с текстом, поэтому вместо неё — «Нарисовать картинку»."""
    cid = cand["id"]
    source = _btn("🔗 Источник" + _views_label(cand.get("views")), url=cand["url"])
    if can_publish:
        rows = [
            [_btn("✅ Опубликовать сейчас", f"vf:pub:{cid}", style="success"), _btn("⏭ Пропустить", f"vf:skip:{cid}")],
            [_btn("📤 Премиум-эмодзи: пришли, перешлю сам", f"vf:fwd:{cid}")],
            [_btn("🔄 Другая картинка", f"vf:img:{cid}"), source],
        ]
    else:
        rows = [
            [_btn("🎨 Нарисовать картинку", f"vf:img:{cid}", style="primary"), _btn("⏭ Пропустить", f"vf:skip:{cid}")],
            [source],
        ]
    channel_kb = channel_markup(contact_url)
    return InlineKeyboardMarkup(inline_keyboard=(channel_kb.inline_keyboard if channel_kb else []) + rows)


def _post_html(cand: dict[str, Any]) -> tuple[str, str | None, bool]:
    """Пост как уйдёт в канал: с тегами премиум-эмодзи канала (в личку владельцу они доходят; в канал Telegram от бота их отбрасывает,
    остаются обычные эмодзи), влезает в подпись к фото. → (html, ссылка «написать работодателю», сокращён ли текст)."""
    data = feed.data_from_dict(cand["data"])
    post, trimmed = vac.fit_post(data, premium=True, footer_url=settings.vacancy_footer_url)
    return post, vac.build_contact_url(data.telegram), trimmed


def _read_image(cid: str) -> bytes | None:
    try:
        return feed.image_path(cid).read_bytes()
    except OSError:
        return None


async def _make_image(cand: dict[str, Any], *, again: bool = False) -> tuple[bytes | None, str | None]:
    """→ (картинка или None, предупреждение, если проверка баннера не прошла и после перерисовок).
    Дизайн каждый раз другой (vacancy.pick_design: по профессии, но не из последних); «Другая картинка» — ещё и другой дизайн."""
    data = feed.data_from_dict(cand["data"])
    recent = ([cand["design"]] if again and cand.get("design") else []) + feed.recent_designs()
    design = vac.pick_design(data, cand.get("scene"), recent=recent, seed=f"{cand['id']}:{cand.get('regen', 0)}",
                             allowed=feed.allowed_designs())
    try:
        banner = await image_gen.vacancy_image(data, cand.get("scene"), design=design)
    except image_gen.ImageError as exc:
        logger.warning("vacancy_feed: картинка %s не вышла: %s", cand["id"], exc)
        return None, None
    except Exception:
        logger.exception("vacancy_feed: картинка %s сломалась", cand["id"])
        return None, None
    cand["design"] = banner.design or design["id"]
    feed.note_design(cand["design"])
    try:
        feed.image_path(cand["id"]).write_bytes(banner.image)
    except OSError:
        logger.warning("vacancy_feed: не сохранил картинку %s", cand["id"], exc_info=True)
    return banner.image, banner.warning


async def send_card(bot: Bot, chat_id: int, cand: dict[str, Any], *, regenerate: bool = False) -> bool:
    """Карточка вакансии владельцу: картинка и пост одним сообщением — так же, как уйдёт в канал. Картинка рисуется здесь
    (только для тех, кого показываем — деньги не тратим зря).
    Фото обязательно: если картинка не вышла, карточку не показываем (в следующий круг попробуем снова); после IMAGE_TRIES неудач
    или по его кнопке «Другая картинка» — карточка без картинки и без кнопки «Опубликовать». → True, если карточка ушла."""
    image = None if regenerate else _read_image(cand["id"])
    warning = cand.get("image_warning") if image else None
    if image is None:
        image, warning = await _make_image(cand, again=regenerate)
        cand["image_warning"] = warning
        if image is None:
            cand["img_tries"] = int(cand.get("img_tries") or 0) + 1
            feed.save()
            if not regenerate and cand["img_tries"] < IMAGE_TRIES:
                return False
    post, contact_url, trimmed = _post_html(cand)
    shown = post if image else post + "\n\n⚠️ Картинка не получилась — без неё публиковать нельзя. Нажми «Нарисовать картинку»."
    head = f"📥 {h(str(cand['data'].get('headline') or 'Вакансия'))} · из @{cand['source']}"
    ids, file_id, _ = await _send_post(bot, chat_id, shown, image, _card_markup(cand, contact_url, can_publish=bool(image)), head=head)
    cand.update({"status": "carded", "card_ids": ids, "chat_id": chat_id, "file_id": file_id, "has_image": bool(image)})
    feed.note_card_sent()
    feed.save()
    notes = [n for n in (warning, "✂️ Текст сокращён до лимита подписи (1024 знака), чтобы фото и текст шли одним постом. Полный — по кнопке «Источник»."
                         if trimmed else None) if n]
    if notes:
        from .. import screen as screen_mod

        await screen_mod.send_note(bot, chat_id, "\n".join(notes), ttl=3600)
    return True


async def _delete_card(bot: Bot, cand: dict[str, Any]) -> None:
    for mid in cand.get("card_ids") or []:
        try:
            await bot.delete_message(cand["chat_id"], mid)
        except Exception:
            pass
    cand["card_ids"] = []


async def publish_candidate(bot: Bot, cand: dict[str, Any]) -> tuple[list[int], int]:
    """Опубликовать вакансию в канал: фото и текст одним сообщением. Без картинки не публикуем никогда.
    Свои сообщения записываем — чужими (ручными) их считать нельзя. → (id сообщений в канале, id поста).
    Бросает исключение, если картинки нет или Telegram отказал."""
    post, contact_url, _ = _post_html(cand)
    image: bytes | str | None = cand.get("file_id") or _read_image(cand["id"])
    if image is None:
        raise RuntimeError("нет картинки — сначала нарисуй её кнопкой «Другая картинка»")
    ids, _, post_id = await _send_post(bot, settings.vacancy_channel, post, image, channel_markup(contact_url))
    feed.mark_own(ids)
    feed.note_feed_post()
    feed.mark_handled(cand, "published")
    feed.save()
    return ids, post_id


def _post_link(post_id: int) -> str:
    channel = str(settings.vacancy_channel or "")
    return f"https://t.me/{channel.lstrip('@')}/{post_id}" if channel.startswith("@") else ""


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, feed.TZ).strftime("%H:%M")


@router.callback_query(F.data.startswith("vf:pub:"))
async def cb_publish(callback: CallbackQuery) -> None:
    cand = feed.get_candidate(callback.data.split(":", 2)[2])
    if cand is None or cand.get("status") in {"published", "scheduled"}:
        await answer_now(callback, "Эта вакансия уже обработана или стоит в очереди", alert=True)
        return
    if not settings.vacancy_channel:
        await answer_now(callback, "Канал не настроен (VACANCY_CHANNEL)", alert=True)
        return
    if not (cand.get("file_id") or _read_image(cand["id"])):
        await answer_now(callback, "Без картинки не публикую — фото должно быть в одном посте с текстом. Нажми «Нарисовать картинку»", alert=True)
        return
    from .. import screen as screen_mod

    # 07.10: «Опубликовать сейчас» — сразу, без очереди (он сам решает; раньше при платном посте наверху вакансия уходила в очередь, а бот
    # принимал за платные и его бесплатные посты). Про платный пост наверху — только предупреждение после публикации.
    left = feed.hold_left()
    try:
        _, post_id = await publish_candidate(callback.bot, cand)
    except Exception as exc:
        logger.exception("vacancy_feed: публикация %s не вышла", cand["id"])
        await answer_now(callback, f"Не вышло: {str(exc)[:150]}", alert=True)
        return
    await answer_now(callback, "Опубликовано ✅")
    await _delete_card(callback.bot, cand)
    link = _post_link(post_id)
    warn = f"\n⚠️ Платный пост наверху был ещё {feed.human_wait(left)} — эта вакансия встала выше него." if left > 0 else ""
    await screen_mod.send_note(callback.bot, cand["chat_id"], "✅ Опубликовано в канал" + (f": {link}" if link else "") + warn, ttl=60 if warn else 20)


@router.callback_query(F.data.startswith("vf:fwd:"))
async def cb_forward(callback: CallbackQuery) -> None:
    """Telegram не пускает премиум-эмодзи от бота в канал (проверено 07.10: в личку бот их ставит, в канал — нет, и через copyMessage тоже).
    Поэтому второй путь: бот присылает ему готовый пост — фото и текст одним сообщением, без кнопок, с премиум-эмодзи, — а в канал
    его пересылает он сам (у пересылки Premium-аккаунта эмодзи сохраняются)."""
    cand = feed.get_candidate(callback.data.split(":", 2)[2])
    if cand is None or cand.get("status") in {"published", "scheduled"}:
        await answer_now(callback, "Эта вакансия уже обработана или стоит в очереди", alert=True)
        return
    image = cand.get("file_id") or _read_image(cand["id"])
    if image is None:
        await answer_now(callback, "Без картинки не делаю — нажми «Нарисовать картинку»", alert=True)
        return
    post, _, _ = _post_html(cand)
    chat_id = cand.get("chat_id") or callback.from_user.id
    try:
        await _send_post(callback.bot, chat_id, post, image, None)
    except Exception as exc:
        logger.exception("vacancy_feed: чистый пост %s не отправился", cand["id"])
        await answer_now(callback, f"Не вышло: {str(exc)[:150]}", alert=True)
        return
    await answer_now(callback, "Готовый пост — выше")
    await _delete_card(callback.bot, cand)
    feed.mark_handled(cand, "published")                       # раз он взял его себе — такую же вакансию больше не предлагаем
    from .. import screen as screen_mod

    await screen_mod.send_note(callback.bot, chat_id, "📤 Готовый пост выше: перешли его в канал (при пересылке выбери «Скрыть отправителя») — "
                               "премиум-эмодзи сохранятся.", ttl=3600)


async def publish_due(bot: Bot) -> int:
    """Отложенные (пока был платный пост наверху) — публикуем, когда защита кончилась. По одной за раз: между постами интервал."""
    if not settings.vacancy_channel:
        return 0
    from .. import screen as screen_mod

    now = time.time()
    for cand in sorted(feed.candidates("scheduled"), key=lambda c: float(c.get("publish_at") or 0)):
        if float(cand.get("publish_at") or 0) > now and feed.hold_left() > 0:
            continue          # защита ещё идёт; если её уже нет (сняли или была ошибочной) — не ждём назначенного времени
        ok, _, wait = feed.publish_gate(respect_schedule=False)
        if not ok:
            cand["publish_at"] = now + wait + 30
            feed.save()
            continue
        try:
            _, post_id = await publish_candidate(bot, cand)
        except Exception:
            logger.exception("vacancy_feed: отложенная публикация %s не вышла", cand["id"])
            cand["publish_at"] = now + 600
            feed.save()
            continue
        owner = owner_id()
        if owner is not None:
            link = _post_link(post_id)
            await screen_mod.send_note(bot, owner, "⏰ Опубликовал отложенную вакансию" + (f": {link}" if link else ""), ttl=3600)
        return 1
    return 0


@router.callback_query(F.data.startswith("vf:skip:"))
async def cb_skip(callback: CallbackQuery) -> None:
    cand = feed.get_candidate(callback.data.split(":", 2)[2])
    if cand is None:
        await answer_now(callback, "Уже обработано")
        return
    await answer_now(callback, "Пропускаю")
    await _delete_card(callback.bot, cand)
    feed.mark_handled(cand, "skipped")


@router.callback_query(F.data.startswith("vf:img:"))
async def cb_new_image(callback: CallbackQuery) -> None:
    cand = feed.get_candidate(callback.data.split(":", 2)[2])
    if cand is None:
        await answer_now(callback, "Уже обработано", alert=True)
        return
    if int(cand.get("regen") or 0) >= 2 and cand.get("has_image"):
        await answer_now(callback, "Больше двух перерисовок не делаю — можно опубликовать как есть или пропустить", alert=True)
        return
    await answer_now(callback, "Рисую заново, около 15 секунд…")
    cand["regen"] = int(cand.get("regen") or 0) + 1
    await _delete_card(callback.bot, cand)
    await send_card(callback.bot, cand["chat_id"], cand, regenerate=True)


# ------------------------------------------------------------------ фоновый круг
async def tick(bot: Bot, *, manual: bool = False) -> dict[str, Any]:
    """Один круг: прочитать каналы → разобрать → (раз в 3 дня) поискать новые → показать одну карточку. Воркер зовёт раз в 30 минут."""
    from .. import caller

    out: dict[str, Any] = {"pulled": None, "card": False, "note": ""}
    owner = owner_id()
    if owner is None or not feed.load()["enabled"]:
        out["note"] = "выключен"
        return out
    async with _busy:
        feed.ensure_favorites()
        ready_now = manual and feed.next_card(datetime.now(feed.TZ), force=True) is not None   # «Следующая вакансия»: есть готовая — без чтения каналов
        if feed.sources("approved") and not ready_now:
            if caller.user_client() is None:
                out["note"] = "аккаунт JES не в сети"
            else:
                try:
                    out["pulled"] = await feed.pull_all()
                except feed.FeedUnavailable as exc:
                    out["note"] = str(exc)
        feed.cleanup()
        now = datetime.now(feed.TZ)
        in_hours = feed.in_window(now)
        discovery_due = (not manual and in_hours and caller.user_client() is not None
                         and len(feed.sources("approved")) < MAX_SOURCES_AUTO and len(feed.sources("pending")) < 6
                         and time.time() - float(feed.load().get("last_discovery") or 0) > DISCOVERY_EVERY_S)
        if discovery_due:
            feed.load()["last_discovery"] = time.time()  # сначала отметка: упадёт — не будем долбить каждые полчаса
            feed.save()
            _spawn(_discover_and_report(bot, owner))
        cand = feed.next_card(now, force=manual)
        if cand is not None:
            try:
                out["card"] = await send_card(bot, owner, cand)
                if not out["card"]:
                    out["note"] = out["note"] or "картинка пока не получилась — попробую ещё раз в следующий круг"
            except Exception:
                logger.exception("vacancy_feed: вакансия %s не отправилась", cand["id"])
    return out


@router.callback_query(F.data == "vf:now")
async def cb_now(callback: CallbackQuery) -> None:
    await answer_now(callback, "Ищу следующую вакансию…")
    await safe_edit(callback, panel_text() + "\n\n⏳ Готовлю следующую вакансию и картинку — до минуты…", None)
    res = await tick(callback.bot, manual=True)
    pulled = res.get("pulled") or {}
    if res.get("note"):
        note = f"ℹ️ {res['note']}."
    elif not feed.sources("approved"):
        note = "Каналов в работе пока нет — нажми «Найти каналы»."
    else:
        read = (f"Прочитано каналов: {pulled.get('sources', 0)} · в очередь добавлено: {pulled.get('queued', 0)} · "
                f"отброшено: {pulled.get('rejected', 0)} · ") if pulled else ""
        note = read + ("карточка отправлена ниже" if res.get("card") else "подходящих новых вакансий для карточки нет")
    await _show_panel(callback, note)
