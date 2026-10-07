"""Автоподбор вакансий из чужих каналов (07.10): JES находит публичные каналы, читает их, отбирает только надёжные вакансии.

Его выбор: каналы ищет JES, он одобряет список; публикация — только после его «Опубликовать» (карточка в чате с картинкой).
Отбрасываем: нет контакта работодателя, просят деньги вперёд, работа за границей, нет зарплаты или условий
(«зарплата по собеседованию» — это зарплата, такое берём), а также MLM/крипта/аренда карт.

Читаем аккаунтом JES (caller.user_client): публичные каналы читаются без вступления — бана за вступления не боимся.
Здесь — логика и хранилище без aiogram; карточки и кнопки — в handlers/vacancy_feed.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import vacancy as vac
from .ai import VacancyData, VacancySection

logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=5))
DEFAULT_CAP = 5                  # карточек в день
CAPS = (3, 5, 10, 15)
MIN_MEMBERS = 1500               # совсем маленькие каналы не предлагаем
MAX_AGE_H = 48                   # вакансии старше — не берём
SEEN_DAYS = 30
NEW_HOURS = 36                   # не показанная вовремя вакансия протухает
CARD_DAYS = 7
SEND_FROM, SEND_TO = 8, 21       # карточки шлём только днём (Ташкент)
MAX_OPEN_CARDS = 3               # не завалить чат, если он не отвечает
PULL_LIMIT = 40
SOURCE_PAUSE_S = 2.0
SEARCH_PAUSE_S = 1.0
REQUIRE_SALARY = True            # «нет зарплаты» отбрасываем; «по договорённости/собеседованию» — это зарплата

KEYWORDS = ("vakansiya", "vakansiyalar", "ish bor", "ish e'lonlari", "bo'sh ish o'rinlari", "ishga taklif", "ish toshkent",
            "ish o'rinlari", "вакансии ташкент", "работа ташкент", "работа в узбекистане", "ish izlovchilar uchun")


# ---------------------------------------------------------------- хранилище
_state: dict[str, Any] | None = None


def _dir() -> Path:
    from .tg_user import data_dir

    folder = data_dir() / "vacancy_feed"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _file() -> Path:
    from .tg_user import data_dir

    return data_dir() / "vacancy_feed.json"


def load() -> dict[str, Any]:
    global _state
    if _state is None:
        _state = {}
        try:
            _state = json.loads(_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _state = {}
        for key, default in (("enabled", True), ("cap", DEFAULT_CAP), ("premium", False), ("sources", {}), ("seen", {}),
                             ("published", {}), ("queue", {}), ("daily", {}), ("flood_until", 0.0), ("last_discovery", 0.0)):
            _state.setdefault(key, default)
    return _state


def save() -> None:
    path = _file()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(load(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("vacancy_feed: не сохранил состояние", exc_info=True)


def reset_cache() -> None:
    """Для тестов."""
    global _state
    _state = None


def image_path(cid: str) -> Path:
    return _dir() / f"{cid}.jpg"


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


# ---------------------------------------------------------------- признаки обмана
def _norm(text: str) -> str:
    low = (text or "").lower().replace("ё", "е")
    for ch in "ʻʼ‘’`´‛′":
        low = low.replace(ch, "'")
    return re.sub(r"\s+", " ", low)


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(parts))


# деньги вперёд (RU/UZ, лат. и кирилл.)
_UPFRONT = _rx(
    r"залог", r"депозит", r"предоплат", r"вступительн\w* взнос", r"стартов\w* (?:взнос|капитал|пакет)", r"\bвзнос",
    r"оплат\w* (?:за )?(?:оформлен|обучен|документ|регистрац|курс|стажировк|форм|спецодежд|анкет|страховк|медкниж|визы)",
    r"платн\w* (?:обучен|курс|стажировк|тренинг)", r"купить (?:набор|комплект|товар|форму|пакет)",
    r"внести (?:деньги|сумму|плату|оплату)", r"инвестиц", r"вложить",
    r"\bzalog", r"depozit", r"oldindan (?:to'lov|pul|to'lash)", r"to'lov qilish kerak", r"pul to'lash kerak", r"pul to'la",
    r"pulli (?:o'qish|kurs|trening|o'quv)", r"kirish (?:to'lov|badal|puli)", r"ro'yxatdan o'tish uchun (?:to'lov|pul)",
    r"(?:rasmiylashtirish|hujjat|viza|sertifikat)\w* uchun (?:to'lov|pul|badal)", r"investitsiya", r"sarmoya",
    r"тўлов қилиш", r"олдиндан тўлов", r"\bзалог", r"инвестиция",
)
# MLM, крипта, ставки, аренда карт, «лёгкие деньги»
_SCAM = _rx(
    r"сетев\w+ маркетинг", r"\bmlm\b", r"network marketing", r"tarmoq(?:li)? marketing", r"пирамид", r"piramida",
    r"пассивн\w+ доход", r"passiv daromad", r"быстр\w+ деньги", r"tez pul", r"без вложени", r"vlojenie?siz",
    r"крипт", r"kripto", r"bitcoin", r"форекс", r"\bforex\b", r"трейдинг", r"\btrading\b", r"бинарн", r"binary option",
    r"ставки на спорт", r"букмекер", r"bukmeker", r"казино", r"kazino", r"1xbet", r"mostbet",
    r"дроппер", r"dropper", r"аренд\w* (?:банковск\w* )?карт", r"сда\w+ (?:банковск\w* )?карт", r"карт\w* в аренд", r"karta(?:ngiz)? ijara", r"kartani ijaraga",
    r"заработок в интернете", r"онлайн.?заработок", r"onlayn pul ishlash", r"internetda pul ishlash",
)
# работа не в Узбекистане: страны, города, виза, вахта
_ABROAD = _rx(
    r"\bкоре[яеию]\b", r"южн\w+ коре", r"koreya\w*", r"\bseul\b", r"\bсеул",
    r"\bросси[яиюе]\b", r"\bроссией\b", r"rossiya\w*", r"moskva\w*", r"москв", r"санкт-?петербург", r"\bпитер", r"подмосков", r"\bрф\b",
    r"\bпольш", r"polsha\w*", r"\bгермани", r"germaniya\w*", r"\bтурци", r"turkiya\w*", r"istanbul", r"стамбул",
    r"\bоаэ\b", r"\bbaa\b", r"dubay\w*", r"dubai", r"\bдуба[йяеи]", r"абу-?даби", r"эмират", r"emirat",
    r"казахстан", r"qozog'?iston\w*", r"\bалмат\w", r"almat[iy]", r"\bастан\w", r"shymkent", r"шымкент",
    r"киргиз", r"qirg'?iz", r"бишкек", r"bishkek", r"япони", r"yaponiya\w*", r"\bлитв", r"litva", r"чехи", r"chexiya",
    r"сербия", r"serbiya", r"\bкатар\b", r"\bqatar\b", r"саудов", r"saudiya", r"\bизраил", r"isroil", r"\bсша\b", r"\bамерик[аеуи]\b",
    r"amerika\w*", r"канад[аеуы]", r"\bангли[яеию]\b", r"angliya", r"лондон", r"франци", r"fransiya", r"италии", r"italiya", r"испани", r"ispaniya",
    r"хорвати", r"xorvatiya", r"венгри", r"vengriya", r"румыни", r"ruminiya", r"болгари", r"bolgariya", r"словаки", r"slovakiya",
    r"латви", r"latviya", r"эстони", r"estoniya", r"финлянди", r"finlyandiya", r"швеци", r"shvetsiya", r"норвегии", r"голланди",
    r"niderlandiya", r"беларус", r"belarus", r"азербайджан", r"ozarbayjon", r"грузии", r"gruziya", r"малайзи", r"malayziya",
    r"сингапур", r"singapur", r"\bкита[йяюе]\b", r"xitoy", r"таиланд", r"tailand", r"египет", r"\bвахт(?:а|у|е|ой)\b", r"вахтов", r"\bvaxta\b",
    r"за границ", r"за рубеж", r"xorijda", r"chet el(?:da|ga|dan|lik)", r"\bviza\b", r"\bvisa\b", r"\bвиза\b", r"визов", r"work permit",
    r"relokac", r"ish vizasi",
)

_ADMINISH = re.compile(r"admin|adm_|_adm\b|reklam|\bads?\b|support|ishdasiz|kanal_?admin|menejer_?kanal", re.IGNORECASE)
_HANDLE = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})|(?<![\w.])@([A-Za-z0-9_]{4,})", re.IGNORECASE)
_SALARY_AMOUNT = re.compile(r"\d[\d\s.,]*\s*(?:so'?m|сум|sum|usd|\$|млн|mln|ming|тыс|k\b|%)|\$\s?\d|(?:maosh|oylik|ish haqi|зарплат|оклад)\w*\W{0,12}\d")
_SALARY_NEGOTIABLE = re.compile(r"kelishiladi|kelishuv|suhbat|по договор|по собеседован|договорн|обсуждается")


def hard_flags(text: str) -> list[str]:
    """Надёжные тревожные признаки по тексту — без нейросети. → теги: upfront | scam | abroad."""
    low = _norm(text)
    flags = []
    if _UPFRONT.search(low):
        flags.append("upfront")
    if _SCAM.search(low):
        flags.append("scam")
    if _ABROAD.search(low):
        flags.append("abroad")
    return flags


FLAG_TEXT = {"upfront": "просят деньги вперёд", "scam": "признаки обмана (MLM/крипта/карты)", "abroad": "работа за границей"}


def employer_contacts(text: str, source: str = "") -> tuple[list[str], list[str]]:
    """Телефоны и @ники, которые могут быть контактом работодателя (без админа канала-источника и рекламы)."""
    phones = vac.extract_phones(text or "")
    handles: list[str] = []
    src = (source or "").lower().lstrip("@")
    for match in _HANDLE.finditer(text or ""):
        name = (match.group(1) or match.group(2) or "").lower()
        if not name or name == src or name in handles or name.startswith("joinchat") or _ADMINISH.search(name):
            continue
        handles.append(name)
    return phones, handles


def salary_hint(text: str) -> str:
    low = _norm(text)
    if _SALARY_AMOUNT.search(low):
        return "amount"
    if _SALARY_NEGOTIABLE.search(low):
        return "negotiable"
    return "none"


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    salary: str = "none"


def judge(raw_text: str, assessment: dict[str, Any] | None, source: str = "") -> Verdict:
    """Брать или нет. assessment — ответ ai.assess_vacancy (факты из текста); решение принимаем здесь."""
    a = assessment or {}
    reasons: list[str] = []
    flags = hard_flags(raw_text)
    if a.get("abroad") is True and "abroad" not in flags:
        flags.append("abroad")
    if a.get("pay_upfront") is True and "upfront" not in flags:
        flags.append("upfront")
    if (a.get("scam_signals") or a.get("salary_unrealistic") is True) and "scam" not in flags:
        flags.append("scam")
    reasons += [FLAG_TEXT[f] for f in ("abroad", "upfront", "scam") if f in flags]
    if a.get("is_vacancy") is False:
        reasons.append("не вакансия")

    phones, handles = employer_contacts(raw_text, source)
    kind = str(a.get("contact_kind") or "").lower()
    if kind == "admin_or_ad":
        reasons.append("контакт админа канала, а не работодателя")
    elif not phones and not handles:
        reasons.append("нет контакта работодателя")

    salary = str(a.get("salary") or "").lower()
    if salary not in {"amount", "negotiable"}:
        salary = salary_hint(raw_text)
    if REQUIRE_SALARY and salary not in {"amount", "negotiable"}:
        reasons.append("нет зарплаты")
    if not a.get("has_conditions"):
        reasons.append("нет условий (график, обязанности, требования)")
    return Verdict(ok=not reasons, reasons=reasons, salary=salary)


def quick_check(text: str, source: str = "") -> bool:
    """Дёшево, без нейросети: годится ли пост на вид (для оценки канала при поиске)."""
    if not prefilter(text)[0]:
        return False
    if hard_flags(text):
        return False
    phones, handles = employer_contacts(text, source)
    return bool(phones or handles) and salary_hint(text) != "none"


# ---------------------------------------------------------------- отпечатки и очередь
def fingerprint(text: str) -> str:
    base = re.sub(r"[\W_]+", "", _norm(text))[:500]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def content_key(data: VacancyData) -> str:
    """Одна и та же вакансия из двух каналов: те же телефоны + та же должность."""
    digits = sorted(re.sub(r"\D", "", p)[-9:] for p in re.split(r"\s*\|\s*", data.phone or "") if p.strip())
    head = re.sub(r"[\W_]+", "", _norm(data.headline))
    return hashlib.sha1(("|".join(digits) + "#" + head).encode("utf-8")).hexdigest()[:16] if (digits or data.telegram) else ""


def prefilter(text: str) -> tuple[bool, str]:
    text = (text or "").strip()
    if len(text) < 80:
        return False, "короткий пост"
    if len(text) > 3500:
        return False, "слишком длинный"
    if not vac.looks_like_vacancy(text):
        return False, "не похоже на вакансию"
    if len(set(vac.extract_phones(text))) > 3 or len(re.findall(r"kerak|требуется|ищем", _norm(text))) > 6:
        return False, "подборка нескольких вакансий"
    return True, ""


def data_to_dict(data: VacancyData) -> dict[str, Any]:
    return asdict(data)


def data_from_dict(raw: dict[str, Any]) -> VacancyData:
    body = dict(raw)
    body["extra_sections"] = [VacancySection(title=s["title"], items=list(s.get("items") or [])) for s in body.get("extra_sections") or []]
    return VacancyData(**body)


def fix_contacts(data: VacancyData, raw_text: str, source: str) -> VacancyData:
    """После разбора: @ник не должен быть ником канала-источника или рекламы (обычный разбор берёт первый @ник в тексте)."""
    _, handles = employer_contacts(f"{data.telegram or ''} {raw_text}", source)
    given = vac.username_from_telegram(data.telegram)
    if given and given.lower() in handles:
        return data
    data.telegram = f"@{handles[0]}" if handles else None
    return data


def score(data: VacancyData, verdict: Verdict, source_ok: float = 0.5) -> int:
    pts = 40
    pts += 15 if verdict.salary == "amount" else 5
    pts += 8 if data.company else 0
    pts += 6 if data.address else 0
    pts += 6 if data.schedule else 0
    pts += min(12, 3 * (len(data.requirements) + len(data.duties) + len(data.benefits)))
    pts += 6 if data.phone and data.telegram else 0
    pts += int(10 * max(0.0, min(1.0, source_ok)))
    return min(100, pts)


# ---------------------------------------------------------------- источники
def sources(status: str | None = None) -> dict[str, dict[str, Any]]:
    items = load()["sources"]
    return {k: v for k, v in items.items() if status is None or v.get("status") == status}


def add_pending(name: str, info: dict[str, Any]) -> bool:
    items = load()["sources"]
    if name in items:
        return False
    items[name] = {**info, "status": "pending", "last_id": 0, "found": datetime.now(TZ).isoformat(timespec="seconds"),
                   "stats": {"seen": 0, "queued": 0, "rejected": 0, "reasons": {}}}
    save()
    return True


def set_source_status(name: str, status: str) -> bool:
    item = load()["sources"].get(name)
    if item is None:
        return False
    item["status"] = status
    save()
    return True


def source_quality(name: str) -> float:
    """Доля принятых из всего, что мы оценили в канале (0.5 — пока мало данных)."""
    st = (load()["sources"].get(name) or {}).get("stats") or {}
    done = int(st.get("queued") or 0) + int(st.get("rejected") or 0)
    return 0.5 if done < 5 else int(st.get("queued") or 0) / done


def _bump(name: str, key: str, reasons: list[str] | None = None) -> None:
    st = load()["sources"].get(name, {}).setdefault("stats", {"seen": 0, "queued": 0, "rejected": 0, "reasons": {}})
    st[key] = int(st.get(key) or 0) + 1
    for reason in reasons or []:
        st["reasons"][reason] = int(st["reasons"].get(reason) or 0) + 1


# ---------------------------------------------------------------- очередь кандидатов
def candidates(status: str | None = None) -> list[dict[str, Any]]:
    return [c for c in load()["queue"].values() if status is None or c.get("status") == status]


def get_candidate(cid: str) -> dict[str, Any] | None:
    return load()["queue"].get(cid)


def drop_candidate(cid: str) -> None:
    load()["queue"].pop(cid, None)
    try:
        image_path(cid).unlink(missing_ok=True)
    except OSError:
        pass
    save()


def cards_today() -> int:
    return int(load()["daily"].get(_today()) or 0)


def note_card_sent() -> None:
    daily = load()["daily"]
    daily[_today()] = cards_today() + 1
    for day in [d for d in daily if d < (datetime.now(TZ) - timedelta(days=10)).date().isoformat()]:
        daily.pop(day)
    save()


def next_card(now: datetime | None = None, *, ignore_hours: bool = False) -> dict[str, Any] | None:
    """Лучший не показанный кандидат — если сейчас можно слать карточку (включено, день, лимит, нет завала без ответа)."""
    st = load()
    now = now or datetime.now(TZ)
    if not st["enabled"] or not (ignore_hours or SEND_FROM <= now.hour < SEND_TO):
        return None
    if cards_today() >= int(st["cap"]) or len(candidates("carded")) >= MAX_OPEN_CARDS:
        return None
    fresh = sorted(candidates("new"), key=lambda c: (-int(c.get("score") or 0), str(c.get("created") or "")))
    return fresh[0] if fresh else None


def cleanup(now: datetime | None = None) -> None:
    now = now or datetime.now(TZ)
    st = load()
    for cand in list(st["queue"].values()):
        age = now - datetime.fromisoformat(cand["created"])
        limit = timedelta(hours=NEW_HOURS) if cand.get("status") == "new" else timedelta(days=CARD_DAYS)
        if cand.get("status") in {"published", "skipped"} or age > limit:
            drop_candidate(cand["id"])
    horizon = time.time() - SEEN_DAYS * 86400
    for bucket in ("seen", "published"):
        for key in [k for k, ts in st[bucket].items() if float(ts) < horizon]:
            st[bucket].pop(key)
    save()


def mark_handled(cand: dict[str, Any], status: str) -> None:
    """Опубликовано или пропущено — такую вакансию (по телефонам и должности) из другого канала больше не предлагаем."""
    st = load()
    key = cand.get("key")
    if key:
        st["published"][key] = time.time()
    cand["status"] = status
    save()


# ---------------------------------------------------------------- разбор одного поста
async def consider(raw_text: str, *, source: str, msg_id: int, ai: Any | None = None, now: datetime | None = None) -> tuple[str, Any]:
    """Один пост из канала → ("skip", причина) | ("reject", [причины]) | ("queued", кандидат) | ("error", текст).
    Дорогое (оформление поста) — только если проверка прошла. Ошибка нейросети отпечаток не записывает: попробуем на следующем круге."""
    if ai is None:
        from .context import ai as ai_service

        ai = ai_service
    st = load()
    ok, why = prefilter(raw_text)
    if not ok:
        return "skip", why
    fp = fingerprint(raw_text)
    if fp in st["seen"]:
        return "skip", "уже видели"
    _bump(source, "seen")
    try:
        assessment = await ai.assess_vacancy(raw_text)
    except Exception as exc:
        logger.warning("vacancy_feed: проверка %s/%s не вышла: %s", source, msg_id, exc)
        return "error", str(exc)[:120]
    st["seen"][fp] = time.time()
    verdict = judge(raw_text, assessment, source)
    if not verdict.ok:
        _bump(source, "rejected", verdict.reasons)
        save()
        return "reject", verdict.reasons
    try:
        data = await ai.rewrite_vacancy(raw_text, default_region_tag=vac.VACANCY_DEFAULT_REGION_TAG)
    except Exception as exc:
        logger.warning("vacancy_feed: оформление %s/%s не вышло: %s", source, msg_id, exc)
        st["seen"].pop(fp, None)
        return "error", str(exc)[:120]
    scene = data.image_prompt
    data = vac.finalize(data, raw_text)
    data = fix_contacts(data, raw_text, source)
    if not data.phone and not data.telegram:
        _bump(source, "rejected", ["нет контакта работодателя"])
        save()
        return "reject", ["нет контакта работодателя"]
    key = content_key(data)
    if key and (key in st["published"] or any(c.get("key") == key for c in st["queue"].values())):
        save()
        return "skip", "такая вакансия уже есть"
    now = now or datetime.now(TZ)
    cid = hashlib.sha1(f"{source}/{msg_id}".encode()).hexdigest()[:10]
    cand = {"id": cid, "source": source, "msg_id": msg_id, "url": f"https://t.me/{source}/{msg_id}", "status": "new",
            "created": now.isoformat(timespec="seconds"), "key": key, "scene": scene or "", "regen": 0,
            "salary": verdict.salary, "score": score(data, verdict, source_quality(source)), "data": data_to_dict(data),
            "reason": str(assessment.get("reason") or "")[:80]}
    st["queue"][cid] = cand
    _bump(source, "queued")
    save()
    return "queued", cand


# ---------------------------------------------------------------- Telegram (JES)
class FeedUnavailable(RuntimeError):
    """JES не в сети или Telegram просит подождать."""


def _flood_seconds(exc: Exception) -> int | None:
    seconds = getattr(exc, "seconds", None)
    return int(seconds) if type(exc).__name__ == "FloodWaitError" and seconds else None


def _client() -> Any:
    from . import caller

    client = caller.user_client()
    if client is None:
        raise FeedUnavailable("аккаунт JES сейчас не в сети")
    wait = float(load().get("flood_until") or 0) - time.time()
    if wait > 0:
        raise FeedUnavailable(f"Telegram просит подождать ещё {int(wait // 60) + 1} мин")
    return client


def _flood(exc: Exception) -> None:
    seconds = _flood_seconds(exc)
    if seconds is None:
        return
    load()["flood_until"] = time.time() + seconds + 30
    save()
    logger.warning("vacancy_feed: Telegram просит подождать %s с", seconds)
    raise FeedUnavailable(f"Telegram просит подождать {seconds // 60 + 1} мин") from exc


async def fetch_posts(client: Any, name: str, *, last_id: int = 0, limit: int = PULL_LIMIT) -> list[dict[str, Any]]:
    """Последние посты публичного канала (без вступления): id, текст, дата. Новые первыми."""
    try:
        entity = await client.get_entity(name)
        posts = []
        async for message in client.iter_messages(entity, limit=limit, min_id=last_id):
            text = (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()
            posts.append({"id": int(message.id), "text": text, "date": getattr(message, "date", None)})
        return posts
    except Exception as exc:
        _flood(exc)
        raise


def _age_hours(date: datetime | None, now: datetime) -> float:
    if date is None:
        return 0.0
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return (now - date).total_seconds() / 3600


async def pull_source(client: Any, name: str, ai: Any | None = None, now: datetime | None = None) -> dict[str, int]:
    """Прочитать новые посты одного канала и разобрать. → счётчики. last_id двигаем только по разобранным постам."""
    now = now or datetime.now(TZ)
    src = load()["sources"][name]
    posts = await fetch_posts(client, name, last_id=int(src.get("last_id") or 0))
    counts = {"posts": len(posts), "queued": 0, "rejected": 0, "skipped": 0, "errors": 0}
    done_id = int(src.get("last_id") or 0)
    for post in sorted(posts, key=lambda p: p["id"]):
        if _age_hours(post["date"], now) > MAX_AGE_H:
            counts["skipped"] += 1
        else:
            result, _ = await consider(post["text"], source=name, msg_id=post["id"], ai=ai, now=now)
            if result == "error":
                counts["errors"] += 1
                break  # этот и следующие посты — на следующий круг: id дальше не двигаем
            counts[{"queued": "queued", "reject": "rejected"}.get(result, "skipped")] += 1
        done_id = post["id"]
    src["last_id"] = max(int(src.get("last_id") or 0), done_id)
    src["last_pull"] = now.isoformat(timespec="seconds")
    save()
    return counts


async def pull_all(ai: Any | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Круг по одобренным каналам. FeedUnavailable — наружу (воркер подождёт)."""
    client = _client()
    total = {"sources": 0, "queued": 0, "rejected": 0, "errors": 0}
    for name in list(sources("approved")):
        try:
            counts = await pull_source(client, name, ai, now)
        except FeedUnavailable:
            raise
        except Exception as exc:
            logger.warning("vacancy_feed: канал @%s не прочитался: %s: %s", name, type(exc).__name__, exc)
            load()["sources"][name]["error"] = f"{type(exc).__name__}"
            save()
            continue
        load()["sources"][name].pop("error", None)
        total["sources"] += 1
        for key in ("queued", "rejected", "errors"):
            total[key] += counts[key]
        await asyncio.sleep(SOURCE_PAUSE_S)
    return total


