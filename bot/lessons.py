"""YouTube: где он остановился (29.09, его выбор «сам помнит секунду»).

30.09 (его решение): бот помнит ТОЛЬКО видео и плейлисты, которые он сам прислал ссылкой (реестр DATA_DIR/media/saved_<uid>.json:
save_video / add_daily со ссылкой; при желании — как цель или задача). Всё остальное, что он смотрит на YouTube, не сохраняется.
Телефон (MediaWatcher, приложение 2.13) присылает название ролика, канал, секунду, длину — когда он ставит на паузу, закрывает
или переключает ролик; note() принимает это, только если ролик из реестра. «Джес, продолжи урок» (или кнопка в напоминании
ежедневного дела) → тот же ролик с той же секунды; досмотрел (осталось < 45 с) и есть плейлист — следующий урок с начала.
Позиции хранятся в DATA_DIR/media/<uid>.json — последние 60 роликов (строки без `src` — старый сбор всего подряд — не читаются).
"""
from __future__ import annotations

import json
import logging
import re
import time
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

KEEP = 60
KEEP_SAVED = 100
FINISHED_LEFT_S = 45     # до конца осталось меньше — досмотрел
REWIND_S = 3             # продолжаем чуть раньше места, где остановился
RETRY_FETCH_S = 600      # не удалось открыть страницу — повторим не раньше чем через 10 минут
YT_LINK = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s<>\"')]+", re.IGNORECASE)
_AUTO_LISTS = ("RD", "WL", "LL")   # «микс», «смотреть позже», «понравившиеся» — не его плейлисты


def _dir():  # noqa: ANN202
    from .tg_user import data_dir

    path = data_dir() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file(uid: int):  # noqa: ANN202
    return _dir() / f"{uid}.json"


def _saved_file(uid: int):  # noqa: ANN202
    return _dir() / f"saved_{uid}.json"


