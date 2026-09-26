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

# Кто как может быть записан в контактах. Ключ — нормализованная форма. Первое слово группы — «корень»: по нему
# запоминаем, кто у него «брат» (phone.learn_alias). С падежами из речи («маме», «брату», «akamga»).
# 26.09: «ака»/«опа» убраны — это вежливые обращения («Abdulatif Aka»: у него ~180 таких контактов), и «брат»
# звонил случайному «… Aka»; «ука» (младший брат) и «синглим» (младшая сестра) — отдельные группы.
_KIN = [
    ("мама", "маме", "мамочка", "мамочке", "мамуля", "ойи", "ойижон", "онам", "она", "онажон", "ойим", "oyi", "oyijon", "ona", "onam",
     "onajon", "oyim", "mama", "mamochka", "mom", "mother", "ойижоним", "онажоним", "onajonim", "oyijonim", "onamga", "oyimga",
     "onajonimga", "oyijonimga", "онамга", "ойимга"),
    ("папа", "папе", "папочка", "дада", "дадажон", "ота", "отам", "отажон", "дадам", "ada", "adajon", "dada", "dadajon", "ota", "otam",
     "otajon", "papa", "dad", "father", "дадажоним", "dadajonim", "otajonim", "adajonim", "dadamga", "otamga", "dadajonimga", "дадамга",
     "отамга"),
    ("брат", "брату", "брата", "братом", "акам", "акажон", "акажоним", "akam", "akajon", "akajonim", "brat", "brother", "akamga",
     "акамга", "akajonimga"),
    ("братишка", "братишке", "укам", "укажон", "ukam", "ukajon", "ukajonim", "ukamga", "укамга", "младший брат"),
    ("сестра", "сестре", "сестру", "опам", "опажон", "opam", "opajon", "opajonim", "sestra", "sister", "opamga", "опамга"),
    ("сестрёнка", "сестрёнке", "синглим", "сингил", "singlim", "singil", "singlimga", "синглимга", "младшая сестра"),
    ("жена", "жене", "жёнушка", "аёлим", "хотиним", "рафиқам", "ayolim", "xotinim", "rafiqam", "jena", "wife", "жана", "ayolimga",
     "xotinimga"),
    ("муж", "мужу", "эрим", "turmush", "erim", "muj", "husband", "erimga"),
    ("бабушка", "бабушке", "буви", "бувижон", "momo", "buvi", "buvijon", "babushka", "grandma", "бувим", "buvim", "buvimga",
     "buvajon", "buvajonim", "buvijonim", "бувижоним"),
    ("дедушка", "дедушке", "бобо", "бобожон", "bobo", "bobojon", "dedushka", "grandpa", "бобом", "bobom", "bobomga", "bobojonim"),
]


def norm(value: Any) -> str:
    """Любое имя → латиница в нижнем регистре без апострофов и знаков."""
    low = str(value or "").lower()
    out = "".join(_CYR.get(ch, ch) for ch in low)
    out = _APOS.sub("", out)
    out = _NON_WORD.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


_KIN_GROUPS = [{norm(w) for w in group} for group in _KIN]
_KIN_ROOTS = [norm(group[0]) for group in _KIN]


def kin_root(value: Any) -> str | None:
    """«брату», «akamga», «Акам» → «brat» (корень группы); не родство → None."""
    n = norm(value)
    for root, group in zip(_KIN_ROOTS, _KIN_GROUPS):
        if n in group:
            return root
    return None


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
    # «ака», «опа» — вежливость, а не имя: «Срачбек ака» не должен совпадать с «ABDULATIF AKA» по слову «aka»
    named = [qt for qt in q_tokens if qt not in HONORIFICS] or q_tokens
    best_tok = 0.0
    for qt in named:
        for t in tokens:
            best_tok = max(best_tok, SequenceMatcher(None, qt, t).ratio())
    # однословный запрос против многословного имени сравниваем по лучшему слову
    tok_score = best_tok * (0.95 if len(q_tokens) == 1 else 0.85)
    return max(whole, tok_score)


HONORIFICS = {"aka", "akam", "akajon", "opa", "opam", "opajon", "uka", "ukam", "xon", "xonim", "domla", "ustoz", "janob"}
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


