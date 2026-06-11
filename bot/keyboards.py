from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import emoji as _pe

Lang = str


def _btn(
    text: str,
    callback_data: str | None = None,
    *,
    url: str | None = None,
    style: str | None = None,
    icon: str | None = None,
) -> InlineKeyboardButton:
    """Build an inline button with optional Bot API 9.4 color style and a
    premium-emoji icon. aiogram passes unknown fields through, so this works
    without a library upgrade."""
    kwargs: dict[str, object] = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style:
        kwargs["style"] = style
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(**kwargs)


_LEADING_SYMBOLS = re.compile(r"^[^\w(]+", re.UNICODE)


def _label(text: str) -> str:
    """Strip a leading unicode emoji so a button icon doesn't double up."""
    cleaned = _LEADING_SYMBOLS.sub("", text).strip()
    return cleaned or text

TEXTS: dict[Lang, dict[str, str]] = {
    "ru": {
        "menu_nutrition": "🍽️ Питание",
        "menu_finance": "💰 Финансы",
        "menu_habits": "✅ Привычки",
        "menu_goals": "🎯 Цели",
        "menu_trainer": "🏋️ Тренер",
        "menu_report": "📊 Отчет",
        "menu_vacancy": "📣 Вакансии",
        "menu_ai": "🤖 AI",
        "menu_language": "🌐 Язык",
        "menu_refresh": "🔄 Обновить",
        "menu_analytics": "📊 Аналитика",
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
        "calorie_meals": "🍽️ Приемы",
        "finance_settings": "⚙️ Настройки",
        "finance_ops": "📂 Операции",
        "finance_set_card": "💳 Карта",
        "finance_set_cash": "💵 Наличные",
        "finance_set_lent": "🤝 Дал в долг",
        "finance_set_debt": "📌 Мои долги",
        "finance_set_credit": "🏦 Кредит/мес",
        "finance_set_back_settings": "⬅️ К настройкам",
        "period_day": "📅 День",
        "period_week": "🗓️ Неделя",
        "period_month": "📆 Месяц",
        "report_weekly": "🔔 Раз в неделю",
        "report_monthly": "🗓️ Раз в месяц",
        "report_off": "⛔ Выключить",
        "report_view_week": "📅 Неделя",
        "report_view_month": "🗓️ Месяц",
        "report_view_all": "🧾 Всё",
        "report_status_on": "Сейчас: включено",
        "report_status_off": "Сейчас: выключено",
        "lang_ru": "Русский",
        "lang_uz": "O'zbekcha",
        "trainer_fat": "🔥 Сжечь жир",
        "trainer_muscle": "💪 Набор мышц",
        "trainer_cardio": "🏃 Кардио",
        "trainer_mobility": "🧘 Мобилити",
        "trainer_ask": "✍️ Спросить тренера",
        "vacancy_again": "📣 Еще вакансия",
        "vacancy_contact": "📩 Связаться",
        "vacancy_publish": "📢 @ishdasiz ga e'lon qilish",
        "delete_reminder": "Удалить • {time} {text}",
    },
    "uz": {
        "menu_nutrition": "🍽️ Oziqlanish",
        "menu_finance": "💰 Moliya",
        "menu_habits": "✅ Odatlar",
        "menu_goals": "🎯 Maqsadlar",
        "menu_trainer": "🏋️ Trener",
        "menu_report": "📊 Hisobot",
        "menu_vacancy": "📣 Vakansiya",
        "menu_ai": "🤖 AI",
        "menu_language": "🌐 Til",
        "menu_refresh": "🔄 Yangilash",
        "menu_analytics": "📊 Tahlil",
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
        "calorie_meals": "🍽️ Qabullar",
        "finance_settings": "⚙️ Sozlamalar",
        "finance_ops": "📂 Operatsiyalar",
        "finance_set_card": "💳 Karta",
        "finance_set_cash": "💵 Naqd",
        "finance_set_lent": "🤝 Qarzga berilgan",
        "finance_set_debt": "📌 Mening qarzim",
        "finance_set_credit": "🏦 Kredit/oy",
        "finance_set_back_settings": "⬅️ Sozlamalarga",
        "period_day": "📅 Kun",
        "period_week": "🗓️ Hafta",
        "period_month": "📆 Oy",
        "report_weekly": "🔔 Haftada bir marta",
        "report_monthly": "🗓️ Oyda bir marta",
        "report_off": "⛔ O'chirish",
        "report_view_week": "📅 Hafta",
        "report_view_month": "🗓️ Oy",
        "report_view_all": "🧾 Hammasi",
        "report_status_on": "Hozir: yoqilgan",
        "report_status_off": "Hozir: o'chirilgan",
        "lang_ru": "Русский",
        "lang_uz": "O'zbekcha",
        "trainer_fat": "🔥 Yog' yoqish",
        "trainer_muscle": "💪 Mushak yig'ish",
        "trainer_cardio": "🏃 Kardio",
        "trainer_mobility": "🧘 Mobiliti",
        "trainer_ask": "✍️ Trenerga savol",
        "vacancy_again": "📣 Yana vakansiya",
        "vacancy_contact": "📩 Bog'lanish",
        "vacancy_publish": "📢 @ishdasiz ga e'lon qilish",
        "delete_reminder": "O'chirish • {time} {text}",
    },
}


