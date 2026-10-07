"""Вакансии: детект, нормализация данных от AI и сборка поста для канала."""
from __future__ import annotations

import html
import re
from urllib.parse import quote

from .ai import VacancyData, VacancySection

VACANCY_DEFAULT_REGION_TAG = "#TOSHKENT"
VACANCY_CONTACT_TEMPLATE = (
    "Assalomu Alaykum. @ishdasiz kanalida joylashtirilgan vakansiya bo'yicha bezovta qilyapman. "
    "Menga to'liqroq ma'lumot bera olasizmi ?"
)

_EMOJI_META = {
    "top": ("✅", "5389061359403039918"),
    "intro": ("💬", "5877301185639091664"),
    "location": ("📍", "5886446115905082831"),
    "salary": ("💰", "5348418461838098123"),
    "schedule": ("🕔", "5258419835922030550"),
    "requirements": ("⚠️", "5881702736843511327"),
    "benefits": ("✅", "5985596818912712352"),
    "duties": ("❗️", "5879813604068298387"),
    "extra": ("📌", "5886446115905082831"),
    "phone": ("📞", "5897938112654348733"),
    "telegram": ("✈️", "5875465628285931233"),
    "footer": ("➡️", "5260450573768990626"),
}

_VACANCY_DISCLAIMER_LINES = (
    "❗️E'lonlardagi ma'lumotlar uchun kanal ma'muriyati javobgar emas. "
    "Shaxsiy ma'lumotlaringizni bermang, ish beruvchi pul so'rasa - adminni ogohlantiring.",
    "Ogoh bo'ling!",
)
_VACANCY_FOOTER_TEXT = "Tez va oson ish toping!"
_VACANCY_DIVIDER = "— — — — — — — — — — —"
_BULLET = "•"

_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_TELEGRAM_RE = re.compile(r"(https?://t\.me/[A-Za-z0-9_]{3,}|@[A-Za-z0-9_]{3,})", re.IGNORECASE)
_AD_TOKENS = (
    "ishdasiz", "join our", "подпис", "subscribe", "our channel", "telegram channel", "obuna bo",
    "kanalga", "kanalimiz", "каналу", "канал ", "adminni ogohlantiring", "ma'muriyati javobgar emas",
)

_REGION_MAP = {
    "toshkent": "#TOSHKENT", "tashkent": "#TOSHKENT", "ташкент": "#TOSHKENT",
    "andijon": "#ANDIJON", "andijan": "#ANDIJON", "андижан": "#ANDIJON",
    "samarqand": "#SAMARQAND", "samarkand": "#SAMARQAND", "самарканд": "#SAMARQAND",
    "buxoro": "#BUXORO", "bukhara": "#BUXORO", "бухара": "#BUXORO",
    "farg'ona": "#FARGONA", "fargona": "#FARGONA", "fergana": "#FARGONA", "фергана": "#FARGONA",
    "namangan": "#NAMANGAN", "наманган": "#NAMANGAN",
    "jizzax": "#JIZZAX", "джизак": "#JIZZAX",
    "sirdaryo": "#SIRDARYO", "сырдар": "#SIRDARYO",
    "qashqadaryo": "#QASHQADARYO", "кашкадар": "#QASHQADARYO", "qarshi": "#QASHQADARYO",
    "surxondaryo": "#SURXONDARYO", "сурхандар": "#SURXONDARYO", "termiz": "#SURXONDARYO",
    "xorazm": "#XORAZM", "хорезм": "#XORAZM", "urganch": "#XORAZM",
    "navoiy": "#NAVOIY", "навои": "#NAVOIY",
    "nukus": "#QORAQALPOGISTON", "qoraqalpog": "#QORAQALPOGISTON", "каракалпак": "#QORAQALPOGISTON",
}


# ----------------------------------------------------------------- helpers
def _h(value: str) -> str:
    return html.escape(value, quote=False)


def _emoji(name: str, premium: bool) -> str:
    fallback, emoji_id = _EMOJI_META[name]
    if not premium:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _is_ad_line(line: str) -> bool:
    low = line.lower()
    return any(token in low for token in _AD_TOKENS)


def extract_phones(text: str) -> list[str]:
    """Все валидные узбекские номера (dedupe по последним 9 цифрам)."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _PHONE_RE.finditer(text or ""):
        raw = re.sub(r"\s+", " ", match.group(0)).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 9 or len(digits) > 12:
            continue
        if len(digits) <= 10 and not raw.startswith("+") and not digits.startswith("998"):
            continue
        key = digits[-9:]
        if key in seen:
            continue
        seen.add(key)
        if len(digits) == 9:
            raw = f"+998{digits}"
        elif digits.startswith("998") and not raw.startswith("+"):
            raw = f"+{digits}"
        found.append(_pretty_phone(raw))
    return found


def _pretty_phone(raw: str) -> str:
    """Номер слитно: +998901234567 (так удобнее копировать и нажимать)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("998"):
        return f"+{digits}"
    return re.sub(r"[\s().-]", "", raw)


