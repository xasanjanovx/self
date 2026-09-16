"""«Джарвис»: свободная команда → действие.

Удалить/исправить операцию, убрать долг «без имени», лимиты, регулярные платежи,
напоминания (в т.ч. видео-уроки по расписанию), совет по еде. Выполняем сразу;
спрашиваем только если нашли несколько подходящих операций или не хватает времени
для напоминания. Любое изменение можно откатить кнопкой «↩️ Отменить».
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import cache
from .. import categories as cats
from .. import emoji as pe
from .. import finance as fin
from .. import nutrition as nutri
from .. import screen as screen_mod
from .. import services
from .. import ui
from ..context import ai, db
from ..keyboards import _btn, t
from ..profile import Profile, h
from ..states import BotStates
from .common import answer_now, get_profile, safe_delete, safe_edit, show_panel, show_progress

router = Router(name="agent")
logger = logging.getLogger(__name__)

# Слова-триггеры: такие фразы идут в командный слой раньше финансового парсера.
_COMMAND_HINTS = (
    "удали", "удалить", "убери", "убрать", "сотри", "отмени последн", "исправ", "поправ", "ошиб", "не правильно", "неправильно",
    "измени", "поменяй", "замени", "лимит", "бюджет", "больше нет", "больше нету", "нету больше", "нет больше",
    "напомни", "напоминай", "напоминание", "отправляй", "присылай", "каждый день", "каждое утро", "каждый вечер", "по будням",
    "что мне поесть", "что поесть", "что съесть", "посоветуй", "чем перекусить", "что приготовить",
    "o'chir", "ochir", "tuzat", "xato", "limit", "eslat", "har kuni", "yubor", "nima yeyin", "nima yesam", "maslahat",
    "не 1", "не 2", "не 3", "не 4", "не 5", "не 6", "не 7", "не 8", "не 9",
)
_NOT_A_COMMAND = ("должен", "qarz", "дал ", "взял", "вернул")


def looks_like_command(text: str) -> bool:
    low = f" {str(text or '').lower()} "
    if not any(k in low for k in _COMMAND_HINTS):
        return False
    # «дал Алишеру 200000, а он вернул» — это операции, а не команда; но «удали …» — команда всегда
    strong = ("удали", "убери", "исправ", "поправ", "ошиб", "измени", "лимит", "напомин", "отправляй", "присылай", "больше нет", "o'chir", "tuzat", "eslat")
    if any(k in low for k in strong):
        return True
    return not any(k in low for k in _NOT_A_COMMAND)


# ------------------------------------------------------------------ helpers
def _undo_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("↩️ " + t(lang, "cancel"), "agent:undo", icon=pe.id_for("🔄")), _btn(t(lang, "to_menu"), "menu:open", icon=pe.ID_HOME)]])


def _menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(t(lang, "to_menu"), "menu:open", style="primary", icon=pe.ID_HOME)]])


async def _say(message: Message, state: FSMContext, profile: Profile, text: str, *, undo: bool = False) -> None:
    await state.clear()
    kb = _undo_kb(profile.lang) if undo else _menu_kb(profile.lang)
    await show_panel(message, state, text, kb)


def _remember_undo(uid: int, payload: dict[str, Any]) -> None:
    cache.put(uid, ("undo",), payload, 1800)


def _entry_line(row: dict[str, Any], lang: str) -> str:
    amount = float(row.get("amount") or 0)
    day = str(row.get("entry_date") or "")[:10]
    try:
        day = date.fromisoformat(day).strftime("%d.%m")
    except Exception:
        pass
    note = fin.clean_note(row.get("note"))
    transfer = fin.transfer_from_note(row.get("note"))
    if transfer:
        return f"{day} · ↔ {fin.fmt_money(amount)} · {fin.transfer_label(transfer[0], transfer[1], lang)}" + (f" · {h(note)}" if note else "")
    sign = "+" if row.get("entry_type") == "income" else "−"
    return f"{day} · {sign}{fin.fmt_money(amount)} · {cats.label(fin.entry_category_key(row), lang)}" + (f" · {h(note)}" if note else "")


def _parse_date(value: Any, today: date) -> date | None:
    v = str(value or "").strip().lower()
    if not v or v == "null":
        return None
    if v == "today":
        return today
    if v == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(v[:10])
    except Exception:
        return None


def _match_entries(entries: list[dict[str, Any]], flt: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Подбор операций под фильтр из команды. Возвращает кандидатов (новые сверху)."""
    kind = str(flt.get("kind") or "any").lower()
    category = cats.normalize(flt.get("category"), "expense") if flt.get("category") and cats.get(str(flt.get("category"))) else None
    if flt.get("category") and not category:
        category = str(flt.get("category")).lower()
    amount = fin.parse_amount(str(flt.get("amount"))) if flt.get("amount") not in (None, "", "null") else None
    amount_val = amount[0] if amount else None
    note = str(flt.get("note") or "").strip().casefold() or None
    day = _parse_date(flt.get("date"), today)
    unnamed = bool(flt.get("unnamed"))

    out = []
    for row in entries:
        transfer = fin.transfer_from_note(row.get("note"))
        row_kind = "transfer" if transfer else str(row.get("entry_type") or "expense")
        if kind in {"lent", "debt"}:
            if not transfer or kind not in transfer:
                continue
        elif kind != "any" and row_kind != kind:
            continue
        if category and not transfer and fin.entry_category_key(row) != category:
            continue
        if amount_val is not None and abs(float(row.get("amount") or 0) - amount_val) > 0.5:
            continue
        clean = (fin.clean_note(row.get("note")) or "").casefold()
        if note and note not in clean:
            continue
        if unnamed and clean:
            continue
        if day is not None and str(row.get("entry_date") or "")[:10] != day.isoformat():
            continue
        out.append(row)
    return out


