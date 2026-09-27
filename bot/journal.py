"""Журнал промахов JES и ошибок приложения — для ночного отчёта владельцу (27.09.2026, его выбор «Журнал ошибок + ночной
отчёт»). Сервер пишет сюда промахи разговора (звонок не тому, ответ не на том языке, переспрос, инструмент не сработал),
телефон присылает свои ошибки и сбои (/jarvis/v1/log). Вечером — сводка в его вечернем отчёте, файлы остаются на сервере
(DATA_DIR/journal/ГГГГ-ММ-ДД.jsonl), чтобы по ним чинить.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KEEP_DAYS = 30
LABELS = {
    "wrong_call": "звонок не тому (остановлен)",
    "lang": "ответ не на том языке",
    "ask_again": "переспросил вместо дела",
    "tool_error": "действие не вышло",
    "app_error": "ошибка в приложении",
    "app_crash": "сбой приложения",
    "false_wake": "ложное «Джес» (отсеяно)",
    "agent_error": "ошибка бота",
}
# в отчёт — только то, что стоит чинить; ложные «Джес» — одной цифрой
QUIET = {"false_wake"}


def _dir() -> Path:
    from .tg_user import data_dir

    path = data_dir() / "journal"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file(day: date) -> Path:
    return _dir() / f"{day.isoformat()}.jsonl"


def _mask(text: str) -> str:
    from .secrets_guard import mask

    return mask(text)


def miss(uid: int, kind: str, text: str, *, when: datetime | None = None) -> None:
    """Записать промах (не бросает исключений — журнал не должен ломать разговор)."""
    try:
        now = when or datetime.now()
        line = json.dumps({"t": now.strftime("%H:%M:%S"), "uid": uid, "kind": kind, "text": _mask(str(text))[:300]}, ensure_ascii=False)
        with _file(now.date()).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("journal: не записалось", exc_info=True)


def app_events(uid: int, events: list[dict[str, Any]]) -> int:
    """События с телефона: [{"t": мс, "level": "error"|"crash"|"info", "tag", "msg"}] → журнал. Возвращает, сколько записано."""
    n = 0
    for e in events[:200]:
        if not isinstance(e, dict):
            continue
        level = str(e.get("level") or "error")
        kind = "app_crash" if level == "crash" else "app_error" if level == "error" else "app_info"
        try:
            when = datetime.fromtimestamp(int(e.get("t")) / 1000) if e.get("t") else None
        except Exception:
            when = None
        miss(uid, kind, f"{e.get('tag') or ''}: {e.get('msg') or ''}".strip(": "), when=when)
        n += 1
    return n


def day_entries(day: date, uid: int | None = None) -> list[dict[str, Any]]:
    path = _file(day)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if uid is None or item.get("uid") == uid:
            out.append(item)
    return out


def report_lines(day: date, uid: int, lang: str = "ru") -> list[str]:
    """Сводка для вечернего отчёта владельца: сколько каких промахов и по одному примеру."""
    items = [i for i in day_entries(day, uid) if i.get("kind") in LABELS]
    if not items:
        return []
    counts = Counter(i["kind"] for i in items)
    fixable = [k for k in counts if k not in QUIET]
    uz = lang == "uz"
    lines = ["", f"🛠 <b>{'JES xatolari bugun' if uz else 'Промахи JES за день'}</b>"]
    if not fixable:
        lines.append(f"   {'jiddiy xato yo‘q' if uz else 'серьёзных нет'} · {LABELS['false_wake']}: {counts.get('false_wake', 0)}")
        return lines
    for kind, n in counts.most_common():
        example = next((i["text"] for i in reversed(items) if i["kind"] == kind), "")
        line = f"   • {LABELS[kind]}: {n}"
        if kind not in QUIET and example:
            line += f" — «{_esc(example[:90])}»"
        lines.append(line)
    return lines


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ разбор разговора с телефона
_CYR = re.compile(r"[а-яё]", re.IGNORECASE)
_LAT = re.compile(r"[a-z]", re.IGNORECASE)
_ASK = re.compile(r"\b(кому|куда|что именно|какой именно|уточните|kimga|qayerga|qaysi)\b", re.IGNORECASE)


def _script(text: str) -> str | None:
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    if cyr + lat < 6:
        return None
    return "cyr" if cyr > lat * 2 else "lat" if lat > cyr * 2 else None


def check_session(uid: int, user_lines: list[str], jes_lines: list[str]) -> None:
    """После разговора с телефона: ответил не на том языке (он по-русски — JES по-узбекски) или переспросил вместо дела."""
    pairs = list(zip(user_lines, jes_lines))
    for said, answer in pairs:
        s, a = _script(said), _script(answer)
        if s and a and s != a:
            miss(uid, "lang", f"он: «{said[:80]}» → JES: «{answer[:80]}»")
            break
    for said, answer in pairs:
        if _ASK.search(answer) and "?" in answer:
            miss(uid, "ask_again", f"он: «{said[:80]}» → JES: «{answer[:80]}»")
            break


def cleanup() -> None:
    """Старше месяца — удалить."""
    try:
        cutoff = date.today().toordinal() - KEEP_DAYS
        for path in _dir().glob("*.jsonl"):
            try:
                if date.fromisoformat(path.stem).toordinal() < cutoff:
                    path.unlink()
            except ValueError:
                continue
    except Exception:
        logger.warning("journal: уборка не вышла", exc_info=True)


__all__ = ["miss", "app_events", "day_entries", "report_lines", "check_session", "cleanup", "LABELS"]
