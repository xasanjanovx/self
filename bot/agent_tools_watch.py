"""Часы Amazfit (04.10.2026): «какой у меня пульс?», «сколько я спал?», «сколько шагов?» — из сводок часов (bot/watch.py).

Регистрируется в общем реестре `agent_tools.TOOLS` (импорт в конце bot/agent_tools.py) — и в чате, и голосом через bot_task.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .agent_tools import P, ToolContext, tool


@tool(
    "watch_health",
    "Данные с его часов Amazfit: пульс (сейчас и в покое), шаги, калории, стресс, SpO2, сон прошлой ночи (длительность, "
    "глубокий, оценка), заряд часов, надеты ли. «Какой у меня пульс?», «сколько я спал?», «сколько шагов сегодня?». "
    "hours > 0 — ещё и история сводок за последние часы (как менялся пульс/стресс).",
    {"hours": P("INTEGER", "история за последние N часов (0 — только последняя сводка)")},
)
async def _watch_health(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    from . import watch
    from .tg_user import data_dir

    uid = ctx.profile.telegram_id
    st = watch.load_state(uid)
    if not st:
        return {"error": "часы ещё не подключены: на часах должно быть открыто приложение JES"}
    out: dict[str, Any] = {"now": watch.health_line(uid, max_age_s=24 * 3600) or "свежих данных нет",
                           "last_seen_min_ago": round((time.time() - float(st.get("seen_at") or 0)) / 60)}
    try:
        hours = max(0, min(48, int(a.get("hours") or 0)))
    except (TypeError, ValueError):
        hours = 0
    if hours:
        since = time.time() - hours * 3600
        rows = []
        try:
            with open(data_dir() / "watch" / f"health_{uid}.jsonl", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if float(r.get("at") or 0) >= since:
                        rows.append({k: r.get(k) for k in ("hr", "stress", "steps", "spo2", "bat") if r.get(k) is not None}
                                    | {"time": time.strftime("%H:%M", time.localtime(float(r["at"])))})
        except OSError:
            pass
        out["history"] = rows[-48:]
    return out
