"""Экранное время: «у вас есть дела поважнее» (04.10.2026, его просьба «сделать JES умнее»).

Телефон (приложение JES) сам считает, сколько он сидит в приложениях (Android «история использования»: проверка только при
включённом экране и только к моменту, когда может сработать порог, — без GPS, без чтения экрана, без опроса каждую секунду).
Сработал порог — JES говорит ВСЛУХ только нейтральную записанную фразу («Сэр, у вас есть дела поважнее», «Сэр, проверьте
Telegram — там важное уведомление»; записи — bot/nudge_voice.py, токены не тратятся), а подробности («Instagram 1 ч 12 мин,
лимит 1 ч» и его важные дела) — сюда, в чат бота: на улице и в транспорте никто рядом не услышит лишнего. Беззвучный режим — вибрация.

Его выбор 04.10:
  • лимит в день на группу приложений + «подряд без перерыва» + после 23:00 (до времени будильника);
  • соцсети и видео, Telegram (лимит больше: работа @ishdasiz тоже там — «JES, я работаю» снимает напоминания), браузер
    (только общее время, сайты не видим) и всё остальное, кроме исключённого (общий лимит); телефон «по делу» (звонки,
    навигатор, такси, банки, камера, настройки, JES) не считается вовсе;
  • лимиты JES предлагает сам по его статистике за неделю (на 20% меньше обычного), он правит кнопками;
  • все важные дела на сегодня сделаны — мягче (лимиты ×1.3); есть несделанное главное — как есть и напоминаем про дела;
  • напоминания нарастают (тихо → настойчивее → с вибрацией), не больше 3 в час; про намаз и названия приложений вслух — нет;
  • итог экранного времени — строкой в вечерней сводке бота.
Состояние — DATA_DIR/screentime_<uid>.json.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATS = ("social", "telegram", "browser", "total")
DEFAULT_LIMITS = {"social": 60, "telegram": 120, "browser": 60, "total": 300}   # минут в день, пока нет его статистики
FLOORS = {"social": 15, "telegram": 30, "browser": 15, "total": 60}
CEILINGS = {"social": 240, "telegram": 360, "browser": 240, "total": 720}
STREAK_MIN = 20          # подряд без перерыва
NIGHT_STREAK_MIN = 5     # после 23:00 — столько подряд, и JES говорит
NIGHT_FROM = "23:00"
SOFT_FACTOR = 1.3        # все важные дела на сегодня сделаны — мягче
PROPOSE_CUT = 0.8        # предложение — на 20% меньше обычного
ESCALATE_MIN = 10
MAX_PER_HOUR = 3
DEFAULT_BUSY_MIN = 120

# группы приложений (пакет → группа); остальное — «other» (идёт только в общий лимит)
SOCIAL = {
    "com.instagram.android", "com.instagram.lite", "com.zhiliaoapp.musically", "com.ss.android.ugc.trill", "com.ss.android.ugc.aweme",
    "com.google.android.youtube", "com.google.android.apps.youtube.mango", "app.revanced.android.youtube", "com.facebook.katana",
    "com.facebook.lite", "com.twitter.android", "com.snapchat.android", "video.like", "com.vkontakte.android", "ru.ok.android",
    "com.pinterest", "com.reddit.frontpage", "tv.twitch.android.app", "com.netflix.mediaclient", "com.smile.gifmaker",
    "tv.danmaku.bili", "com.xingin.xhs", "com.sina.weibo", "com.threads.android", "com.instagram.barcelona", "com.kwai.video",
    "com.ss.android.ugc.aweme.lite", "com.tencent.qqlive", "com.youku.phone", "com.qiyi.video", "com.hunantv.imgo.activity",
}
TELEGRAM = {"org.telegram.messenger", "org.telegram.messenger.web", "org.telegram.plus", "org.thunderdog.challegram",
            "tw.nekomimi.nekogram", "xyz.nextalone.nagram", "org.telegram.mdgram", "com.radolyn.ayugram", "uz.dasturlash.telegram"}
BROWSER = {"com.android.chrome", "com.chrome.beta", "com.android.browser", "com.mi.globalbrowser", "org.mozilla.firefox",
           "com.opera.browser", "com.opera.mini.native", "com.yandex.browser", "ru.yandex.searchplugin", "com.microsoft.emmx",
           "com.brave.browser", "com.UCMobile", "com.UCMobile.intl", "com.quark.browser", "com.duckduckgo.mobile.android",
           "com.sec.android.app.sbrowser", "com.huawei.browser", "com.vivo.browser", "com.heytap.browser"}
# «телефон по делу» — не считается никогда (подстроки в имени пакета)
WORK_HINTS = ("dialer", "incallui", "contacts", "telecom", "phone", "mms", "messaging", "camera", "settings", "launcher", "systemui",
              "inputmethod", "keyboard", "clock", "deskclock", "calculator", "calendar", "maps", "navi", "taxi", "yandex.go", "uber",
              "mytaxi", "bank", "pay", "click", "payme", "wallet", "miui.home", "globallauncher", "security", "packageinstaller", "permissioncontroller",
              "uz.flow.jes", "uz.flow.jarvis", "gallery", "files", "filemanager", "fileexplorer", "notes", "weather", "health",
              "fitness", "translate", "scanner", "authenticator")


def category(pkg: str) -> str:
    """Группа приложения: social | telegram | browser | work (не считаем) | other (только в общий лимит)."""
    p = (pkg or "").strip()
    if p in SOCIAL:
        return "social"
    if p in TELEGRAM:
        return "telegram"
    if p in BROWSER:
        return "browser"
    low = p.lower()
    if any(h in low for h in WORK_HINTS):
        return "work"
    return "other"


def _file(uid: int) -> Path | None:
    folder = os.getenv("DATA_DIR")
    return Path(folder) / f"screentime_{int(uid)}.json" if folder else None


def load(uid: int) -> dict[str, Any]:
    path = _file(uid)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(uid: int, data: dict[str, Any]) -> None:
    path = _file(uid)
    if path is None:
        return
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("screentime: не сохранил", exc_info=True)


def limits(st: dict[str, Any]) -> dict[str, int]:
    out = dict(DEFAULT_LIMITS)
    out.update({k: int(v) for k, v in (st.get("limits") or {}).items() if k in CATS and str(v).isdigit()})
    return out


# ------------------------------------------------------------------ его важные дела на сегодня
async def important_open(profile) -> list[str]:  # noqa: ANN001
    """Несделанное главное на сегодня: «главное» из плана дня + задачи на сегодня и просроченные."""
    from . import plan as plan_mod
    from . import services, tasks as tasks_mod

    out: list[str] = []
    try:
        st = plan_mod.load(profile.telegram_id)
        if st.get("day") == profile.today.isoformat():
            out += [str(it.get("text") or "") for it in st.get("items") or [] if it.get("kind") == "main" and not it.get("done")]
    except Exception:
        logger.debug("screentime: план не прочитан", exc_info=True)
    try:
        g = tasks_mod.group(await services.tasks(profile.telegram_id), profile.today)
        out += [str(t.get("text") or "") for t in (g.overdue + g.today)]
    except Exception:
        logger.debug("screentime: задачи не прочитаны", exc_info=True)
    return [t for t in dict.fromkeys(x.strip() for x in out) if t][:5]


def _night_to(profile) -> str:  # noqa: ANN001
    return "05:00"


async def _wake_hhmm(profile) -> str:  # noqa: ANN001
    """Ночь кончается подъёмом: время будильника (завтра), иначе 05:00."""
    try:
        from . import wake_runner

        _, plan = await wake_runner.plan_for(profile, profile.today + timedelta(days=1))
        if plan.active and plan.wake_at:
            return plan.wake_at.astimezone(profile.tz).strftime("%H:%M")
    except Exception:
        logger.debug("screentime: время будильника не узнал", exc_info=True)
    return _night_to(profile)


def busy_until(st: dict[str, Any]) -> datetime | None:
    try:
        until = datetime.fromisoformat(str(st.get("busy_until")))
    except (TypeError, ValueError):
        return None
    return until if until > datetime.now(timezone.utc) else None


async def rules(profile) -> dict[str, Any]:  # noqa: ANN001
    """Что телефону считать и когда говорить (GET /jarvis/v1/screen_rules). Пороги — уже с поправкой «дела сделаны — мягче»."""
    st = load(profile.telegram_id)
    base = limits(st)
    soft = not await important_open(profile)
    factor = SOFT_FACTOR if soft else 1.0
    busy = busy_until(st)
    return {
        "enabled": bool(st.get("enabled", True)),
        "limits": {k: int(round(v * factor)) for k, v in base.items()},
        "streak": int(round(int(st.get("streak") or STREAK_MIN) * (1.5 if soft else 1.0))),
        "night_streak": NIGHT_STREAK_MIN,
        "night_from": NIGHT_FROM,
        "night_to": await _wake_hhmm(profile),
        "busy_until_ms": int(busy.timestamp() * 1000) if busy else 0,
        "escalate_min": ESCALATE_MIN,
        "max_per_hour": MAX_PER_HOUR,
        "categories": {**{p: "social" for p in SOCIAL}, **{p: "telegram" for p in TELEGRAM}, **{p: "browser" for p in BROWSER}},
        "work_hints": list(WORK_HINTS),
        "excluded": list(st.get("excluded") or []),
        "soft": soft,
    }


# ------------------------------------------------------------------ статистика с телефона и предложение лимитов
def _by_cat(apps: list[dict[str, Any]], excluded: set[str]) -> dict[str, float]:
    out = {k: 0.0 for k in CATS}
    for a in apps:
        pkg = str(a.get("pkg") or "")
        cat = category(pkg)
        if cat == "work" or pkg in excluded:
            continue
        m = float(a.get("min") or 0)
        if cat in out:
            out[cat] += m
        out["total"] += m
    return out


def _round5(v: float) -> int:
    return int(5 * round(v / 5))


def propose(history: list[dict[str, Any]], excluded: set[str]) -> dict[str, Any] | None:
    """Лимиты по его неделе: среднее в день по группе × 0.8, в разумных пределах. Меньше 3 дней данных — рано."""
    days = [d for d in history if isinstance(d.get("apps"), list)]
    if len(days) < 3:
        return None
    sums = {k: 0.0 for k in CATS}
    for d in days:
        for k, v in _by_cat(d["apps"], excluded).items():
            sums[k] += v
    avg = {k: v / len(days) for k, v in sums.items()}
    lim = {k: max(FLOORS[k], min(CEILINGS[k], _round5(avg[k] * PROPOSE_CUT))) for k in CATS}
    return {"avg": {k: int(round(v)) for k, v in avg.items()}, "limits": lim, "days": len(days)}


def record_usage(uid: int, data: dict[str, Any]) -> dict[str, Any] | None:
    """Сегодняшние минуты по приложениям (и раз в день — прошлые 7 дней). Вернёт предложение лимитов, если пора его прислать."""
    st = load(uid)
    days = st.get("days") or {}
    day = str(data.get("day") or "")[:10]
    if day:
        days[day] = {"apps": [{"pkg": str(a.get("pkg") or "")[:120], "label": str(a.get("label") or "")[:60], "min": round(float(a.get("min") or 0), 1)}
                              for a in (data.get("apps") or [])[:40] if isinstance(a, dict)],
                     "pickups": int(data.get("pickups") or 0), "screen_min": round(float(data.get("screen_min") or 0), 1),
                     "at": datetime.now(timezone.utc).isoformat()}
    for h in data.get("history") or []:
        d = str((h or {}).get("day") or "")[:10]
        if d and d != day and d not in days:
            days[d] = {"apps": [{"pkg": str(a.get("pkg") or "")[:120], "label": str(a.get("label") or "")[:60], "min": round(float(a.get("min") or 0), 1)}
                                for a in (h.get("apps") or [])[:40] if isinstance(a, dict)], "history": True}
    keep = sorted(days)[-30:]
    st["days"] = {d: days[d] for d in keep}
    proposal = None
    if not st.get("proposed"):
        past = [st["days"][d] for d in keep if d != day][-7:]
        proposal = propose(past, set(st.get("excluded") or []))
        if proposal:
            st["proposed"] = True
            st["limits"] = proposal["limits"]   # действует сразу; он поправит кнопками «мягче / строже»
    save(uid, st)
    return proposal


def fmt_min(m: float, uz: bool = False) -> str:
    m = int(round(m))
    h, mm = divmod(m, 60)
    if uz:
        return (f"{h} soat " if h else "") + (f"{mm} daq" if mm or not h else "")
    return (f"{h} ч " if h else "") + (f"{mm} мин" if mm or not h else "")


LABELS = {"social": ("Соцсети и видео", "Ijtimoiy tarmoq va video"), "telegram": ("Telegram", "Telegram"),
          "browser": ("Браузер", "Brauzer"), "total": ("Всего", "Jami")}


def label(cat: str, uz: bool) -> str:
    pair = LABELS.get(cat, (cat, cat))
    return pair[1] if uz else pair[0]


def proposal_text(profile, p: dict[str, Any]) -> str:  # noqa: ANN001
    uz = profile.lang == "uz"
    head = ("📱 <b>Ekran vaqti</b> — so'nggi {n} kunda o'rtacha:" if uz else "📱 <b>Экранное время</b> — в среднем за {n} дн.:").format(n=p["days"])
    rows = [f"• {label(k, uz)}: {fmt_min(p['avg'][k], uz)} → <b>{fmt_min(p['limits'][k], uz)}</b>" for k in CATS]
    tail = ("Limitlar 20% kamroq — allaqachon ishlayapti. Ketma-ket {s} daqiqadan ko'p yoki 23:00 dan keyin — JES aytadi. "
            "Muhim ishlaringiz bajarilgan kunlarda — yumshoqroq." if uz else
            "Лимиты — на 20% меньше обычного, уже действуют. Подряд больше {s} мин или после 23:00 — JES скажет. "
            "В дни, когда важные дела сделаны, — мягче.").format(s=STREAK_MIN)
    return "\n".join([head, *rows, "", tail])


def settings_keyboard(profile, st: dict[str, Any]):  # noqa: ANN001, ANN201
    from aiogram.types import InlineKeyboardMarkup

    from .keyboards import _btn

    uz = profile.lang == "uz"
    on = bool(st.get("enabled", True))
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("➖ " + ("Qattiqroq (−20%)" if uz else "Строже (−20%)"), "scr:hard"), _btn("➕ " + ("Yumshoqroq (+20%)" if uz else "Мягче (+20%)"), "scr:soft")],
        [_btn(("😌 Ishlayapman 2 soat" if uz else "😌 Я работаю 2 ч"), "scr:busy:120"), _btn(("Bandlik tugadi" if uz else "Снять «занят»"), "scr:busy:0")],
        [_btn(("📱 Kuzatish: yoqilgan" if uz else "📱 Следить: вкл") if on else ("📱 Kuzatish: o'chiq" if uz else "📱 Следить: выкл"),
              "scr:toggle", style="success" if on else None)],
    ])


def settings_text(profile, st: dict[str, Any]) -> str:  # noqa: ANN001
    uz = profile.lang == "uz"
    lim = limits(st)
    lines = ["📱 <b>" + ("Ekran vaqti" if uz else "Экранное время") + "</b>"]
    lines += [f"• {label(k, uz)}: {'kuniga' if uz else 'в день'} <b>{fmt_min(lim[k], uz)}</b>" for k in CATS]
    lines.append(("• Ketma-ket: " if uz else "• Подряд без перерыва: ") + f"<b>{int(st.get('streak') or STREAK_MIN)} {'daq' if uz else 'мин'}</b>")
    busy = busy_until(st)
    if busy:
        lines.append(("😌 Band: " if uz else "😌 Занят до ") + f"<b>{busy.astimezone(profile.tz):%H:%M}</b>")
    today = (st.get("days") or {}).get(profile.today.isoformat())
    if today:
        by = _by_cat(today.get("apps") or [], set(st.get("excluded") or []))
        lines.append("")
        lines.append(("Bugun: " if uz else "Сегодня: ") + " · ".join(f"{label(k, uz)} {fmt_min(by[k], uz)}" for k in CATS if by[k] >= 1))
    return "\n".join(lines)


def adjust(uid: int, factor: float) -> dict[str, int]:
    st = load(uid)
    lim = {k: max(FLOORS[k], min(CEILINGS[k], _round5(v * factor))) for k, v in limits(st).items()}
    st["limits"] = lim
    save(uid, st)
    return lim


def set_busy(uid: int, minutes: int | None) -> datetime | None:
    """«Я работаю» — до какого времени не напоминать (None / 0 — снять)."""
    st = load(uid)
    until = datetime.now(timezone.utc) + timedelta(minutes=int(minutes)) if minutes else None
    st["busy_until"] = until.isoformat() if until else None
    save(uid, st)
    return until


def set_enabled(uid: int, on: bool) -> None:
    st = load(uid)
    st["enabled"] = bool(on)
    save(uid, st)


# ------------------------------------------------------------------ сработал порог на телефоне
async def alert(bot, profile, data: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    """Телефон: «пора сказать» (kind limit|streak|night, level 1–3). Отвечаем, говорить ли, и шлём подробности в чат."""
    from aiogram.types import InlineKeyboardMarkup

    from .keyboards import _btn

    uid = profile.telegram_id
    st = load(uid)
    if not st.get("enabled", True):
        return {"speak": False, "reason": "off"}
    if busy_until(st):
        return {"speak": False, "reason": "busy"}
    now = datetime.now(timezone.utc)
    recent = [t for t in st.get("alerts") or [] if now - datetime.fromisoformat(t) < timedelta(hours=1)]
    if len(recent) >= MAX_PER_HOUR:
        return {"speak": False, "reason": "hour_cap"}
    st["alerts"] = recent + [now.isoformat()]
    uz = profile.lang == "uz"
    kind = str(data.get("kind") or "limit")
    cat = str(data.get("category") or category(str(data.get("pkg") or "")))
    app = re.sub(r"[<>&]", "", str(data.get("label") or data.get("pkg") or ""))[:40]
    today_min = float(data.get("today_min") or 0)
    streak_min = float(data.get("streak_min") or 0)
    limit_min = float(data.get("limit_min") or 0)
    if kind == "night":
        head = (f"🌙 {app}: {fmt_min(streak_min, uz)} ketma-ket, soat {profile.now:%H:%M}." if uz
                else f"🌙 {app} — {fmt_min(streak_min, uz)} подряд, а уже {profile.now:%H:%M}.")
    elif kind == "streak":
        head = (f"⏱ {app}: ketma-ket {fmt_min(streak_min, uz)} (chegara {fmt_min(limit_min, uz)})." if uz
                else f"⏱ {app} — {fmt_min(streak_min, uz)} подряд (порог {fmt_min(limit_min, uz)}).")
    else:
        head = (f"📱 {label(cat, uz)}: bugun {fmt_min(today_min, uz)} (limit {fmt_min(limit_min, uz)}) — hozir {app}." if uz
                else f"📱 {label(cat, uz)} — сегодня {fmt_min(today_min, uz)} (лимит {fmt_min(limit_min, uz)}), сейчас {app}.")
    lines = [head]
    todo = await important_open(profile)
    if todo:
        lines.append("")
        lines.append("🎯 <b>" + ("Muhimroq ishlar:" if uz else "Дела поважнее:") + "</b>")
        lines += [f"• {re.sub(r'[<>&]', '', t)[:80]}" for t in todo[:3]]
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        _btn(("😌 Ishlayapman 1 soat" if uz else "😌 Я работаю 1 ч"), "scr:busy:60"),
        _btn(("⚙️ Limitlar" if uz else "⚙️ Лимиты"), "scr:show")]])
    old = st.get("alert_msg")
    try:
        if old:
            await bot.delete_message(uid, int(old))
    except Exception:
        pass
    try:
        sent = await bot.send_message(uid, "\n".join(lines), reply_markup=markup, disable_notification=False)
        st["alert_msg"] = sent.message_id
    except Exception:
        logger.warning("screentime: подробности не отправились", exc_info=True)
    save(uid, st)
    logger.info("screentime %s: %s %s ур.%s (%s, сегодня %.0f мин, подряд %.0f мин)", uid, kind, cat, data.get("level"), app, today_min, streak_min)
    return {"speak": True}


# ------------------------------------------------------------------ вечерняя сводка
def brief_lines(profile) -> list[str]:  # noqa: ANN001
    """Строка в вечернюю сводку: сколько в телефоне сегодня, топ приложений, сколько раз брал, сравнение со вчера."""
    st = load(profile.telegram_id)
    days = st.get("days") or {}
    today = days.get(profile.today.isoformat())
    if not today or not today.get("apps"):
        return []
    uz = profile.lang == "uz"
    excluded = set(st.get("excluded") or [])
    counted = [a for a in today["apps"] if category(str(a.get("pkg"))) != "work" and a.get("pkg") not in excluded]
    total = sum(float(a.get("min") or 0) for a in counted)
    top = sorted(counted, key=lambda a: -float(a.get("min") or 0))[:3]
    line = (f"📱 {'Telefonda' if uz else 'В телефоне'}: <b>{fmt_min(total, uz)}</b>"
            + (" · " + ", ".join(f"{re.sub(r'[<>&]', '', str(a.get('label') or a.get('pkg')))[:20]} {fmt_min(float(a.get('min') or 0), uz)}" for a in top) if top else ""))
    if today.get("pickups"):
        line += f" · {'oldingiz' if uz else 'брали'} {int(today['pickups'])} {'marta' if uz else 'раз'}"
    yday = days.get((profile.today - timedelta(days=1)).isoformat())
    if yday and yday.get("apps"):
        ytotal = sum(float(a.get("min") or 0) for a in yday["apps"] if category(str(a.get("pkg"))) != "work" and a.get("pkg") not in excluded)
        diff = total - ytotal
        if abs(diff) >= 10:
            line += (f" · {'kechagidan' if uz else 'чем вчера'} {'+' if diff > 0 else '−'}{fmt_min(abs(diff), uz)}")
    return [line]


__all__ = ["category", "rules", "record_usage", "propose", "alert", "brief_lines", "set_busy", "adjust", "set_enabled",
           "settings_text", "settings_keyboard", "proposal_text", "important_open", "load"]