# организации в книге («Ona va bola markazi», «Hamkorbank») — на «мама», «брат» их не выбираем
_ORG_WORDS = ("markaz", "center", "centr", "tsentr", "klinik", "clinic", "shifoxona", "bank", "dokon", "magazin", "servis", "service",
              "taxi", "taksi", "ofis", "office", "apteka", "dorixona", "maktab", "school", "universitet", "kafe", "cafe", "restoran",
              "salon", "market", "sklad", "support", "saloni", "poliklinika", "bolnitsa", "hokimiyat", "idora", "firma", "mchj", "ooo")


def is_kin(queries: Iterable[Any]) -> bool:
    return any(norm(q) in group for q in queries for group in _KIN_GROUPS)


AMBIGUOUS_GAP = 0.05   # двое почти одинаково похожи — и частота звонков их не развела: лучше коротко спросить
CLEAR_SCORE = 0.9      # уверенно (можно запомнить, как он назвал человека)


def frequency_boost(calls: int) -> float:
    """Кому он звонит чаще — тот вероятнее: 1 звонок +0.03, 3 — +0.06, 10 — +0.1, 30+ — +0.12."""
    if calls <= 0:
        return 0.0
    import math

    return round(min(0.12, 0.03 * math.log2(1 + calls)), 3)


def pick(queries: Iterable[Any], items: list[dict[str, Any]], *, name_keys: tuple[str, ...] = ("name",),
         boosts: dict[str, float] | None = None, alias: str | None = None) -> dict[str, Any]:
    """Один лучший человек. {"match", "score", "others", "clear", "ambiguous": [второй]} | {}.

    alias — как он уже называл этого человека раньше («мама» → «ONAJONIM»), выигрывает сразу;
    boosts — надбавка по нормализованному имени (кому чаще звонит / с кем недавно переписывался);
    на родственные слова организации из книги отодвигаем назад.
    ambiguous — второй почти так же похож (разница < AMBIGUOUS_GAP с учётом частоты): 26.09 он выбрал «звонить тому,
    кому чаще, а если оба редкие — коротко спросить» (раньше — всегда первому, и «Сирожбек» уходил к «Сирожиддину»)."""
    queries = [q for q in queries if q]
    if alias:
        a = norm(alias)
        for item in items:
            if any(norm(item.get(k)) == a for k in name_keys if item.get(k)):
                return {"match": item, "score": 1.0, "others": [], "learned": True, "clear": True}
    variants = expand(queries)
    kin = is_kin(queries)
    boosts = boosts or {}
    scored: list[tuple[float, int]] = []
    for idx, item in enumerate(items):
        names = [norm(item.get(k)) for k in name_keys if item.get(k)]
        best = max((score(v, n) * w for v, w in variants for n in names if n), default=0.0)
        if best < MIN_SCORE:
            continue
        if kin and any(o in n for n in names for o in _ORG_WORDS):
            best *= 0.8
        best += max((boosts.get(n, 0.0) for n in names), default=0.0)
        scored.append((best, idx))
    if not scored:
        return {}
    scored.sort(key=lambda s: -s[0])
    first = items[scored[0][1]]
    top = scored[0][0]
    # тёзки с одинаковым именем (три «ONAJONIM») — это один человек, не вопрос
    first_name = norm(first.get(name_keys[0]))
    rivals = [(s, i) for s, i in scored[1:] if norm(items[i].get(name_keys[0])) != first_name]
    second = rivals[0][0] if rivals else 0.0
    others = [str(items[i].get(name_keys[0]) or "") for _, i in rivals[:3]]
    out: dict[str, Any] = {"match": first, "score": round(top, 2), "others": others,
                           "clear": top >= CLEAR_SCORE and top - second >= CLEAR_GAP}
    if rivals and top - second < AMBIGUOUS_GAP:
        out["ambiguous"] = items[rivals[0][1]]
    return out


_PHONE_RE = re.compile(r"^\+?[\d\s\-()]{5,}$")


def as_phone_number(value: Any) -> str | None:
    """«+998 90 123-45-67» → «+998901234567»; не номер → None."""
    text = str(value or "").strip()
    if not _PHONE_RE.match(text):
        return None
    digits = re.sub(r"[^\d+]", "", text)
    return digits if len(digits.lstrip("+")) >= 5 else None


__all__ = ["kin_root", "frequency_boost", "norm", "expand", "score", "resolve", "pick", "is_kin", "as_phone_number"]
