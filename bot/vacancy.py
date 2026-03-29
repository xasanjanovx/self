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
    "title": ("✅", "6307344346748290621"),
    "location": ("📍", "5886446115905082831"),
    "salary": ("💰", "5348418461838098123"),
    "schedule": ("🕔", "5258419835922030550"),
    "requirements": ("⚠️", "5881702736843511327"),
    "benefits": ("✅", "5985596818912712352"),
    "duties": ("❗️", "5879813604068298387"),
    "details": ("ℹ️", "5875465628285931233"),
    "phone": ("📞", "5897938112654348733"),
    "telegram": ("✈️", "5875465628285931233"),
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
        contact_url = build_contact_url(f"@{username}")
        if contact_url:
            return f'<a href="{html.escape(contact_url)}"><b>@{_h(username)}</b></a>'
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


def format_vacancy_post(data: VacancyTemplateData, *, premium: bool = True) -> str:
    titles = _clean_list(data.titles)
    region = _clean_value(data.region_tag)
    if region and not region.startswith("#"):
        region = f"#{region}"
    address = _clean_value(data.address)
    salary = _clean_value(data.salary)
    schedule = _clean_value(data.schedule)
    requirements = _clean_list(data.requirements)
    benefits = _clean_list(data.benefits)
    duties = _clean_list(data.duties)
    details = _clean_list(data.details)
    phone = _clean_value(data.phone)
    telegram = _telegram_block(data.telegram)

    first_title = titles[0] if titles else "Xodim"
    first_region = region.upper().lstrip("#") if region else None
    first_line = f"{first_region}ga {first_title} kerak!" if first_region else f"{first_title} kerak!"

    lines: list[str] = [
        f"{_emoji('top', premium)} <b>{_h(first_line)}</b>",
        "<i>Quyida vakansiya bo'yicha aniq va to'liq ma'lumotlar.</i>",
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

    _append_section(lines, f"{_emoji('requirements', premium)} <b>Talablar:</b>", requirements)
    _append_section(lines, f"{_emoji('benefits', premium)} <b>Qulayliklar:</b>", benefits)
    _append_section(lines, f"{_emoji('duties', premium)} <b>Vazifalar:</b>", duties)
    _append_section(lines, f"{_emoji('details', premium)} <b>Qo'shimcha ma'lumotlar:</b>", details)

    if phone:
        lines.append(f"{_emoji('phone', premium)} <b>Aloqa:</b> {_h(phone)}")
    if telegram:
        lines.append(f"{_emoji('telegram', premium)} <b>Telegram:</b> {telegram}")

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
