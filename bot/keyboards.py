from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from . import categories as cats
from . import emoji as _pe
from . import finance as fin

Lang = str


def _btn(
    text: str,
    callback_data: str | None = None,
    *,
    url: str | None = None,
    style: str | None = None,
    icon: str | None = None,
    copy_text: str | None = None,
) -> InlineKeyboardButton:
    """Кнопка с опциональным цветом (Bot API 9.4) и премиум-иконкой. copy_text — «копировать» (≤256 символов)."""
    kwargs: dict[str, object] = {"text": text[:64]}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if copy_text:
        kwargs["copy_text"] = CopyTextButton(text=copy_text[:256])
    if style:
        kwargs["style"] = style
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(**kwargs)


TEXTS: dict[Lang, dict[str, str]] = {
    "ru": {
        "menu_nutrition": "Питание",
        "menu_finance": "Финансы",
        "menu_vacancy": "Вакансии",
        "menu_analytics": "Аналитика",
        "menu_language": "Язык",
        "menu_settings": "Настройки",
        "menu_refresh": "Обновить",
        "menu_tasks": "Задачи",
        "menu_goals": "Цели",
        "tasks_add": "Добавить",
        "tasks_done_view": "Выполненные",
        "tasks_open_view": "Открытые",
        "tasks_notes": "Заметки",
        "goal_add": "Новая цель",
        "goal_deposit": "Отложить",
        "goal_close": "Закрыть цель",
        "delete": "Удалить",
        "finance_budgets": "Лимиты",
        "finance_debts": "Долги",
        "finance_note": "Комментарий",
        "skip": "Пропустить",
        "finance_recurring": "Регулярные",
        "finance_excel": "Excel",
        "brief_morning_on": "🌅 Утро 08:00: вкл",
        "brief_morning_off": "🌅 Утро 08:00: выкл",
        "brief_evening_on": "🌙 Вечер 21:00: вкл",
        "brief_evening_off": "🌙 Вечер 21:00: выкл",
        "proactive_on": "💡 Подсказки Джарвиса: вкл",
        "proactive_off": "💡 Подсказки Джарвиса: выкл",
        "voice_on": "🎙 Голосовые ответы: вкл",
        "voice_off": "🎙 Голосовые ответы: выкл",
        "rec_add": "Добавить платёж",
        "rec_pay_now": "Записать оплату",
        "rec_pause": "Пауза",
        "rec_resume": "Включить",
        "rec_done": "✅ Да, записать",
        "rec_skip": "⏭ Пропустить в этом месяце",
        "amt_income": "➕ Это доход",
        "amt_expense": "➖ Это расход",
        "back": "Назад",
        "to_menu": "В меню",
        "save": "Сохранить",
        "cancel": "Отменить",
        "delete": "Удалить",
        "yes_delete": "Да, удалить",
        "no": "Нет",
        "calorie_goal": "Цель и профиль",
        "calorie_meals": "Приёмы",
        "finance_settings": "Настройки",
        "finance_ops": "Операции",
        "finance_stats": "Статистика",
        "finance_chart": "График",
        "finance_category": "Категория",
        "period_day": "День",
        "period_week": "Неделя",
        "period_month": "Месяц",
        "period_prev_month": "Прошлый месяц",
        "period_year": "Год",
        "vacancy_again": "Ещё вакансия",
        "vacancy_contact": "📩 Связаться",
        "vacancy_publish": "Опубликовать в канал",
        "vacancy_copy": "Чистая копия для канала",
        "vacancy_prompt": "Скопировать промпт для фото",
        "goal_loss": "Снижение",
        "goal_maintain": "Поддержание",
        "goal_gain": "Набор",
        "goal_muscle": "Масса",
        "goal_custom": "Ручной план",
        "lang_ru": "Русский",
        "lang_uz": "O'zbekcha",
        "report_weekly": "Раз в неделю",
        "report_monthly": "Раз в месяц",
        "report_off": "Выключить",
        "status_on": "Авто-отчёт: включён",
        "status_off": "Авто-отчёт: выключен",
    },
    "uz": {
        "menu_nutrition": "Oziqlanish",
        "menu_finance": "Moliya",
        "menu_vacancy": "Vakansiya",
        "menu_analytics": "Tahlil",
        "menu_language": "Til",
        "menu_settings": "Sozlamalar",
        "menu_refresh": "Yangilash",
        "menu_tasks": "Vazifalar",
        "menu_goals": "Maqsadlar",
        "tasks_add": "Qo'shish",
        "tasks_done_view": "Bajarilgan",
        "tasks_open_view": "Ochiq",
        "tasks_notes": "Eslatmalar",
        "goal_add": "Yangi maqsad",
        "goal_deposit": "Qo'shish",
        "goal_close": "Maqsadni yopish",
        "delete": "O'chirish",
        "finance_budgets": "Limitlar",
        "finance_debts": "Qarzlar",
        "finance_note": "Izoh",
        "skip": "O'tkazib yuborish",
        "finance_recurring": "Doimiy to'lovlar",
        "finance_excel": "Excel",
        "brief_morning_on": "🌅 Ertalab 08:00: yoq",
        "brief_morning_off": "🌅 Ertalab 08:00: o'chiq",
        "brief_evening_on": "🌙 Kechqurun 21:00: yoq",
        "brief_evening_off": "🌙 Kechqurun 21:00: o'chiq",
        "proactive_on": "💡 Jarvis maslahatlari: yoniq",
        "proactive_off": "💡 Jarvis maslahatlari: o'chiq",
        "voice_on": "🎙 Ovozli javoblar: yoniq",
        "voice_off": "🎙 Ovozli javoblar: o'chiq",
        "rec_add": "To'lov qo'shish",
        "rec_pay_now": "To'lovni yozish",
        "rec_pause": "Pauza",
        "rec_resume": "Yoqish",
        "rec_done": "✅ Ha, yozish",
        "rec_skip": "⏭ Bu oy o'tkazib yuborish",
        "amt_income": "➕ Bu kirim",
        "amt_expense": "➖ Bu chiqim",
        "back": "Ortga",
        "to_menu": "Menyu",
        "save": "Saqlash",
        "cancel": "Bekor qilish",
        "delete": "O'chirish",
        "yes_delete": "Ha, o'chirish",
        "no": "Yo'q",
        "calorie_goal": "Maqsad va profil",
        "calorie_meals": "Qabullar",
        "finance_settings": "Sozlamalar",
        "finance_ops": "Operatsiyalar",
        "finance_stats": "Statistika",
        "finance_chart": "Grafik",
        "finance_category": "Kategoriya",
        "period_day": "Kun",
        "period_week": "Hafta",
        "period_month": "Oy",
        "period_prev_month": "O'tgan oy",
        "period_year": "Yil",
        "vacancy_again": "Yana vakansiya",
        "vacancy_contact": "📩 Bog'lanish",
        "vacancy_publish": "Kanalga joylash",
        "vacancy_copy": "Kanal uchun toza nusxa",
        "vacancy_prompt": "Rasm promptini nusxalash",
        "goal_loss": "Kamayish",
        "goal_maintain": "Ushlab turish",
        "goal_gain": "Vazn yig'ish",
        "goal_muscle": "Mushak",
        "goal_custom": "Qo'lda reja",
        "lang_ru": "Русский",
        "lang_uz": "O'zbekcha",
        "report_weekly": "Haftada bir",
        "report_monthly": "Oyda bir",
        "report_off": "O'chirish",
        "status_on": "Avto-hisobot: yoqilgan",
        "status_off": "Avto-hisobot: o'chirilgan",
    },
}