async def _context(profile: Profile) -> str:
    entries, limits, recurring, rems = (
        await services.finance_entries(profile.telegram_id),
        await services.budgets(profile.telegram_id),
        await services.recurring(profile.telegram_id),
        await services.reminders(profile.telegram_id),
    )
    recent = entries[:12]
    parts = [f"Сегодня {profile.today.isoformat()}, время {profile.now.strftime('%H:%M')}."]
    if recent:
        parts.append("Последние операции: " + "; ".join(_entry_line(r, "ru") for r in recent))
    if limits:
        parts.append("Лимиты: " + ", ".join(f"{k}={fin.fmt_money(v)}" for k, v in limits.items()))
    if recurring:
        parts.append("Регулярные платежи: " + ", ".join(f"{r.get('title')} {fin.fmt_money(float(r.get('amount') or 0))}" for r in recurring))
    if rems:
        parts.append("Напоминания: " + ", ".join(_reminder_title(r) for r in rems))
    return "\n".join(parts)


# ------------------------------------------------------------------ reminders storage helpers
def _reminder_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = str(row.get("reminder_text") or "")
    if raw.startswith("R1:"):
        try:
            return json.loads(raw[3:])
        except Exception:
            pass
    return {"text": raw, "links": [], "idx": 0}


def _reminder_title(row: dict[str, Any]) -> str:
    p = _reminder_payload(row)
    return f"{str(row.get('reminder_time') or '')[:5]} {p.get('text') or ''}".strip()


def reminder_message(row: dict[str, Any]) -> tuple[str, int]:
    """Текст для отправки и следующий индекс ссылки (ротация по кругу)."""
    p = _reminder_payload(row)
    links = [x for x in (p.get("links") or []) if x]
    idx = int(p.get("idx") or 0)
    text = f"⏰ <b>{h(p.get('text') or 'Напоминание')}</b>"
    if links:
        link = links[idx % len(links)]
        text += f"\n{link}"
        if len(links) > 1:
            text += f"\n<i>{idx % len(links) + 1} / {len(links)}</i>"
        return text, (idx + 1) % len(links)
    return text, 0


