"""Картинки для вакансий: Nano Banana 2.1 ТОЛЬКО через Vertex AI + логотип @ishdasiz снизу слева (07.10).

Его выбор: самая качественная версия — «Nano Banana 2.1» (gemini-nano-banana-2.1), запасная — Nano Banana 2 (gemini-3.1-flash-image).
Расход — с кредита Google Cloud ($300). AI Studio для картинок НЕ используем никогда («AI Studio вообще не надо»): Vertex не ответил —
картинки не будет, карточка уйдёт к нему без неё, с кнопкой «Другая картинка». Работает независимо от переключателя «Gemini через…»
в настройках и не трогает его состояние (пауза ключа и «модели нет» относятся к текстовым запросам).
На баннере — текст вакансии на узбекской латинице (должность, зарплата, место, график, плюс, телефон) по его промпту
vacancy.build_poster_prompt (по его референсам — тёмный/светлый дизайн с акцентом); левый нижний угол оставлен под логотип.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from . import billing, gcloud
from . import vacancy as vac
from .ai import VacancyData

logger = logging.getLogger(__name__)

MODELS = ("gemini-nano-banana-2.1", "gemini-3.1-flash-image")
ASPECT = "3:2"                        # как референсы: 1536×1024 — больше места под дизайн, чем у 16:9
IMAGE_SIZE = "2K"                     # крупнее — мелкий текст и иконки чётче (Telegram потом сам уменьшит)
LOGO = Path(__file__).parent / "assets" / "ishdasiz_logo.png"
LOGO_WIDTH_SHARE = 0.30               # ширина логотипа — доля ширины картинки
LOGO_MARGIN_SHARE = 0.03              # отступ от левого и нижнего края
_RETRY = {429, 500, 502, 503, 504}
_client: Any = None


class ImageError(RuntimeError):
    """Картинку получить не удалось (Vertex не ответил)."""


def _http() -> Any:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    return _client


def build_prompt(data: VacancyData, scene: str | None = None) -> str:
    """Дизайн-бриф постера (vacancy.build_poster_prompt): тёмный стиль с акцентом, двухцветный заголовок, карточки зарплаты и графика,
    ряд преимуществ, карточка контактов, бейдж возраста, фото людей; все тексты точно, на узбекской латинице; левый нижний угол пустой под логотип."""
    return vac.build_poster_prompt(data, scene=scene)


def _extract(data: dict[str, Any]) -> bytes | None:
    for candidate in data.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                return base64.b64decode(inline["data"])
    return None


def _payload(prompt: str, aspect: str = "", size: str | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"aspectRatio": aspect or ASPECT}
    if size is None:
        size = IMAGE_SIZE
    if size:
        config["imageSize"] = size
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": config},
    }


async def _try(client: Any, url: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None, str]:
    """Один запрос с одним повтором на временные сбои. → (код, json, текст ошибки)."""
    status: int | None = None
    text = ""
    for attempt in (1, 2):
        try:
            response = await client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            status, text = None, type(exc).__name__
        else:
            status, text = response.status_code, ""
            if status == 200:
                return status, response.json(), ""
            text = response.text[:300]
            if status not in _RETRY:
                break
        if attempt == 1:
            await asyncio.sleep(10 if status == 429 else 3)
    return status, None, text


async def generate(prompt: str, *, client: Any | None = None, models: tuple[str, ...] | None = None, aspect: str = "",
                   size: str | None = None) -> tuple[bytes, str]:
    """Картинка по промпту через Vertex → (байты как пришли от модели, имя модели). Бросает ImageError, если не вышло."""
    if not gcloud.vertex_key():
        raise ImageError("нет VERTEX_API_KEY — картинки только через Vertex AI")
    client = client or _http()
    errors: list[str] = []
    for model in models or MODELS:
        status, data, text = await _try(client, gcloud.url(model, "generateContent"), gcloud.headers(), _payload(prompt, aspect, size))
        if status == 400 and "size" in text.lower() and (size is None and IMAGE_SIZE or size):
            # эта модель размера не знает — тот же запрос без imageSize
            status, data, text = await _try(client, gcloud.url(model, "generateContent"), gcloud.headers(), _payload(prompt, aspect, ""))
        if data is not None:
            image = _extract(data)
            if image:
                billing.record(model, data.get("usageMetadata"), kind="image", provider="vertex")
                return image, model
            errors.append(f"{model}: {(data.get('promptFeedback') or {}).get('blockReason') or 'ответ без картинки'}")
            continue
        errors.append(f"{model}: {status} {text[:80]}")
        if status in {401, 403}:   # ключ не пускают — вторая модель не поможет
            raise ImageError("Vertex не пускает ключ: " + errors[-1])
    raise ImageError("; ".join(errors)[:400])


def add_logo(image: bytes, *, logo_path: Path | None = None) -> bytes:
    """Логотип канала — в левый нижний угол; на выходе JPEG (в Telegram он легче PNG от модели)."""
    from PIL import Image

    base = Image.open(BytesIO(image)).convert("RGB")
    logo = Image.open(logo_path or LOGO).convert("RGBA")
    width = max(40, int(base.width * LOGO_WIDTH_SHARE))
    height = max(1, round(logo.height * width / logo.width))
    logo = logo.resize((width, height), Image.LANCZOS)
    margin = int(base.width * LOGO_MARGIN_SHARE)
    base.paste(logo, (margin, base.height - height - margin), logo)
    out = BytesIO()
    base.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


ATTEMPTS = 3                          # столько раз перерисовываем, если проверка баннера не прошла
_INSPECT_PROMPT = (
    "Это рекламный баннер вакансии. Прочитай ВЕСЬ текст на нём дословно, ничего не исправляя и не додумывая. "
    'Ответь ТОЛЬКО JSON: {"lines":["строка 1","строка 2"],"text_in_bottom_left":false}. '
    "text_in_bottom_left — true, если в левом нижнем углу (левая треть ширины, нижние 20% высоты) есть любой текст, цифры, значок или плашка."
)


@dataclass
class Banner:
    image: bytes
    warning: str | None = None        # проверка не прошла и после перерисовок — владельцу скажем, что сверить


def _salary_numbers(salary: str | None) -> list[str]:
    """Числа из зарплаты вакансии цифрами без пробелов: «4 000 000 so'mdan» → ['4000000']. Короткие (до 3 цифр) не сверяем."""
    return [d for chunk in re.findall(r"\d[\d\s.,]*\d", salary or "") if len(d := re.sub(r"\D", "", chunk)) >= 4]