def t(lang: str, key: str) -> str:
    data = TEXTS.get(lang if lang in TEXTS else "ru", TEXTS["ru"])
    return data.get(key) or TEXTS["ru"].get(key) or key


def _cat_btn(key: str, lang: str, callback_data: str, *, style: str | None = None, suffix: str = "") -> InlineKeyboardButton:
    """Кнопка категории: премиум-иконка вместо обычного эмодзи, если она есть в паке."""
    cat = cats.get(key)
    if cat is None:
        return _btn(cats.label(key, lang) + suffix, callback_data, style=style)
    icon = _pe.id_for(cat.emoji)
    if icon:
        return _btn(cat.label(lang) + suffix, callback_data, style=style, icon=icon)
    return _btn(cat.title(lang) + suffix, callback_data, style=style)


def _back(lang: str, target: str = "menu:open") -> InlineKeyboardButton:
    return _btn(t(lang, "back"), target, icon=_pe.ID_BACK)


# ------------------------------------------------------------------ main
def main_menu_keyboard(lang: str = "ru", *, undo: bool = False) -> InlineKeyboardMarkup:
    P = "primary"
    undo_rows = [[_btn("↩️ " + ("Bekor qilish" if lang == "uz" else "Отменить запись"), "agent:undo", icon=_pe.id_for("🔄"))]] if undo else []
    return InlineKeyboardMarkup(
        inline_keyboard=undo_rows + [
            [
                _btn(t(lang, "menu_nutrition"), "menu:calorie", style=P, icon=_pe.ID_NUTRITION),
                _btn(t(lang, "menu_finance"), "menu:finance", style=P, icon=_pe.ID_FINANCE),
            ],
            [
                _btn(t(lang, "menu_tasks"), "menu:tasks", style=P, icon=_pe.ID_TASKS),
                _btn(t(lang, "menu_goals"), "menu:goals", style=P, icon=_pe.ID_GOAL),
            ],
            [
                _btn("Jarvis" if lang == "uz" else "Джарвис", "menu:jarvis", style=P, icon=_pe.ID_JARVIS),
                _btn(t(lang, "menu_analytics"), "menu:dashboard", style="success", icon=_pe.ID_ANALYTICS),
            ],
            [
                _btn(t(lang, "menu_settings"), "menu:settings", icon=_pe.ID_SETTINGS),
                _btn(t(lang, "menu_refresh"), "menu:open", icon=_pe.ID_REFRESH),
            ],
        ]
    )