def _days_from(value: Any) -> list[int]:
    v = value
    if isinstance(v, list):
        out = sorted({int(x) for x in v if str(x).isdigit() and 1 <= int(x) <= 7})
        return out or [1, 2, 3, 4, 5, 6, 7]
    v = str(v or "daily").lower()
    if v == "weekdays":
        return [1, 2, 3, 4, 5]
    if v == "weekend":
        return [6, 7]
    return [1, 2, 3, 4, 5, 6, 7]


_URL_RE = re.compile(r"https?://\S+")


async def _create_reminder(profile: Profile, params: dict[str, Any], time_hhmm: str) -> str:
    links = [str(x) for x in (params.get("links") or []) if x]
    text = str(params.get("text") or "").strip()
    text = _URL_RE.sub("", text).strip(" :—-") or ("Напоминание" if profile.lang != "uz" else "Eslatma")
    days = _days_from(params.get("days"))
    once = str(params.get("days") or "").lower() == "once"
    payload = {"text": text, "links": links, "idx": 0, "once": once, "date": params.get("date")}
    await db.add_reminder(profile.telegram_id, text="R1:" + json.dumps(payload, ensure_ascii=False), reminder_time=time_hhmm, days_of_week=days, tz_name=profile.tz_name)
    services.invalidate_reminders(profile.telegram_id)
    when = {"daily": "каждый день", "weekdays": "по будням", "weekend": "по выходным", "once": "один раз"}.get(str(params.get("days") or "daily").lower(), "каждый день")
    if profile.lang == "uz":
        when = {"каждый день": "har kuni", "по будням": "ish kunlari", "по выходным": "dam olish kunlari", "один раз": "bir marta"}[when]
    extra = f" · {len(links)} {'ta havola, har kuni navbatma-navbat' if profile.lang == 'uz' else ('ссылка' if len(links) == 1 else 'ссылок, по одной в день по кругу')}" if links else ""
    return f"⏰ {'Eslatma yaratildi' if profile.lang == 'uz' else 'Напоминание создано'}: <b>{h(text)}</b> · {when} · {time_hhmm}{extra}"