def _t(lang: str, key: str, **kwargs: object) -> str:
    data = TEXTS.get(lang if lang in TEXTS else "ru", TEXTS["ru"])
    template = data.get(key) or TEXTS["ru"].get(key) or key
    return template.format(**kwargs)


def _money(value: float) -> str:
    return f"{float(value):,.0f}".replace(",", " ")


def main_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    P = "primary"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(_label(_t(lang, "menu_nutrition")), "menu:calorie", style=P, icon=_pe.ID_NUTRITION),
                _btn(_label(_t(lang, "menu_finance")), "menu:finance", style=P, icon=_pe.ID_FINANCE),
            ],
            [
                _btn(_label(_t(lang, "menu_habits")), "menu:habits", style=P, icon=_pe.ID_HABITS),
                _btn(_label(_t(lang, "menu_goals")), "menu:goals", style=P, icon=_pe.ID_GOALS),
            ],
            [
                _btn(_label(_t(lang, "menu_trainer")), "menu:trainer", style=P, icon=_pe.ID_TRAINER),
                _btn(_label(_t(lang, "menu_report")), "menu:report", style=P, icon=_pe.ID_REPORT),
            ],
            [
                _btn(_label(_t(lang, "menu_vacancy")), "menu:vacancy", style=P, icon=_pe.ID_VACANCY),
                _btn(_label(_t(lang, "menu_language")), "menu:language", style=P, icon=_pe.ID_LANGUAGE),
            ],
            [
                _btn(_label(_t(lang, "menu_analytics")), "menu:dashboard", style="success", icon=_pe.ID_ANALYTICS),
                _btn(_label(_t(lang, "menu_refresh")), "menu:open", icon=_pe.ID_REFRESH),
            ],
        ]
    )


def nutrition_goal_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(_t(lang, "goal_loss"), "nutri:set:loss", style="primary"),
                _btn(_t(lang, "goal_maintain"), "nutri:set:maintain", style="primary"),
            ],
            [
                _btn(_t(lang, "goal_gain"), "nutri:set:gain", style="primary"),
                _btn(_t(lang, "goal_muscle"), "nutri:set:muscle", style="primary"),
            ],
            [_btn(_t(lang, "goal_custom"), "nutri:set:custom", icon=_pe.ID_EDIT)],
            [_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)],
        ]
    )


def calorie_confirm_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(_label(_t(lang, "save")), "calorie:confirm", style="success", icon=_pe.ID_SAVE),
                _btn(_label(_t(lang, "cancel")), "calorie:cancel", style="danger", icon=_pe.ID_CANCEL),
            ],
            [_btn(_label(_t(lang, "back")), "calorie:panel", icon=_pe.ID_BACK)],
        ]
    )


