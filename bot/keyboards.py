from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

Lang = str

TEXTS: dict[Lang, dict[str, str]] = {
    "ru": {
        "menu_nutrition": "🍽️ Питание",
        "menu_finance": "💰 Финансы",
        "menu_habits": "✅ Привычки",
        "menu_goals": "🎯 Цели",
        "menu_trainer": "🏋️ Тренер",
        "menu_report": "📊 Отчет",
        "menu_language": "🌐 Язык",
        "back": "⬅️ Назад",
        "to_menu": "🏠 В меню",
        "save": "✅ Сохранить",
        "cancel": "✖️ Отменить",
        "delete": "🗑️ Удалить",
        "yes_delete": "Да, удалить",
        "no": "Нет",
        "add": "➕ Добавить",
        "refresh": "🔄 Обновить",
        "done_today": "Выполнено сегодня: {count}",
        "meal_default": "Блюдо",
        "kcal_none": "без ккал",
        "finance_other": "прочее",
        "goal_loss": "Снижение",
        "goal_maintain": "Поддержание",
        "goal_gain": "Набор",
        "goal_muscle": "Масса",
        "goal_custom": "Ручной план",
        "calorie_goal": "🎯 Цель и профиль",
        "finance_settings": "⚙️ Настройки",
        "report_weekly": "🔔 Раз в неделю",
        "report_monthly": "🗓️ Раз в месяц",
        "report_off": "⛔ Выключить",
        "report_status_on": "Сейчас: включено",
        "report_status_off": "Сейчас: выключено",
        "lang_ru": "Русский",
        "lang_uz": "O'zbekcha",
        "trainer_fat": "🔥 Сжечь жир",
        "trainer_muscle": "💪 Набор мышц",
        "trainer_cardio": "🏃 Кардио",
        "trainer_mobility": "🧘 Мобилити",
        "trainer_ask": "✍️ Спросить тренера",
        "delete_reminder": "Удалить • {time} {text}",
    },
    "uz": {
        "menu_nutrition": "🍽️ Oziqlanish",
        "menu_finance": "💰 Moliya",
        "menu_habits": "✅ Odatlar",
        "menu_goals": "🎯 Maqsadlar",
        "menu_trainer": "🏋️ Trener",
        "menu_report": "📊 Hisobot",
        "menu_language": "🌐 Til",
        "back": "⬅️ Ortga",
        "to_menu": "🏠 Menyu",
        "save": "✅ Saqlash",
        "cancel": "✖️ Bekor qilish",
        "delete": "🗑️ O'chirish",
        "yes_delete": "Ha, o'chirish",
        "no": "Yo'q",
        "add": "➕ Qo'shish",
        "refresh": "🔄 Yangilash",
        "done_today": "Bugun bajarildi: {count}",
        "meal_default": "Taom",
        "kcal_none": "kkalsiz",
        "finance_other": "boshqa",
        "goal_loss": "Kamayish",
        "goal_maintain": "Ushlab turish",
        "goal_gain": "Vazn yig'ish",
        "goal_muscle": "Mushak",
        "goal_custom": "Qo'lda reja",
        "calorie_goal": "🎯 Maqsad va profil",
        "finance_settings": "⚙️ Sozlamalar",
        "report_weekly": "🔔 Haftada bir marta",
        "report_monthly": "🗓️ Oyda bir marta",
        "report_off": "⛔ O'chirish",
        "report_status_on": "Hozir: yoqilgan",
        "report_status_off": "Hozir: o'chirilgan",
        "lang_ru": "Русский",
        "lang_uz": "O'zbekcha",
        "trainer_fat": "🔥 Yog' yoqish",
        "trainer_muscle": "💪 Mushak yig'ish",
        "trainer_cardio": "🏃 Kardio",
        "trainer_mobility": "🧘 Mobiliti",
        "trainer_ask": "✍️ Trenerga savol",
        "delete_reminder": "O'chirish • {time} {text}",
    },
}


def _t(lang: str, key: str, **kwargs: object) -> str:
    data = TEXTS.get(lang if lang in TEXTS else "ru", TEXTS["ru"])
    template = data.get(key) or TEXTS["ru"].get(key) or key
    return template.format(**kwargs)


def main_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "menu_nutrition"), callback_data="menu:calorie"),
                InlineKeyboardButton(text=_t(lang, "menu_finance"), callback_data="menu:finance"),
            ],
            [
                InlineKeyboardButton(text=_t(lang, "menu_habits"), callback_data="menu:habits"),
                InlineKeyboardButton(text=_t(lang, "menu_goals"), callback_data="menu:goals"),
            ],
            [
                InlineKeyboardButton(text=_t(lang, "menu_trainer"), callback_data="menu:trainer"),
                InlineKeyboardButton(text=_t(lang, "menu_report"), callback_data="menu:report"),
            ],
            [InlineKeyboardButton(text=_t(lang, "menu_language"), callback_data="menu:language")],
        ]
    )


def nutrition_goal_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "goal_loss"), callback_data="nutri:set:loss"),
                InlineKeyboardButton(text=_t(lang, "goal_maintain"), callback_data="nutri:set:maintain"),
            ],
            [
                InlineKeyboardButton(text=_t(lang, "goal_gain"), callback_data="nutri:set:gain"),
                InlineKeyboardButton(text=_t(lang, "goal_muscle"), callback_data="nutri:set:muscle"),
            ],
            [InlineKeyboardButton(text=_t(lang, "goal_custom"), callback_data="nutri:set:custom")],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")],
        ]
    )


