"""Нечёткий поиск людей по имени: «мама» → контакт «Ойижон», «Алишер» → «Alisher Aka».

Голосовой ввод даёт имя в любой транскрипции (кириллица/латиница, узбекский/русский),
а в телефонной книге и в Telegram оно записано как угодно. Поэтому сравниваем
нормализованные строки (всё в латиницу, без апострофов) и добавляем синонимы родства.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
}
_APOS = re.compile(r"['`ʻʼ’‘]")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")

# Кто как может быть записан в контактах. Ключ — нормализованная форма.
_KIN = [
    ("мама", "мамочка", "мамуля", "мамa", "ойи", "ойижон", "онам", "она", "онажон", "ойим", "oyi", "oyijon", "ona", "onam", "onajon", "oyim",
     "mama", "mamochka", "mom", "mother", "ойижоним"),
    ("папа", "папочка", "дада", "дадажон", "ота", "отам", "отажон", "дадам", "ada", "adajon", "dada", "dadajon", "ota", "otam", "otajon",
     "papa", "dad", "father", "дадажоним"),
    ("брат", "братишка", "ака", "акам", "акажон", "ука", "укам", "укажон", "aka", "akam", "akajon", "uka", "ukam", "ukajon", "brat", "brother"),
    ("сестра", "сестрёнка", "опа", "опам", "опажон", "синглим", "сингил", "opa", "opam", "opajon", "singlim", "singil", "sestra", "sister"),
    ("жена", "жёнушка", "аёлим", "хотиним", "рафиқам", "ayolim", "xotinim", "rafiqam", "jena", "wife", "жана"),
    ("муж", "эрим", "turmush", "erim", "muj", "husband"),
    ("бабушка", "буви", "бувижон", "momo", "buvi", "buvijon", "babushka", "grandma", "бувим", "buvim"),
    ("дедушка", "бобо", "бобожон", "bobo", "bobojon", "dedushka", "grandpa", "бобом", "bobom"),
]


def norm(value: Any) -> str:
    """Любое имя → латиница в нижнем регистре без апострофов и знаков."""
    low = str(value or "").lower()
    out = "".join(_CYR.get(ch, ch) for ch in low)
    out = _APOS.sub("", out)
    out = _NON_WORD.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


_KIN_GROUPS = [{norm(w) for w in group} for group in _KIN]


def expand(queries: Iterable[Any]) -> list[tuple[str, float]]:
    """Запрос + варианты от модели → [(нормализованная строка, вес)]. Если запрос — слово родства
    («мама», «ota»), добавляем синонимы с весом чуть ниже: контакт «Мама» важнее контакта «Ойижон»."""
    direct: list[str] = []
    for q in queries:
        n = norm(q)
        if n and n not in direct:
            direct.append(n)
    out = [(q, 1.0) for q in direct]
    for group in _KIN_GROUPS:
        if any(q in group for q in direct):
            out.extend((w, 0.97) for w in sorted(group) if w not in direct)
    return out


def score(query: str, name: str) -> float:
    """Похожесть нормализованного запроса на нормализованное имя, 0..1."""
    if not query or not name:
        return 0.0
    if query == name:
        return 1.0
    tokens = name.split()
    if query in tokens:
        return 0.93
    if len(query) >= 3 and any(t.startswith(query) for t in tokens):
        return 0.88
    if len(query) >= 4 and query in name:
        return 0.85
    # падежи из речи: «Лоле», «Алишеру», «маме» — основа без окончания
    if " " not in query and len(query) >= 4:
        for cut in (1, 2) if len(query) >= 6 else (1,):
            stem = query[:-cut]
            if any(t.startswith(stem) for t in tokens):
                return 0.86
    whole = SequenceMatcher(None, query, name).ratio()
    q_tokens = query.split()
    best_tok = 0.0
    for qt in q_tokens:
        for t in tokens:
            best_tok = max(best_tok, SequenceMatcher(None, qt, t).ratio())
    # однословный запрос против многословного имени сравниваем по лучшему слову
    tok_score = best_tok * (0.95 if len(q_tokens) == 1 else 0.85)
    return max(whole, tok_score)


MIN_SCORE = 0.72
CLEAR_GAP = 0.08


def resolve(queries: Iterable[Any], items: list[dict[str, Any]], *, name_keys: tuple[str, ...] = ("name",), limit: int = 4) -> dict[str, Any]:
    """Найти одного человека среди items (у каждого — поля с именами из name_keys).

    Возвращает {"match": item} — если найден однозначно; {"candidates": [item, …]} — если
    похожих несколько; {} — если никого."""
    variants = expand(queries)
    scored: list[tuple[float, int]] = []
    for idx, item in enumerate(items):
        names = [norm(item.get(k)) for k in name_keys if item.get(k)]
        best = max((score(v, n) * w for v, w in variants for n in names if n), default=0.0)
        if best >= MIN_SCORE:
            scored.append((best, idx))
    if not scored:
        return {}
    scored.sort(key=lambda s: -s[0])
    top = scored[0][0]
    if len(scored) == 1 or top - scored[1][0] >= CLEAR_GAP:
        return {"match": items[scored[0][1]], "score": round(top, 2)}
    close = [items[i] for s, i in scored if top - s < CLEAR_GAP][:limit]
    return {"candidates": close}


_PHONE_RE = re.compile(r"^\+?[\d\s\-()]{5,}$")


def as_phone_number(value: Any) -> str | None:
    """«+998 90 123-45-67» → «+998901234567»; не номер → None."""
    text = str(value or "").strip()
    if not _PHONE_RE.match(text):
        return None
    digits = re.sub(r"[^\d+]", "", text)
    return digits if len(digits.lstrip("+")) >= 5 else None


__all__ = ["norm", "expand", "score", "resolve", "as_phone_number"]
