from aiogram.fsm.state import State, StatesGroup


class BotStates(StatesGroup):
    waiting_calorie_input = State()
    waiting_calorie_confirm = State()
    waiting_nutrition_goal = State()
    waiting_nutrition_profile = State()
    waiting_nutrition_custom = State()
    waiting_finance_input = State()
    waiting_finance_confirm = State()
    waiting_finance_settings = State()
    waiting_habit_name = State()
    waiting_checkin = State()
    waiting_goal = State()
    waiting_reminder = State()
    waiting_ai_question = State()
    waiting_trainer_profile = State()
    waiting_trainer_question = State()
