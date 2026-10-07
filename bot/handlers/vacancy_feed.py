"""Автоподбор вакансий: экран, карточки каналов и вакансий, фоновый круг (07.10). Только владелец.

JES читает одобренные каналы (bot/vacancy_feed.py), к каждой прошедшей проверку вакансии рисуется картинка (Nano Banana 2.1 +
логотип) и приходит карточка: «Опубликовать» / «Пропустить» / «Другая картинка». В канал — только по его кнопке.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
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

CAPTION_LIMIT = 1024
DISCOVERY_EVERY_S = 3 * 86400
MAX_SOURCES_AUTO = 8             # столько каналов в работе — автопоиск новых больше не тревожит
_tasks: set[asyncio.Task] = set()
_busy = asyncio.Lock()           # один круг за раз (кнопка «проверить сейчас» и воркер не наступают друг на друга)

router.callback_query.filter(lambda c: bool(c.data) and c.data.startswith("vf:") and access.is_owner(c.from_user.id))


def owner_id() -> int | None:
    ids = sorted(settings.allowed_telegram_ids)
    return ids[0] if ids else None


def _spawn(coro: Any) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------ экран
def panel_text() -> str:
    s = feed.status()
    lines = [
        "🤖 <b>Автоподбор вакансий</b>",
        "",
        f"Статус: {'включён ✅' if s['enabled'] else 'выключен ⏸'} · до {s['cap']} карточек в день",
        f"Каналы: {s['approved']} в работе · {s['pending']} ждут твоего решения",
        f"Сегодня показано {s['today']} из {s['cap']} · в очереди {s['new']} · ждут ответа {s['carded']}",
        "",
        "JES читает одобренные каналы и отбрасывает ненадёжное: нет контакта работодателя, деньги вперёд, работа за границей, "
        "нет зарплаты или условий. Остальное оформляет по шаблону канала, рисует картинку (Nano Banana 2.1 только через Vertex, логотип снизу слева) "
        "и присылает карточку. В канал уходит только после «Опубликовать».",
    ]
    if float(s["flood_until"] or 0) > time.time():
        lines += ["", f"⏳ Telegram просит JES подождать до {datetime.fromtimestamp(float(s['flood_until']), feed.TZ):%H:%M}."]
    return "\n".join(lines)


def panel_keyboard() -> InlineKeyboardMarkup:
    s = feed.status()
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔍 Найти каналы", "vf:find", style="primary"), _btn(f"📡 Каналы ({s['approved']}+{s['pending']})", "vf:srcs")],
        [_btn("🔄 Проверить сейчас", "vf:now")],
        [_btn("⏸ Выключить" if s["enabled"] else "▶️ Включить", "vf:toggle"), _btn(f"📥 Лимит: {s['cap']} в день", "vf:cap")],
        [_btn("⬅️ Назад", "menu:vacancy")],
    ])


async def _show_panel(callback: CallbackQuery, note: str = "") -> None:
    await safe_edit(callback, panel_text() + (f"\n\n{note}" if note else ""), panel_keyboard())


@router.callback_query(F.data == "vf:panel")
async def cb_panel(callback: CallbackQuery) -> None:
    await answer_now(callback)
    feed.remember_premium(bool(getattr(callback.from_user, "is_premium", False)))
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
    base = f"@{name}"
    if info.get("status") == "pending":
        return f"{base} · {int(info.get('members') or 0):,} подписчиков · {int(info.get('passing') or 0)} из {int(info.get('vacancies') or 0)} вакансий проходят"
    reasons = sorted((st.get("reasons") or {}).items(), key=lambda kv: -kv[1])[:2]
    why = ", ".join(f"{r} ×{n}" for r, n in reasons)
    line = f"{base} · принято {int(st.get('queued') or 0)}, отброшено {int(st.get('rejected') or 0)}" + (f" ({why})" if why else "")
    return line + (" · ⚠️ не читается" if info.get("error") else "")


@router.callback_query(F.data == "vf:srcs")
async def cb_sources(callback: CallbackQuery) -> None:
    await answer_now(callback)
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
def _visible_len(post_html: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", post_html)))


async def _send_post(bot: Bot, chat_id: int | str, post: str, image: bytes | str | None, markup: InlineKeyboardMarkup | None,
                     *, head: str | None = None) -> tuple[list[int], str | None, int]:
    """Фото + подпись, если пост влезает в 1024 знака; иначе фото, а следом текст с кнопками. → (id сообщений, file_id, id поста)."""
    if image is None:
        msg = await bot.send_message(chat_id, post, reply_markup=markup)
        return [msg.message_id], None, msg.message_id
    photo = BufferedInputFile(image, filename="vacancy.jpg") if isinstance(image, bytes) else image
    if _visible_len(post) <= CAPTION_LIMIT:
        msg = await bot.send_photo(chat_id, photo, caption=post, reply_markup=markup)
        return [msg.message_id], msg.photo[-1].file_id, msg.message_id
    first = await bot.send_photo(chat_id, photo, caption=head)
    second = await bot.send_message(chat_id, post, reply_markup=markup)
    return [first.message_id, second.message_id], first.photo[-1].file_id, second.message_id


def _card_markup(cand: dict[str, Any], contact_url: str | None) -> InlineKeyboardMarkup:
    cid = cand["id"]
    rows = [
        [_btn("✅ Опубликовать", f"vf:pub:{cid}", style="success"), _btn("⏭ Пропустить", f"vf:skip:{cid}")],
        [_btn("🔄 Другая картинка", f"vf:img:{cid}"), _btn("🔗 Источник", url=cand["url"])],
    ]
    channel_kb = vacancy_channel_keyboard("uz", contact_url)
    return InlineKeyboardMarkup(inline_keyboard=(channel_kb.inline_keyboard if channel_kb else []) + rows)


def _post_html(cand: dict[str, Any], premium: bool) -> tuple[str, str | None]:
    data = feed.data_from_dict(cand["data"])
    post = vac.format_vacancy_post(data, premium=premium, footer_url=settings.vacancy_footer_url)
    return post, vac.build_contact_url(data.telegram)


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
    design = vac.pick_design(data, cand.get("scene"), recent=recent, seed=f"{cand['id']}:{cand.get('regen', 0)}")
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


async def send_card(bot: Bot, chat_id: int, cand: dict[str, Any], *, regenerate: bool = False) -> None:
    """Карточка вакансии владельцу. Картинка рисуется здесь (только для тех, кого показываем — деньги не тратим зря)."""
    image = None if regenerate else _read_image(cand["id"])
    warning = cand.get("image_warning") if image else None
    if image is None:
        image, warning = await _make_image(cand, again=regenerate)
        cand["image_warning"] = warning
    post, contact_url = _post_html(cand, bool(feed.load().get("premium")))
    shown = post if image else post + '\n\n' + "⚠️ Картинка не получилась — можно опубликовать без неё или нажать «Другая картинка»."
    head = f"📥 {h(str(cand['data'].get('headline') or 'Вакансия'))} · из @{cand['source']}"
    ids, file_id, _ = await _send_post(bot, chat_id, shown, image, _card_markup(cand, contact_url), head=head)
    cand.update({"status": "carded", "card_ids": ids, "chat_id": chat_id, "file_id": file_id, "has_image": bool(image)})
    feed.note_card_sent()
    feed.save()
    if warning:
        from .. import screen as screen_mod

        await screen_mod.send_note(bot, chat_id, warning, ttl=3600)


async def _delete_card(bot: Bot, cand: dict[str, Any]) -> None:
    for mid in cand.get("card_ids") or []:
        try:
            await bot.delete_message(cand["chat_id"], mid)
        except Exception:
            pass
    cand["card_ids"] = []


@router.callback_query(F.data.startswith("vf:pub:"))
async def cb_publish(callback: CallbackQuery) -> None:
    cand = feed.get_candidate(callback.data.split(":", 2)[2])
    if cand is None or cand.get("status") == "published":
        await answer_now(callback, "Эта вакансия уже обработана", alert=True)
        return
    channel = settings.vacancy_channel
    if not channel:
        await answer_now(callback, "Канал не настроен (VACANCY_CHANNEL)", alert=True)
        return
    premium = bool(getattr(callback.from_user, "is_premium", False))
    feed.remember_premium(premium)
    post, contact_url = _post_html(cand, premium)
    image: bytes | str | None = cand.get("file_id") or _read_image(cand["id"])
    try:
        _, _, post_id = await _send_post(callback.bot, channel, post, image, vacancy_channel_keyboard("uz", contact_url))
    except Exception as exc:
        logger.exception("vacancy_feed: публикация %s не вышла", cand["id"])
        await answer_now(callback, f"Не вышло: {str(exc)[:150]}", alert=True)
        return
    await answer_now(callback, "Опубликовано ✅")
    await _delete_card(callback.bot, cand)
    feed.mark_handled(cand, "published")
    link = f"https://t.me/{channel.lstrip('@')}/{post_id}" if str(channel).startswith("@") else ""
    from .. import screen as screen_mod

    await screen_mod.send_note(callback.bot, cand["chat_id"], "✅ Опубликовано в канал" + (f": {link}" if link else ""), ttl=20)


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
    if int(cand.get("regen") or 0) >= 2:
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
        if feed.sources("approved"):
            if caller.user_client() is None:
                out["note"] = "аккаунт JES не в сети"
            else:
                try:
                    out["pulled"] = await feed.pull_all()
                except feed.FeedUnavailable as exc:
                    out["note"] = str(exc)
        feed.cleanup()
        now = datetime.now(feed.TZ)
        in_hours = feed.SEND_FROM <= now.hour < feed.SEND_TO
        discovery_due = (not manual and in_hours and caller.user_client() is not None
                         and len(feed.sources("approved")) < MAX_SOURCES_AUTO and len(feed.sources("pending")) < 6
                         and time.time() - float(feed.load().get("last_discovery") or 0) > DISCOVERY_EVERY_S)
        if discovery_due:
            feed.load()["last_discovery"] = time.time()  # сначала отметка: упадёт — не будем долбить каждые полчаса
            feed.save()
            _spawn(_discover_and_report(bot, owner))
        cand = feed.next_card(now, ignore_hours=manual)
        if cand is not None:
            try:
                await send_card(bot, owner, cand)
                out["card"] = True
            except Exception:
                logger.exception("vacancy_feed: карточка %s не отправилась", cand["id"])
    return out


@router.callback_query(F.data == "vf:now")
async def cb_now(callback: CallbackQuery) -> None:
    await answer_now(callback, "Проверяю каналы…")
    feed.remember_premium(bool(getattr(callback.from_user, "is_premium", False)))
    await safe_edit(callback, panel_text() + "\n\n⏳ Читаю каналы и оформляю вакансии — минуту…", None)
    res = await tick(callback.bot, manual=True)
    pulled = res.get("pulled") or {}
    if res.get("note"):
        note = f"ℹ️ {res['note']}."
    elif not feed.sources("approved"):
        note = "Каналов в работе пока нет — нажми «Найти каналы»."
    else:
        note = (f"Прочитано каналов: {pulled.get('sources', 0)} · в очередь добавлено: {pulled.get('queued', 0)} · "
                f"отброшено: {pulled.get('rejected', 0)}" + (f" · карточка отправлена ниже" if res.get("card") else
                                                              " · подходящих новых вакансий для карточки нет"))
    await _show_panel(callback, note)