def settings_keyboard(lang: str = "ru", *, owner: bool = False) -> InlineKeyboardMarkup:
    """Главный экран настроек — только разделы бота. Всё про Джарвиса (голос, будильник, звонки) —
    отдельно, в кнопке «Джарвис» главного меню. «Пользователи» — только владельцу."""
    uz = lang == "uz"
    rows = [
        [_btn("🔔 " + ("Bildirishnomalar" if uz else "Уведомления"), "settings:notify", style="primary"),
         _btn("🗒 " + ("Eslatmalar" if uz else "Напоминания"), "settings:reminders", style="primary")],
        [_btn(t(lang, "menu_language"), "menu:language", icon=_pe.ID_LANGUAGE)],
    ]
    if owner:
        rows[1].append(_btn("👥 " + ("Foydalanuvchilar" if uz else "Пользователи"), "members:open", style="primary"))
    rows.append([_back(lang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def members_keyboard(lang: str, members: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    uz = lang == "uz"
    rows = [[_btn("➕ " + ("Taklif qilish" if uz else "Пригласить"), "members:invite", style="success")]]
    rows += [[_btn(f"🗑 {name}"[:40], f"members:del:{uid}")] for uid, name in members[:20]]
    rows.append([_back(lang, "menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invite_keyboard(lang: str, link: str) -> InlineKeyboardMarkup:
    from urllib.parse import quote

    uz = lang == "uz"
    share = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote(
        "Shaxsiy yordamchi Jarvis — kirish uchun bosing" if uz else "Личный помощник Джарвис — нажми, чтобы войти")
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📤 " + ("Yuborish" if uz else "Отправить"), url=share, style="success")],
        [_btn("📋 " + ("Havolani nusxalash" if uz else "Скопировать ссылку"), copy_text=link)],
        [_back(lang, "members:open")],
    ])


def notify_keyboard(lang: str, *, morning: bool, morning_wake: bool, evening: bool, proactive: bool, voice: bool,
                    report_enabled: bool, report_frequency: str) -> InlineKeyboardMarkup:
    """Уведомления: утро/вечер (вкл + время), подсказки, голосовые ответы, авто-отчёт."""
    uz = lang == "uz"

    def toggle(on: bool, label: str, data: str) -> InlineKeyboardButton:
        return _btn(("✅ " if on else "⛔ ") + label, data, style="success" if on else None)

    def pick(label: str, data: str, on: bool) -> InlineKeyboardButton:
        return _btn(label + (" ✓" if on else ""), data, style="primary" if on else None)

    weekly = report_enabled and report_frequency == "weekly"
    monthly = report_enabled and report_frequency == "monthly"
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle(morning, "🌅 " + ("Ertalabki xulosa" if uz else "Утренняя сводка"), "settings:brief:morning")],
        [pick("⏰ " + ("Turgandan keyin" if uz else "После подъёма"), "notify:morning:wake", morning_wake),
         _btn("⌨️ " + ("Vaqt" if uz else "Время"), "notify:time:morning", style=None if morning_wake else "primary")],
        [toggle(evening, "🌙 " + ("Kechki xulosa" if uz else "Вечерняя сводка"), "settings:brief:evening"),
         _btn("⌨️ " + ("Vaqt" if uz else "Время"), "notify:time:evening")],
        [toggle(proactive, "💡 " + ("Jarvis maslahatlari" if uz else "Подсказки Джарвиса"), "settings:toggle:proactive")],
        [toggle(voice, "🎙 " + ("Ovozli javoblar" if uz else "Голосовые ответы"), "settings:toggle:voice_reply")],
        [pick("📊 " + ("Haftalik" if uz else "Отчёт: неделя"), "report:set:weekly", weekly),
         pick("Oylik" if uz else "месяц", "report:set:monthly", monthly),
         pick("Yo'q" if uz else "выкл", "report:set:off", not report_enabled)],
        [_back(lang, "menu:settings")],
    ])


def reminders_keyboard(lang: str, reminders: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"🗑 {title}", f"settings:rem_del:{rem_id}", icon=_pe.ID_DELETE)] for rem_id, title in reminders[:12]]
    rows.append([_back(lang, "menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def jarvis_hub_keyboard(lang: str, *, alert_calls: bool = False, morning_voice: bool = False,
                        photo_intent: bool = False) -> InlineKeyboardMarkup:
    """Кнопка «Джарвис» в главном меню: всё про Джарвиса в одном месте."""
    uz = lang == "uz"

    def toggle(on: bool, label: str, data: str) -> InlineKeyboardButton:
        return _btn(("✅ " if on else "⛔ ") + label, data, style="success" if on else None)

    rows = [
        [_btn("📞 " + ("Qo'ng'iroq qil" if uz else "Позвонить"), "wakeset:calltest", style="success")],
        [_btn("⏰ " + ("Budilnik" if uz else "Будильник"), "settings:wake", style="primary"),
         _btn("🎭 " + ("Ovoz va xarakter" if uz else "Голос и характер"), "settings:jarvis", style="primary")],
        [toggle(alert_calls, "📞 " + ("Muhim bo'lsa qo'ng'iroq" if uz else "Звонок о важном"), "jarvis:toggle:alert_calls")],
        [toggle(morning_voice, "🎙 " + ("Ertalab ovozli xulosa" if uz else "Утро голосом"), "jarvis:toggle:morning_voice")],
    ]
    if photo_intent:
        rows.append([_btn("📷 " + ("Rasm kelishuvini bekor qilish" if uz else "Отменить договорённость о фото"), "jarvis:photo:off")])
    rows.append([_back(lang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wake_settings_keyboard(lang: str, *, enabled: bool, call_enabled: bool, talk: bool, voice_lang: str,
                           mode: str, days: list[int], hardness: str, tasks: list[str]) -> InlineKeyboardMarkup:
    """Экран «Подъём и звонки»: всё, что чаще всего меняют, — кнопками."""
    uz = lang == "uz"
    weekdays = {1, 2, 3, 4, 5}
    only_weekdays = set(days) == weekdays
    rows = [
        [_btn(("⏰ Uyg'otish: yoqilgan" if uz else "⏰ Подъём: включён") if enabled else ("⏰ Uyg'otish: o'chirilgan" if uz else "⏰ Подъём: выключен"),
              "wakeset:toggle:enabled", style="success" if enabled else None)],
        [_btn(("📞 Qo'ng'iroq: bor" if uz else "📞 Звонок: да") if call_enabled else ("📞 Faqat xabar" if uz else "📞 Только сообщение"),
              "wakeset:toggle:call_enabled", style="success" if call_enabled else None)],
        [_btn(("💬 Suhbat rejimi" if uz else "💬 Режим разговора") if talk else ("🔈 Faqat gapiradi" if uz else "🔈 Просто говорит"),
              "wakeset:toggle:talk", style="success" if talk else None)],
        [
            _btn(("🕌 Bomdodga" if uz else "🕌 К фаджру") + (" ✓" if mode == "fajr" else ""), "wakeset:mode:fajr", style="primary" if mode == "fajr" else None),
            _btn(("🕘 Aniq vaqt" if uz else "🕘 Точное время") + (" ✓" if mode == "fixed" else ""), "jarvis:alarm:time", style="primary" if mode == "fixed" else None),
        ],
        [
            _btn("−5 " + ("daq" if uz else "мин"), "wakeset:offset:-5"),
            _btn("+5 " + ("daq" if uz else "мин"), "wakeset:offset:5"),
            _btn(("takbir −5" if uz else "такбир −5"), "wakeset:takbir:-5"),
            _btn(("takbir +5" if uz else "такбир +5"), "wakeset:takbir:5"),
        ],
        [
            _btn(("📅 Har kuni" if uz else "📅 Каждый день") + ("" if only_weekdays else " ✓"), "wakeset:days:all", style=None if only_weekdays else "primary"),
            _btn(("📅 Ish kunlari" if uz else "📅 Будни") + (" ✓" if only_weekdays else ""), "wakeset:days:work", style="primary" if only_weekdays else None),
        ],
        [_btn(("🔥 Qattiqroq ✓" if hardness == "hard" else "🔥 Qattiqroq") if uz else ("🔥 Жёстче ✓" if hardness == "hard" else "🔥 Жёстче"),
              "wakeset:toggle:hardness", style="danger" if hardness == "hard" else None)],
    ]
    labels_ru = {"water": "💧 Вода", "squats": "🏋️ Приседания", "pushups": "💪 Отжимания", "question": "🧮 Вопрос"}
    labels_uz = {"water": "💧 Suv", "squats": "🏋️ Cho'kkalash", "pushups": "💪 Otjimaniye", "question": "🧮 Savol"}
    labels = labels_uz if uz else labels_ru
    task_row = []
    for key in ("water", "squats", "pushups", "question"):
        on = key in tasks
        task_row.append(_btn(labels[key] + (" ✓" if on else ""), f"wakeset:task:{key}", style="success" if on else None))
        if len(task_row) == 2:
            rows.append(task_row)
            task_row = []
    if task_row:
        rows.append(task_row)
    rows.append([_back(lang, "menu:jarvis")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def jarvis_settings_keyboard(lang: str, *, voice: str, call_lang: str, address: str, tone: str, verbosity: str,
                             honorific: str = "mix") -> InlineKeyboardMarkup:
    """«Голос и характер» Джарвиса: голос, язык, ты/вы, как величать, тон, длина ответов."""
    from .persona import HONORIFICS, VOICES

    uz = lang == "uz"

    def pick(label: str, data: str, on: bool) -> InlineKeyboardButton:
        return _btn(label + (" ✓" if on else ""), data, style="primary" if on else None)

    rows: list[list[InlineKeyboardButton]] = []
    voice_row: list[InlineKeyboardButton] = []
    for key, (ru_name, uz_name) in VOICES.items():
        voice_row.append(pick(uz_name if uz else ru_name, f"jarvis:voice:{key}", key == voice))
        if len(voice_row) == 3:
            rows.append(voice_row)
            voice_row = []
    if voice_row:
        rows.append(voice_row)
    rows.append([_btn("🔊 " + ("Ovozni eshitish" if uz else "Послушать голос"), "jarvis:sample")])
    rows.append([pick("🇺🇿 O'zbekcha", "jarvis:lang:uz", call_lang == "uz"), pick("🇷🇺 Русский", "jarvis:lang:ru", call_lang == "ru"),
                 pick("🇬🇧 English", "jarvis:lang:en", call_lang == "en")])
    rows.append([pick("Sen (ты)" if uz else "На «ты»", "jarvis:address:sen", address == "sen"),
                 pick("Siz (вы)" if uz else "На «вы»", "jarvis:address:siz", address == "siz")])
    rows.append([pick(HONORIFICS[k][1] if uz else HONORIFICS[k][0], f"jarvis:honorific:{k}", honorific == k) for k in ("shef", "ser", "boss")])
    rows.append([pick(HONORIFICS[k][1] if uz else HONORIFICS[k][0], f"jarvis:honorific:{k}", honorific == k) for k in ("mix", "none")])
    rows.append([pick("🙂 " + ("Do'stona" if uz else "Дружелюбный"), "jarvis:tone:friendly", tone == "friendly"),
                 pick("😌 " + ("Xotirjam" if uz else "Спокойный"), "jarvis:tone:calm", tone == "calm"),
                 pick("🧐 " + ("Qat'iy" if uz else "Строгий"), "jarvis:tone:strict", tone == "strict")])
    rows.append([pick("Qisqa" if uz else "Коротко", "jarvis:verbosity:short", verbosity == "short"),
                 pick("O'rtacha" if uz else "Обычно", "jarvis:verbosity:normal", verbosity == "normal"),
                 pick("Batafsil" if uz else "Подробно", "jarvis:verbosity:detailed", verbosity == "detailed")])
    rows.append([_back(lang, "menu:jarvis")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(t(lang, "to_menu"), "menu:open", style="primary", icon=_pe.ID_HOME)]])


def language_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🇺🇿 O'zbekcha" + (" ✓" if lang == "uz" else ""), "lang:set:uz"),
             _btn("🇷🇺 Русский" + (" ✓" if lang == "ru" else ""), "lang:set:ru"),
             _btn("🇬🇧 English" + (" ✓" if lang == "en" else ""), "lang:set:en")],
            [_back(lang, "menu:settings")],
        ]
    )


# ------------------------------------------------------------------ quick rows
def _quick_rows(prefix: str, labels: list[str]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, label in enumerate(labels):
        text = str(label or "").strip()
        if not text:
            continue
        row.append(_btn(text, f"{prefix}:quick:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


# ------------------------------------------------------------------ nutrition
def nutrition_goal_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t(lang, "goal_loss"), "nutri:set:loss", style="primary"), _btn(t(lang, "goal_maintain"), "nutri:set:maintain", style="primary")],
            [_btn(t(lang, "goal_gain"), "nutri:set:gain", style="primary"), _btn(t(lang, "goal_muscle"), "nutri:set:muscle", style="primary")],
            [_btn(t(lang, "goal_custom"), "nutri:set:custom", icon=_pe.ID_EDIT)],
            [_back(lang)],
        ]
    )


def calorie_confirm_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(t(lang, "save"), "calorie:confirm", style="success", icon=_pe.ID_SAVE),
                _btn(t(lang, "cancel"), "calorie:cancel", style="danger", icon=_pe.ID_CANCEL),
            ],
            [_back(lang, "calorie:panel")],
        ]
    )


