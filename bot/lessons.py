"""YouTube: где он остановился (29.09, его выбор «сам помнит секунду»).

Телефон (MediaWatcher, приложение 2.13) присылает: название ролика, канал, секунду, длину — когда он ставит на паузу, закрывает
или переключает ролик. «Джес, продолжи урок» (или кнопка в напоминании ежедневного дела) → тот же ролик с той же секунды;
досмотрел (осталось < 45 с) и у дела есть ссылка на плейлист — следующий урок плейлиста с начала.
Хранится в DATA_DIR/media/<uid>.json — последние 60 роликов.
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
FINISHED_LEFT_S = 45     # до конца осталось меньше — досмотрел
REWIND_S = 3             # продолжаем чуть раньше места, где остановился


def _file(uid: int):  # noqa: ANN202
    from .tg_user import data_dir

    path = data_dir() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{uid}.json"


def items(uid: int) -> list[dict[str, Any]]:
    try:
        return list(json.loads(_file(uid).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _save(uid: int, rows: list[dict[str, Any]]) -> None:
    _file(uid).write_text(json.dumps(rows[:KEEP], ensure_ascii=False), encoding="utf-8")


def finished(item: dict[str, Any]) -> bool:
    duration = int(item.get("duration") or 0)
    return duration > 0 and int(item.get("position") or 0) >= duration - FINISHED_LEFT_S


def note(uid: int, data: dict[str, Any]) -> dict[str, Any] | None:
    """Место в ролике от телефона. Тот же ролик — обновляем (свежий — первым)."""
    title = " ".join(str(data.get("title") or "").split())[:200]
    if not title:
        return None
    try:
        position, duration = int(data.get("position_s") or 0), int(data.get("duration_s") or 0)
    except (TypeError, ValueError):
        return None
    rows = items(uid)
    old = next((r for r in rows if r.get("title") == title), {})
    item = {**old, "title": title, "channel": str(data.get("channel") or "")[:100], "position": max(0, position),
            "duration": max(0, duration), "at": time.time(), "state": str(data.get("state") or "")[:20]}
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
    """Что он смотрел: по словам («уроки английского», канал, часть названия) или последнее."""
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


async def playlist(list_id: str) -> list[dict[str, str]]:
    """Уроки плейлиста по порядку (страница YouTube, без ключей API)."""
    import httpx

    from .media import _INITIAL, _INITIAL_STR, _HEX, _UA

    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA, "Accept-Language": "ru,uz;q=0.8,en;q=0.6"},
                                     follow_redirects=True, cookies={"CONSENT": "YES+"}) as http:
            res = await http.get(f"https://www.youtube.com/playlist?list={list_id}&hl=ru")
            res.raise_for_status()
    except Exception:
        logger.warning("lessons: плейлист не открылся", exc_info=True)
        return []
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
    return out


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
        return {"error": "не нашла, что вы смотрели на YouTube (телефон присылает место, когда вы ставите на паузу или закрываете ролик)"}
    vid = await _resolve_id(uid, item)
    if not vid:
        return {"error": f"не нашла на YouTube ролик «{item.get('title')}»"}
    start = 0 if finished(item) else max(0, int(item.get("position") or 0) - REWIND_S)
    return {"video_id": vid, "start": start, "title": item.get("title"), "position": fmt(int(item.get("position") or 0)),
            "finished": finished(item), "next": False}


def url(res: dict[str, Any]) -> str:
    t = int(res.get("start") or 0)
    return f"https://www.youtube.com/watch?v={res['video_id']}" + (f"&t={t}s" if t else "")


__all__ = ["note", "items", "find", "resume", "playlist", "playlist_id", "video_id_of", "url", "fmt", "finished"]
