"""Проверка «бухгалтера долгов» на настоящей модели агента, но на КОПИИ данных владельца в памяти: Supabase не меняется.

    docker exec -w /app -e PYTHONPATH=/app codex-self-bot python scripts/debt_probe.py

Каждый сценарий — реплики подряд в одном диалоге (ответы на вопросы с кнопками — следующей репликой).
Печатает вызовы инструментов, вопросы, ответ JES и что стало с долгами и картой.
"""
from __future__ import annotations

import asyncio
import json
import sys

# id 64/65 — записи 26.09 (Uzum → TEZ): для сценариев 1–2 проигрываем тот день заново (без них)
REPLAY = {1: {64, 65}, 2: {64, 65}}
SCENARIOS = [
    ["я взял долг у узум банка 1 050 000 и этим погасил свой долг в TEZ", "Через месяц — 26.10"],
    ["вернул TEZ 1 050 000", "Проценты / комиссия"],
    ["взял у Алишера 500 тысяч", "💵 Наличные"],
    ["Иззатилло ака вернул 600 тысяч на карту"],
    ["погасил долг Узум", "Весь долг"],
    ["купил телефон в рассрочку в Uzum Nasiya за 3 млн на 6 месяцев"],
    ["вернул Асилбеку 200 тысяч"],
    ["взял в долг 300 тысяч"],
    ["сколько я должен и кому? что закрыть первым?"],
]


async def main() -> None:
    from bot import agent_tools as tools
    from bot import agent_tools_debts as dt
    from bot import finance as fin
    from bot import services
    from bot.context import db, settings
    from bot.handlers import agent
    from bot.handlers.common import profile_by_id

    await db.connect()
    await db.health_check()
    uid = sorted(settings.allowed_telegram_ids)[0]
    profile = await profile_by_id(uid)
    real_entries = await db.list_finance_entries_all(uid)
    real_settings = await db.get_finance_settings(uid)
    real_deadlines = await db.list_debt_deadlines(uid) if db.available("debt_deadlines") else []

    class MemDB:
        """Записи — в память; чтение всего остального (лимиты, напоминания, еда) — из настоящей базы."""

        def __init__(self) -> None:
            self.entries = [dict(r) for r in real_entries]
            self.deadlines = [dict(r) for r in real_deadlines]
            self.next_id = 10_000_000

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(db, name)

        async def add_finance_entries(self, _uid, rows, *, entry_date, source="manual"):
            out = []
            for r in rows:
                self.next_id += 1
                row = {"id": self.next_id, "entry_type": r.get("entry_type"), "amount": float(r["amount"]), "category": r.get("category"),
                       "note": r.get("note"), "entry_date": str(r.get("entry_date") or entry_date.isoformat())[:10], "created_at": "9999"}
                self.entries.insert(0, row)
                out.append(row)
            return out

        async def get_finance_entry(self, _uid, rid):
            return next((dict(r) for r in self.entries if str(r["id"]) == str(rid)), None)

        async def update_finance_entry(self, _uid, rid, fields):
            for r in self.entries:
                if str(r["id"]) == str(rid):
                    r.update(fields)

        async def delete_finance_entries(self, _uid, ids):
            ids = {str(i) for i in ids}
            self.entries = [r for r in self.entries if str(r["id"]) not in ids]

        async def upsert_debt_deadline(self, _uid, *, person, side, due_date, note=None):
            self.deadlines = [d for d in self.deadlines if not (d["person"] == person and d["side"] == side)]
            self.deadlines.append({"person": person, "side": side, "due_date": due_date, "created_at": "9999-12-31"})
            return self.deadlines[-1]

        async def delete_debt_deadline(self, _uid, *, person, side):
            self.deadlines = [d for d in self.deadlines if not (d["person"] == person and d["side"] == side)]

    only = [int(a) for a in sys.argv[1:] if a.isdigit()]
    for n, scenario in enumerate(SCENARIOS, 1):
        if only and n not in only:
            continue
        mem = MemDB()
        for r in mem.entries:  # исправление 26.09: TEZ был закрыт на 986 250, а не на 1 050 000
            if str(r["id"]) == "64":
                r["amount"] = 986_250.0
        mem.entries = [r for r in mem.entries if int(r["id"]) not in REPLAY.get(n, set())]

        async def entries(_uid, mem=mem):
            return list(mem.entries)

        async def fin_settings(_uid):
            return dict(real_settings)

        async def deadlines(_uid, mem=mem):
            return list(mem.deadlines)

        services.finance_entries, services.finance_settings, services.debt_deadlines = entries, fin_settings, deadlines
        tools.db = dt.db = mem
        import bot.agent_tools_assistant as asst
        import bot.agent_tools_bulk as bulk
        asst.db = bulk.db = mem

        before = fin.compute_balances(mem.entries, real_settings)
        history: list = []
        print(f"\n=== {n}. " + " → ".join(scenario))
        for text in scenario:
            snapshot = await tools.snapshot(profile)
            res = await agent.run_agent(profile, text, history, snapshot=snapshot)
            new_msgs = res.contents[len(history):]
            history = res.contents
            for msg in new_msgs:
                for part in msg.get("parts") or []:
                    call = part.get("functionCall") if isinstance(part, dict) else None
                    if call and msg.get("role") == "model":
                        print(f"   🔧 {call.get('name')} {json.dumps(call.get('args'), ensure_ascii=False)[:400]}")
            print(f"   👤 {text}\n   🤖 {res.text}" + (f"\n   ❓ {res.ctx.ask['options']}" if res.ctx.ask else ""))
            history = history[-40:]
        after = fin.compute_balances(mem.entries, real_settings)
        book = fin.debt_book(mem.entries, real_settings, mem.deadlines)
        from bot import debts

        print("   💼 карта {} → {}, наличные {} → {}, я должен {} → {}, мне должны {} → {}".format(
            *(fin.fmt_money(x) for b in ("card", "cash", "debt", "lent") for x in (before[b], after[b]))))
        print("   📌 " + "; ".join(debts.describe(cp) for cp in book["debt"].values() if abs(cp.total) >= 1 or cp.paid))


if __name__ == "__main__":
    asyncio.run(main())