def calorie_panel_keyboard(quick_labels: list[str], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.extend(_quick_rows("calorie", quick_labels))
    rows.append(
        [
            _btn(t(lang, "calorie_meals"), "calorie:meals:day", style="primary", icon=_pe.ID_NUTRITION),
            _btn(t(lang, "calorie_goal"), "calorie:goals", style="primary", icon=_pe.ID_GOAL),
        ]
    )
    rows.append([_back(lang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calorie_meals_keyboard(entries: list[dict], period: str, lang: str = "ru") -> InlineKeyboardMarkup:
    def _p(code: str, key: str) -> InlineKeyboardButton:
        label = t(lang, key)
        return _btn(f"✅ {label}" if code == period else label, f"calorie:meals:{code}")

    rows: list[list[InlineKeyboardButton]] = [[_p("day", "period_day"), _p("week", "period_week"), _p("month", "period_month")]]
    unit = "kkal" if lang == "uz" else "ккал"
    for entry in entries[:12]:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        desc = str(entry.get("meal_desc") or ("Taom" if lang == "uz" else "Блюдо")).strip()
        kcal = entry.get("calories")
        kcal_text = f"{int(float(kcal))} {unit}" if kcal is not None else "—"
        rows.append([_btn(f"{desc[:28]} • {kcal_text}", f"calorie:view:{entry_id}")])
    rows.append([_back(lang, "calorie:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calorie_detail_keyboard(log_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t(lang, "delete"), f"calorie:ask_del:{log_id}", style="danger", icon=_pe.ID_DELETE)],
            [_back(lang, "calorie:meals:day")],
        ]
    )


def calorie_delete_confirm_keyboard(log_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(t(lang, "yes_delete"), f"calorie:del:{log_id}", style="danger", icon=_pe.ID_DELETE),
                _btn(t(lang, "no"), f"calorie:view:{log_id}", icon=_pe.ID_CANCEL),
            ],
            [_back(lang, "calorie:panel")],
        ]
    )


# ------------------------------------------------------------------ finance
def finance_panel_keyboard(quick_labels: list[str], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.extend(_quick_rows("finance", quick_labels))
    rows.append(
        [
            _btn(t(lang, "finance_ops"), "finance:ops:day", style="primary", icon=_pe.ID_REPORT),
            _btn(t(lang, "finance_stats"), "finance:stats:month", style="success", icon=_pe.ID_ANALYTICS),
        ]
    )
    rows.append(
        [
            _btn(t(lang, "finance_budgets"), "finance:budgets", icon=_pe.ID_GOAL),
            _btn(t(lang, "finance_recurring"), "finance:recurring", icon=_pe.ID_CALENDAR),
        ]
    )
    rows.append(
        [
            _btn(t(lang, "finance_settings"), "finance:settings", icon=_pe.ID_SETTINGS),
            _back(lang),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def debt_note_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t(lang, "skip"), "finance:debt_note_skip", icon=_pe.id_for("⏭")), _btn(t(lang, "cancel"), "menu:finance", style="danger", icon=_pe.ID_CANCEL)],
        ]
    )


def finance_budgets_keyboard(limits: dict[str, float], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cat in cats.EXPENSE:
        limit = limits.get(cat.key)
        row.append(_cat_btn(cat.key, lang, f"finance:budget:{cat.key}", style="primary" if limit else None, suffix=f" · {fin.fmt_money(limit)}" if limit else ""))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_back(lang, "menu:finance")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_recurring_keyboard(items: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items[:15]:
        mark = "✅" if item.get("enabled", True) else "⏸"
        label = f"{mark} {int(item.get('day_of_month') or 1):02d} · {str(item.get('title') or '')[:20]} · {fin.fmt_money(float(item.get('amount') or 0))}"
        rows.append([_btn(label, f"finance:rec:{item.get('id')}")])
    rows.append([_btn(t(lang, "rec_add"), "finance:rec_add", style="success", icon=_pe.ID_ADD)])
    rows.append([_back(lang, "menu:finance")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_recurring_detail_keyboard(rec_id: str | int, enabled: bool, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t(lang, "rec_pay_now"), f"finance:rec_pay:{rec_id}", style="success", icon=_pe.ID_SAVE)],
            [
                _btn(t(lang, "rec_pause" if enabled else "rec_resume"), f"finance:rec_toggle:{rec_id}"),
                _btn(t(lang, "delete"), f"finance:rec_del:{rec_id}", style="danger", icon=_pe.ID_DELETE),
            ],
            [_back(lang, "finance:recurring")],
        ]
    )


def recurring_prompt_keyboard(rec_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t(lang, "rec_done"), f"rec:done:{rec_id}", style="success")],
            [_btn(t(lang, "rec_skip"), f"rec:skip:{rec_id}")],
        ]
    )


