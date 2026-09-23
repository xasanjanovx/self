"""Фиксированный справочник категорий финансов.

В БД (finance_entries.category) хранится КЛЮЧ категории (например "transport"),
а не свободный текст — только так можно строить статистику. Старые записи со
свободным текстом ("еда", "такси") приводятся к ключу через `normalize`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# английские названия (интерфейс на английском; графики — картинки, их переводчик не видит)
EN: dict[str, str] = {
    "food": "Food (cafe)", "groceries": "Groceries", "transport": "Transport", "shopping": "Shopping", "home": "Home/utilities",
    "telecom": "Phone/internet", "health": "Health", "clothes": "Clothes", "fun": "Entertainment", "education": "Education",
    "gifts": "Gifts", "debt": "Debts/loans", "other": "Other", "salary": "Salary", "side": "Side income", "gift_in": "Gift",
    "debt_in": "Debt repaid", "other_in": "Other",
}


@dataclass(frozen=True)
class Category:
    key: str
    emoji: str
    ru: str
    uz: str
    kind: str  # "expense" | "income"
    aliases: tuple[str, ...] = ()

    def label(self, lang: str = "ru") -> str:
        if lang == "en":
            return EN.get(self.key, self.ru)
        return self.uz if lang == "uz" else self.ru

    def title(self, lang: str = "ru") -> str:
        return f"{self.emoji} {self.label(lang)}"


EXPENSE: tuple[Category, ...] = (
    Category("food", "🍔", "Еда (кафе)", "Ovqat (kafe)", "expense", (
        "еда", "кафе", "ресторан", "обед", "ужин", "завтрак", "перекус", "кофе", "чай", "фастфуд", "пицца",
        "бургер", "шаурма", "самса", "плов", "лагман", "столов", "доставка еды", "бизнес-ланч",
        "ovqat", "kafe", "restoran", "tushlik", "nonushta", "kechki ovqat", "kofe", "choy", "osh", "somsa",
        "lag'mon", "lagmon", "shaurma", "burger", "pitsa", "fastfud",
    )),
    Category("groceries", "🛒", "Продукты", "Oziq-ovqat", "expense", (
        "продукт", "супермаркет", "базар", "рынок", "хлеб", "молоко", "мясо", "овощ", "фрукт", "макро", "корзинка",
        "havas", "makro", "korzinka", "bozor", "oziq", "non ", "sut", "go'sht", "gosht", "sabzavot", "meva", "market",
    )),
    Category("transport", "🚕", "Транспорт", "Transport", "expense", (
        "транспорт", "такси", "яндекс", "yandex", "метро", "автобус", "маршрут", "бензин", "заправ", "парков",
        "проезд", "поезд", "самолет", "самолёт", "билет", "мойка", "шиномонтаж", "ремонт машин", "масло",
        "taksi", "metro", "avtobus", "marshrut", "benzin", "yoqilg'i", "yoqilgi", "zapravka", "parkovka",
        "yo'l", "yol kira", "poyezd", "samolyot", "chipta", "moyka", "mashina",
    )),
    Category("shopping", "🛍", "Магазин/покупки", "Xaridlar", "expense", (
        "магазин", "покупк", "техник", "телефон купил", "наушник", "гаджет", "электрон", "мебель", "посуд", "хозтовар",
        "uzum", "ozon", "wildberries", "aliexpress", "xarid", "do'kon", "dokon", "texnika", "mebel", "idish",
    )),
    Category("home", "🏠", "Дом/коммуналка", "Uy/kommunal", "expense", (
        "аренд", "квартир", "коммунал", "свет", "электричеств", "газ", "вода", "квартплат", "ремонт", "дом",
        "ijara", "kvartira", "kommunal", "svet", "elektr", "suv", "ta'mir", "tamir", "uy ",
    )),
    Category("telecom", "📱", "Связь", "Aloqa", "expense", (
        "связь", "интернет", "мобильн", "тариф", "ucell", "beeline", "билайн", "uzmobile", "mobiuz", "humans",
        "подписк", "netflix", "spotify", "youtube", "chatgpt", "openai", "хостинг", "домен", "сервер",
        "aloqa", "internet", "tarif", "obuna", "hosting", "domen", "server",
    )),
    Category("health", "💊", "Здоровье", "Sog'liq", "expense", (
        "здоров", "аптек", "лекарств", "врач", "клиник", "больниц", "стоматолог", "зуб", "анализ", "витамин",
        "спортзал", "фитнес", "тренаж", "бассейн",
        "sog'liq", "sogliq", "dorixona", "dori", "shifokor", "klinika", "kasalxona", "tish", "analiz", "vitamin",
        "sportzal", "fitnes", "trenajor", "basseyn",
    )),
    Category("clothes", "👕", "Одежда", "Kiyim", "expense", (
        "одежд", "обувь", "кроссов", "кед", "куртк", "джинс", "футболк", "рубашк", "костюм", "носк", "шапк",
        "kiyim", "oyoq kiyim", "krossovka", "kurtka", "jins", "futbolka", "ko'ylak", "koylak", "kostyum", "paypoq",
    )),
    Category("fun", "🎉", "Развлечения", "Ko'ngilochar", "expense", (
        "развлеч", "кино", "театр", "концерт", "игр", "клуб", "бар", "кальян", "отдых", "путешеств", "отель", "туризм",
        "kino", "teatr", "konsert", "o'yin", "oyin", "klub", "bar", "kalyan", "dam olish", "sayohat", "mehmonxona",
    )),
    Category("education", "📚", "Образование", "Ta'lim", "expense", (
        "образован", "курс", "учеб", "книг", "универ", "школ", "репетитор", "обучен", "экзамен", "ielts",
        "ta'lim", "talim", "kurs", "o'qish", "oqish", "kitob", "universitet", "maktab", "repetitor", "imtihon",
    )),
    Category("gifts", "🎁", "Подарки", "Sovg'alar", "expense", (
        "подар", "цвет", "день рожден", "свадьб", "праздник", "благотвор", "садака", "пожертв",
        "sovg'a", "sovga", "gul", "tug'ilgan kun", "tugilgan kun", "to'y", "toy ", "bayram", "sadaqa", "xayriya",
    )),
    Category("debt", "💳", "Долги/кредит", "Qarz/kredit", "expense", (
        "долг", "кредит", "рассрочк", "займ", "ипотек", "процент", "банк",
        "qarz", "kredit", "muddatli", "ipoteka", "foiz", "bank",
    )),
    Category("other", "📦", "Прочее", "Boshqa", "expense", ("прочее", "разное", "другое", "boshqa", "har xil")),
)

INCOME: tuple[Category, ...] = (
    Category("salary", "💼", "Зарплата", "Oylik", "income", (
        "зарплат", "оклад", "аванс", "зп", "премия", "oylik", "maosh", "avans", "mukofot", "ish haqi",
    )),
    Category("side", "💻", "Подработка", "Qo'shimcha daromad", "income", (
        "подработ", "фриланс", "заказ", "халтур", "проект", "продал", "продажа", "клиент",
        "qo'shimcha", "qoshimcha", "frilans", "buyurtma", "loyiha", "sotdim", "mijoz",
    )),
    Category("gift_in", "🎁", "Подарок", "Sovg'a", "income", ("подар", "дали", "sovg'a", "sovga", "berishdi")),
    Category("debt_in", "🤝", "Возврат долга", "Qarz qaytdi", "income", ("вернул", "возврат", "qaytar", "qaytdi")),
    Category("other_in", "📦", "Прочее", "Boshqa", "income", ("доход", "прочее", "kirim", "daromad", "boshqa")),
)

TRANSFER_KEY = "transfer"

_BY_KEY: dict[str, Category] = {c.key: c for c in EXPENSE + INCOME}
# Прямое соответствие старых русских/узбекских названий (как хранил старый бот)
_LEGACY_LABELS: dict[str, str] = {}
for _c in EXPENSE + INCOME:
    _LEGACY_LABELS[_c.ru.casefold()] = _c.key
    _LEGACY_LABELS[_c.uz.casefold()] = _c.key
    _LEGACY_LABELS[_c.key] = _c.key
_LEGACY_LABELS.update({
    "еда": "food", "продукты": "groceries", "транспорт": "transport", "доход": "other_in",
    "прочее": "other", "перевод": TRANSFER_KEY, "o'tkazma": TRANSFER_KEY, "otkazma": TRANSFER_KEY,
    "погашение долга": "debt", "возврат долга": "debt_in", "взял в долг": "debt", "оплата за друга": "debt",
    "снятие наличных": TRANSFER_KEY, "пополнение карты": TRANSFER_KEY,
})


def get(key: str | None) -> Category | None:
    return _BY_KEY.get(str(key or "").strip().lower())


def categories_for(kind: str) -> tuple[Category, ...]:
    return INCOME if kind == "income" else EXPENSE


def default_key(kind: str) -> str:
    return "other_in" if kind == "income" else "other"


def guess_from_text(text: str, kind: str = "expense") -> str | None:
    """Подобрать категорию по ключевым словам. Возвращает ключ или None."""
    low = f" {str(text or '').casefold()} "
    if not low.strip():
        return None
    best: tuple[int, str] | None = None
    for cat in categories_for(kind):
        for alias in cat.aliases:
            if alias in low:
                # чем длиннее совпавший алиас — тем точнее попадание
                score = len(alias)
                if best is None or score > best[0]:
                    best = (score, cat.key)
    return best[1] if best else None


def normalize(raw: str | None, kind: str = "expense", *, note: str | None = None) -> str:
    """Привести любое значение категории (ключ / старое название / свободный
    текст от AI) к валидному ключу справочника."""
    value = str(raw or "").strip()
    low = value.casefold()
    if low in _BY_KEY:
        cat = _BY_KEY[low]
        if cat.kind == kind:
            return cat.key
    if low == TRANSFER_KEY:
        return TRANSFER_KEY
    mapped = _LEGACY_LABELS.get(low)
    if mapped:
        if mapped == TRANSFER_KEY:
            return TRANSFER_KEY
        cat = _BY_KEY.get(mapped)
        if cat and cat.kind == kind:
            return mapped
    guessed = guess_from_text(f"{value} {note or ''}", kind)
    return guessed or default_key(kind)


def label(key: str | None, lang: str = "ru", *, with_emoji: bool = True) -> str:
    if key == TRANSFER_KEY:
        text = "O'tkazma" if lang == "uz" else "Transfer" if lang == "en" else "Перевод"
        return f"↔ {text}" if with_emoji else text
    cat = get(key)
    if cat is None:
        # неизвестный ключ — показываем как есть, чтобы ничего не потерять
        return str(key or ("boshqa" if lang == "uz" else "other" if lang == "en" else "прочее"))
    return cat.title(lang) if with_emoji else cat.label(lang)


def prompt_catalog(kind: str) -> str:
    """Строка для промпта AI: key — описание."""
    parts = []
    for cat in categories_for(kind):
        parts.append(f'"{cat.key}" ({cat.ru} / {cat.uz})')
    return ", ".join(parts)


_SLUG_RE = re.compile(r"[^a-z_]+")


def is_valid_key(key: str | None) -> bool:
    k = str(key or "").strip().lower()
    return k in _BY_KEY or k == TRANSFER_KEY
