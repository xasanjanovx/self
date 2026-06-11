"""Premium (custom) emoji helpers.

Each constant renders as a premium animated emoji for Telegram Premium users and
falls back to a regular emoji for everyone else (Telegram handles the fallback
automatically from the character inside the <tg-emoji> tag).

Custom emoji only work inside message text/captions (HTML parse mode), NOT on
inline-button labels — that is a Telegram platform limitation.

IDs provided by the project owner from premium emoji packs.
"""
from __future__ import annotations


def e(fallback: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# General
HELLO = e("👋", "5870734657384877785")
CALENDAR = e("📅", "5413879192267805083")
CHECK = e("✅", "5206607081334906820")
CROSS = e("❌", "5210952531676504517")
FIRE = e("🔥", "5424972470023104089")
INFO = e("ℹ️", "5879785854284599288")
BELL = e("🔔", "5458603043203327669")
IDEA = e("💡", "5422439311196834318")
STAR = e("⭐", "5438496463044752972")
PIN = e("📌", "5397782960512444700")
SETTINGS = e("⚙️", "5341715473882955310")

# Finance
WALLET = e("💼", "5769403330761593044")
CASH = e("💵", "5409048419211682843")
ARROW_UP = e("↗️", "5449683594425410231")
ARROW_DOWN = e("↘️", "5447183459602669338")
CHART = e("📊", "5231200819986047254")
CHART_UP = e("📈", "5244837092042750681")
CHART_DOWN = e("📉", "5246762912428603768")


# Raw custom-emoji IDs for use as button icons (icon_custom_emoji_id, Bot API 9.4).
ID_FINANCE = "5769403330761593044"   # wallet
ID_HABITS = "5206607081334906820"    # check
ID_GOALS = "5438496463044752972"     # star
ID_TRAINER = "5935847413859225147"   # sport
ID_REPORT = "5231200819986047254"    # diagram
ID_VACANCY = "5967389567781703494"   # work briefcase
ID_LANGUAGE = "5778184941154078090"  # translate
ID_ANALYTICS = "5244837092042750681" # chart up
ID_REFRESH = "5877410604225924969"   # updates
ID_SAVE = "5206607081334906820"      # check
ID_CANCEL = "5210952531676504517"    # cross
ID_DELETE = "5879896690210639947"    # trash bin
ID_ADD = "5397916757333654639"       # plus
ID_BACK = "5875082500023258804"      # back arrow
ID_HOME = "5391032818111363540"      # geo/home-ish
