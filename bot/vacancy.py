from __future__ import annotations

import html
import re

from .ai import VacancyTemplateData

VACANCY_DEFAULT_REGION_TAG = "#TOSHKENT"

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


def _list_block(items: list[str]) -> str:
    if not items:
        return "-"
    return "\n".join(f"- {_h(item)}" for item in items)


def _titles_block(titles: list[str], premium: bool) -> str:
    if not titles:
        return f"{_emoji('title', premium)} <b>-</b>"
    return "\n".join(f"{_emoji('title', premium)} <b>{_h(title)}</b>" for title in titles)


def _telegram_block(value: str | None) -> str:
    if not value:
        return "-"
    handle = value.strip()
    if handle.startswith("@"):
        username = handle[1:]
        return f'<a href="https://t.me/{html.escape(username)}"><b>{html.escape(handle)}</b></a>'
    return _h(handle)


def build_vacancy_panel_text(lang: str = "ru") -> str:
    if lang == "uz":
        return (
            "📣 <b>Vakansiya shabloni</b>\n\n"
            "Vakansiya matnini yoki forward qilingan postni yuboring.\n"
            "Bot uni avtomatik ravishda premium emoji bilan sizning formatga soladi."
        )
    return (
        "📣 <b>Шаблон вакансии</b>\n\n"
        "Пришли текст вакансии или перешли пост.\n"
        "Бот сам соберет его в твой шаблон с premium emoji."
    )


def format_vacancy_post(data: VacancyTemplateData, *, premium: bool = True) -> str:
    region = data.region_tag if data.region_tag.startswith("#") else f"#{data.region_tag}"
    address = data.address if data.address and data.address != "-" else "-"
    salary = data.salary if data.salary and data.salary != "-" else "-"
    schedule = data.schedule if data.schedule and data.schedule != "-" else "-"
    phone = data.phone or "-"
    telegram = _telegram_block(data.telegram)

    lines = [
        f"<b>{_emoji('top', premium)}</b>",
        "— — — — — — — — — — —",
        "<b>Bo'sh ish o'rinlari:</b>",
        _titles_block(data.titles, premium),
        "",
        f"{_emoji('location', premium)}<b>Hudud:</b> <b>{_h(region.upper())}</b>",
        f"<b>Manzil:</b> {_h(address)}",
        "",
        f"{_emoji('salary', premium)}<b>Oylik maosh:</b>",
        _h(salary),
        "",
        f"{_emoji('schedule', premium)}<b>Ish vaqti:</b>",
        _h(schedule),
        "",
        f"{_emoji('requirements', premium)}<b>Talablar:</b>",
        _list_block(data.requirements),
        "",
        f"{_emoji('benefits', premium)}<b>Qulayliklar:</b>",
        _list_block(data.benefits),
        "",
        f"<b>{_emoji('duties', premium)}</b><b>Vazifalar:</b>",
        _list_block(data.duties),
        "",
        f"{_emoji('phone', premium)}<b>Aloqa:</b> {_h(phone)}",
        f"{_emoji('telegram', premium)}<b>Telegram:</b> {telegram}",
        "",
        (
            "<blockquote><b>❗️E'lonlardagi ma'lumotlar uchun kanal ma'muriyati javobgar emas. "
            "Shaxsiy ma'lumotlaringizni bermang, ish beruvchi pul so'rasa - adminni ogohlantiring.\n"
            "Ogoh bo'ling!</b></blockquote>"
        ),
        "",
        f"{_emoji('footer', premium)} <a href=\"https://t.me/ishdasiz\"><b>ISHDASIZ</b></a> - "
        "<b>Tez va oson ish toping!</b>",
    ]
    return "\n".join(lines)


def looks_like_vacancy(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return False

    hits = sum(1 for token in _VACANCY_KEYWORDS if token in normalized)
    has_phone = bool(re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", normalized))
    has_tg = "t.me/" in normalized or "telegram" in normalized or "телеграм" in normalized

    return hits >= 2 or (hits >= 1 and has_phone) or (has_phone and has_tg)