def calorie_confirm_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "save"), callback_data="calorie:confirm"),
                InlineKeyboardButton(text=_t(lang, "cancel"), callback_data="calorie:cancel"),
            ],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="calorie:panel")],
        ]
    )


def calorie_panel_keyboard(entries: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text=_t(lang, "calorie_goal"), callback_data="calorie:goals")])
    for entry in entries[:6]:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        desc = str(entry.get("meal_desc") or _t(lang, "meal_default")).strip()
        kcal = entry.get("calories")
        kcal_text = f"{int(float(kcal))} ккал" if kcal is not None else _t(lang, "kcal_none")
        title = f"{desc[:24]} • {kcal_text}"[:64]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"calorie:view:{entry_id}")])

    rows.append([InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calorie_detail_keyboard(log_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_t(lang, "delete"), callback_data=f"calorie:ask_del:{log_id}")],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="calorie:panel")],
        ]
    )


def calorie_delete_confirm_keyboard(log_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "yes_delete"), callback_data=f"calorie:del:{log_id}"),
                InlineKeyboardButton(text=_t(lang, "no"), callback_data=f"calorie:view:{log_id}"),
            ],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="calorie:panel")],
        ]
    )


def finance_panel_keyboard(entries: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries[:8]:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        amount = float(entry.get("amount") or 0)
        category = str(entry.get("category") or _t(lang, "finance_other")).strip()
        note_raw = str(entry.get("note") or "").strip().lower()
        transfer_match = re.match(r"^\[x:(card|cash|lent|debt)>(card|cash|lent|debt)\]\s*", note_raw)
        if transfer_match:
            title = f"↔ {amount:,.0f} {category}".replace(",", " ")
        else:
            sign = "+" if str(entry.get("entry_type")) == "income" else "-"
            title = f"{sign}{amount:,.0f} {category}".replace(",", " ")
        rows.append([InlineKeyboardButton(text=title[:64], callback_data=f"finance:view:{entry_id}")])

    rows.append([InlineKeyboardButton(text=_t(lang, "finance_settings"), callback_data="finance:settings")])
    rows.append([InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_detail_keyboard(entry_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_t(lang, "delete"), callback_data=f"finance:ask_del:{entry_id}")],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:finance")],
        ]
    )


def finance_delete_confirm_keyboard(entry_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "yes_delete"), callback_data=f"finance:del:{entry_id}"),
                InlineKeyboardButton(text=_t(lang, "no"), callback_data=f"finance:view:{entry_id}"),
            ],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:finance")],
        ]
    )


def finance_add_confirm_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "save"), callback_data="finance:add_confirm"),
                InlineKeyboardButton(text=_t(lang, "cancel"), callback_data="finance:add_cancel"),
            ],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:finance")],
        ]
    )


def habits_keyboard(habits: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    pending = [habit for habit in habits if not habit.get("completed_today")]
    done = [habit for habit in habits if habit.get("completed_today")]

    for habit in pending[:8]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(habit.get("name") or "").strip()[:48] or "Habit",
                    callback_data=f"habit:done:{habit['id']}",
                )
            ]
        )

    if done:
        rows.append([InlineKeyboardButton(text=_t(lang, "done_today", count=len(done)), callback_data="noop")])

    rows.append(
        [
            InlineKeyboardButton(text=_t(lang, "add"), callback_data="habit:add"),
            InlineKeyboardButton(text=_t(lang, "refresh"), callback_data="menu:habits"),
        ]
    )
    rows.append([InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminders_keyboard(reminders: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for reminder in reminders[:8]:
        rid = reminder.get("id")
        rtime = str(reminder.get("reminder_time") or "")[:5]
        text = str(reminder.get("reminder_text") or "").strip()
        title = _t(lang, "delete_reminder", time=rtime, text=text)[:60]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"rem:del:{rid}")])

    rows.append(
        [
            InlineKeyboardButton(text=_t(lang, "add"), callback_data="rem:add"),
            InlineKeyboardButton(text=_t(lang, "refresh"), callback_data="menu:reminders"),
        ]
    )
    rows.append([InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_settings_keyboard(lang: str = "ru", *, frequency: str = "weekly", enabled: bool = True) -> InlineKeyboardMarkup:
    if enabled:
        current_label = _t(lang, "report_weekly") if frequency == "weekly" else _t(lang, "report_monthly")
        status = f"{_t(lang, 'report_status_on')} · {current_label}"
    else:
        status = _t(lang, "report_status_off")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=status, callback_data="noop")],
            [
                InlineKeyboardButton(text=_t(lang, "report_weekly"), callback_data="report:set:weekly"),
                InlineKeyboardButton(text=_t(lang, "report_monthly"), callback_data="report:set:monthly"),
            ],
            [InlineKeyboardButton(text=_t(lang, "report_off"), callback_data="report:set:off")],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")],
        ]
    )


def language_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "lang_ru"), callback_data="lang:set:ru"),
                InlineKeyboardButton(text=_t(lang, "lang_uz"), callback_data="lang:set:uz"),
            ],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")],
        ]
    )


def trainer_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "trainer_fat"), callback_data="trainer:plan:fat"),
                InlineKeyboardButton(text=_t(lang, "trainer_muscle"), callback_data="trainer:plan:muscle"),
            ],
            [InlineKeyboardButton(text=_t(lang, "trainer_ask"), callback_data="trainer:ask")],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:open")],
        ]
    )


def back_to_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_t(lang, "to_menu"), callback_data="menu:open")]]
    )
