"""Настройки вакансий и защита ленты — экраны владельца (07.10): сколько в день, окно, интервал, режим, картинки, дизайны, реклама.

Всё хранится в состоянии автоподбора (bot/vacancy_feed.py: cfg/set_cfg); экраны — тонкая обвязка. Только владелец.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from .. import channel_guard as guard
from .. import vacancy as vac
from .. import vacancy_feed as feed
from ..keyboards import _btn
from ..profile import h
from .common import answer_now, safe_edit
from .vacancy_feed import owner_filter

router = Router(name="vacancy_settings")
logger = logging.getLogger(__name__)
router.callback_query.filter(owner_filter)

WINDOW_FROM = tuple(range(5, 13))      # с какого часа можно публиковать
WINDOW_TO = tuple(range(15, 25))       # до какого


def _next(choices: tuple, current: Any) -> Any:
    for choice in choices:
        if choice > current:
            return choice
    return choices[0]


# ------------------------------------------------------------------ настройки
def settings_text() -> str:
    start, end = feed.window()
    designs = len(feed.allowed_designs())
    auto = feed.cfg("mode") == "auto"
    return "\n".join([
        "⚙️ <b>Настройки вакансий</b>",
        "",
        f"Режим: {'🤖 сам размещает (после защиты и проверки)' if auto else '✋ присылает карточку, публикуешь ты'}",
        f"В день: <b>{feed.load()['cap']}</b>",
        f"Время публикации: <b>{start:02d}:00–{end:02d}:00</b> (Ташкент)",
        f"Интервал между постами: <b>{feed.cfg('gap_min')} мин</b>",
        f"Защита платного поста наверху: <b>{feed.protect_seconds() / 3600:.0f} ч</b> (меньше 3 нельзя)",
        f"Картинки: <b>{'вкл (Nano Banana 2.1, Vertex)' if feed.cfg('images') else 'выкл — только текст'}</b>",
        f"Зарплата обязательна: <b>{'да («по собеседованию» подходит)' if feed.cfg('require_salary') else 'нет'}</b>",
        f"Дизайны: <b>{designs} из {len(vac.DESIGNS)}</b>",
        "",
        "В авто-режиме пост выходит только если картинка прошла проверку; иначе приходит карточка. Каждый авто-пост — с кнопкой «Удалить».",
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    start, end = feed.window()
    auto = feed.cfg("mode") == "auto"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🤖 Режим: сам размещает" if auto else "✋ Режим: с подтверждением", "vf:s:mode", style="primary")],
        [_btn(f"📥 В день: {feed.load()['cap']}", "vf:s:cap"), _btn(f"🕗 С {start:02d}:00", "vf:s:wf"), _btn(f"🕘 До {end:02d}:00", "vf:s:wt")],
        [_btn(f"⏱ Интервал: {feed.cfg('gap_min')} мин", "vf:s:gap"), _btn(f"🛡 Защита: {feed.protect_seconds() / 3600:.0f} ч", "vf:s:prot")],
        [_btn(f"🖼 Картинки: {'вкл' if feed.cfg('images') else 'выкл'}", "vf:s:imgs"),
         _btn(f"💰 Зарплата обязательна: {'да' if feed.cfg('require_salary') else 'нет'}", "vf:s:sal")],
        [_btn(f"🎨 Дизайны ({len(feed.allowed_designs())}/{len(vac.DESIGNS)})", "vf:ds"), _btn("🛡 Реклама и защита", "vf:ads")],
        [_btn("⬅️ Назад", "vf:panel")],
    ])


async def _show_settings(callback: CallbackQuery) -> None:
    await safe_edit(callback, settings_text(), settings_keyboard())


@router.callback_query(F.data == "vf:cfg")
async def cb_settings(callback: CallbackQuery) -> None:
    await answer_now(callback)
    await _show_settings(callback)


@router.callback_query(F.data.startswith("vf:s:"))
async def cb_setting(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 2)[2]
    note = None
    if key == "mode":
        feed.set_cfg("mode", "confirm" if feed.cfg("mode") == "auto" else "auto")
        note = "Теперь сам размещает" if feed.cfg("mode") == "auto" else "Теперь присылает карточки"
    elif key == "cap":
        feed.set_cap(_next(feed.CAPS, int(feed.load()["cap"])))
    elif key == "wf":
        feed.set_cfg("window_from", _next(WINDOW_FROM, feed.window()[0]))
    elif key == "wt":
        feed.set_cfg("window_to", _next(WINDOW_TO, feed.window()[1]))
    elif key == "gap":
        feed.set_cfg("gap_min", _next(feed.GAP_CHOICES, int(feed.cfg("gap_min"))))
    elif key == "prot":
        feed.set_cfg("protect_hours", _next(feed.PROTECT_CHOICES, int(feed.protect_seconds() // 3600)))
    elif key == "imgs":
        feed.set_cfg("images", not feed.cfg("images"))
    elif key == "sal":
        feed.set_cfg("require_salary", not feed.cfg("require_salary"))
    await answer_now(callback, note)
    await _show_settings(callback)


# ------------------------------------------------------------------ дизайны
@router.callback_query(F.data == "vf:ds")
async def cb_designs(callback: CallbackQuery) -> None:
    await answer_now(callback)
    await _show_designs(callback)


async def _show_designs(callback: CallbackQuery) -> None:
    allowed = feed.allowed_designs()
    rows, pair = [], []
    for design in vac.DESIGNS:
        mark = "✅" if design["id"] in allowed else "⛔"
        pair.append(_btn(f"{mark} {vac.DESIGN_LABELS.get(design['id'], design['id'])}"[:40], f"vf:d:{design['id']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("✅ Включить все", "vf:d:all"), _btn("⬅️ Назад", "vf:cfg")])
    text = (f"🎨 <b>Дизайны баннеров</b> — в работе {len(allowed)} из {len(vac.DESIGNS)}\n\n"
            "Дизайн выбирается по профессии и не повторяется, пока не пройдут ещё 6 других. Выключи те, что не нравятся, — их не возьму.")
    await safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("vf:d:"))
async def cb_design_toggle(callback: CallbackQuery) -> None:
    design_id = callback.data.split(":", 2)[2]
    if design_id == "all":
        feed.set_cfg("off_designs", [])
    elif design_id in {d["id"] for d in vac.DESIGNS}:
        feed.toggle_design(design_id)
    await answer_now(callback)
    await _show_designs(callback)


# ------------------------------------------------------------------ защита ленты и реклама
def ads_text() -> str:
    cats = feed.cfg("ads_cats")
    log = feed.load()["ads_log"]
    lines = [
        "🛡 <b>Защита ленты и реклама</b>",
        "",
        f"<b>Платный пост наверху.</b> {('Сейчас защита: ещё ' + feed.human_wait(feed.hold_left())) if feed.hold_left() > 0 else 'Сейчас платного поста наверху нет.'}",
        f"Если ты сам разместил пост (заказ на размещение), бот {feed.protect_seconds() / 3600:.0f} ч ничего не публикует, чтобы он постоял на топе. "
        "Отложенные вакансии выйдут сами, когда защита кончится.",
        "",
        f"<b>Реклама (#reklama).</b> Фильтр: {'включён' if feed.cfg('ads_on') else 'выключен'} · "
        f"режим: {'удалять сразу' if feed.cfg('ads_mode') == 'delete' else 'только сообщать'}",
        "Темы: " + " · ".join(f"{'✅' if cats.get(key) else '⛔'} {label}" for key, label in feed.AD_CATEGORIES.items()),
        "Твои вакансии в шаблоне канала не трогаю, даже если они про банк.",
    ]
    deleted = [a for a in log if a.get("deleted")]
    lines += ["", f"Удалено рекламы: {len(deleted)}"]
    for entry in log[:3]:
        when = datetime.fromtimestamp(float(entry["ts"]), feed.TZ).strftime("%d.%m %H:%M")
        topics = ", ".join(feed.AD_CATEGORIES.get(t, t) for t in entry.get("topics") or [])
        lines.append(f"• {when} · {'удалил' if entry.get('deleted') else 'не удалил'} · {h(topics)} · {h(str(entry.get('text') or '')[:40])}…")
    return "\n".join(lines)


def ads_keyboard() -> InlineKeyboardMarkup:
    cats = feed.cfg("ads_cats")
    rows = [
        [_btn("🚫 Фильтр: вкл" if feed.cfg("ads_on") else "🚫 Фильтр: выкл", "vf:a:on", style="primary"),
         _btn("🗑 Режим: удалять" if feed.cfg("ads_mode") == "delete" else "🔔 Режим: сообщать", "vf:a:mode")],
    ]
    rows += [[_btn(f"{'✅' if cats.get(key) else '⛔'} {label}", f"vf:a:c:{key}")] for key, label in feed.AD_CATEGORIES.items()]
    rows.append([_btn("🔄 Проверить канал сейчас", "vf:a:poll")])
    rows.append([_btn("⬅️ Назад", "vf:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_ads(callback: CallbackQuery, note: str = "") -> None:
    await safe_edit(callback, ads_text() + (f"\n\n{note}" if note else ""), ads_keyboard())


@router.callback_query(F.data == "vf:ads")
async def cb_ads(callback: CallbackQuery) -> None:
    await answer_now(callback)
    await _show_ads(callback)


@router.callback_query(F.data.startswith("vf:a:"))
async def cb_ads_setting(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 2)[2]
    note = ""
    if key == "on":
        feed.set_cfg("ads_on", not feed.cfg("ads_on"))
    elif key == "mode":
        feed.set_cfg("ads_mode", "notify" if feed.cfg("ads_mode") == "delete" else "delete")
    elif key.startswith("c:") and key[2:] in feed.AD_CATEGORIES:
        feed.toggle_ad_category(key[2:])
    elif key == "poll":
        await answer_now(callback, "Читаю канал…")
        from ..context import ai

        result = await guard.poll(callback.bot, ai=ai)
        if result.get("error"):
            note = f"ℹ️ Канал не прочитался: {h(str(result['error']))}."
        else:
            note = (f"Прочитано постов: {result['posts']} · защита от платных: {result['hold']} · рекламы удалено: {result['deleted']} · "
                    f"не тронуто: {result['ad_ok']}")
        await _show_ads(callback, note)
        return
    await answer_now(callback)
    await _show_ads(callback)