# ------------------------------------------------------------------ main entry
async def handle_command(message: Message, state: FSMContext, profile: Profile, text: str) -> bool:
    """Возвращает True, если фраза была командой и обработана."""
    await show_progress(message, profile.tr("⏳ Понял, делаю…", "⏳ Tushundim, bajaryapman…"))
    try:
        plan = await ai.plan_command(text, await _context(profile))
    except Exception:
        logger.exception("plan_command failed")
        return False
    action, params, conf = plan["action"], plan["params"], plan["confidence"]
    logger.info("agent: %s conf=%.2f params=%s", action, conf, json.dumps(params, ensure_ascii=False)[:300])
    if action == "none" or conf < 0.4:
        return False
    await safe_delete(message)
    lang = profile.lang
    uid = profile.telegram_id
    today = profile.today

    # «удали без имени из долгов» — это сумма из «Счетов» (base) + операции без имени
    if action == "delete_entry" and params.get("unnamed") and str(params.get("kind") or "") in {"lent", "debt"}:
        action, params = "clear_unnamed_debt", {"side": params.get("kind")}

    # ---------- удалить операцию
    if action == "delete_entry":
        entries = await services.finance_entries(uid)
        found = _match_entries(entries, params, today)
        if not found:
            await _say(message, state, profile, profile.tr("Не нашёл такую операцию.", "Bunday operatsiya topilmadi."))
            return True
        if len(found) > 1 and not params.get("unnamed"):
            await _ask_pick(message, state, profile, found[:8], mode="delete", params=params)
            return True
        rows = found if params.get("unnamed") else found[:1]
        await db.delete_finance_entries(uid, [r["id"] for r in rows])
        cache.invalidate(uid, "fin_entries")
        _remember_undo(uid, {"type": "restore_entries", "rows": rows})
        lines = [f"🗑 {_entry_line(r, lang)}" for r in rows]
        await _say(message, state, profile, profile.tr("Удалил:\n", "O'chirdim:\n") + "\n".join(lines), undo=True)
        return True

    # ---------- исправить операцию
    if action == "edit_entry":
        entries = await services.finance_entries(uid)
        find = params.get("find") if isinstance(params.get("find"), dict) else {}
        changes = params.get("set") if isinstance(params.get("set"), dict) else {}
        found = _match_entries(entries, find, today)
        if not found and find.get("amount") is not None:
            # возможно, сумма «неправильная» стоит в set — попробуем без суммы
            found = _match_entries(entries, {k: v for k, v in find.items() if k != "amount"}, today)
        if not found:
            await _say(message, state, profile, profile.tr("Не нашёл операцию для исправления.", "Tuzatish uchun operatsiya topilmadi."))
            return True
        if len(found) > 1:
            await _ask_pick(message, state, profile, found[:8], mode="edit", params=params)
            return True
        await _apply_edit(message, state, profile, found[0], changes)
        return True

    # ---------- убрать «без имени» из долгов
    if action == "clear_unnamed_debt":
        side = "lent" if str(params.get("side") or "lent") == "lent" else "debt"
        settings_ = dict(await services.finance_settings(uid))
        entries = await services.finance_entries(uid)
        unnamed = [r for r in entries if (tr := fin.transfer_from_note(r.get("note"))) and side in tr and not fin.clean_note(r.get("note"))]
        before_base = float(settings_.get(f"{side}_base") or 0)
        settings_[f"{side}_base"] = 0.0
        await services.save_finance_settings(uid, settings_)
        if unnamed:
            await db.delete_finance_entries(uid, [r["id"] for r in unnamed])
            cache.invalidate(uid, "fin_entries")
        _remember_undo(uid, {"type": "restore_debt", "side": side, "base": before_base, "rows": unnamed})
        label = ("Дал в долг" if side == "lent" else "Мои долги") if lang != "uz" else ("Qarzga berilgan" if side == "lent" else "Mening qarzim")
        await _say(message, state, profile, f"✅ {label}: {'«без имени» убрано' if lang != 'uz' else '«nomsiz» olib tashlandi'} ({fin.fmt_money(before_base + sum(float(r.get('amount') or 0) for r in unnamed))}).", undo=True)
        return True

    # ---------- остаток счёта
    if action == "set_base":
        bucket = fin.normalize_bucket(params.get("bucket"))
        parsed = fin.parse_amount(str(params.get("amount") or ""))
        if parsed is None or bucket == fin.INIT:
            return False
        snap = await services.finance_snapshot(profile)
        live = fin.compute_balances(snap.entries)
        settings_ = dict(snap.settings)
        before = dict(settings_)
        settings_[f"{bucket}_base"] = parsed[0] - live[bucket]
        await services.save_finance_settings(uid, settings_)
        _remember_undo(uid, {"type": "restore_settings", "settings": before})
        await _say(message, state, profile, f"✅ {fin.bucket_label(bucket, lang)}: <b>{fin.fmt_money(parsed[0])} {profile.currency}</b>", undo=True)
        return True

    # ---------- лимиты
    if action in {"set_budget", "remove_budget"}:
        if not await db.ensure_available("budgets"):
            await _say(message, state, profile, profile.tr("Лимиты недоступны: нужна миграция 004.", "Limitlar ishlamaydi: 004 migratsiyasi kerak."))
            return True
        raw_cat = str(params.get("category") or "")
        if action == "remove_budget" and raw_cat.lower() == "all":
            limits = await services.budgets(uid)
            for k in list(limits):
                await services.set_budget(uid, k, 0)
            _remember_undo(uid, {"type": "restore_budgets", "limits": limits})
            await _say(message, state, profile, profile.tr("✅ Все лимиты убраны.", "✅ Barcha limitlar o'chirildi."), undo=True)
            return True
        key = cats.normalize(raw_cat, "expense") if raw_cat else None
        if not key or key == "other" and "проч" not in raw_cat.lower() and "other" != raw_cat.lower():
            await _say(message, state, profile, profile.tr("Не понял категорию. Например: «лимит на еду 700 тыс».", "Kategoriya tushunarsiz. Masalan: «ovqatga limit 700 ming»."))
            return True
        limits = await services.budgets(uid)
        prev = limits.get(key, 0.0)
        if action == "remove_budget":
            await services.set_budget(uid, key, 0)
            _remember_undo(uid, {"type": "restore_budgets", "limits": {key: prev}})
            await _say(message, state, profile, f"✅ {cats.label(key, lang)}: {'лимит убран' if lang != 'uz' else 'limit o`chirildi'}", undo=True)
            return True
        parsed = fin.parse_amount(str(params.get("amount") or ""))
        if parsed is None or parsed[0] <= 0:
            await _say(message, state, profile, profile.tr("Не понял сумму лимита.", "Limit summasi tushunarsiz."))
            return True
        await services.set_budget(uid, key, parsed[0])
        _remember_undo(uid, {"type": "restore_budgets", "limits": {key: prev}})
        statuses = await services.month_budget_statuses(profile)
        st = next((b for b in statuses if b.category == key), None)
        used = f"\n{fin.bar(min(st.ratio, 1.0), 12)} {ui.pct(st.ratio)} · {'sarflandi' if lang == 'uz' else 'потрачено'} {fin.fmt_money(st.spent)}" if st else ""
        await _say(message, state, profile, f"🎯 {cats.label(key, lang)}: {'лимит на месяц' if lang != 'uz' else 'oylik limit'} <b>{fin.fmt_money(parsed[0])} {profile.currency}</b>{used}\n"
                   + ui.muted("Показывается на главном экране и в статистике." if lang != "uz" else "Asosiy ekranda va statistikada ko'rinadi."), undo=True)
        return True

    # ---------- регулярные платежи
    if action in {"clear_recurring", "pause_recurring", "resume_recurring"}:
        items = await services.recurring(uid)
        title = str(params.get("title") or "all").strip().casefold()
        targets = items if title == "all" else [r for r in items if title in str(r.get("title") or "").casefold()]
        if not targets:
            await _say(message, state, profile, profile.tr("Регулярных платежей не найдено.", "Doimiy to'lovlar topilmadi."))
            return True
        if action == "clear_recurring":
            for r in targets:
                await db.delete_recurring(uid, r["id"])
            _remember_undo(uid, {"type": "restore_recurring", "rows": targets})
            msg = profile.tr("✅ Регулярные платежи убраны: ", "✅ Doimiy to'lovlar o'chirildi: ")
        else:
            enabled = action == "resume_recurring"
            for r in targets:
                await db.update_recurring(uid, r["id"], {"enabled": enabled})
            _remember_undo(uid, {"type": "recurring_enabled", "ids": [r["id"] for r in targets], "enabled": not enabled})
            msg = profile.tr("⏸ На паузе: " if not enabled else "▶️ Включены: ", "⏸ Pauza: " if not enabled else "▶️ Yoqildi: ")
        services.invalidate_recurring(uid)
        await _say(message, state, profile, msg + ", ".join(h(r.get("title")) for r in targets), undo=True)
        return True

    # ---------- напоминания
    if action == "add_reminder":
        links = [str(x) for x in (params.get("links") or []) if x] or _URL_RE.findall(text)
        params["links"] = links
        time_hhmm = str(params.get("time") or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", time_hhmm):
            await state.set_state(BotStates.waiting_reminder_time)
            await state.update_data(pending_reminder=params)
            await show_panel(message, state, ui.join(ui.title("⏰", "Eslatma" if lang == "uz" else "Напоминание"),
                                                     ui.card(f"<b>{h(params.get('text') or '')}</b>", [h(x) for x in links] or [ui.muted("—")]),
                                                     f"<b>{'Soat nechada yuboray?' if lang == 'uz' else 'Во сколько присылать?'}</b> " + ui.muted("masalan 20:00" if lang == "uz" else "например 20:00")),
                             _menu_kb(lang))
            return True
        hh, mm = time_hhmm.split(":")
        result = await _create_reminder(profile, params, f"{int(hh):02d}:{int(mm):02d}")
        await _say(message, state, profile, result)
        return True

    if action == "delete_reminder":
        rems = await services.reminders(uid)
        frag = str(params.get("text") or "all").strip().casefold()
        targets = rems if frag == "all" else [r for r in rems if frag in _reminder_title(r).casefold()]
        if not targets:
            await _say(message, state, profile, profile.tr("Напоминаний не найдено.", "Eslatmalar topilmadi."))
            return True
        for r in targets:
            await db.delete_reminder(uid, r["id"])
        services.invalidate_reminders(uid)
        _remember_undo(uid, {"type": "restore_reminders", "rows": targets})
        await _say(message, state, profile, profile.tr("✅ Удалено: ", "✅ O'chirildi: ") + ", ".join(h(_reminder_title(r)) for r in targets), undo=True)
        return True

    if action == "list_reminders":
        rems = await services.reminders(uid)
        body = [f"• {h(_reminder_title(r))}" for r in rems] or [ui.muted("yo'q" if lang == "uz" else "нет")]
        await _say(message, state, profile, ui.card(f"<b>⏰ {'Eslatmalar' if lang == 'uz' else 'Напоминания'}</b>", body))
        return True

    # ---------- питание
    if action == "food_advice":
        ctx = await _nutrition_context(profile)
        try:
            answer = await ai.food_advice(str(params.get("question") or text), ctx, lang)
        except Exception:
            logger.exception("food_advice failed")
            answer = profile.tr("Не смог подобрать сейчас, попробуй позже.", "Hozir tanlay olmadim, keyinroq urining.")
        await _say(message, state, profile, f"{pe.IDEA} {h(answer)}")
        return True

    if action == "log_food":
        from .nutrition import handle_text as nutrition_handle

        await nutrition_handle(message, state, profile, str(params.get("text") or text), reroute=False)
        return True

    if action == "question":
        from .finance import answer_question

        await answer_question(message, profile, str(params.get("question") or text))
        return True

    return False


async def _nutrition_context(profile: Profile) -> str:
    np_, logs = await services.nutrition_profile(profile.telegram_id), await services.today_calorie_logs(profile)
    totals = nutri.totals(logs)
    parts = [f"Время: {profile.now.strftime('%H:%M')}."]
    if np_:
        parts.append(f"План на день: {np_.get('daily_calories')} ккал, белки {np_.get('protein')} г, жиры {np_.get('fat')} г, углеводы {np_.get('carbs')} г. Цель: {np_.get('title')}.")
        left = float(np_.get("daily_calories") or 0) - totals["calories"]
        parts.append(f"Съедено сегодня: {int(totals['calories'])} ккал, Б {int(totals['protein'])} / Ж {int(totals['fat'])} / У {int(totals['carbs'])} г. Осталось: {int(left)} ккал, белка {int(float(np_.get('protein') or 0) - totals['protein'])} г.")
    if logs:
        parts.append("Сегодня ел: " + ", ".join(str(r.get("meal_desc") or "") for r in logs[:8]))
    return "\n".join(parts)


async def _apply_edit(message: Message | CallbackQuery, state: FSMContext, profile: Profile, row: dict[str, Any], changes: dict[str, Any]) -> None:
    fields: dict[str, Any] = {}
    if changes.get("amount") not in (None, "", "null"):
        parsed = fin.parse_amount(str(changes.get("amount")))
        if parsed:
            fields["amount"] = parsed[0]
    if changes.get("category") and cats.get(cats.normalize(str(changes["category"]), "income" if row.get("entry_type") == "income" else "expense")):
        fields["category"] = cats.normalize(str(changes["category"]), "income" if row.get("entry_type") == "income" else "expense")
    if changes.get("note") not in (None, "", "null"):
        transfer = fin.transfer_from_note(row.get("note"))
        fields["note"] = fin.note_with_transfer(str(changes["note"]), *transfer) if transfer else fin.note_with_bucket(str(changes["note"]), fin.bucket_from_note(row.get("note")))
    if not fields:
        text = profile.tr("Не понял, что именно изменить.", "Nimani o'zgartirishni tushunmadim.")
        if isinstance(message, CallbackQuery):
            await safe_edit(message, text, _menu_kb(profile.lang))
        else:
            await _say(message, state, profile, text)
        return
    before = {k: row.get(k) for k in fields}
    await db.update_finance_entry(profile.telegram_id, row["id"], fields)
    cache.invalidate(profile.telegram_id, "fin_entries")
    _remember_undo(profile.telegram_id, {"type": "restore_fields", "entry_id": row["id"], "fields": before})
    after = {**row, **fields}
    text = f"✏️ {_entry_line(row, profile.lang)}\n→ {_entry_line(after, profile.lang)}"
    await state.clear()
    if isinstance(message, CallbackQuery):
        await safe_edit(message, text, _undo_kb(profile.lang))
    else:
        await show_panel(message, state, text, _undo_kb(profile.lang))


async def _ask_pick(message: Message, state: FSMContext, profile: Profile, found: list[dict[str, Any]], *, mode: str, params: dict[str, Any]) -> None:
    lang = profile.lang
    rows = [[_btn(_entry_line(r, lang)[:60], f"agent:pick:{i}")] for i, r in enumerate(found)]
    rows.append([_btn(t(lang, "cancel"), "menu:open", style="danger", icon=pe.ID_CANCEL)])
    await state.set_state(BotStates.waiting_agent_pick)
    await state.update_data(agent_pick={"mode": mode, "ids": [r["id"] for r in found], "params": params})
    title = ("Qaysi birini o'chiray?" if mode == "delete" else "Qaysi birini tuzatay?") if lang == "uz" else ("Какую удалить?" if mode == "delete" else "Какую исправить?")
    await show_panel(message, state, f"🤔 <b>{title}</b>", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("agent:pick:"))
async def cb_pick(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    data = (await state.get_data()).get("agent_pick") or {}
    try:
        idx = int(callback.data.split(":")[-1])
        entry_id = data["ids"][idx]
    except Exception:
        await answer_now(callback)
        return
    await answer_now(callback)
    row = await db.get_finance_entry(profile.telegram_id, entry_id)
    if not row:
        await safe_edit(callback, profile.tr("Операция уже удалена.", "Operatsiya allaqachon o'chirilgan."), _menu_kb(profile.lang))
        return
    if data.get("mode") == "delete":
        await db.delete_finance_entries(profile.telegram_id, [entry_id])
        cache.invalidate(profile.telegram_id, "fin_entries")
        _remember_undo(profile.telegram_id, {"type": "restore_entries", "rows": [row]})
        await state.clear()
        await safe_edit(callback, profile.tr("Удалил:\n", "O'chirdim:\n") + f"🗑 {_entry_line(row, profile.lang)}", _undo_kb(profile.lang))
        return
    changes = (data.get("params") or {}).get("set") or {}
    await _apply_edit(callback, state, profile, row, changes)


@router.message(BotStates.waiting_reminder_time, F.text)
async def msg_reminder_time(message: Message, state: FSMContext) -> None:
    profile = await get_profile(message.from_user)
    raw = (message.text or "").strip().replace(".", ":").replace(" ", "")
    await safe_delete(message)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", raw)
    params = (await state.get_data()).get("pending_reminder") or {}
    if not m or not params:
        await show_panel(message, state, profile.tr("Напиши время, например <code>20:00</code>", "Vaqtni yozing, masalan <code>20:00</code>"), _menu_kb(profile.lang))
        return
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        await show_panel(message, state, profile.tr("Время от 00:00 до 23:59", "Vaqt 00:00 dan 23:59 gacha"), _menu_kb(profile.lang))
        return
    result = await _create_reminder(profile, params, f"{hh:02d}:{mm:02d}")
    await _say(message, state, profile, result)


@router.message(BotStates.waiting_reminder_time)
async def msg_reminder_time_other(message: Message) -> None:
    await safe_delete(message)


@router.callback_query(F.data == "agent:undo")
async def cb_undo(callback: CallbackQuery, state: FSMContext) -> None:
    profile = await get_profile(callback.from_user)
    uid = profile.telegram_id
    payload = cache.get(uid, ("undo",))
    if not payload:
        await answer_now(callback, profile.tr("Отменять нечего", "Bekor qiladigan narsa yo'q"), alert=True)
        return
    await answer_now(callback, profile.tr("Отменено ↩️", "Bekor qilindi ↩️"))
    cache.put(uid, ("undo",), None, 1)
    kind = payload.get("type")
    try:
        if kind == "restore_entries":
            rows = payload.get("rows") or []
            for r in rows:
                await db.add_finance_entries(uid, [{"entry_type": r.get("entry_type"), "amount": r.get("amount"), "category": r.get("category"), "note": r.get("note"), "source": r.get("source") or "restored"}],
                                             entry_date=date.fromisoformat(str(r.get("entry_date"))[:10]))
            cache.invalidate(uid, "fin_entries")
        elif kind == "restore_fields":
            await db.update_finance_entry(uid, payload["entry_id"], payload["fields"])
            cache.invalidate(uid, "fin_entries")
        elif kind == "restore_debt":
            settings_ = dict(await services.finance_settings(uid))
            settings_[f"{payload['side']}_base"] = payload.get("base") or 0.0
            await services.save_finance_settings(uid, settings_)
            for r in payload.get("rows") or []:
                await db.add_finance_entries(uid, [{"entry_type": r.get("entry_type"), "amount": r.get("amount"), "category": r.get("category"), "note": r.get("note"), "source": "restored"}],
                                             entry_date=date.fromisoformat(str(r.get("entry_date"))[:10]))
            cache.invalidate(uid, "fin_entries")
        elif kind == "restore_settings":
            await services.save_finance_settings(uid, payload["settings"])
        elif kind == "restore_budgets":
            for k, v in (payload.get("limits") or {}).items():
                await services.set_budget(uid, k, float(v or 0))
        elif kind == "restore_recurring":
            for r in payload.get("rows") or []:
                await db.add_recurring(uid, title=r.get("title"), amount=float(r.get("amount") or 0), category=r.get("category") or "home", bucket=r.get("bucket") or "card", day_of_month=int(r.get("day_of_month") or 1))
            services.invalidate_recurring(uid)
        elif kind == "recurring_enabled":
            for rid in payload.get("ids") or []:
                await db.update_recurring(uid, rid, {"enabled": bool(payload.get("enabled"))})
            services.invalidate_recurring(uid)
        elif kind == "restore_reminders":
            for r in payload.get("rows") or []:
                await db.add_reminder(uid, text=r.get("reminder_text") or "", reminder_time=str(r.get("reminder_time") or "20:00")[:5], days_of_week=r.get("days_of_week") or [1, 2, 3, 4, 5, 6, 7], tz_name=r.get("timezone") or profile.tz_name)
            services.invalidate_reminders(uid)
    except Exception:
        logger.exception("undo failed")
        await safe_edit(callback, profile.tr("Не удалось отменить.", "Bekor qilib bo'lmadi."), _menu_kb(profile.lang))
        return
    from .menu import build_dashboard
    from ..keyboards import main_menu_keyboard

    await state.clear()
    if callback.message is not None:
        await screen_mod.drop_chart(callback.bot, callback.message.chat.id)
    await safe_edit(callback, await build_dashboard(profile), main_menu_keyboard(profile.lang))