def username_from_telegram(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"t\.me/([A-Za-z0-9_]{3,})", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"@([A-Za-z0-9_]{3,})", text)
    if match:
        return match.group(1)
    return None


def normalize_region_tag(value: str | None, raw_text: str, default: str = VACANCY_DEFAULT_REGION_TAG) -> str:
    text = str(value or "").strip()
    if text and text != "-":
        tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", text.lstrip("#")).upper()
        if len(tag) > 2:
            return tag
    low = raw_text.lower()
    for token, tag in _REGION_MAP.items():
        if token in low:
            return tag
    return default


def build_contact_url(telegram_value: str | None) -> str | None:
    username = username_from_telegram(telegram_value)
    if not username:
        return None
    return f"tg://resolve?domain={username}&text={quote(VACANCY_CONTACT_TEMPLATE, safe='')}"


def finalize(data: VacancyData, raw_text: str) -> VacancyData:
    """Дополняем ответ AI тем, что надёжнее извлекается регулярками."""
    phones = extract_phones(" | ".join(filter(None, [data.phone or "", raw_text])))
    data.phone = " | ".join(phones) if phones else None

    tg = username_from_telegram(data.telegram)
    if not tg:
        match = _TELEGRAM_RE.search(raw_text)
        tg = username_from_telegram(match.group(0)) if match else None
    data.telegram = f"@{tg}" if tg else None

    data.region_tag = normalize_region_tag(data.region_tag, raw_text)

    def _strip(items: list[str]) -> list[str]:
        return [item for item in items if item and not _is_ad_line(item)]

    data.requirements = _strip(data.requirements)
    data.duties = _strip(data.duties)
    data.benefits = _strip(data.benefits)
    data.extra_sections = [
        VacancySection(title=s.title, items=_strip(s.items)) for s in data.extra_sections if _strip(s.items)
    ]
    if data.intro and _is_ad_line(data.intro):
        data.intro = None
    if not data.headline:
        data.headline = "Xodim kerak"
    scene = data.image_prompt  # от AI приходит только описание фона
    data.image_prompt = build_full_prompt(data, scene=scene)
    return data


def region_name(tag: str) -> str:
    """#TOSHKENT → Toshkent, #QORAQALPOGISTON → Qoraqalpog'iston."""
    names = {
        "#TOSHKENT": "Toshkent", "#ANDIJON": "Andijon", "#SAMARQAND": "Samarqand", "#BUXORO": "Buxoro",
        "#FARGONA": "Farg'ona", "#NAMANGAN": "Namangan", "#JIZZAX": "Jizzax", "#SIRDARYO": "Sirdaryo",
        "#QASHQADARYO": "Qashqadaryo", "#SURXONDARYO": "Surxondaryo", "#XORAZM": "Xorazm", "#NAVOIY": "Navoiy",
        "#QORAQALPOGISTON": "Qoraqalpog'iston",
    }
    return names.get(str(tag or "").upper(), str(tag or "").lstrip("#").capitalize())


def _short(value: str | None, limit: int = 60) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .;,")
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


PROMPT_MAX_LEN = 256  # лимит текста кнопки «копировать» в Telegram


def build_image_prompt(data: VacancyData, *, scene: str | None = None, max_len: int = PROMPT_MAX_LEN) -> str:
    """Промпт для ChatGPT: сочный баннер 16:9 с ключевыми данными вакансии на узбекском (латиница).
    Компактный (≤256 символов), чтобы уходить кнопкой «копировать». Данные добавляются по приоритету,
    пока влезают: заголовок → зарплата → место → график → плюс → фон."""
    region = region_name(data.region_tag)
    addr = _short(data.address, 28)
    place = addr if addr and region.lower() in addr.lower() else f"{addr}, {region}" if addr else region
    head = "Сочный премиальный баннер 16:9 для вакансии. Текст на узбекской латинице, ровно так: "
    tail = " Без другого текста."
    optional: list[str] = []
    if data.salary:
        optional.append(f"Maosh: {_short(data.salary, 34)}")
    optional.append(place)
    if data.schedule:
        optional.append(f"Ish vaqti: {_short(data.schedule, 30)}")
    if data.benefits:
        optional.append(f"✓ {_short(data.benefits[0], 30)}")
    scene_part = f" Фон: {_short(scene, 45)}." if scene else ""

    def _compose(parts: list[str], with_scene: bool) -> str:
        return head + " · ".join(parts + ["@ishdasiz"]) + "." + (scene_part if with_scene else "") + tail

    chosen: list[str] = [f"«{_short(data.headline, 60)}»"]
    for part in optional:
        if len(_compose(chosen + [part], False)) <= max_len:
            chosen.append(part)
    text = _compose(chosen, True)  # фон — самый низкий приоритет: добавляем, только если влезает
    if len(text) > max_len:
        text = _compose(chosen, False)
    if len(text) > max_len:  # крайний случай — режем хвост
        text = text[: max_len - 1] + "…"
    return text