async def discover(*, own_channel: str = "", limit_new: int = 6) -> list[dict[str, Any]]:
    """Поиск публичных каналов с вакансиями по ключевым словам. Оценка без нейросети: сколько из последних постов — вакансии
    и сколько из них проходят проверку. Новые попадают в «ждут решения»; в работу идут только после его «ок»."""
    client = _client()
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore
        from telethon.tl.functions.contacts import SearchRequest  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise FeedUnavailable("не установлен telethon") from exc
    known = set(load()["sources"])
    own = own_channel.lower().lstrip("@")
    found: dict[str, Any] = {}
    for word in KEYWORDS:
        try:
            result = await client(SearchRequest(q=word, limit=25))
        except Exception as exc:
            _flood(exc)
            logger.warning("vacancy_feed: поиск «%s» не вышел: %s", word, exc)
            continue
        for chat in getattr(result, "chats", []) or []:
            username = (getattr(chat, "username", None) or "").lower()
            if (not username or not getattr(chat, "broadcast", False) or getattr(chat, "megagroup", False)
                    or username in known or username == own or username in found):
                continue
            found[username] = chat
        await asyncio.sleep(SEARCH_PAUSE_S)
    rated: list[dict[str, Any]] = []
    now = datetime.now(TZ)
    for username, chat in list(found.items())[:25]:
        try:
            full = await client(GetFullChannelRequest(chat))
            members = int(getattr(getattr(full, "full_chat", None), "participants_count", 0) or getattr(chat, "participants_count", 0) or 0)
            if members < MIN_MEMBERS:
                continue
            posts = await fetch_posts(client, username, limit=30)
        except FeedUnavailable:
            raise
        except Exception as exc:
            logger.info("vacancy_feed: канал @%s пропущен: %s", username, type(exc).__name__)
            continue
        texts = [p["text"] for p in posts if p["text"]]
        vacancies = [t for t in texts if vac.looks_like_vacancy(t)]
        passing = [t for t in vacancies if quick_check(t, username)]
        last_age = min((_age_hours(p["date"], now) for p in posts), default=999.0)
        if len(vacancies) < 5 or len(passing) < 3 or last_age > 72:
            continue
        rated.append({"name": username, "title": getattr(chat, "title", username), "members": members,
                      "posts": len(texts), "vacancies": len(vacancies), "passing": len(passing),
                      "ratio": round(len(passing) / max(1, len(vacancies)), 2), "sample": passing[0][:160].replace("\n", " ")})
        await asyncio.sleep(SOURCE_PAUSE_S)
    rated.sort(key=lambda r: (-r["ratio"], -r["members"]))
    added = []
    for item in rated[:limit_new]:
        if add_pending(item["name"], {k: item[k] for k in ("title", "members", "posts", "vacancies", "passing", "ratio", "sample")}):
            added.append(item)
    load()["last_discovery"] = time.time()
    save()
    return added


def status() -> dict[str, Any]:
    st = load()
    return {"enabled": st["enabled"], "cap": st["cap"], "approved": len(sources("approved")), "pending": len(sources("pending")),
            "new": len(candidates("new")), "carded": len(candidates("carded")), "today": cards_today(), "flood_until": st["flood_until"]}


def set_enabled(value: bool) -> None:
    load()["enabled"] = bool(value)
    save()


def set_cap(value: int) -> None:
    load()["cap"] = max(1, min(30, int(value)))
    save()


def remember_premium(value: bool) -> None:
    st = load()
    if st.get("premium") != bool(value):
        st["premium"] = bool(value)
        save()