def calorie_panel_keyboard(entries: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([_btn(_label(_t(lang, "calorie_goal")), "calorie:goals", style="primary", icon=_pe.ID_GOAL)])
    rows.append([_btn(_label(_t(lang, "calorie_meals")), "calorie:meals:day", style="primary", icon=_pe.ID_NUTRITION)])
    rows.append([_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calorie_meals_keyboard(entries: list[dict], period: str, lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=_t(lang, "period_day"), callback_data="calorie:meals:day"),
            InlineKeyboardButton(text=_t(lang, "period_week"), callback_data="calorie:meals:week"),
            InlineKeyboardButton(text=_t(lang, "period_month"), callback_data="calorie:meals:month"),
        ]
    ]

    for entry in entries[:12]:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        desc = str(entry.get("meal_desc") or _t(lang, "meal_default")).strip()
        kcal = entry.get("calories")
        kcal_text = f"{int(float(kcal))} ккал" if kcal is not None else _t(lang, "kcal_none")
        title = f"{desc[:28]} • {kcal_text}"[:64]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"calorie:view:{entry_id}")])

    rows.append([_btn(_label(_t(lang, "back")), "calorie:panel", icon=_pe.ID_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calorie_detail_keyboard(log_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(_label(_t(lang, "delete")), f"calorie:ask_del:{log_id}", style="danger", icon=_pe.ID_DELETE)],
            [_btn(_label(_t(lang, "back")), "calorie:panel", icon=_pe.ID_BACK)],
        ]
    )


def calorie_delete_confirm_keyboard(log_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(_label(_t(lang, "yes_delete")), f"calorie:del:{log_id}", style="danger", icon=_pe.ID_DELETE),
                _btn(_label(_t(lang, "no")), f"calorie:view:{log_id}", icon=_pe.ID_CANCEL),
            ],
            [_btn(_label(_t(lang, "back")), "calorie:panel", icon=_pe.ID_BACK)],
        ]
    )


def finance_panel_keyboard(entries: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            _btn(_label(_t(lang, "finance_ops")), "finance:ops:day", style="primary", icon=_pe.ID_REPORT),
            _btn(_label(_t(lang, "finance_settings")), "finance:settings", style="primary", icon=_pe.ID_SETTINGS),
        ]
    )
    rows.append([_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_settings_keyboard(settings: dict[str, float], currency: str, lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{_t(lang, 'finance_set_card')} • {_money(float(settings.get('card_base') or 0))} {currency}"[:64],
                callback_data="finance:set:card",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{_t(lang, 'finance_set_cash')} • {_money(float(settings.get('cash_base') or 0))} {currency}"[:64],
                callback_data="finance:set:cash",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{_t(lang, 'finance_set_lent')} • {_money(float(settings.get('lent_base') or 0))} {currency}"[:64],
                callback_data="finance:set:lent",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{_t(lang, 'finance_set_debt')} • {_money(float(settings.get('debt_base') or 0))} {currency}"[:64],
                callback_data="finance:set:debt",
            )
        ],
        [
            InlineKeyboardButton(
                text=(
                    f"{_t(lang, 'finance_set_credit')} • "
                    f"{_money(float(settings.get('monthly_credit_payment') or 0))} {currency}"
                )[:64],
                callback_data="finance:set:credit",
            )
        ],
        [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:finance")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_setting_input_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_t(lang, "finance_set_back_settings"), callback_data="finance:settings")],
            [InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:finance")],
        ]
    )


def finance_operations_keyboard(entries: list[dict], period: str, lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=_t(lang, "period_day"), callback_data="finance:ops:day"),
            InlineKeyboardButton(text=_t(lang, "period_week"), callback_data="finance:ops:week"),
            InlineKeyboardButton(text=_t(lang, "period_month"), callback_data="finance:ops:month"),
        ]
    ]

    for entry in entries[:12]:
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

    rows.append([InlineKeyboardButton(text=_t(lang, "back"), callback_data="menu:finance")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_detail_keyboard(entry_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(_label(_t(lang, "delete")), f"finance:ask_del:{entry_id}", style="danger", icon=_pe.ID_DELETE)],
            [_btn(_label(_t(lang, "back")), "menu:finance", icon=_pe.ID_BACK)],
        ]
    )


def finance_delete_confirm_keyboard(entry_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(_label(_t(lang, "yes_delete")), f"finance:del:{entry_id}", style="danger", icon=_pe.ID_DELETE),
                _btn(_label(_t(lang, "no")), f"finance:view:{entry_id}", icon=_pe.ID_CANCEL),
            ],
            [_btn(_label(_t(lang, "back")), "menu:finance", icon=_pe.ID_BACK)],
        ]
    )