def build_full_prompt(data: VacancyData, *, scene: str | None = None, for_logo: bool = False) -> str:
    """Полный промпт для картинки: ВСЯ вакансия (контекст) + внизу задача на баннер.

    for_logo (07.10, автоподбор): баннер рисует Nano Banana, логотип канала потом ставится в левый нижний угол — угол оставляем
    пустым, а «@ishdasiz» на баннер не пишем (он уже в логотипе).

    Отдаётся отдельным сообщением-блоком (копируется целиком нажатием), поэтому без
    лимита кнопки в 256 символов. На сам баннер модель выносит только главное.
    """
    region = region_name(data.region_tag)
    place = data.address if data.address and region.lower() in data.address.lower() else ", ".join(filter(None, [data.address, region]))
    lines = ["VAKANSIYA — barcha ma'lumotlar:", f"Lavozim: {data.headline}"]
    if data.company:
        lines.append(f"Kompaniya: {data.company}")
    if data.intro:
        lines.append(f"Tavsif: {data.intro}")
    if place:
        lines.append(f"Manzil: {place}")
    if data.salary:
        lines.append(f"Maosh: {data.salary}")
    if data.schedule:
        lines.append(f"Ish vaqti: {data.schedule}")
    for title, items in (("Talablar", data.requirements), ("Vazifalar", data.duties), ("Qulayliklar", data.benefits)):
        if items:
            lines.append(f"{title}: " + "; ".join(items))
    for section in data.extra_sections:
        if section.items:
            lines.append(f"{section.title}: " + "; ".join(section.items))
    contacts = ", ".join(filter(None, [data.phone, data.telegram]))
    if contacts:
        lines.append(f"Aloqa: {contacts}")

    must = ["крупно — должность", "зарплата" if data.salary else None, "место" if place else None,
            "график" if data.schedule else None, "1–2 самых сильных преимущества" if data.benefits else None,
            "телефон" if data.phone else None, None if for_logo else "@ishdasiz"]
    task = [
        "",
        "ЗАДАЧА: сделай ГОРИЗОНТАЛЬНЫЙ баннер 16:9 для этой вакансии в Telegram-канал " + ("вакансий." if for_logo else "@ishdasiz."),
        "На баннер вынеси ТОЛЬКО самое важное, текстом на узбекской латинице — ровно как в данных, без ошибок: "
        + ", ".join(m for m in must if m) + ".",
        "Остальные данные — только для понимания контекста, на баннер их не выписывай.",
        "Стиль: сочный, современный, премиальный — яркие контрастные цвета, крупная читаемая типографика, "
        "чёткая иерархия (должность → зарплата → остальное), аккуратная сетка, много воздуха, лёгкая глубина и свет.",
        f"Фон: {scene.strip()}." if scene and scene.strip() else "Фон: реалистичная сцена по теме профессии, люди в работе.",
        "Без водяных знаков, логотипов брендов и лишнего текста.",
    ]
    if for_logo:
        task.append("КОМПОЗИЦИЯ: весь текст размести в верхних 75% высоты кадра, телефон и Telegram — тоже выше этой зоны. "
                    "Нижние 25% кадра — только фон, без текста, плашек и значков; особенно Левый нижний угол (треть ширины) "
                    "должен быть пустым — туда добавят логотип. Фон в нижней зоне — естественное продолжение сцены до самого края "
                    "кадра, а не белая или однотонная полоса.")
    return "\n".join(lines + task)


