"""Premium (custom) emoji — curated "serious" set selected from the IDs
provided by the project owner (resolved via getCustomEmojiStickers).

Custom emoji render as premium for Telegram Premium users and as the plain
fallback character for everyone else. They work in message text/captions and,
since Bot API 9.4, as inline-button icons (icon_custom_emoji_id).

Where the provided pack has no sensible match (food, dumbbell, banknote, wave,
up/down arrows) we use a close premium icon or a plain unicode emoji.
"""
from __future__ import annotations


def e(fallback: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# --- Text emoji (used in message bodies) ---
CALENDAR = e("📅", "5967782394080530708")    # 📅
CHECK = e("✅", "5985596818912712352")        # ✅
CROSS = e("❌", "5388627550526270988")        # ❌
FIRE = e("🔥", "5420315771991497307")         # 🔥
INFO = e("📄", "5877301185639091664")         # 📄 (help/info)
BELL = e("🔔", "5242628160297641831")         # 🔔
IDEA = e("🧠", "6257767895732848636")         # 🧠 (insight)
STAR = e("⭐", "5274046919809704653")         # ⭐
PIN = e("📍", "5886446115905082831")          # 📍
SETTINGS = e("⚙️", "5350396951407895212")     # ⚙️
WALLET = e("💰", "5348418461838098123")       # 💰
HANDSHAKE = e("🤝", "5357080225463149588")    # 🤝
INCOME = e("🟢", "5852777287451151788")       # 🟢 (income)
EXPENSE = e("🔴", "5291899179008798421")      # 🔴 (expense)
CHART = e("🧾", "5458458113826910668")        # 🧾 (report)
NUTRITION = e("🗒", "5877597667231534929")    # 🗒 (food diary)
HOME = e("🏠", "5188561131995690450")         # 🏠
ROCKET = e("🚀", "5458555944591981600")       # 🚀

# No premium match in the provided pack -> plain unicode:
HELLO = "👋"
CASH = "💵"
CARD = "💳"
BANK = "🏦"

# --- Raw IDs for inline-button icons (icon_custom_emoji_id) ---
ID_NUTRITION = "5877597667231534929"  # 🗒 (food diary)
ID_FINANCE = "5348418461838098123"    # 💰
ID_HABITS = "5420315771991497307"     # 🔥
ID_GOALS = "5780530293945405228"      # 🎯
ID_TRAINER = "6257767895732848636"    # 🧠
ID_REPORT = "5458458113826910668"     # 🧾
ID_VACANCY = "5458809519461136265"    # 💼
ID_LANGUAGE = "5188381825701021648"   # 🌐
ID_ANALYTICS = "5188311512791393083"  # 🔎
ID_REFRESH = "5877410604225924969"    # 🔄
ID_SAVE = "5985596818912712352"       # ✅
ID_CANCEL = "5240241223632954241"     # 🚫
ID_DELETE = "5841541824803509441"     # 🗑
ID_ADD = "5406829076465861567"        # ➕
ID_BACK = "5258236805890710909"       # ⬅️
ID_HOME = "5188561131995690450"       # 🏠
ID_SETTINGS = "5350396951407895212"   # ⚙️
ID_PIN = "5886446115905082831"        # 📍
ID_CALENDAR = "5967782394080530708"   # 📅
ID_EDIT = "5879841310902324730"       # ✏️
ID_FIRE = "5420315771991497307"       # 🔥
ID_STAR = "5274046919809704653"       # ⭐
ID_GOAL = "5780530293945405228"       # 🎯
ID_PERIOD = "5967782394080530708"     # 📅
