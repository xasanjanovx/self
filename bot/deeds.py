"""Память дел (29.09, его «да»): что JES делала для него и что происходило — «что я делал вчера?», «когда я последний раз
звонил Алишеру?», и из этого же — итоги недели.

Каждое действие (звонок, сообщение, запись траты, напоминание, поиск, будильник, урок, входящий звонок, подъём) — одна
строка в DATA_DIR/deeds/<uid>.jsonl: {"t": местное время, "src": телефон|звонок|чат, "tool", "text"}. Пишется там, где
инструменты выполняются (agent_tools.run, phone.make_runner), поэтому инструкции ответов не растут — токены тратятся,
только когда он спросит (recall_deeds), ~$0.001 за вопрос.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

KEEP_DAYS = 120
MAX_LINES = 6000

# откуда пришло действие: телефон (голос JES), звонок (Telegram), чат (сообщения боту)
source: contextvars.ContextVar[str] = contextvars.ContextVar("deeds_source", default="чат")

# только чтение и служебное — в историю не пишем (иначе «что я делал» утонет в «посмотрела погоду»)
_SKIP = {"ai_status", "calculate", "currency_rates", "weather", "recall_deeds", "bot_task", "phone_task", "end_call", "hand_off",
         "ask_user", "open_screen", "expect_photo", "remember_about_me", "remember_contact", "screen_look", "look", "phone_status",
         "recent_calls", "telegram_read", "telegram_search", "youtube_search", "cancel_send", "confirm_awake", "snooze",
         "device_action", "set_volume", "brightness", "media", "flashlight", "undo_last", "settings_panel", "live_mode",
         "send_to_chat", "gallery", "set_ai_balance", "video_resume_link", "list_place_reminders", "list_daily"}
_SKIP_PREFIX = ("get_", "list_", "find_", "search_", "show_", "check_")
_LABELS = {
    "phone_call": "звонок", "call_back": "перезвонила", "send_sms": "SMS", "telegram_send": "сообщение в Telegram",
    "confirm_send": "отправила сообщение", "whatsapp_send": "WhatsApp", "set_alarm": "будильник", "set_timer": "таймер",
    "open_app": "открыла приложение", "open_link": "открыла ссылку", "navigate": "маршрут", "play_media": "включила",
    "resume_video": "продолжила видео", "taxi": "такси", "calendar_add": "в календарь", "save_place_here": "запомнила место",
    "web_search": "искала", "add_finance_entries": "записала", "add_calorie_logs": "еда", "call_me": "позвонила вам в Telegram",
    "incoming_call": "вам звонил(а)", "awake": "подъём", "lesson": "урок", "add_daily": "каждый день", "daily_done": "сделано",
    "call_forwarding": "переадресация", "do_not_disturb": "«Не беспокоить»", "ringer_mode": "режим звонка",
}
# что в аргументах/ответе говорит «кому/что» — остальное (id, флаги) не храним
_KEYS = ("who", "name", "contact", "to", "query", "title", "text", "app", "url", "place", "destination", "time", "at", "minutes",
         "city", "note", "description", "category", "amount", "on", "mode", "summary", "request")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("APP_TIMEZONE") or "Asia/Tashkent")
    except Exception:
        return ZoneInfo("Asia/Tashkent")


def _file(uid: int) -> Path | None:
    folder = os.getenv("DATA_DIR")
    if not folder:
        return None  # тесты и локальный запуск — без диска (кроме тестов, где DATA_DIR задан)
    return Path(folder) / "deeds" / f"{int(uid)}.jsonl"


def wanted(tool: str) -> bool:
    return bool(tool) and tool not in _SKIP and not tool.startswith(_SKIP_PREFIX)


def _short(value: Any, limit: int = 70) -> str:
    if isinstance(value, (list, tuple)):
        value = ", ".join(_short(v, 40) for v in value[:4] if v not in (None, "", []))
    elif isinstance(value, dict):
        value = " ".join(_short(value.get(k), 40) for k in ("amount", "category", "note", "description", "title", "name") if value.get(k))
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(tool: str, args: dict[str, Any] | None, result: dict[str, Any] | None = None) -> str:
    """«звонок: Mashxurbek Aka» — по-человечески и коротко. Имя из ответа (как записан контакт) — точнее, чем «мама»."""
    args = args or {}
    result = result if isinstance(result, dict) else {}
    label = _LABELS.get(tool, tool)
    if tool == "add_finance_entries":
        rows = args.get("entries") or []
        parts = [f"{_short(r.get('amount'), 20)} {_short(r.get('category') or r.get('note') or '', 30)}".strip()
                 for r in rows[:4] if isinstance(r, dict)]
        return f"{label}: " + "; ".join(p for p in parts if p) if parts else label
    bits: list[str] = []
    for key in ("contact", "who", "name", "title", "query", "text", "app", "destination", "place", "time", "at", "minutes",
                "city", "url", "request", "summary", "description", "on", "mode"):
        val = result.get(key) if key in ("contact", "title") and result.get(key) else args.get(key)
        if val in (None, "", [], {}) or (key == "who" and bits):
            continue
        if key == "text" and bits:
            bits.append(f"«{_short(val, 60)}»")
        else:
            bits.append(_short(val))
        if len(bits) >= 2:
            break
    return f"{label}: {' '.join(bits)}" if bits else label


_recent: dict[tuple[int, str, str], float] = {}


def note(uid: int | None, tool: str, args: dict[str, Any] | None = None, result: dict[str, Any] | None = None, *,
         src: str | None = None, text: str | None = None, dedupe_s: float = 0) -> None:
    """Записать дело. Ошибки, «ждёт подтверждения» и чтение — не дела. dedupe_s — то же самое недавно уже записано
    (телефон присылает входящий звонок и место в ролике по нескольку раз)."""
    if not uid or not wanted(tool):
        return
    if isinstance(result, dict) and (result.get("error") or result.get("ask_exactly") or result.get("status") == "awaiting_confirmation"):
        return
    path = _file(uid)
    if path is None:
        return
    row = {"t": datetime.now(_tz()).strftime("%Y-%m-%dT%H:%M"), "src": src or source.get(), "tool": tool,
           "text": text or describe(tool, args, result)}
    if dedupe_s:
        key, now = (int(uid), tool, row["text"]), time.monotonic()
        if now - _recent.get(key, -1e9) < dedupe_s:
            return
        _recent[key] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if path.stat().st_size > MAX_LINES * 160:
            _prune(path)
    except Exception:
        logger.warning("deeds: не записала", exc_info=True)


def _prune(path: Path) -> None:
    cutoff = (datetime.now(_tz()) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line[7:17] >= cutoff][-MAX_LINES:]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def rows(uid: int, *, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
    path = _file(uid)
    if path is None or not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        day = str(row.get("t") or "")[:10]
        if (start and day < start.isoformat()) or (end and day > end.isoformat()):
            continue
        out.append(row)
    return out


def _latin(text: str) -> str:
    """Всё в одну латиницу: «Алишер» и «Alisher Aka» совпадут (как имена контактов, bot/names.py)."""
    from . import names

    low = str(text or "").lower().replace("ё", "е")
    try:
        return names.norm(low)
    except Exception:
        return low


def _stem(word: str) -> str:
    """«Алишеру» → «alisher», «такси» → «taks»: падежи не мешают поиску."""
    base = word[:-1] if len(word) > 4 and word[-1] in "уеаиояюьыi" else word
    return _latin(base)


def search(uid: int, query: str = "", *, variants: list[str] | None = None, start: date | None = None, end: date | None = None,
           limit: int = 40) -> list[dict[str, Any]]:
    """Свежие — первыми. query — все его слова должны встретиться; variants — другие написания (мама → Onajonim, oyi):
    подходит любое."""
    words = [_stem(w) for w in re.split(r"\W+", str(query or "").lower()) if len(w) >= 3]
    alts = [_stem(v) for v in (variants or []) if len(str(v).strip()) >= 3]
    found = []
    for row in reversed(rows(uid, start=start, end=end)):
        hay = _latin(f"{row.get('text')} {row.get('tool')} {_LABELS.get(str(row.get('tool')), '')}")
        if (words and all(w in hay for w in words)) or any(a in hay for a in alts) or not (words or alts):
            found.append(row)
            if len(found) >= limit:
                break
    return found


def line(row: dict[str, Any]) -> str:
    t = str(row.get("t") or "")
    return f"{t[8:10]}.{t[5:7]} {t[11:16]} · {row.get('src') or ''} · {row.get('text') or row.get('tool')}"


def week_counts(uid: int, start: date, end: date) -> dict[str, int]:
    """Для итогов недели: сколько звонков, сообщений, записей… сделала JES."""
    groups = {"звонки": {"phone_call", "call_back", "call_me"}, "сообщения": {"confirm_send", "send_sms", "whatsapp_send"},
              "записи": {"add_finance_entries", "add_calorie_logs"}, "поиск": {"web_search"}, "входящие": {"incoming_call"},
              "видео и музыка": {"play_media", "resume_video", "lesson"}}
    counts: Counter[str] = Counter()
    for row in rows(uid, start=start, end=end):
        for name, tools in groups.items():
            if row.get("tool") in tools:
                counts[name] += 1
    counts["всего"] = len(rows(uid, start=start, end=end))
    return dict(counts)


__all__ = ["note", "search", "rows", "line", "describe", "week_counts", "source", "wanted"]
