from __future__ import annotations

import html
import re
from urllib.parse import quote

from .ai import VacancyTemplateData

VACANCY_DEFAULT_REGION_TAG = "#TOSHKENT"
VACANCY_CONTACT_TEMPLATE = (
    "Assalomu Alaykum. @ishdasiz kanalida joylashtirilgan vakansiya bo'yicha bezovta qilyapman. "
    "Menga to'liqroq ma'lumo bera olasizmi ?"
)

_EMOJI_META = {
    "top": ("✅", "5389061359403039918"),
    "title": ("✅", "6307344346748290621"),
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
    "vakans",
    "bo'sh ish",
    "bo‘sh ish",
    "maosh",
    "aloqa",
)


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


def _clean_titles(titles: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for title in titles:
        clean = _clean_value(title)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _clean_items(items: list[str]) -> list[str]:
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


def _telegram_block(value: str | None) -> str | None:
    username = _username_from_telegram(value)
    if username:
        return f"@{_h(username)}"
    clean = _clean_value(value)
    return _h(clean) if clean else None


def build_contact_url(telegram_value: str | None) -> str | None:
    username = _username_from_telegram(telegram_value)
    if not username:
        return None
    return f"https://t.me/{username}?text={quote(VACANCY_CONTACT_TEMPLATE, safe='')}"


def build_vacancy_panel_text(lang: str = "ru") -> str:
    if lang == "uz":
        return (
            "📣 <b>Vakansiya shabloni</b>\n\n"
            "Vakansiya matnini yoki forward qilingan postni yuboring.\n"
            "Bot bo'sh kriteriyalarni chiqarib tashlaydi va postni aniq ko'rinishga keltiradi.\n"
            "Keyin xohlasangiz, preview foto yuborib matn bilan birlashtirishingiz mumkin."
        )
    return (
        "📣 <b>Шаблон вакансии</b>\n\n"
        "Пришли текст вакансии или перешли пост.\n"
        "Бот уберет пустые критерии и соберет аккуратный пост.\n"
        "Потом можно отправить фото превью, и бот объединит его с этим текстом."
    )


def _append_blank(lines: list[str]) -> None:
    if lines and lines[-1] != "":
        lines.append("")


def format_vacancy_post(data: VacancyTemplateData, *, premium: bool = True) -> str:
    titles = _clean_titles(data.titles)
    region = _clean_value(data.region_tag)
    if region and not region.startswith("#"):
        region = f"#{region}"
    address = _clean_value(data.address)
    salary = _clean_value(data.salary)
    schedule = _clean_value(data.schedule)
    requirements = _clean_items(data.requirements)
    benefits = _clean_items(data.benefits)
    duties = _clean_items(data.duties)
    phone = _clean_value(data.phone)
    telegram = _telegram_block(data.telegram)

    first_title = titles[0] if titles else "Xodim"
    first_region = region.upper() if region else None
    if first_region:
        first_line = f"{first_region.lstrip('#')}ga {first_title} kerak!"
    else:
        first_line = f"{first_title} kerak!"

    lines: list[str] = [
        f"{_emoji('top', premium)} <b>{_h(first_line)}</b>",
        "<i>Diqqat: dolzarb vakansiya, quyida batafsil ma'lumot.</i>",
        "",
    ]

    if len(titles) > 1:
        lines.append("<b>Bo'sh ish o'rinlari:</b>")
        for title in titles:
            lines.append(f"{_emoji('title', premium)} <b>{_h(title)}</b>")
        _append_blank(lines)

    if region:
        lines.append(f"{_emoji('location', premium)} <b>Hudud:</b> <b>{_h(region.upper())}</b>")
    if address:
        lines.append(f"<b>Manzil:</b> {_h(address)}")
    if region or address:
        _append_blank(lines)

    if salary:
        lines.append(f"{_emoji('salary', premium)} <b>Oylik maosh:</b>")
        lines.append(_h(salary))
        _append_blank(lines)

    if schedule:
        lines.append(f"{_emoji('schedule', premium)} <b>Ish vaqti:</b>")
        lines.append(_h(schedule))
        _append_blank(lines)

    if requirements:
        lines.append(f"{_emoji('requirements', premium)} <b>Talablar:</b>")
        lines.extend(f"- {_h(item)}" for item in requirements)
        _append_blank(lines)

    if benefits:
        lines.append(f"{_emoji('benefits', premium)} <b>Qulayliklar:</b>")
        lines.extend(f"- {_h(item)}" for item in benefits)
        _append_blank(lines)

    if duties:
        lines.append(f"{_emoji('duties', premium)} <b>Vazifalar:</b>")
        lines.extend(f"- {_h(item)}" for item in duties)
        _append_blank(lines)

    if phone:
        lines.append(f"{_emoji('phone', premium)} <b>Aloqa:</b> {_h(phone)}")
    if telegram:
        lines.append(f"{_emoji('telegram', premium)} <b>Telegram:</b> {telegram}")

    _append_blank(lines)
    lines.append(f"{_emoji('footer', premium)} <b>ISHDASIZ</b> - <b>Tez va oson ish toping!</b>")

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def looks_like_vacancy(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return False

    hits = sum(1 for token in _VACANCY_KEYWORDS if token in normalized)
    has_phone = bool(re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", normalized))
    has_tg = "t.me/" in normalized or "telegram" in normalized or "телеграм" in normalized

    return hits >= 2 or (hits >= 1 and has_phone) or (has_phone and has_tg)