# ------------------------------------------------------------------ постер для автоподбора (07.10)
# Его референсы: тёмный фон + акцентный цвет, огромный двухцветный заголовок, плашка «ISHGA TAKLIF QILAMIZ!», карточки с иконками
# (зарплата, график), ряд преимуществ, карточка контактов, бейдж возраста, фотореалистичные улыбающиеся люди. Просто «баннер с текстом»
# (build_full_prompt) выглядел как шаблон — этот промпт задаёт целый дизайн.
# И его просьба: «все вакансии не одним и тем же дизайном, каждый раз по-другому» — поэтому не один стиль, а набор DESIGNS: палитра,
# шрифт, раскладка и подача фото у каждого свои. Выбор — pick_design: по профессии (fit), но без повторов подряд.
DESIGNS: list[dict] = [
    {
        "id": "gold_black",
        "fit": (),
        "style": ("Deep black / charcoal background with a warm cinematic gradient. Signature accent: rich GOLD-YELLOW (#FFC400) with a subtle "
                  "metallic gradient and soft glow; secondary text pure white; thin gold outlines; dark glass rounded cards. Ultra-bold "
                  "geometric sans for the headline, clean sans for details."),
        "layout": ("The left ~55% is the text column over a smooth dark gradient that blends seamlessly into the photo that bleeds to the right "
                   "edge. Huge headline at the top-left, the pill under it, then the salary and schedule cards (side by side or stacked), "
                   "the location row, the contact card at the bottom-right over the photo, the round badge in the top-right corner."),
        "photo": ("One confident person, waist-up, looking at the camera, in the real working environment of this job; warm window light, shallow "
                  "depth of field."),
    },
    {
        "id": "fastfood_red",
        "fit": ("oshpaz", "povar", "ofitsiant", "donarchi", "kafe", "restoran", "fast food", "barista", "pitsa", "pizza", "shashlik",
                "qandolat", "повар", "официант", "кафе", "ресторан", "пекар", "кондитер", "bufet", "oshxona"),
        "style": ("Black background with fiery warm lighting; accents RED (#E02424) and golden YELLOW (#FFC400); white text; heavy "
                  "condensed italic headline; red ribbon labels with a slanted edge; a diagonal yellow corner shape."),
        "layout": ("Headline top-left in white and yellow; the pill as a red slanted ribbon label; the salary in a bold black box with a red "
                   "tab over the lower right of the photo; a dark horizontal information strip along the bottom made of three columns "
                   "(schedule | perks | contact) that starts to the right of the reserved logo corner; the location sits in the yellow "
                   "corner shape in the top-right; the age badge, if any, next to it."),
        "photo": ("Dynamic action shot of the worker doing the job (cooking, serving, steam, warm sparks of light), photo fills the right and "
                  "centre, shallow depth of field."),
    },
    {
        "id": "neon_green",
        "fit": ("sotuv", "savdo", "menejer", "operator", "call", "marketing", "agent", "sales", "менеджер", "продаж", "оператор"),
        "style": ("Near-black background with an emerald-black gradient; neon LIME-GREEN accent (#9BE000) with a glow; white text; modern "
                  "rounded geometric sans; frosted glass cards with a soft green glow; a flowing lime swoosh wave in the bottom-right corner."),
        "layout": ("Small hashtag chip top-left; huge headline on two lines (white + lime) on the left; three equal glass cards in a row "
                   "(salary, schedule, location); the pill and the contact block as a lime pill-shaped button with the Telegram icon and "
                   "handle plus the phone next to it near the bottom centre-right; the photo takes the right half."),
        "photo": ("Two confident smiling colleagues in the foreground, a modern open-space office with a busy team softly blurred behind them."),
    },
    {
        "id": "clean_teal",
        "fit": ("shifokor", "klinika", "hamshira", "vrach", "stomatolog", "dorixona", "apteka", "laborant", "врач", "клиник", "медсестр",
                "аптек", "stomatolog", "o'qituvchi", "tarbiyachi", "учител", "воспитател"),
        "style": ("Bright, clean, trustworthy look: white background with soft teal-turquoise (#14A3A8) accents and a deep navy (#0B2A4A) "
                  "headline; soft shadows; light rounded cards with thin teal outlines; a friendly humanist sans."),
        "layout": ("The left ~52% is a white panel with a smooth curved right edge overlapping the photo: large navy + teal headline, the "
                   "pill, two info cards side by side with round teal icon badges, the location row and the perks as compact bullet "
                   "columns with small teal check icons; the contact card as a wide teal rounded bar across the lower part of the photo side."),
        "photo": ("A friendly smiling professional in clean work clothes at a bright, airy, modern reception or clinic interior."),
    },
    {
        "id": "worker_left_dark",
        "fit": ("qurilish", "elektrik", "usta", "payvand", "zavod", "seh", "haydovchi", "kuryer", "ombor", "xavfsizlik", "quruvchi",
                "santexnik", "mexanik", "slesar", "stroit", "электр", "водител", "курьер", "склад", "охран", "сварщ"),
        "style": ("Dark navy-black with a strong industrial feel; accent GOLD-YELLOW (#FFC400) with subtle safety-stripe details; thick "
                  "condensed italic headline; faint haze and warm sparks."),
        "layout": ("Mirrored layout: the full-height photo of the worker fills the LEFT ~48% (its lower-left area dark and calm), the text "
                   "column is on the RIGHT: tag chip, huge two-line headline (white + yellow), a salary card with a big number, the "
                   "schedule card, the location row with a pin icon, the contact card at the bottom-right, the round badge top-right."),
        "photo": ("Dramatic cinematic shot of one worker in protective gear (hard hat, gloves) mid-task, strong rim light and warm sparks."),
    },
    {
        "id": "red_white_bold",
        "fit": ("ishchi", "ishlab chiqarish", "tikuvchi", "yig'uvchi", "sex", "paketlov", "фабрик", "рабоч", "швея", "упаков", "grunt"),
        "style": ("Clean WHITE background with bold RED (#D7191C) and black condensed type, flat vector accents (megaphone icon, speech "
                  "burst), a catalogue-like corporate look; a red wave along the bottom edge that rises only on the right side."),
        "layout": ("A megaphone icon top-left; the headline in two lines (red, then black) on the upper left; the salary inside a red rounded "
                   "rectangle with a white money icon; the contact lines as outlined rounded rows with red icon badges; the photo of the "
                   "people at the bottom-right standing in front of the red wave."),
        "photo": ("Two smiling workers in company uniforms and hard hats, arms crossed, cut out cleanly on the white background."),
    },
    {
        "id": "navy_cyan_tech",
        "fit": ("bank", "kredit", "moliya", "buxgalter", "dasturchi", "programmist", "kompyuter", "банк", "кредит", "бухгалтер",
                "программист", "mikromoliya", "kassir", "inkassator"),
        "style": ("Deep midnight-navy to electric-blue gradient with a subtle hexagon / data-grid pattern and glowing CYAN (#27E1FF) accents; "
                  "futuristic but friendly; white text; geometric sans; thin glowing lines and soft lens flares."),
        "layout": ("The photo of the person on the right with a cyan rim light and glowing circuit-line decorations fading out of it; text on "
                   "the left: huge white headline with a cyan second line, the pill as a glowing outlined capsule, the info cards as "
                   "translucent glass panels with cyan icons, the contact card at the bottom-right."),
        "photo": ("One smiling professional at a desk with a laptop or monitor glow, cool blue ambient light with a cyan rim light."),
    },
    {
        "id": "sunset_orange",
        "fit": ("do'kon", "sotuvchi", "kiyim", "kosmetika", "go'zallik", "bolalar", "moda", "magazin", "мага", "продавец", "одежд", "salon",
                "konsultant", "консультант"),
        "style": ("Vibrant sunset gradient (orange #FF7A18 → hot pink #FF2E63 → deep purple), energetic and glossy; white text with soft "
                  "shadows; chunky rounded sans; confetti-like geometric shapes and a big diagonal split."),
        "layout": ("A bold diagonal split: the gradient with the text on the left; the cut-out person on the right overlapping the diagonal "
                   "and slightly the end of the headline; the pill as a white capsule with colourful text; info cards as white rounded cards "
                   "with gradient icon badges; the contact as a white pill at the bottom-right."),
        "photo": ("A cheerful person cut out with a soft white outline and glow, dynamic friendly pose, hints of a shop or salon behind."),
    },
    {
        "id": "emerald_cream",
        "fit": ("mehmonxona", "hotel", "administrator", "resepshn", "reception", "salon", "ofitsiant", "администратор", "ресепшн",
                "gostinitsa", "menejer-administrator"),
        "style": ("Premium emerald-green (#0F5C4A) and warm cream (#F6EFE0) with fine brass-gold (#C9A24B) lines; elegant: a refined serif "
                  "headline mixed with a clean sans; a subtle paper texture and a thin ornamental frame."),
        "layout": ("A cream panel on the left carries the text: the headline in deep green serif with the second line in gold, the pill, "
                   "details as elegant rows with thin line icons separated by hairlines; the photo on the right inside a tall arch-shaped "
                   "frame with a soft green glow; the contact in an emerald rounded rectangle at the bottom-right."),
        "photo": ("An elegant, warm, upscale environment; one polished smiling person in smart business-casual or a neat uniform."),
    },
    {
        "id": "purple_glass",
        "fit": (),
        "style": ("Rich violet (#5B2EFF) to deep indigo to magenta (#C026D3) gradient with glassmorphism: frosted translucent cards, soft "
                  "glowing orbs and light streaks; white text; a modern rounded sans."),
        "layout": ("The photo of the person inside a large circle with a glowing ring on the right-centre, partly overlapping the headline "
                   "block; text on the left; frosted glass cards for salary and schedule; the contact in a glowing pill at the bottom-right."),
        "photo": ("A friendly person in a bright modern setting, framed in a circle, soft bokeh."),
    },
    {
        "id": "yellow_pop",
        "fit": ("kuryer", "yetkazib", "haydovchi", "taksi", "talaba", "kurier", "курьер", "студент", "promouter", "targ'ibotchi"),
        "style": ("Bold BRAND-YELLOW (#FFD400) background with thick black type and black geometric blocks — a high-contrast pop-poster look; "
                  "a black diagonal band; star-burst stickers and hand-drawn arrows; a chunky heavy grotesque headline."),
        "layout": ("The headline huge in black on yellow at the top-left; the salary inside a black star-burst sticker overlapping the photo; "
                   "a black diagonal band carries the schedule and perks in yellow and white; the contact card black with yellow text at the "
                   "bottom-right; the person cut out on the right with a thick sticker outline."),
        "photo": ("An energetic cheerful person, cut-out sticker style with a thick white-and-black outline."),
    },
    {
        "id": "editorial_light",
        "fit": (),
        "style": ("Minimal editorial magazine look: off-white paper (#F4F1EA), a huge black sans-serif headline with tight tracking, one bold "
                  "accent colour (vermilion #FF4B2B), thin black rules, generous white space, small-caps labels."),
        "layout": ("A huge headline across the top-left in black with the second line in vermilion; a large circular photo cut-out on the right "
                   "overlapping a thin ring; the details in a clean grid below separated by thin rules with small outlined icons; the "
                   "contact line at the bottom-right with a vermilion underline; the age badge as a vermilion circle."),
        "photo": ("A natural lifestyle portrait in soft daylight, a relaxed smile, the working environment gently blurred."),
    },
]
_DESIGN_BY_ID = {d["id"]: d for d in DESIGNS}
RECENT_AVOID = 6                  # столько последних дизайнов не повторяем


