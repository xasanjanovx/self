"""04.10: экранное время — «у вас есть дела поважнее» нейтрально вслух, подробности в чат (bot/screentime.py, bot/nudge_voice.py)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot import nudge_voice, screentime
from bot.persona import Persona


def _profile(uid: int = 1):
    tz = ZoneInfo("Asia/Tashkent")
    return SimpleNamespace(telegram_id=uid, lang="ru", tz=tz, today=datetime.now(tz).date(), now=datetime.now(tz))


def test_categories_count_fun_not_work():
    assert screentime.category("com.instagram.android") == "social"
    assert screentime.category("com.google.android.youtube") == "social"
    assert screentime.category("org.telegram.messenger") == "telegram"
    assert screentime.category("com.android.chrome") == "browser"
    # телефон «по делу» не считается никогда
    for pkg in ("com.android.dialer", "ru.yandex.yandexnavi", "uz.kapitalbank.android", "com.android.settings", "uz.flow.jes",
                "com.miui.home", "com.android.camera"):
        assert screentime.category(pkg) == "work", pkg
    assert screentime.category("com.some.game") == "other"   # остальное — только в общий лимит


def test_proposal_is_20_percent_below_his_week_within_bounds():
    week = [{"day": f"2026-09-{d}", "apps": [{"pkg": "com.instagram.android", "min": 100}, {"pkg": "org.telegram.messenger", "min": 150},
                                                {"pkg": "com.android.chrome", "min": 5}, {"pkg": "com.android.dialer", "min": 60}]}
            for d in range(27, 31)]
    p = screentime.propose(week, set())
    assert p["days"] == 4 and p["avg"]["social"] == 100 and p["avg"]["total"] == 255   # звонилка не в счёт
    assert p["limits"]["social"] == 80 and p["limits"]["telegram"] == 120 and p["limits"]["total"] == 205
    assert p["limits"]["browser"] == screentime.FLOORS["browser"]                       # 4 мин — не ниже разумного минимума
    assert screentime.propose(week[:2], set()) is None                                 # меньше 3 дней — рано
    assert screentime.propose(week, {"com.instagram.android"})["avg"]["social"] == 0   # исключённое не считается


def test_record_usage_proposes_once_and_applies(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    hist = [{"day": f"2026-10-0{d}", "apps": [{"pkg": "com.instagram.android", "label": "Instagram", "min": 90}]} for d in (1, 2, 3)]
    p = screentime.record_usage(1, {"day": "2026-10-04", "apps": [{"pkg": "com.instagram.android", "label": "Instagram", "min": 30}],
                                    "pickups": 40, "history": hist})
    assert p and p["limits"]["social"] == 70
    st = screentime.load(1)
    assert screentime.limits(st)["social"] == 70 and st["days"]["2026-10-04"]["pickups"] == 40
    assert screentime.record_usage(1, {"day": "2026-10-04", "apps": [], "history": hist}) is None   # второй раз не предлагаем


def test_rules_are_softer_when_important_things_are_done(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    todo: list[str] = ["Сдать отчёт"]

    async def important_open(profile):  # noqa: ANN001
        return list(todo)

    async def wake_hhmm(profile):  # noqa: ANN001
        return "05:10"

    monkeypatch.setattr(screentime, "important_open", important_open)
    monkeypatch.setattr(screentime, "_wake_hhmm", wake_hhmm)
    strict = asyncio.run(screentime.rules(_profile()))
    assert strict["limits"]["social"] == 60 and strict["streak"] == 20 and not strict["soft"]
    assert strict["night_from"] == "23:00" and strict["night_to"] == "05:10" and strict["categories"]["org.telegram.messenger"] == "telegram"
    todo.clear()
    soft = asyncio.run(screentime.rules(_profile()))
    assert soft["limits"]["social"] == 78 and soft["streak"] == 30 and soft["soft"]


class _Bot:
    def __init__(self):
        self.sent: list[str] = []
        self.deleted: list[int] = []

    async def send_message(self, uid, text, **kw):  # noqa: ANN001, ANN003
        self.sent.append(text)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def delete_message(self, uid, mid):  # noqa: ANN001
        self.deleted.append(mid)


def test_alert_details_go_to_chat_and_busy_or_hour_cap_keep_jes_quiet(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def important_open(profile):  # noqa: ANN001
        return ["Сдать отчёт", "Позвонить в банк"]

    monkeypatch.setattr(screentime, "important_open", important_open)
    bot = _Bot()
    data = {"kind": "limit", "pkg": "com.instagram.android", "label": "Instagram", "category": "social", "level": 2,
            "today_min": 72, "limit_min": 60}
    assert asyncio.run(screentime.alert(bot, _profile(), data)) == {"speak": True}
    assert "Instagram" in bot.sent[0] and "1 ч 12 мин" in bot.sent[0] and "Сдать отчёт" in bot.sent[0]   # подробности — только в чат
    asyncio.run(screentime.alert(bot, _profile(), data))
    assert bot.deleted == [101]                                              # прошлое сообщение убрано — не копим
    asyncio.run(screentime.alert(bot, _profile(), data))
    assert asyncio.run(screentime.alert(bot, _profile(), data))["reason"] == "hour_cap"   # не больше 3 в час
    screentime.save(1, {**screentime.load(1), "alerts": []})
    screentime.set_busy(1, 60)                                               # «я работаю»
    assert asyncio.run(screentime.alert(bot, _profile(), data)) == {"speak": False, "reason": "busy"}
    screentime.set_busy(1, None)
    screentime.set_enabled(1, False)
    assert asyncio.run(screentime.alert(bot, _profile(), data))["speak"] is False


def test_evening_line(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    p = _profile()
    y = (p.today - timedelta(days=1)).isoformat()
    screentime.record_usage(1, {"day": y, "apps": [{"pkg": "com.instagram.android", "label": "Instagram", "min": 60}]})
    screentime.save(1, {**screentime.load(1), "proposed": True})
    screentime.record_usage(1, {"day": p.today.isoformat(), "pickups": 57, "apps": [
        {"pkg": "com.instagram.android", "label": "Instagram", "min": 95}, {"pkg": "org.telegram.messenger", "label": "Telegram", "min": 40},
        {"pkg": "com.android.dialer", "label": "Телефон", "min": 30}]})
    line = screentime.brief_lines(p)[0]
    assert "2 ч 15 мин" in line and "Instagram 1 ч 35 мин" in line and "57 раз" in line and "+1 ч 15 мин" in line
    assert "Телефон" not in line                                             # звонки — не «залипание»


def test_spoken_phrases_are_neutral_and_cover_every_level():
    items = nudge_voice.texts(Persona(lang="ru", honorific="mix", mirror=True))
    kinds = {k for k, _, _ in items}
    assert kinds == {"l1", "l2", "l3", "night"} and {lang for _, _, lang in items} == {"ru", "uz"}
    for _, text, _ in items:
        low = text.lower()
        # на улице и в транспорте рядом не должны услышать личного: ни названий приложений, ни молитвы
        assert not any(w in low for w in ("намаз", "namoz", "instagram", "youtube", "tiktok", "соцсет", "{")), text
    assert any("дела поважнее" in t for _, t, _ in items) and any("Telegram" in t and "важное уведомление" in t for _, t, _ in items)


def test_busy_tool_until_time(monkeypatch, tmp_path):
    from bot import agent_tools
    from bot.agent_tools import ToolContext

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    p = _profile()
    ctx = ToolContext(profile=p, text="я работаю до 18:00")
    soon = (p.now + timedelta(hours=2)).strftime("%H:%M")
    res = asyncio.run(agent_tools.TOOLS["screen_busy"].handler(ctx, {"until": soon}))
    assert res["ok"] and res["busy_until"] == soon
    assert screentime.busy_until(screentime.load(1)) is not None
    res = asyncio.run(agent_tools.TOOLS["screen_busy"].handler(ctx, {"off": True}))
    assert res == {"ok": True, "busy": False} and screentime.busy_until(screentime.load(1)) is None


def test_screen_question_is_answered_from_the_phone_without_a_model():
    """04.10: «сколько я пользовался телефоном?» — JES открывал настройки. Теперь ответ из device["screen"], без Gemini."""
    from bot import instant

    for q in ("Сколько я сегодня пользовался телефоном?", "сколько я сидел в телефоне", "Сколько времени я провёл в телефоне сегодня",
              "какое у меня экранное время"):
        assert instant.quick(q) == "screen", q
    assert instant.quick("сколько я потратил сегодня") == "ask"            # деньги — по-прежнему агент бота
    screen = {"total_min": 290, "pickups": 81, "top": [{"app": "AyuGram", "min": 76}, {"app": "Chrome", "min": 46}, {"app": "Instagram", "min": 37}]}
    text = instant.local_answer("screen", datetime.now(), {"screen": screen})
    assert text == ("Сегодня в телефоне 4 часа 50 минут. Больше всего — AyuGram 1 час 16 минут, Chrome 46 минут, Instagram 37 минут. "
                    "Брали телефон 81 раз.")
    assert "История использования" in instant.local_answer("screen", datetime.now(), {"screen": {"allowed": False}})
    assert instant.local_answer("screen", datetime.now(), {}) is None      # старое приложение — ответит модель


def test_phone_tool_screen_time_reads_the_phone_snapshot():
    from bot import live_call, phone
    from bot.agent_tools import ToolContext

    assert "phone_usage" in live_call.PHONE_LIVE_CORE                       # в голосе — сразу, без phone_task и настроек
    turn = phone.PhoneTurn(uid=1, device={"screen": {"total_min": 120, "top": []}})
    ctx = ToolContext(profile=_profile(), text="")
    assert asyncio.run(phone.PHONE_TOOLS["phone_usage"].handler(turn, ctx, {}))["total_min"] == 120
    assert "error" in asyncio.run(phone.PHONE_TOOLS["phone_usage"].handler(phone.PhoneTurn(uid=1), ctx, {}))


def test_app_names_are_human():
    assert screentime.pretty("com.instagram.android", "com.instagram.android") == "Instagram"
    assert screentime.pretty("com.radolyn.ayugram", "AyuGram") == "AyuGram"
    assert screentime.pretty("com.readygo.barrel.gp", "com.readygo.barrel.gp") == "Readygo Barrel"
    for pkg in ("com.xiaomi.subscreencenter", "com.android.incallui", "com.xiaomi.aiasst.service", "com.huami.watch.hmwatchmanager",
                "uz.ucell.ucellmobile", "com.miui.securitycore"):
        assert screentime.category(pkg) == "work", pkg
