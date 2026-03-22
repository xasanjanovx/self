from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Калории", callback_data="menu:calorie"),
                InlineKeyboardButton(text="Финансы", callback_data="menu:finance"),
            ],
            [
                InlineKeyboardButton(text="Привычки", callback_data="menu:habits"),
                InlineKeyboardButton(text="Отметка", callback_data="menu:checkin"),
            ],
            [
                InlineKeyboardButton(text="Отчет", callback_data="menu:weekly"),
                InlineKeyboardButton(text="AI", callback_data="menu:ai"),
            ],
            [
                InlineKeyboardButton(text="Цели", callback_data="menu:goals"),
                InlineKeyboardButton(text="Напоминания", callback_data="menu:reminders"),
            ],
            [
                InlineKeyboardButton(text="Экспорт", callback_data="menu:export"),
                InlineKeyboardButton(text="Обновить", callback_data="menu:open"),
            ],
        ]
    )


def nutrition_goal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Снижение веса", callback_data="nutri:set:loss"),
                InlineKeyboardButton(text="Поддержание", callback_data="nutri:set:maintain"),
            ],
            [
                InlineKeyboardButton(text="Набор веса", callback_data="nutri:set:gain"),
                InlineKeyboardButton(text="Мышечная масса", callback_data="nutri:set:muscle"),
            ],
            [InlineKeyboardButton(text="Свой план", callback_data="nutri:set:custom")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:open")],
        ]
    )


def calorie_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data="calorie:confirm"),
                InlineKeyboardButton(text="Отмена", callback_data="calorie:cancel"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="calorie:panel")],
        ]
    )


def calorie_panel_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries[:6]:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        desc = str(entry.get("meal_desc") or "Блюдо").strip()
        kcal = entry.get("calories")
        kcal_text = f"{int(float(kcal))} ккал" if kcal is not None else "без ккал"
        title = f"{desc[:24]} • {kcal_text}"[:64]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"calorie:view:{entry_id}")])

    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calorie_detail_keyboard(log_id: str | int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить", callback_data=f"calorie:ask_del:{log_id}")],
            [InlineKeyboardButton(text="Назад", callback_data="calorie:panel")],
        ]
    )


def calorie_delete_confirm_keyboard(log_id: str | int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"calorie:del:{log_id}"),
                InlineKeyboardButton(text="Нет", callback_data=f"calorie:view:{log_id}"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="calorie:panel")],
        ]
    )


def finance_panel_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries[:8]:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        amount = float(entry.get("amount") or 0)
        sign = "+" if str(entry.get("entry_type")) == "income" else "-"
        category = str(entry.get("category") or "прочее").strip()
        title = f"{sign}{amount:,.0f} {category}".replace(",", " ")
        rows.append([InlineKeyboardButton(text=title[:64], callback_data=f"finance:view:{entry_id}")])

    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_detail_keyboard(entry_id: str | int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить", callback_data=f"finance:ask_del:{entry_id}")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:finance")],
        ]
    )


def finance_delete_confirm_keyboard(entry_id: str | int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"finance:del:{entry_id}"),
                InlineKeyboardButton(text="Нет", callback_data=f"finance:view:{entry_id}"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:finance")],
        ]
    )


def finance_add_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сохранить", callback_data="finance:add_confirm"),
                InlineKeyboardButton(text="Отмена", callback_data="finance:add_cancel"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:finance")],
        ]
    )


def habits_keyboard(habits: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    pending = [habit for habit in habits if not habit.get("completed_today")]
    done = [habit for habit in habits if habit.get("completed_today")]

    for habit in pending[:8]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=habit["name"],
                    callback_data=f"habit:done:{habit['id']}",
                )
            ]
        )

    if done:
        rows.append([InlineKeyboardButton(text=f"Готово сегодня: {len(done)}", callback_data="noop")])

    rows.append(
        [
            InlineKeyboardButton(text="Добавить", callback_data="habit:add"),
            InlineKeyboardButton(text="Обновить", callback_data="menu:habits"),
        ]
    )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:open")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminders_keyboard(reminders: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for reminder in reminders[:8]:
        rid = reminder.get("id")
        rtime = str(reminder.get("reminder_time") or "")[:5]
        text = str(reminder.get("reminder_text") or "").strip()
        title = f"Удалить: {rtime} {text}"[:60]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"rem:del:{rid}")])

    rows.append(
        [
            InlineKeyboardButton(text="Добавить", callback_data="rem:add"),
            InlineKeyboardButton(text="Обновить", callback_data="menu:reminders"),
        ]
    )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="В меню", callback_data="menu:open")]]
    )