# ------------------------------------------------------------------ реестр: что он сам прислал
def saved(uid: int) -> list[dict[str, Any]]:
    """Видео и плейлисты, которые он прислал (свежие первыми)."""
    try:
        return list(json.loads(_saved_file(uid).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _save_registry(uid: int, rows: list[dict[str, Any]]) -> None:
    _saved_file(uid).write_text(json.dumps(rows[:KEEP_SAVED], ensure_ascii=False), encoding="utf-8")


def find_links(text: str) -> list[str]:
    """Ссылки YouTube в тексте (видео, Shorts, плейлисты) по порядку."""
    return [m.group(0).rstrip(".,;:!?") for m in YT_LINK.finditer(str(text or ""))]


def parse_link(link: str) -> dict[str, str | None] | None:
    """{"key", "kind": video|playlist, "id", "list_id"} | None. Ссылка на ролик внутри плейлиста — это плейлист."""
    if not find_links(link):
        return None
    list_id = playlist_id(link)
    if list_id and list_id.startswith(_AUTO_LISTS):
        list_id = None
    vid = video_id_of(link)
    if list_id:
        return {"key": f"p:{list_id}", "kind": "playlist", "id": None, "list_id": list_id}
    if vid:
        return {"key": f"v:{vid}", "kind": "video", "id": vid, "list_id": None}
    return None


def _same_title(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if re.findall(r"\d+", na) != re.findall(r"\d+", nb):   # «Урок 1» и «Урок 10» — разные ролики
        return False
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 15 and short in long_:   # «… - YouTube» и т. п. хвосты
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.9


def source_for(uid: int, title: str) -> dict[str, Any] | None:
    """Запись реестра, которой принадлежит ролик с таким названием (сам ролик или ролик его плейлиста)."""
    for s in saved(uid):
        if s.get("kind") == "playlist":
            if any(_same_title(title, v.get("title", "")) for v in s.get("videos") or []):
                return s
        elif s.get("title") and _same_title(title, s["title"]):
            return s
    return None


def _video_of(src: dict[str, Any], title: str) -> str | None:
    if src.get("kind") == "playlist":
        vids = src.get("videos") or []
        exact = next((v.get("id") for v in vids if _norm(title) == _norm(v.get("title", ""))), None)
        return exact or next((v.get("id") for v in vids if _same_title(title, v.get("title", ""))), None)
    return src.get("id")


def register(uid: int, *, key: str, kind: str, video_id: str | None = None, list_id: str | None = None, title: str = "", channel: str = "",
             link: str = "", videos: list[dict[str, str]] | None = None, why: str | None = None, attempted: bool = True) -> dict[str, Any]:
    """Занести видео/плейлист в реестр (или обновить). why: goal | task | None (просто помнить).
    attempted — открывали страницу YouTube за названием (от этого зависит, когда пробовать снова)."""
    rows = saved(uid)
    old = next((r for r in rows if r.get("key") == key), {})
    entry = {**old, "key": key, "kind": kind, "id": video_id or old.get("id"), "list_id": list_id or old.get("list_id"),
             "title": " ".join((title or old.get("title") or "").split())[:200], "channel": (channel or old.get("channel") or "")[:100],
             "link": (link or old.get("link") or "")[:300], "videos": (videos if videos else old.get("videos")) or [],
             "why": why if why is not None else old.get("why"), "saved_at": old.get("saved_at") or time.time(),
             "tried": time.time() if attempted else float(old.get("tried") or 0)}
    _save_registry(uid, [entry] + [r for r in rows if r.get("key") != key])
    return entry


def set_why(uid: int, key: str, why: str | None) -> None:
    rows = saved(uid)
    for r in rows:
        if r.get("key") == key:
            r["why"] = why
    _save_registry(uid, rows)


def forget(uid: int, query: str = "") -> dict[str, Any] | None:
    """Убрать из реестра (позиции роликов пропадут сами): по словам из названия; без слов — самое свежее."""
    rows = saved(uid)
    hit = _match_saved(rows, query)
    if hit is None:
        return None
    _save_registry(uid, [r for r in rows if r.get("key") != hit.get("key")])
    return hit


def _match_saved(rows: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    words = [w for w in _norm(query).split() if len(w) >= 3 and w not in {"урок", "уроки", "видео", "ролик", "плейлист", "смотреть", "продолжи"}]
    if not words:
        goals = [r for r in rows if r.get("why") in ("goal", "task")]
        return (goals or rows or [None])[0]
    best, score = None, 0.0
    for r in rows:
        hay = _norm(f"{r.get('title')} {r.get('channel')}")
        s = sum(1 for w in words if w in hay) / len(words)
        if s > score:
            best, score = r, s
    return best if score >= 0.5 else None


def tracked(uid: int, row: dict[str, Any]) -> bool:
    """Ролик из того, что он поставил целью или задачей (только про такие бот сам напоминает)."""
    src = next((s for s in saved(uid) if s.get("key") == row.get("src")), None)
    return bool(src and src.get("why") in ("goal", "task"))


async def _oembed(video_id: str) -> dict[str, str]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as http:
            res = await http.get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
            res.raise_for_status()
            data = res.json()
    except Exception:
        logger.warning("lessons: название ролика не получено", exc_info=True)
        return {}
    return {"title": str(data.get("title") or ""), "channel": str(data.get("author_name") or "")}


async def save(uid: int, link: str, *, why: str | None = None) -> dict[str, Any]:
    """Запомнить ролик/плейлист по ссылке (название и уроки плейлиста — со страницы YouTube). Тот же — обновляем.
    ValueError — это не ссылка на ролик или плейлист."""
    info = parse_link(link)
    if info is None:
        raise ValueError("не ссылка на видео или плейлист YouTube")
    key, kind = str(info["key"]), str(info["kind"])
    old = next((r for r in saved(uid) if r.get("key") == key), None)
    complete = bool(old and old.get("title") and (kind == "video" or old.get("videos")))
    skip = complete or bool(old and time.time() - float(old.get("tried") or 0) < RETRY_FETCH_S)
    fetched: dict[str, Any] = {}
    if not skip:
        fetched = await playlist_info(str(info["list_id"])) if kind == "playlist" else await _oembed(str(info["id"]))
    canonical = f"https://www.youtube.com/playlist?list={info['list_id']}" if kind == "playlist" else f"https://www.youtube.com/watch?v={info['id']}"
    return register(uid, key=key, kind=kind, video_id=info["id"], list_id=info["list_id"], title=str(fetched.get("title") or ""),
                    channel=str(fetched.get("channel") or ""), link=canonical, videos=fetched.get("videos") or None, why=why, attempted=not skip)


async def adopt_daily_links(uid: int) -> None:
    """Ссылки его ежедневных дел (плейлист урока) — тоже «присланные им»: занести в реестр, если ещё нет."""
    from . import daily_tasks

    for h in daily_tasks.all_items(uid):
        info = parse_link(str(h.get("link") or ""))
        if info is None:
            continue
        old = next((r for r in saved(uid) if r.get("key") == info["key"]), None)
        if old and (old.get("title") or time.time() - float(old.get("tried") or 0) < RETRY_FETCH_S):
            continue
        try:
            await save(uid, str(h["link"]), why="goal")
        except Exception:
            logger.warning("lessons: ссылка дела не занесена", exc_info=True)


# ------------------------------------------------------------------ где остановился (только у присланных)
def _read_progress(uid: int) -> list[dict[str, Any]]:
    try:
        return list(json.loads(_file(uid).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def items(uid: int) -> list[dict[str, Any]]:
    """Где он остановился — только в присланных им роликах (свежие первыми)."""
    keys = {s.get("key") for s in saved(uid)}
    return [r for r in _read_progress(uid) if r.get("src") in keys]


def _save(uid: int, rows: list[dict[str, Any]]) -> None:
    _file(uid).write_text(json.dumps(rows[:KEEP], ensure_ascii=False), encoding="utf-8")


def finished(item: dict[str, Any]) -> bool:
    duration = int(item.get("duration") or 0)
    return duration > 0 and int(item.get("position") or 0) >= duration - FINISHED_LEFT_S


def note(uid: int, data: dict[str, Any]) -> dict[str, Any] | None:
    """Место в ролике от телефона. Чужой ролик (не из реестра) — не запоминаем. Тот же — обновляем (свежий — первым)."""
    title = " ".join(str(data.get("title") or "").split())[:200]
    if not title:
        return None
    src = source_for(uid, title)
    if src is None:
        return None
    try:
        position, duration = int(data.get("position_s") or 0), int(data.get("duration_s") or 0)
    except (TypeError, ValueError):
        return None
    rows = items(uid)
    old = next((r for r in rows if r.get("title") == title), {})
    item = {**old, "title": title, "channel": str(data.get("channel") or "")[:100], "position": max(0, position),
            "duration": max(0, duration), "at": time.time(), "state": str(data.get("state") or "")[:20], "src": src["key"]}
    if (vid := _video_of(src, title)) and not item.get("id"):
        item["id"] = vid
    _save(uid, [item] + [r for r in rows if r.get("title") != title])
    logger.info("lessons: «%s» — остановился на %s из %s (%s)", title[:60], fmt(position), fmt(duration), item["state"])
    return item


def fmt(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _norm(text: str) -> str:
    from .names import norm

    return norm(text)


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def find(uid: int, query: str = "") -> dict[str, Any] | None:
    """Что он смотрел из присланного: по словам («уроки английского», канал, часть названия) или последнее."""
    rows = items(uid)
    if not rows:
        return None
    words = [w for w in _norm(query).split() if len(w) >= 3 and w not in {"урок", "уроки", "видео", "ролик", "смотреть", "продолжи"}]
    if not words:
        return rows[0]
    best, score = None, 0.0
    for r in rows:
        hay = _norm(f"{r.get('title')} {r.get('channel')}")
        s = sum(1 for w in words if w in hay) / len(words)
        if s > score:
            best, score = r, s
    return best if score >= 0.5 else None


def playlist_id(link: str) -> str | None:
    try:
        q = parse_qs(urlparse(str(link or "")).query)
    except ValueError:
        return None
    value = (q.get("list") or [""])[0]
    return value if re.fullmatch(r"[\w-]{10,64}", value or "") else None


def video_id_of(link: str) -> str | None:
    m = re.search(r"(?:youtu\.be/|v=|/shorts/|/live/)([\w-]{11})", str(link or ""))
    return m.group(1) if m else None


def _videos(node: Any, out: list[dict[str, str]]) -> None:
    """Все ролики страницы по порядку: {"id", "title"} (плейлист: playlistVideoRenderer, новые страницы — любые videoId)."""
    if isinstance(node, dict):
        vid = node.get("videoId")
        title = node.get("title")
        if isinstance(vid, str) and len(vid) == 11 and isinstance(title, dict):
            text = "".join(r.get("text", "") for r in title.get("runs") or []) or str(title.get("simpleText") or "")
            if text and all(v["id"] != vid for v in out):
                out.append({"id": vid, "title": text[:200]})
        for v in node.values():
            _videos(v, out)
    elif isinstance(node, list):
        for v in node:
            _videos(v, out)


async def playlist_info(list_id: str) -> dict[str, Any]:
    """Плейлист: {"title", "videos": [{"id", "title"}]} по порядку (страница YouTube, без ключей API); не открылся — {}."""
    import httpx

    from .media import _INITIAL, _INITIAL_STR, _HEX, _UA

    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA, "Accept-Language": "ru,uz;q=0.8,en;q=0.6"},
                                     follow_redirects=True, cookies={"CONSENT": "YES+"}) as http:
            res = await http.get(f"https://www.youtube.com/playlist?list={list_id}&hl=ru")
            res.raise_for_status()
    except Exception:
        logger.warning("lessons: плейлист не открылся", exc_info=True)
        return {}
    m = _INITIAL.search(res.text)
    raw = m.group(1) if m else None
    if raw is None and (m2 := _INITIAL_STR.search(res.text)):
        raw = _HEX.sub(lambda h: chr(int(h.group(1), 16)), m2.group(1)).replace("\\\\", "\\")
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    out: list[dict[str, str]] = []
    _videos(data, out)
    head = re.search(r"<title>(.*?)</title>", res.text, re.S)
    title = re.sub(r"\s*-\s*YouTube\s*$", "", " ".join((head.group(1) if head else "").split()))
    return {"title": title, "videos": out}


async def playlist(list_id: str) -> list[dict[str, str]]:
    """Уроки плейлиста по порядку."""
    return list((await playlist_info(list_id)).get("videos") or [])


async def _resolve_id(uid: int, item: dict[str, Any]) -> str | None:
    """id ролика по названию (телефон присылает только название) — поиск YouTube, берём совпадающий; запоминаем."""
    if item.get("id"):
        return str(item["id"])
    from . import media

    found = await media.youtube_search(f"{item.get('title')} {item.get('channel') or ''}".strip(), 5)
    best = max(found, key=lambda f: _similar(f.get("title", ""), item.get("title", "")), default=None)
    if not best or _similar(best.get("title", ""), item.get("title", "")) < 0.6:
        return None
    rows = items(uid)
    for r in rows:
        if r.get("title") == item.get("title"):
            r["id"] = best["id"]
    _save(uid, rows)
    return str(best["id"])


NOTHING_SAVED = "нет сохранённых видео — пришли ссылку на ролик или плейлист YouTube, и я запомню"


def _from_saved(uid: int, query: str) -> dict[str, Any] | None:
    """Присланное, но ещё не начатое: открыть с начала (плейлист — первый неначатый урок из сохранённого списка)."""
    s = _match_saved(saved(uid), query)
    if s is None:
        return None
    if s.get("kind") == "video" and s.get("id"):
        return {"video_id": s["id"], "start": 0, "title": s.get("title") or "видео", "position": "0:00", "next": False}
    watched = {r.get("title"): r for r in items(uid)}
    nxt = next((v for v in s.get("videos") or [] if not finished(watched.get(v.get("title"), {}))), None)
    if nxt is None:
        return {"error": "плейлист досмотрен до конца" if s.get("videos") else f"не открылся плейлист «{s.get('title') or ''}» — попробуй позже"}
    return {"video_id": nxt["id"], "start": 0, "title": nxt["title"], "position": "0:00", "next": False}


async def resume(uid: int, query: str = "", link: str = "") -> dict[str, Any]:
    """Что открыть: {"video_id", "start", "title", "position", "next"} | {"error"}. link — плейлист/ролик ежедневного дела."""
    list_id = playlist_id(link)
    if list_id:
        videos = await playlist(list_id)
        if videos:
            watched = {r.get("title"): r for r in items(uid)}
            # последний просмотренный из этого плейлиста
            last_i, last_item = -1, None
            for r in items(uid):
                i = next((k for k, v in enumerate(videos) if v["id"] == r.get("id") or _similar(v["title"], r.get("title", "")) >= 0.9), -1)
                if i >= 0:
                    last_i, last_item = i, r
                    break
            if last_item is not None and not finished(last_item):
                v = videos[last_i]
                return {"video_id": v["id"], "start": max(0, int(last_item.get("position") or 0) - REWIND_S), "title": v["title"],
                        "position": fmt(int(last_item.get("position") or 0)), "next": False}
            nxt = next((v for v in videos[last_i + 1:] if not finished(watched.get(v["title"], {}))), None)
            if nxt is None:
                return {"error": "плейлист досмотрен до конца"}
            return {"video_id": nxt["id"], "start": 0, "title": nxt["title"], "position": "0:00", "next": last_i >= 0}
    single = video_id_of(link)
    item = find(uid, query)
    if item is None and single:
        return {"video_id": single, "start": 0, "title": query or "видео", "position": "0:00", "next": False}
    if item is None:
        return _from_saved(uid, query) or {"error": NOTHING_SAVED}
    vid = await _resolve_id(uid, item)
    if not vid:
        return {"error": f"не нашла на YouTube ролик «{item.get('title')}»"}
    start = 0 if finished(item) else max(0, int(item.get("position") or 0) - REWIND_S)
    return {"video_id": vid, "start": start, "title": item.get("title"), "position": fmt(int(item.get("position") or 0)),
            "finished": finished(item), "next": False}


def url(res: dict[str, Any]) -> str:
    t = int(res.get("start") or 0)
    return f"https://www.youtube.com/watch?v={res['video_id']}" + (f"&t={t}s" if t else "")


__all__ = ["note", "items", "find", "resume", "playlist", "playlist_info", "playlist_id", "video_id_of", "url", "fmt", "finished",
           "saved", "save", "register", "set_why", "forget", "tracked", "find_links", "parse_link", "source_for", "adopt_daily_links"]
