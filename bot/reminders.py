"""Напоминания: формат хранения и сборка сообщения.

`reminders.reminder_text` хранит либо простой текст, либо `R1:{json}` с полями
text, links (ссылки по кругу, по одной в день), idx, once (однократно), date.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .profile import h

URL_RE = re.compile(r"https?://\S+")
DAY_PRESETS = {"daily": [1, 2, 3, 4, 5, 6, 7], "weekdays": [1, 2, 3, 4, 5], "weekend": [6, 7], "once": [1, 2, 3, 4, 5, 6, 7]}


def payload_of(row: dict[str, Any]) -> dict[str, Any]:
    raw = str(row.get("reminder_text") or "")
    if raw.startswith("R1:"):
        try:
            return json.loads(raw[3:])
        except Exception:
            pass
    return {"text": raw, "links": [], "idx": 0}


def encode(payload: dict[str, Any]) -> str:
    return "R1:" + json.dumps(payload, ensure_ascii=False)


def title(row: dict[str, Any]) -> str:
    p = payload_of(row)
    return f"{str(row.get('reminder_time') or '')[:5]} {p.get('text') or ''}".strip()


def days_from(value: Any) -> list[int]:
    if isinstance(value, list):
        out = sorted({int(x) for x in value if str(x).isdigit() and 1 <= int(x) <= 7})
        return out or DAY_PRESETS["daily"]
    return list(DAY_PRESETS.get(str(value or "daily").lower(), DAY_PRESETS["daily"]))


def days_label(days: list[int] | None, *, once: bool = False, lang: str = "ru") -> str:
    uz = lang == "uz"
    if once:
        return "bir marta" if uz else "один раз"
    d = sorted(set(days or DAY_PRESETS["daily"]))
    if d == DAY_PRESETS["daily"]:
        return "har kuni" if uz else "каждый день"
    if d == DAY_PRESETS["weekdays"]:
        return "ish kunlari" if uz else "по будням"
    if d == DAY_PRESETS["weekend"]:
        return "dam olish kunlari" if uz else "по выходным"
    names = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"] if uz else ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return ", ".join(names[i - 1] for i in d)


def message(row: dict[str, Any]) -> tuple[str, int]:
    """Текст для отправки и следующий индекс ссылки (ротация по кругу)."""
    p = payload_of(row)
    links = [x for x in (p.get("links") or []) if x]
    idx = int(p.get("idx") or 0)
    text = f"⏰ <b>{h(p.get('text') or 'Напоминание')}</b>"
    if links:
        link = links[idx % len(links)]
        text += f"\n{link}"
        if len(links) > 1:
            text += f"\n<i>{idx % len(links) + 1} / {len(links)}</i>"
        return text, (idx + 1) % len(links)
    return text, 0


__all__ = ["URL_RE", "DAY_PRESETS", "payload_of", "encode", "title", "days_from", "days_label", "message"]
