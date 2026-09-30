"""Выгрузка дел голосом (30.09, его просьба): «при одном или нескольких голосовых он сделает задачи и цели автоматически
и готовый дневной план». Его выбор — добавлять сразу и с кнопкой «Отменить».

Длинное голосовое с несколькими делами («надо позвонить Алишеру, купить лампочку, хочу накопить на ноутбук к январю, каждый
день по часу английского…») или несколько голосовых подряд собираются в одну пачку (ждём ещё 6 секунд после последнего) и
уходят агенту одним ходом с инструкцией: разложить на задачи (со сроками), цели, ежедневные дела и напоминания, добавить
всё сразу и составить план дня. Отмена — обычная кнопка агента: откатывает весь ход целиком.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

BATCH_WAIT_S = 6.0
MIN_WORDS = 16
_MARKERS = re.compile(
    r"\b(надо|нужно|необходимо|хочу|планирую|собираюсь|цель|не забыть|запиши|запланируй|напомни|каждый день|по будням|"
    r"до конца (?:недели|месяца|года)|к (?:пятнице|понедельнику|выходным)|на этой неделе|на следующей неделе|завтра|послезавтра|"
    r"купить|позвонить|написать|сделать|выучить|накопить|начать|закончить|отправить|оплатить|"
    r"kerak|lozim|rejam|maqsad|har kuni|unutmay|qilish kerak|sotib olish|qo'ng'iroq qilish)\b", re.IGNORECASE)
_EXPLICIT = re.compile(r"(разгрузк|надиктую|составь (?:мне )?(?:задач|цел|план)|сделай (?:из этого )?(?:задач|цел|план)|"
                       r"запиши (?:все )?(?:эти )?(?:дела|задачи)|vazifalar (?:qil|tuz)|reja tuz)", re.IGNORECASE)

PREFIX = (
    "[ВЫГРУЗКА ДЕЛ ГОЛОСОМ — он надиктовал одно или несколько голосовых подряд; ниже их расшифровка по порядку]\n"
    "Сделай ВСЁ за один ход, без вопросов (у него есть кнопка «Отменить»):\n"
    "1) Выдели каждое дело и добавь сразу: разовое дело — add_task (срок и время, если названы: «завтра», «в пятницу», «в 18:00»); "
    "цель («накопить…», «похудеть до…», «читать по…») — add_goal; «каждый день / по будням» — add_daily; «напомни…» — add_reminder; "
    "названные траты, доходы, еду — инструментами записи. Одно дело — одна запись; ничего не пропускай, не дублируй то, что уже есть "
    "в данных, не выдумывай сроки, которых он не называл.\n"
    "2) Затем вызови day_plan — план дня (если он назвал другой день или город — на этот день/город); план уйдёт в чат отдельным сообщением.\n"
    "3) Ответ — коротко: что добавил (задачи · цели · ежедневные · напоминания) и 2–3 главных пункта дня. План не пересказывай.\n"
    "ЕГО СЛОВА:\n")


@dataclass
class Batch:
    texts: list[str] = field(default_factory=list)
    last: Any = None
    timer: asyncio.Task | None = None


_batches: dict[int, Batch] = {}


def eligible(text: str) -> bool:
    """Похоже на выгрузку дел: длинное, несколько намерений, не вопрос — или он прямо попросил."""
    text = str(text or "").strip()
    if _EXPLICIT.search(text):
        return True
    if "?" in text:
        return False
    return len(text.split()) >= MIN_WORDS and len(_MARKERS.findall(text)) >= 2


def open_for(uid: int) -> bool:
    """Пачка уже собирается — следующие голосовые идут в неё, что бы в них ни было."""
    return uid in _batches


def combine(texts: list[str]) -> str:
    if len(texts) == 1:
        return PREFIX + texts[0]
    return PREFIX + "\n".join(f"({i}) {t}" for i, t in enumerate(texts, 1))


async def push(message, state, profile, transcript: str) -> None:  # noqa: ANN001
    """Добавить расшифровку в пачку и (пере)запустить ожидание."""
    uid = profile.telegram_id
    b = _batches.setdefault(uid, Batch())
    b.texts.append(transcript)
    b.last = message
    if b.timer is not None and not b.timer.done():
        b.timer.cancel()
    b.timer = asyncio.create_task(_flush(uid, state, profile), name="capture-flush")
    try:
        from .handlers.common import show_progress

        await show_progress(message, profile.tr(f"🧠 Записал ({len(b.texts)}). Есть ещё — присылайте, жду {int(BATCH_WAIT_S)} сек…",
                                                f"🧠 Yozdim ({len(b.texts)}). Yana bo'lsa — yuboring, {int(BATCH_WAIT_S)} soniya kutaman…"))
    except Exception:
        logger.debug("capture: прогресс не показал", exc_info=True)


async def _flush(uid: int, state, profile) -> None:  # noqa: ANN001
    try:
        await asyncio.sleep(BATCH_WAIT_S)
    except asyncio.CancelledError:
        return
    b = _batches.pop(uid, None)
    if b is None or not b.texts:
        return
    logger.info("capture: выгрузка дел — %d голосовых/фраз, %d слов", len(b.texts), sum(len(t.split()) for t in b.texts))
    from .handlers import agent

    try:
        ok = await agent.handle_command(b.last, state, profile, combine(b.texts), voice=True)
    except Exception:
        logger.exception("capture: агент упал")
        ok = False
    if not ok:
        try:
            await b.last.answer(profile.tr("JES сейчас недоступен — повторите через минуту.", "JES hozir ishlamayapti — bir daqiqadan so'ng qaytaring."))
        except Exception:
            logger.debug("capture: ответ не ушёл", exc_info=True)


__all__ = ["eligible", "open_for", "push", "combine", "PREFIX", "BATCH_WAIT_S"]
