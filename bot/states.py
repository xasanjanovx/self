from aiogram.fsm.state import State, StatesGroup


class BotStates(StatesGroup):
    # питание
    waiting_calorie_input = State()
    waiting_calorie_confirm = State()
    waiting_nutrition_goal = State()
    waiting_nutrition_profile = State()
    waiting_nutrition_custom = State()
    # финансы
    waiting_finance_input = State()
    waiting_finance_confirm = State()
    waiting_finance_settings = State()
    waiting_finance_settings_value = State()
    waiting_finance_amount_category = State()
    waiting_budget_value = State()
    waiting_recurring_input = State()
    waiting_debt_note = State()
    waiting_note_value = State()
    # вакансии
    waiting_vacancy_input = State()
    # задачи / цели
    waiting_task_input = State()
    waiting_goal_input = State()
    waiting_goal_amount = State()
