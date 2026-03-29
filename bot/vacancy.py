from __future__ import annotations

import html
import re
from urllib.parse import quote

from .ai import VacancyTemplateData

VACANCY_DEFAULT_REGION_TAG = "#TOSHKENT"
VACANCY_CONTACT_TEMPLATE = (
    "Assalomu Alaykum. @ishdasiz kanalida joylashtirilgan vakansiya bo'yicha bezovta qilyapman. "
    "Menga to'liqroq ma'lumot bera olasizmi ?"
)

_EMOJI_META = {
    "top": ("✅", "5389061359403039918"),
    "openings": ("✅", "6307344346748290621"),
    "location": ("📍", "5886446115905082831"),
    "salary": ("💰", "5348418461838098123"),
    "schedule": ("🕔", "5258419835922030550"),
    "requirements": ("⚠️", "5881702736843511327"),
    "benefits": ("✅", "5985596818912712352"),
    "duties": ("❗️", "5879813604068298387"),
    "phone": ("📞", "5897938112654348733"),
    "telegram": ("✈️", "5875465628285931233"),
    "footer": ("➡️", "5260450573768990626"),
}

_VACANCY_KEYWORDS = (
    "вакан",
    "требует",
    "требуется",
    "работа",
    "зарплат",
    "оклад",
    "контакт",
    "телеграм",
    "ish kerak",
    "ishga kerak",
    "ishga olamiz",
    "ishga taklif",
    "vakans",
    "vakansiya",
    "bo'sh ish",
    "bo‘sh ish",
    "bo'sh ish o'rni",
    "bo'sh ish o'rinlari",
    "lavozim",
    "xodim kerak",
    "maosh",
    "aloqa",
    "ish vaqti",
    "talablar",
    "vazifalar",
)
_VACANCY_SECTION_TOKENS = (
    "hudud",
    "manzil",
    "maosh",
    "ish vaqti",
    "talab",
    "qulaylik",
    "vazifa",
    "aloqa",
    "telegram",
    "адрес",
    "зарплат",
    "график",
    "треб",
    "услов",
    "обязан",
    "контакт",
)
_VACANCY_DISCLAIMER_LINES = (
    "❗️E'lonlardagi ma'lumotlar uchun kanal ma'muriyati javobgar emas. "
    "Shaxsiy ma'lumotlaringizni bermang, ish beruvchi pul so'rasa - adminni ogohlantiring.",
    "Ogoh bo'ling!",
)
_VACANCY_FOOTER_TEXT = "ISHDASIZ - Tez va oson ish toping!"
_VACANCY_DIVIDER = "_ _ _ _ _ _ _ _ _ _ _"


def _emoji(name: str, premium: bool) -> str:
    fallback, emoji_id = _EMOJI_META[name]
    if not premium:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _h(value: str) -> str:
    return html.escape(value, quote=False)