def amount_category_keyboard(kind: str, recent: list[str], lang: str = "ru") -> InlineKeyboardMarkup:
    """Выбор категории для «голой» суммы: сначала недавние, потом все."""
    rows: list[list[InlineKeyboardButton]] = []
    seen: set[str] = set()
    row: list[InlineKeyboardButton] = []
    ordered = [k for k in recent if cats.get(k) and cats.get(k).kind == kind] + [c.key for c in cats.categories_for(kind)]
    for key in ordered:
        if key in seen:
            continue
        seen.add(key)
        row.append(_cat_btn(key, lang, f"finance:amtcat:{key}", style="primary" if key in recent else None))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn(t(lang, "amt_income" if kind == "expense" else "amt_expense"), f"finance:amtkind:{'income' if kind == 'expense' else 'expense'}")])
    rows.append([_btn(t(lang, "cancel"), "menu:finance", style="danger", icon=_pe.ID_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_stats_keyboard(period: str, lang: str = "ru") -> InlineKeyboardMarkup:
    def _p(code: str, key: str) -> InlineKeyboardButton:
        label = t(lang, key)
        return _btn(f"✅ {label}" if code == period else label, f"finance:stats:{code}")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_p("day", "period_day"), _p("week", "period_week"), _p("month", "period_month")],
            [_p("prev_month", "period_prev_month"), _p("year", "period_year")],
            [
                _btn(t(lang, "finance_chart"), f"finance:chart:{period}", style="primary", icon=_pe.ID_ANALYTICS),
                _btn(t(lang, "finance_excel"), f"finance:excel:{period}", style="primary", icon=_pe.ID_REPORT),
            ],
            [_back(lang, "menu:finance")],
        ]
    )