def finance_add_confirm_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(_label(_t(lang, "save")), "finance:add_confirm", style="success", icon=_pe.ID_SAVE),
                _btn(_label(_t(lang, "cancel")), "finance:add_cancel", style="danger", icon=_pe.ID_CANCEL),
            ],
            [_btn(_label(_t(lang, "back")), "menu:finance", icon=_pe.ID_BACK)],
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
            _btn(_label(_t(lang, "add")), "habit:add", style="success", icon=_pe.ID_ADD),
            _btn(_label(_t(lang, "refresh")), "menu:habits", icon=_pe.ID_REFRESH),
        ]
    )
    rows.append([_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)])

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


def report_settings_keyboard(
    lang: str = "ru",
    *,
    frequency: str = "weekly",
    enabled: bool = True,
    period: str = "week",
) -> InlineKeyboardMarkup:
    if enabled:
        current_label = _t(lang, "report_weekly") if frequency == "weekly" else _t(lang, "report_monthly")
        status = f"{_t(lang, 'report_status_on')} · {current_label}"
    else:
        status = _t(lang, "report_status_off")

    period = period if period in {"week", "month", "all"} else "week"
    week_text = _t(lang, "report_view_week")
    month_text = _t(lang, "report_view_month")
    all_text = _t(lang, "report_view_all")
    if period == "week":
        week_text = f"✅ {week_text}"
    elif period == "month":
        month_text = f"✅ {month_text}"
    else:
        all_text = f"✅ {all_text}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=week_text, callback_data="report:view:week"),
                InlineKeyboardButton(text=month_text, callback_data="report:view:month"),
                InlineKeyboardButton(text=all_text, callback_data="report:view:all"),
            ],
            [InlineKeyboardButton(text=status, callback_data="noop")],
            [
                _btn(_label(_t(lang, "report_weekly")), "report:set:weekly", style="primary", icon=_pe.ID_REFRESH),
                _btn(_label(_t(lang, "report_monthly")), "report:set:monthly", style="primary", icon=_pe.ID_CALENDAR),
            ],
            [_btn(_label(_t(lang, "report_off")), "report:set:off", style="danger", icon=_pe.ID_CANCEL)],
            [_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)],
        ]
    )


def language_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_t(lang, "lang_ru"), callback_data="lang:set:ru"),
                InlineKeyboardButton(text=_t(lang, "lang_uz"), callback_data="lang:set:uz"),
            ],
            [_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)],
        ]
    )


def trainer_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(_label(_t(lang, "trainer_fat")), "trainer:plan:fat", style="primary", icon=_pe.ID_FIRE),
                _btn(_label(_t(lang, "trainer_muscle")), "trainer:plan:muscle", style="primary", icon=_pe.ID_TRAINER),
            ],
            [_btn(_label(_t(lang, "trainer_ask")), "trainer:ask", icon=_pe.ID_EDIT)],
            [_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)],
        ]
    )


def vacancy_panel_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)]]
    )


def vacancy_result_keyboard(
    lang: str = "ru",
    contact_url: str | None = None,
    *,
    show_publish: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if contact_url:
        rows.append([_btn(_label(_t(lang, "vacancy_contact")), url=contact_url, style="primary")])
    if show_publish:
        rows.append([_btn(_label(_t(lang, "vacancy_publish")), "vacancy:publish", style="success", icon=_pe.ID_SAVE)])
    rows.append([_btn(_label(_t(lang, "vacancy_again")), "vacancy:again", icon=_pe.ID_REFRESH)])
    rows.append([_btn(_label(_t(lang, "back")), "menu:open", icon=_pe.ID_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vacancy_channel_keyboard(lang: str = "ru", contact_url: str | None = None) -> InlineKeyboardMarkup | None:
    if not contact_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_t(lang, "vacancy_contact"), url=contact_url)]]
    )


def back_to_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(_label(_t(lang, "to_menu")), "menu:open", style="primary", icon=_pe.ID_HOME)]]
    )
