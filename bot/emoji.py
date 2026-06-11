"""Premium (custom) emoji helpers.

Custom emoji here use ONLY the TgAndroidIcons pack
(https://t.me/addemoji/TgAndroidIcons). Where the pack has no matching icon we
fall back to a plain unicode emoji (no custom entity) — per project owner's rule
"use only these emoji, no others".

Custom emoji render as premium for Telegram Premium users and as the plain
fallback character for everyone else. They work in message text/captions and,
since Bot API 9.4, as inline-button icons (icon_custom_emoji_id).
"""
from __future__ import annotations


def e(fallback: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# --- Text emoji: TgAndroidIcons custom where available, else plain unicode ---
HELLO = e("👋", "5870734657384877785")      # Привет
CHECK = e("✅", "5825794181183836432")       # Галочка
CROSS = e("❌", "5872829476143894491")       # Запрет / Отмена
INFO = e("ℹ️", "5879785854284599288")        # Информация
BELL = e("🔔", "5909201569898827582")        # Уведомления
STAR = e("⭐", "5843843420468024653")        # Избранное
PIN = e("📌", "5796440171364749940")         # Булавка
SETTINGS = e("⚙️", "5877260593903177342")    # Настройки
WALLET = e("💼", "5769403330761593044")      # Кошелек
CHART = e("📊", "5931472654660800739")       # Диаграмма

# No matching icon in the pack -> plain unicode (not a custom emoji):
CALENDAR = "📅"
FIRE = "🔥"
IDEA = "💡"
CASH = "💵"
ARROW_UP = "↗️"
ARROW_DOWN = "↘️"
CHART_UP = "📈"
CHART_DOWN = "📉"

# --- Raw IDs for inline-button icons (all from TgAndroidIcons) ---
ID_FINANCE = "5769403330761593044"   # Кошелек
ID_HABITS = "5825794181183836432"    # Галочка
ID_GOALS = "5843843420468024653"     # Избранное
ID_TRAINER = "5935847413859225147"   # Спорт
ID_REPORT = "5931472654660800739"    # Диаграмма
ID_VACANCY = "5967389567781703494"   # Рабочий портфель
ID_LANGUAGE = "5778184941154078090"  # Перевод
ID_ANALYTICS = "5960714428394507968" # Глаз / Просмотры
ID_REFRESH = "5877410604225924969"   # Обновления
ID_SAVE = "5825794181183836432"      # Галочка
ID_CANCEL = "5872829476143894491"    # Запрет / Отмена
ID_DELETE = "5879896690210639947"    # Корзина
ID_ADD = "5879841310902324730"       # Карандаш
ID_BACK = "5875082500023258804"      # Возврат