def finance_settings_keyboard(view: dict[str, float], currency: str, lang: str = "ru") -> InlineKeyboardMarkup:
    labels = {
        "card": ("💳 Карта", "💳 Karta"),
        "cash": ("💵 Наличные", "💵 Naqd"),
        "lent": ("🤝 Дал в долг", "🤝 Qarzga berilgan"),
        "debt": ("📌 Мои долги", "📌 Mening qarzim"),
        "credit": ("🏦 Кредит/мес", "🏦 Kredit/oy"),
    }
    keys = {"card": "card_base", "cash": "cash_base", "lent": "lent_base", "debt": "debt_base", "credit": "monthly_credit_payment"}
    rows = []
    for field, (ru, uz) in labels.items():
        value = float(view.get(keys[field]) or 0)
        rows.append([_btn(f"{uz if lang == 'uz' else ru} • {fin.fmt_money(value)} {currency}", f"finance:set:{field}")])
    rows.append([_back(lang, "menu:finance")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_setting_input_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(t(lang, "finance_settings"), "finance:settings", icon=_pe.ID_SETTINGS)],
            [_back(lang, "menu:finance")],
        ]
    )


def finance_operations_keyboard(entries: list[dict], period: str, lang: str = "ru") -> InlineKeyboardMarkup:
    def _p(code: str, key: str) -> InlineKeyboardButton:
        label = t(lang, key)
        return _btn(f"✅ {label}" if code == period else label, f"finance:ops:{code}")

    rows: list[list[InlineKeyboardButton]] = [[_p("day", "period_day"), _p("week", "period_week"), _p("month", "period_month")]]
    for entry in entries[:12]:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        amount = float(entry.get("amount") or 0)
        key = fin.entry_category_key(entry)
        cat_label = cats.label(key, lang, with_emoji=True)
        if fin.is_transfer(entry):
            title = f"↔ {fin.fmt_money(amount)} {cats.label(key, lang, with_emoji=False)}"
        else:
            sign = "+" if str(entry.get("entry_type")) == "income" else "-"
            note = fin.clean_note(entry.get("note"))
            title = f"{sign}{fin.fmt_money(amount)} {cat_label}" + (f" · {note}" if note else "")
        rows.append([_btn(title, f"finance:view:{entry_id}")])
    rows.append([_back(lang, "menu:finance")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_detail_keyboard(entry_id: str | int, lang: str = "ru", *, transfer: bool = False) -> InlineKeyboardMarkup:
    rows = []
    edit_row = [_btn(t(lang, "finance_note"), f"finance:note:{entry_id}", style="primary", icon=_pe.ID_EDIT)]
    if not transfer:
        edit_row.append(_btn(t(lang, "finance_category"), f"finance:cat:{entry_id}", style="primary", icon=_pe.id_for("🏷")))
    rows.append(edit_row)
    rows.append([_btn(t(lang, "delete"), f"finance:ask_del:{entry_id}", style="danger", icon=_pe.ID_DELETE)])
    rows.append([_back(lang, "finance:ops:day")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_category_keyboard(entry_id: str | int, kind: str, lang: str = "ru") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cat in cats.categories_for(kind):
        row.append(_cat_btn(cat.key, lang, f"finance:setcat:{entry_id}:{cat.key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_back(lang, f"finance:view:{entry_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_delete_confirm_keyboard(entry_id: str | int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(t(lang, "yes_delete"), f"finance:del:{entry_id}", style="danger", icon=_pe.ID_DELETE),
                _btn(t(lang, "no"), f"finance:view:{entry_id}", icon=_pe.ID_CANCEL),
            ],
            [_back(lang, "menu:finance")],
        ]
    )


def finance_add_confirm_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(t(lang, "save"), "finance:add_confirm", style="success", icon=_pe.ID_SAVE),
                _btn(t(lang, "cancel"), "finance:add_cancel", style="danger", icon=_pe.ID_CANCEL),
            ],
            [_back(lang, "menu:finance")],
        ]
    )


# ------------------------------------------------------------------ vacancy
def vacancy_panel_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_back(lang)]])


