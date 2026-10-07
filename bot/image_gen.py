"""Картинки для вакансий: Nano Banana 2.1 ТОЛЬКО через Vertex AI + логотип @ishdasiz снизу слева (07.10).

Его выбор: самая качественная версия — «Nano Banana 2.1» (gemini-nano-banana-2.1), запасная — Nano Banana 2 (gemini-3.1-flash-image).
Расход — с кредита Google Cloud ($300). AI Studio для картинок НЕ используем никогда («AI Studio вообще не надо»): Vertex не ответил —
картинки не будет, карточка уйдёт к нему без неё, с кнопкой «Другая картинка». Работает независимо от переключателя «Gemini через…»
в настройках и не трогает его состояние (пауза ключа и «модели нет» относятся к текстовым запросам).
Картинка без текста: надписи на узбекском модели искажают, а название канала и слоган уже в логотипе; данные вакансии — в подписи поста.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from . import billing, gcloud

logger = logging.getLogger(__name__)

MODELS = ("gemini-nano-banana-2.1", "gemini-3.1-flash-image")
ASPECT = "16:9"                       # как у прежних баннеров канала
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


def build_prompt(headline: str, scene: str | None = None, company: str | None = None) -> str:
    """Промпт для модели (по-английски — так она рисует точнее). scene — короткое описание фона по-русски от разбора вакансии."""
    scene = (scene or "").strip().rstrip(".")
    job = (headline or "").strip()
    lines = [
        "Create a premium, eye-catching illustration for a job vacancy post in a Telegram channel in Uzbekistan.",
        f"Job: {job}." if job else "",
        f"Scene: {scene}." if scene else "Scene: a realistic workplace for this profession, people at work.",
        "Style: modern, vivid, warm natural light, rich contrasting colours, clean polished flat-3D illustration with depth; "
        "friendly confident people of Central Asian appearance, realistic hands and faces, professional environment in Uzbekistan.",
        f"Wide {ASPECT} composition. Keep the bottom-left corner calm and uncluttered (a logo will be placed there).",
        "Absolutely no text, no letters, no numbers, no signs with writing, no logos, no watermarks.",
    ]
    return "\n".join(line for line in lines if line)


def _extract(data: dict[str, Any]) -> bytes | None:
    for candidate in data.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                return base64.b64decode(inline["data"])
    return None


def _payload(prompt: str) -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ASPECT}},
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
            await asyncio.sleep(3)
    return status, None, text


async def generate(prompt: str, *, client: Any | None = None) -> tuple[bytes, str]:
    """Картинка по промпту через Vertex → (байты как пришли от модели, имя модели). Бросает ImageError, если не вышло."""
    if not gcloud.vertex_key():
        raise ImageError("нет VERTEX_API_KEY — картинки только через Vertex AI")
    client = client or _http()
    payload = _payload(prompt)
    errors: list[str] = []
    for model in MODELS:
        status, data, text = await _try(client, gcloud.url(model, "generateContent"), gcloud.headers(), payload)
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


async def vacancy_image(headline: str, scene: str | None = None, company: str | None = None) -> bytes:
    """Готовая картинка для поста: модель + логотип. Бросает ImageError."""
    raw, model = await generate(build_prompt(headline, scene, company))
    try:
        final = await asyncio.to_thread(add_logo, raw)
    except Exception as exc:  # логотип не лёг — лучше без него, чем без картинки
        logger.warning("image_gen: логотип не добавился (%s)", exc)
        final = raw
    logger.info("image_gen: %s, %d КБ", model, len(final) // 1024)
    return final


__all__ = ["vacancy_image", "generate", "add_logo", "build_prompt", "ImageError", "MODELS"]