async def inspect(raw: bytes, data: VacancyData) -> tuple[bool | None, str]:
    """Нейросеть (Vertex) читает текст с готового баннера: телефон, сумма зарплаты и слова заголовка должны совпасть до буквы, а угол под логотип —
    быть пустым. Модель путает цифры («4 000 010») и садит текст в угол, поэтому неточный баннер без перерисовки не отпускаем.
    → (True, "") всё сошлось | (False, причина) | (None, "") проверить не вышло (лимит Vertex): баннер уйдёт с предупреждением."""
    from PIL import Image

    from .ai import extract_json, vertex_only
    from .context import ai

    answer = None
    buf = BytesIO()
    Image.open(BytesIO(raw)).convert("RGB").save(buf, "JPEG", quality=85)
    parts = [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}, {"text": _INSPECT_PROMPT}]
    for attempt in (1, 2, 3):
        try:
            with vertex_only():
                answer = extract_json(await ai.generate(parts, temperature=0.0, json_mode=True, thinking_budget=0, max_tokens=800))
            if isinstance(answer, dict):
                break
            answer = None
        except Exception as exc:
            logger.warning("image_gen: проверка баннера, попытка %d: %s", attempt, exc)
        if attempt < 3:
            await asyncio.sleep(8)    # чаще всего 429: общий лимит запросов Vertex в минуту
    if answer is None:
        return None, ""
    problems = []
    if answer.get("text_in_bottom_left"):
        problems.append("в левом нижнем углу есть текст — там ляжет логотип")
    seen = re.sub(r"\D", "", " ".join(str(x) for x in answer.get("lines") or []))
    first_phone = re.sub(r"\D", "", (data.phone or "").split("|")[0])[-9:]
    if len(first_phone) == 9 and first_phone not in seen:
        problems.append("телефон на картинке не совпал с вакансией")
    if any(number not in seen for number in _salary_numbers(data.salary)):
        problems.append("сумма зарплаты на картинке не совпала с вакансией")
    letters = re.sub(r"[^0-9A-ZА-ЯЁ]", "", " ".join(str(x) for x in answer.get("lines") or []).upper())
    words = [w for w in (re.sub(r"[^0-9A-ZА-ЯЁ]", "", word) for word in (data.headline or "").upper().split()) if len(w) >= 4]
    if any(word not in letters for word in words):
        problems.append("заголовок на картинке не совпал с вакансией")
    return not problems, "; ".join(problems)


async def vacancy_image(data: VacancyData, scene: str | None = None) -> Banner:
    """Готовый баннер для поста: модель (с текстом вакансии) → проверка → логотип. До ATTEMPTS попыток; не вышло чисто —
    отдаём последнюю с предупреждением. Бросает ImageError, если Vertex картинку не дал вовсе."""
    prompt = build_prompt(data, scene)
    raw, ok, why = b"", False, ""
    for attempt in range(1, ATTEMPTS + 1):
        raw, model = await generate(prompt)
        ok, why = await inspect(raw, data)
        logger.info("image_gen: %s, попытка %d: %s", model, attempt, {True: "принят", None: "не проверен"}.get(ok, why))
        if ok is not False:
            break
    try:
        final = await asyncio.to_thread(add_logo, raw)
    except Exception as exc:  # логотип не лёг — лучше без него, чем без картинки
        logger.warning("image_gen: логотип не добавился (%s)", exc)
        final = raw
    if ok is None:
        warning = "⚠️ Текст на картинке не удалось проверить автоматически: сверь телефон и сумму. Если неверно — «Другая картинка»."
    elif ok is False:
        warning = f"⚠️ Проверь картинку: {why}. Если неверно — «Другая картинка»."
    else:
        warning = None
    return Banner(final, warning)


__all__ = ["vacancy_image", "inspect", "generate", "add_logo", "build_prompt", "Banner", "ImageError", "MODELS"]