def _clean_value(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return None
    return raw


def _clean_list(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = _clean_value(item)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _username_from_telegram(value: str | None) -> str | None:
    handle = _clean_value(value)
    if not handle:
        return None

    if handle.startswith("@"):
        username = handle[1:].strip()
        return username if username else None

    match = re.search(r"t\.me/([A-Za-z0-9_]{3,})", handle, flags=re.IGNORECASE)
    if match:
        return match.group(1)

    return None


def build_contact_url(telegram_value: str | None) -> str | None:
    username = _username_from_telegram(telegram_value)
    if not username:
        return None
    encoded = quote(VACANCY_CONTACT_TEMPLATE, safe="")
    return f"tg://resolve?domain={username}&text={encoded}"


def _telegram_block(value: str | None) -> str | None:
    username = _username_from_telegram(value)
    if username:
        return f"@{_h(username)}"
    clean = _clean_value(value)
    return _h(clean) if clean else None


def build_vacancy_panel_text(lang: str = "ru") -> str:
    if lang == "uz":
        return (
            "📣 <b>Vakansiya shabloni</b>\n\n"
            "Vakansiya matnini yoki forward qilingan postni yuboring.\n"
            "Bot manbadagi ma'lumotlarni to'liq yig'ib, reklama qismini olib tashlaydi.\n"
            "Keyin xohlasangiz preview foto yuborib matn bilan birlashtirishingiz mumkin."
        )
    return (
        "📣 <b>Шаблон вакансии</b>\n\n"
        "Пришли текст вакансии или перешли пост.\n"
        "Бот соберет все данные из источника и уберет рекламу чужого канала.\n"
        "Потом можно отправить фото превью, и бот объединит его с этим текстом."
    )


def _append_blank(lines: list[str]) -> None:
    if lines and lines[-1] != "":
        lines.append("")


def _append_section(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    lines.append(title)
    lines.extend(f"- {_h(item)}" for item in items)
    _append_blank(lines)


def _remove_cross_duplicates(items: list[str], blocked: set[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        key = item.casefold().strip()
        if not key or key in blocked:
            continue
        blocked.add(key)
        cleaned.append(item)
    return cleaned


def _build_headline(
    data: VacancyTemplateData,
    titles: list[str],
    region: str | None,
    company: str | None,
) -> str:
    headline = _clean_value(data.headline)
    if headline:
        return headline

    first_title = titles[0] if titles else "Xodim"
    first_region = region.upper().lstrip("#") if region else None
    if first_region and company:
        return f"{first_region}ga {first_title} ({company})"
    if first_region:
        return f"{first_region}ga {first_title} kerak"
    if company:
        return f"{first_title} ({company})"
    return f"{first_title} kerak"


def format_vacancy_post(data: VacancyTemplateData, *, premium: bool = True) -> str:
    titles = _clean_list(data.titles)[:50]
    titles = [
        title
        for title in titles
        if not title.casefold().strip().startswith(("kompaniya", "компания", "ish beruvchi", "работодатель"))
    ]
    region = _clean_value(data.region_tag)
    if region and not region.startswith("#"):
        region = f"#{region}"
    address = _clean_value(data.address)
    salary = _clean_value(data.salary)
    schedule = _clean_value(data.schedule)
    company = _clean_value(data.company)
    headline = _build_headline(data, titles, region, company)
    blocked_keys: set[str] = set()
    blocked_keys.update(item.casefold().strip() for item in titles if item.strip())
    for direct_value in (region or "", address or "", salary or "", schedule or "", company or "", headline):
        if direct_value.strip():
            blocked_keys.add(direct_value.casefold().strip())
    requirements = _remove_cross_duplicates(_clean_list(data.requirements), blocked_keys)
    benefits = _remove_cross_duplicates(_clean_list(data.benefits), blocked_keys)
    duties = _remove_cross_duplicates(_clean_list(data.duties), blocked_keys)
    details = _remove_cross_duplicates(_clean_list(data.details), blocked_keys)
    details = [
        item
        for item in details
        if not item.casefold().strip().startswith(
            (
                "hudud",
                "manzil",
                "maosh",
                "oylik maosh",
                "ish vaqti",
                "talab",
                "qulaylik",
                "vazifa",
                "aloqa",
                "telegram",
                "адрес",
                "зарплат",
                "график",
                "контакт",
            )
        )
    ]
    phone = _clean_value(data.phone)
    telegram = _telegram_block(data.telegram)

    lines: list[str] = [
        f"{_emoji('top', premium)} <b>{_h(headline)}</b>",
        _VACANCY_DIVIDER,
        "",
    ]

    if titles:
        lines.append("<b>Bo'sh ish o'rinlari:</b>")
        for title in titles:
            lines.append(f"{_emoji('openings', premium)} {_h(title)}")
        _append_blank(lines)

    if company:
        lines.append(f"Kompaniya: <b>{_h(company)}</b>")
    if region:
        lines.append(f"Hudud: <b>{_h(region.upper())}</b>")
    if address:
        lines.append(f"Manzil: {_h(address)}")
    if company or region or address:
        _append_blank(lines)

    if salary:
        lines.append(f"{_emoji('salary', premium)} Oylik maosh:")
        lines.append(_h(salary))
        _append_blank(lines)

    if schedule:
        lines.append(f"{_emoji('schedule', premium)} Ish vaqti:")
        lines.append(_h(schedule))
        _append_blank(lines)

    _append_section(lines, f"{_emoji('requirements', premium)} Talablar:", requirements)
    _append_section(lines, f"{_emoji('benefits', premium)} Qulayliklar:", benefits)
    _append_section(lines, f"{_emoji('duties', premium)} Vazifalar:", duties)
    _append_section(lines, "Qo'shimcha ma'lumotlar:", details)

    if phone:
        lines.append(f"{_emoji('phone', premium)} Aloqa: {_h(phone)}")
    if telegram:
        lines.append(f"{_emoji('telegram', premium)} Telegram: {telegram}")
    if phone or telegram:
        _append_blank(lines)

    lines.append(_h(_VACANCY_DISCLAIMER_LINES[0]))
    lines.append(_h(_VACANCY_DISCLAIMER_LINES[1]))
    _append_blank(lines)
    lines.append(f"{_emoji('footer', premium)} {_h(_VACANCY_FOOTER_TEXT)}")

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def looks_like_vacancy(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return False

    hits = sum(1 for token in _VACANCY_KEYWORDS if token in normalized)
    section_hits = sum(1 for token in _VACANCY_SECTION_TOKENS if token in normalized)
    has_phone = bool(re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", normalized))
    has_tg = "t.me/" in normalized or "telegram" in normalized or "телеграм" in normalized or "@" in normalized
    has_job_word = any(
        token in normalized
        for token in (
            "kerak",
            "ishga",
            "vakans",
            "вакан",
            "требуется",
            "bo'sh ish",
            "bo‘sh ish",
            "lavozim",
            "xodim",
            "ish o'rni",
        )
    )

    if has_job_word and (has_phone or has_tg or section_hits >= 1):
        return True
    if section_hits >= 3 and has_job_word:
        return True
    return hits >= 2 or (hits >= 1 and has_phone) or (has_phone and has_tg)