def vacancy_result_keyboard(
    lang: str = "ru",
    contact_url: str | None = None,
    *,
    can_publish: bool = False,
    image_prompt: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if contact_url:
        rows.append([_btn(t(lang, "vacancy_contact"), url=contact_url, style="primary")])
    publish_key = "vacancy_publish" if can_publish else "vacancy_copy"
    rows.append([_btn(t(lang, publish_key), "vacancy:publish", style="success", icon=_pe.ID_SAVE)])
    if image_prompt:
        # нажатие копирует промпт в буфер — сам текст в чате не показываем
        rows.append([_btn(t(lang, "vacancy_prompt"), icon=_pe.ID_STAR, copy_text=image_prompt)])
    rows.append([_btn(t(lang, "vacancy_again"), "vacancy:again", icon=_pe.ID_REFRESH)])
    rows.append([_back(lang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vacancy_channel_keyboard(lang: str = "ru", contact_url: str | None = None) -> InlineKeyboardMarkup | None:
    if not contact_url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t(lang, "vacancy_contact"), url=contact_url)]])


# ------------------------------------------------------------------ analytics
def dashboard_keyboard(active: str, lang: str = "ru") -> InlineKeyboardMarkup:
    def _p(code: str, label: str) -> InlineKeyboardButton:
        return _btn(f"✅ {label}" if code == active else label, f"dash:{code}")

    labels = {"7d": ("7 дней", "7 kun"), "30d": ("30 дней", "30 kun"), "90d": ("90 дней", "90 kun")}
    idx = 1 if lang == "uz" else 0
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_p("7d", labels["7d"][idx]), _p("30d", labels["30d"][idx]), _p("90d", labels["90d"][idx])],
            [
                _btn("🍱 " + ("Kaloriya" if lang == "uz" else "Калории"), f"dash:kcal:{active}"),
                _btn("🏷 " + ("Toifalar" if lang == "uz" else "Категории"), f"dash:cats:{active}"),
            ],
            [_btn("🧠 " + ("Chuqur tahlil" if lang == "uz" else "Глубокий анализ"), "dash:deep", style="success")],
            [_btn(t(lang, "menu_settings"), "menu:settings", icon=_pe.ID_SETTINGS), _back(lang)],
        ]
    )


