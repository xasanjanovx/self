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
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("998"):
        return f"+998 {digits[3:5]} {digits[5:8]} {digits[8:10]} {digits[10:12]}"
    return raw


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
    if not data.image_prompt:
        data.image_prompt = default_image_prompt(data.headline)
    return data


def default_image_prompt(headline: str) -> str:
    return (
        f"Сочная, привлекательная фотореалистичная сцена по теме вакансии «{headline}»: "
        "современное рабочее место, довольные сотрудники за работой, тёплый естественный свет, яркие живые цвета. "
        "Горизонтальный формат 16:9, фотореалистично, без текста и логотипов."
    )


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


def format_image_prompt_message(prompt: str, lang: str = "ru") -> str:
    title = "🖼 <b>Rasm uchun prompt (ChatGPT)</b>" if lang == "uz" else "🖼 <b>Промпт для картинки (ChatGPT)</b>"
    hint = (
        "Bosib nusxalang va ChatGPT/DALL·E ga yuboring."
        if lang == "uz"
        else "Нажми на текст — скопируется. Вставь в ChatGPT."
    )
    return f"{title}\n<code>{_h(prompt)}</code>\n<i>{hint}</i>"


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