def _plain(text: str) -> str:
    return text.lower().replace("ʻ", "'").replace("‘", "'").replace("’", "'").replace("ё", "е")


def pick_design(data: VacancyData, scene: str | None = None, *, recent: tuple[str, ...] | list[str] = (), seed: str = "") -> dict:
    """Дизайн постера: сначала по профессии (fit), но не из последних RECENT_AVOID; нет подходящих — любой из «давно не было».
    seed делает выбор воспроизводимым (id вакансии + номер перерисовки), а перерисовка получает другой дизайн."""
    import hashlib

    text = _plain(" ".join(filter(None, [data.headline, data.company, scene])))
    recent = list(recent)
    fresh = [d for d in DESIGNS if d["id"] not in recent[:RECENT_AVOID]]
    fitting = [d for d in fresh if any(word in text for word in d["fit"])]
    pool = fitting or fresh or [d for d in DESIGNS if d["id"] != (recent[0] if recent else "")] or DESIGNS
    return pool[int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(pool)]


def split_headline(headline: str) -> tuple[str, str]:
    """«Kredit menejeri kerak» → («KREDIT», «MENEJERI KERAK»): две строки заголовка (первая белая, вторая акцентная)."""
    words = re.sub(r"\s+", " ", headline or "").strip(" !.").upper().split()
    if len(words) <= 1:
        return " ".join(words), ""
    cut = max(1, len(words) // 2)
    return " ".join(words[:cut]), " ".join(words[cut:])


def age_badge(data: VacancyData) -> str | None:
    for item in data.requirements:
        low = item.lower()
        found = re.search(r"(\d{2})\s*[-–—]\s*(\d{2})\s*(?:yosh|лет|yoshgacha|yoshdan)", low)
        if found:
            return f"{found.group(1)}–{found.group(2)} yosh"
        found = re.search(r"(\d{2})\s*(?:yoshdan|yosh va|dan katta|\+)", low)
        if found:
            return f"{found.group(1)}+ yosh"
    return None


def pretty_phone(phone: str | None) -> str | None:
    first = (phone or "").split("|")[0].strip()
    digits = re.sub(r"\D", "", first)
    if len(digits) == 12 and digits.startswith("998"):
        return f"+998 {digits[3:5]} {digits[5:8]} {digits[8:10]} {digits[10:12]}"
    return first or None


def cut_words(text: str | None, limit: int) -> str | None:
    """Обрезка по границе слова БЕЗ многоточия (модель рисует «…» буквально) — запасной путь, если нейросеть не дала короткий текст."""
    text = re.sub(r"\s+", " ", str(text or "")).strip(" .;,:-–—")
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    if cut.count("(") > cut.count(")"):          # не оставляем висящую скобку
        cut = cut[: cut.rfind("(")]
    return cut.strip(" .;,:-–—(") or None


def poster_title(data: VacancyData) -> str:
    """Крупный заголовок плаката: короткий от нейросети (2–4 слова) или обрезанный полный."""
    return data.short_title or cut_words(data.headline, 38) or "Xodim kerak"


def build_poster_prompt(data: VacancyData, *, scene: str | None = None, design: dict | str | None = None) -> str:
    """Промпт постера для Nano Banana: дизайн-бриф выбранного стиля (палитра, раскладка, фото) + ТОЧНЫЕ тексты + зона под логотип слева внизу."""
    if isinstance(design, str):
        design = _DESIGN_BY_ID.get(design)
    design = design or _DESIGN_BY_ID["gold_black"]
    line1, line2 = split_headline(poster_title(data))
    region = region_name(data.region_tag)
    full_place = data.address if data.address and region.lower() in data.address.lower() else ", ".join(filter(None, [data.address, region]))
    place = data.short_place or cut_words(full_place, 40)
    pill = "YANGI VAKANSIYA!" if "taklif" in (data.headline or "").lower() else "ISHGA TAKLIF QILAMIZ!"
    phone = pretty_phone(data.phone)
    handle = vac_handle(data.telegram)
    texts = [f'Small tag (letter-spaced, accent colour): "{data.region_tag}"',
             f'HEADLINE — huge, ALL CAPS, two lines: line 1 "{line1}"'
             + (f', line 2 in the accent colour "{line2}"' if line2 else ""),
             f'Pill label (accent-coloured rounded label, bold): "{pill}"']
    if data.company:
        texts.append(f'Company name (small, near the pill): "{cut_words(data.company, 40)}"')
    salary = data.short_salary or cut_words(data.salary, 32)
    if salary:
        texts.append(f'Info card with a wallet icon in a circle — label "Oylik maosh:" and below it the value, large, bold, accent colour: "{salary}"')
    schedule = data.short_schedule or cut_words(data.schedule, 30)
    if schedule:
        texts.append(f'Info card with a clock icon in a circle — label "Ish vaqti:" and below it, bold: "{schedule}"')
    if place:
        texts.append(f'Location row with a map-pin icon: "{place}"')
    perks = [p for p in (data.short_perks or [cut_words(b, 22) for b in data.benefits]) if p and len(p) >= 4][:4]
    if perks:
        texts.append("Perks row — " + str(len(perks)) + " small line icons, each with a 2–3 word caption under it, exactly: "
                     + ", ".join(f'"{p}"' for p in perks))
    badge = age_badge(data)
    if badge:
        texts.append(f'Round accent-coloured badge: "{badge}"')
    contacts = []
    if phone:
        contacts.append(f'a phone icon followed by the text "{phone}"')
    if handle:
        contacts.append(f'a Telegram paper-plane icon followed by the text "{handle}"')
    if contacts:
        texts.append("Contact card styled to match the design: " + " ; ".join(contacts))
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
    subject = (scene or "").strip().rstrip(".")
    return "\n".join([
        "Design a premium, scroll-stopping JOB VACANCY POSTER for a Telegram jobs channel in Uzbekistan. Landscape 3:2, ultra-sharp, "
        "professional advertising-agency quality — the level of top recruitment ads, not a plain stock template.",
        "",
        f"STYLE: {design['style']}",
        "",
        f"COMPOSITION: {design['layout']} Clear hierarchy: headline → salary → details → contact. Consistent generous margins, perfect "
        "alignment, crisp vector-clean thin line icons inside circles, soft glow and depth. Dense but tidy, every element intentional.",
        "",
        "PHOTO (photorealistic): " + (f"scene — {subject}; " if subject else "") + f"job — {poster_title(data)}. {design['photo']} "
        "People of Central Asian (Uzbek) appearance, realistic faces and hands, wearing work clothes typical for this job; shot on an 85mm "
        "lens; the real working environment of this profession is visible.",
        "",
        "TEXT — write every string EXACTLY as given between the quotes, in Uzbek Latin, letter for letter, keeping the same apostrophes, digits "
        "and spacing. Add no other words, no placeholder or gibberish text:",
        numbered,
        "",
        "RESERVED ZONE: the bottom-left corner (left third of the width, bottom 18% of the height) must look like the ordinary background — "
        "the same gradient, texture or photo simply continues through it. NO panel, plate, box, card, outline, bar, rounded shape or contrasting "
        "rectangle there, and no text or icons; bottom strips or bars start to the right of it. A logo will be pasted into that spot later — "
        "do not draw any logo, placeholder or channel name yourself.",
        "ONLY THE LISTED ELEMENTS: draw only what is listed under TEXT. If the layout above mentions an element that is not listed (a badge, "
        "perks, a company name, a second card), leave it out completely. Never draw seals, stamps or stickers carrying words, never repeat a "
        "word of the headline, and never write labels such as \"badge\", \"logo\" or \"approved\".",
        "QUALITY: flawless spelling, sharp edges, no distorted letters, no watermark, no extra logos.",
    ])


def vac_handle(value: str | None) -> str | None:
    name = username_from_telegram(value)
    return f"@{name}" if name else None


def default_image_prompt(headline: str) -> str:
    return build_image_prompt(VacancyData(headline=headline, intro=None, company=None, region_tag=VACANCY_DEFAULT_REGION_TAG,
                                          address=None, salary=None, schedule=None))


# ------------------------------------------------------------------ render
def _append_blank(lines: list[str]) -> None:
    if lines and lines[-1] != "":
        lines.append("")


def _section(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    lines.append(title)
    lines.extend(f"{_BULLET} {_h(item)}" for item in items)
    _append_blank(lines)


def format_vacancy_post(data: VacancyData, *, premium: bool = True, footer_url: str = "https://t.me/ishdasiz") -> str:
    lines: list[str] = [f"{_emoji('top', premium)} <b><i>{_h(data.headline)}</i></b>", _VACANCY_DIVIDER, ""]

    if data.intro:
        lines.append(f"{_emoji('intro', premium)} {_h(data.intro)}")
        _append_blank(lines)

    if data.company:
        lines.append(f"<b>Kompaniya:</b> <b>{_h(data.company)}</b>")
    lines.append(f"<b>Hudud:</b> <b>{_h(data.region_tag.upper())}</b>")
    if data.address:
        lines.append(f"{_emoji('location', premium)} <b>Manzil:</b> {_h(data.address)}")
    _append_blank(lines)

    if data.salary:
        lines.append(f"{_emoji('salary', premium)} <b>Oylik maosh:</b>")
        lines.append(_h(data.salary))
        _append_blank(lines)

    if data.schedule:
        lines.append(f"{_emoji('schedule', premium)} <b>Ish vaqti:</b>")
        lines.append(_h(data.schedule))
        _append_blank(lines)

    _section(lines, f"{_emoji('requirements', premium)} <b>Talablar:</b>", data.requirements)
    _section(lines, f"{_emoji('duties', premium)} <b>Vazifalar:</b>", data.duties)
    _section(lines, f"{_emoji('benefits', premium)} <b>Qulayliklar:</b>", data.benefits)
    for section in data.extra_sections:
        _section(lines, f"{_emoji('extra', premium)} <b>{_h(section.title.rstrip(':'))}:</b>", section.items)

    if data.phone:
        lines.append(f"{_emoji('phone', premium)} <b>Aloqa:</b> {_h(data.phone)}")
    if data.telegram:
        lines.append(f"{_emoji('telegram', premium)} <b>Telegram:</b> {_h(data.telegram)}")
    if data.phone or data.telegram:
        _append_blank(lines)

    quote_text = _h(_VACANCY_DISCLAIMER_LINES[0]) + "\n" + _h(_VACANCY_DISCLAIMER_LINES[1])
    lines.append(f"<blockquote>{quote_text}</blockquote>")
    _append_blank(lines)
    lines.append(f'{_emoji("footer", premium)} <a href="{footer_url}"><b>ISHDASIZ</b></a> - <b>{_h(_VACANCY_FOOTER_TEXT)}</b>')

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# ------------------------------------------------------------------ detect
_VACANCY_KEYWORDS = (
    "вакан", "требует", "требуется", "должность", "зарплат", "оклад", "график", "обязанност", "требован",
    "ish kerak", "ishga kerak", "ishga olamiz", "ishga taklif", "vakans", "bo'sh ish", "bo‘sh ish", "lavozim",
    "xodim kerak", "maosh", "ish vaqti", "talablar", "vazifalar", "qulayliklar", "ish haqi",
    "vacancy", "hiring", "salary", "requirements", "responsibilities",
)
_JOB_WORDS = (
    "kerak", "ishga", "vakans", "вакан", "требуется", "bo'sh ish", "bo‘sh ish", "lavozim", "xodim", "ish o'rni",
    "taklif qilamiz", "ищем", "приглаша", "hiring",
)


def looks_like_vacancy(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if len(normalized) < 16:
        return False
    hits = sum(1 for token in _VACANCY_KEYWORDS if token in normalized)
    has_phone = bool(extract_phones(normalized))
    has_tg = "t.me/" in normalized or "telegram" in normalized or "телеграм" in normalized or "@" in normalized
    has_job_word = any(token in normalized for token in _JOB_WORDS)
    if has_job_word and (has_phone or has_tg or hits >= 1):
        return True
    return hits >= 2 or (hits >= 1 and has_phone) or (has_phone and has_tg)