__all__ = [name for name in dir() if name.endswith("_keyboard") or name == "t"]


# ------------------------------------------------------------------ tasks / goals
def _short(text: str, limit: int = 28) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def tasks_keyboard(tasks: list[dict], lang: str = "ru", *, done_view: bool = False) -> InlineKeyboardMarkup:
    """Экран задач: по кнопке на задачу (✅ выполнить / 🗑 удалить в списке выполненных)."""
    rows: list[list[InlineKeyboardButton]] = []
    for t_ in tasks[:12]:
        tid = t_.get("id")
        if done_view:
            rows.append([_btn(f"🗑 {_short(t_.get('text'))}", f"task:del:{tid}", icon=_pe.ID_DELETE)])
        else:
            rows.append([_btn(f"✅ {_short(t_.get('text'))}", f"task:done:{tid}", icon=_pe.ID_SAVE)])
    rows.append([
        _btn(t(lang, "tasks_add"), "task:add", style="primary", icon=_pe.ID_ADD),
        _btn(t(lang, "tasks_open_view" if done_view else "tasks_done_view"), "task:view:open" if done_view else "task:view:done", icon=_pe.ID_REFRESH),
    ])
    rows.append([_btn(t(lang, "tasks_notes"), "task:notes", icon=_pe.ID_NUTRITION), _back(lang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notes_keyboard(notes: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows = [[_btn(f"🗑 {_short(n.get('text'))}", f"note:del:{n.get('id')}", icon=_pe.ID_DELETE)] for n in notes[:12]]
    rows.append([_back(lang, "menu:tasks")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_GOAL_ICON = {"save": "🎯", "spend_cap": "💸", "weight": "⚖️", "habit": "🔁", "custom": "🏁"}


def goals_keyboard(goals: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    rows = [[_btn(f"{_GOAL_ICON.get(str(g.get('kind') or 'save'), '🎯')} {_short(g.get('title'))}", f"goal:view:{g.get('id')}", icon=_pe.ID_GOAL)] for g in goals[:10]]
    rows.append([_btn(t(lang, "goal_add"), "goal:add", style="primary", icon=_pe.ID_ADD), _back(lang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def goal_detail_keyboard(goal_id: str | int, lang: str = "ru", *, kind: str = "save", today_checked: bool = False) -> InlineKeyboardMarkup:
    """Первая кнопка зависит от вида цели: отложить / отметить сегодня / записать вес / прогресс %."""
    uz = lang == "uz"
    if kind == "habit":
        first = (_btn(("✅ Bugun bajarildi" if uz else "✅ Сегодня сделано") if today_checked else ("Bugun qildim" if uz else "Отметить сегодня"),
                      f"goal:uncheck:{goal_id}" if today_checked else f"goal:check:{goal_id}", style=None if today_checked else "primary", icon=_pe.ID_SAVE))
    elif kind == "weight":
        first = _btn("⚖️ " + ("Vaznni yozish" if uz else "Записать вес"), f"goal:weight:{goal_id}", style="primary", icon=_pe.ID_ADD)
    elif kind == "custom":
        first = _btn("📈 " + ("Progress %" if uz else "Прогресс %"), f"goal:progress:{goal_id}", style="primary", icon=_pe.ID_ADD)
    elif kind == "spend_cap":
        first = _btn("💸 " + ("Limitni o'zgartirish" if uz else "Изменить лимит"), f"goal:limit:{goal_id}", style="primary", icon=_pe.ID_ADD)
    else:
        first = _btn(t(lang, "goal_deposit"), f"goal:deposit:{goal_id}", style="primary", icon=_pe.ID_ADD)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [first],
            [
                _btn(t(lang, "goal_close"), f"goal:close:{goal_id}", style="success", icon=_pe.ID_SAVE),
                _btn(t(lang, "delete"), f"goal:del:{goal_id}", style="danger", icon=_pe.ID_DELETE),
            ],
            [_back(lang, "menu:goals")],
        ]
    )
