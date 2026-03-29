# Telegram Bot: Личное развитие

Рабочий MVP-бот для Telegram:
- калории по фото
- учет доходов и расходов
- трекер привычек
- ежедневные отметки
- недельный отчет
- AI-помощник
- напоминания
- цели
- экспорт CSV/PDF
- голосовой ввод нескольких операций (доход/расход)
- шаблон вакансий с AI-нормализацией под Telegram-пост

## 1) Быстрый старт

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Заполни `.env`:
- `TELEGRAM_BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `DB_TABLE_PREFIX=bot_` (рекомендуется, чтобы не конфликтовать с существующими таблицами в проекте)
- `GEMINI_API_KEY`
- `GEMINI_MODEL=gemini-3-flash-preview`

## 2) Supabase

1. Создай проект в Supabase.
2. Открой SQL Editor.
3. Выполни SQL из файла [sql/schema.sql](sql/schema.sql).
4. Возьми `Project URL` и `service_role key` в Project Settings -> API.

## 3) Запуск

```bash
python -m bot.main
```

## 4) Railway деплой

1. Подключи репозиторий к Railway.
2. Railway автоматически использует `Dockerfile` и `railway.json`.
3. Добавь переменные окружения из `.env` в Railway Variables.
4. Запусти деплой.

## 5) Основные команды

- `/start`
- `/menu`
- `/help`
- `/habits`
- `/habit_add название`
- `/habit_done номер`
- `/goals`
- `/reminders`
- `/reminder_del номер`
- `/weekly`
- `/export`
- `/ai вопрос`
- `/vacancy`

## 6) Как использовать голос для нескольких операций

1. Нажми `💸 Доход/Расход`.
2. Отправь голосом, например:
   `расход 25000 еда, расход 8000 такси, доход 300000 подработка`
3. Бот распознает голос, разобьет на операции и сохранит.

## Примечания

- Бот использует `service_role` ключ Supabase, поэтому запускай только в защищенной среде.
- Недельный авто-отчет отправляется в воскресенье в `20:00` по часовому поясу пользователя.
- Проверка напоминаний идет раз в `REMINDER_CHECK_SECONDS`.

