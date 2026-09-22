"""Gemini: асинхронный клиент + все AI-задачи бота.

Принципы:
- `httpx.AsyncClient` — ни один вызов не блокирует event loop;
- JSON-режим ответа (`responseMimeType`) — модель не «болтает» вокруг JSON;
- `thinkingBudget=0` для простых задач разбора — ответ за ~1 с вместо 5–10;
- короткий retry (3 попытки, ≤ ~6 с суммарно) — ошибка видна быстро.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import categories as cats
from .config import Settings

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BASE_DELAY = 0.8
_MAX_DELAY = 4.0


def _backoff(attempt: int) -> float:
    delay = min(_BASE_DELAY * (2 ** (attempt - 1)), _MAX_DELAY)
    return delay + random.uniform(0, delay * 0.25)


# ----------------------------------------------------------------- dataclasses
@dataclass
class CalorieEstimate:
    meal_desc: str
    calories: int | None
    protein: float | None
    fat: float | None
    carbs: float | None
    confidence: float | None
    advice: str | None = None


@dataclass
class AgentStep:
    """Ответ модели за один ход агента."""

    parts: list[dict[str, Any]]  # сырые части ответа (для истории)
    text: str  # текст ответа (если модель не зовёт инструменты)
    calls: list[tuple[str, dict[str, Any]]]  # вызовы инструментов (name, args)
    finish: str = ""


@dataclass
class VacancySection:
    title: str
    items: list[str] = field(default_factory=list)


@dataclass
class VacancyData:
    headline: str
    intro: str | None
    company: str | None
    region_tag: str
    address: str | None
    salary: str | None
    schedule: str | None
    requirements: list[str] = field(default_factory=list)
    benefits: list[str] = field(default_factory=list)
    duties: list[str] = field(default_factory=list)
    extra_sections: list[VacancySection] = field(default_factory=list)
    phone: str | None = None
    telegram: str | None = None
    image_prompt: str | None = None


# ---------------------------------------------------------------- json utils
def extract_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    if cleaned.startswith("{") or cleaned.startswith("["):
        return json.loads(cleaned)
    for opener, closer in (("{", "}"), ("[", "]")):
        first = cleaned.find(opener)
        last = cleaned.rfind(closer)
        if first != -1 and last > first:
            return json.loads(cleaned[first : last + 1])
    raise ValueError("JSON not found in model response")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any, *, max_len: int = 300) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip(" -–—")
    if not text or text == "-" or text.lower() in {"null", "none", "yo'q", "нет"}:
        return None
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _clean_list(value: Any, *, max_items: int = 30, max_len: int = 240) -> list[str]:
    if value is None:
        return []
    chunks = re.split(r"[\n;]+", value) if isinstance(value, str) else (value if isinstance(value, list) else [value])
    result: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        text = re.sub(r"^[\-*•·▪️✅❗️⚠️📌👉➡️]+\s*", "", str(chunk or "").strip())
        text = _clean_text(text, max_len=max_len)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def thinking_config(model: str, budget: int | None) -> dict[str, Any] | None:
    """Настройка «размышлений» под поколение модели: 2.5 — thinkingBudget (токены),
    3.x — thinkingLevel (minimal/low/medium/high; по умолчанию у 3.x — high, т.е. медленно)."""
    if budget is None:
        return None
    if "gemini-3" in model:
        level = "minimal" if budget <= 0 else "low" if budget <= 512 else "medium" if budget <= 2048 else "high"
        return {"thinkingLevel": level}
    if "2.5" in model:
        return {"thinkingBudget": int(budget)}
    return None


class AIService:
    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.gemini_api_key
        self.text_model = settings.gemini_model
        self.vision_model = settings.gemini_vision_model
        self.transcribe_model = settings.gemini_transcribe_model
        self.agent_model = settings.agent_model
        self.tts_model: str | None = None  # выбирается в ensure_models, если доступна
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=15.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"x-goog-api-key": self.api_key},
        )

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------- models
    async def list_available_models(self) -> set[str]:
        response = await self._client.get(self.base_url, params={"pageSize": 200})
        response.raise_for_status()
        names: set[str] = set()
        for item in response.json().get("models", []) or []:
            if "generateContent" not in (item.get("supportedGenerationMethods") or []):
                continue
            name = str(item.get("name") or "").split("/")[-1]
            if name:
                names.add(name)
        return names

    async def ensure_models(self) -> None:
        """Если модель из env недоступна для ключа — подменяем на рабочую."""
        try:
            available = await self.list_available_models()
        except Exception as exc:
            logger.warning("Could not list Gemini models, keeping configured ones: %s", exc)
            return
        if not available:
            return
        preferred = ["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.0-flash"]
        newer = sorted(n for n in available if n.startswith("gemini-3") and "flash" in n and "tts" not in n and "image" not in n)
        if newer:
            logger.info("Newer Gemini flash models available for this key: %s", ", ".join(newer))

        def pick(current: str) -> str:
            if current in available:
                return current
            for candidate in preferred:
                if candidate in available:
                    logger.warning("Model '%s' unavailable, using '%s' instead", current, candidate)
                    return candidate
            logger.error("No preferred Gemini model available; keeping '%s'", current)
            return current

        self.text_model = pick(self.text_model)
        self.vision_model = pick(self.vision_model)
        self.transcribe_model = pick(self.transcribe_model)
        self.agent_model = pick(self.agent_model)
        self.tts_model = next((m for m in ("gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts") if m in available), None)
        logger.info("Gemini TTS model: %s", self.tts_model or "unavailable")
        logger.info("Gemini models: text=%s vision=%s transcribe=%s agent=%s", self.text_model, self.vision_model, self.transcribe_model, self.agent_model)

    # ------------------------------------------------------------- core call
    async def _post(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST generateContent с коротким retry на временные ошибки. Возвращает сырой JSON."""
        url = f"{self.base_url}/{model}:generateContent"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(url, json=payload)
                if response.status_code in _RETRY_STATUSES:
                    raise httpx.HTTPStatusError(
                        f"Gemini transient {response.status_code}: {response.text[:200]}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in _RETRY_STATUSES:
                    body = getattr(getattr(exc, "response", None), "text", "")
                    logger.error("Gemini request rejected (%s): %s %s", status, exc, str(body)[:300])
                    raise
                if attempt >= _MAX_ATTEMPTS:
                    logger.error("Gemini call failed after %d attempts (%s): %s", attempt, model, exc)
                    raise
                delay = _backoff(attempt)
                logger.warning("Gemini transient error %d/%d (%s): %s — retry in %.1fs", attempt, _MAX_ATTEMPTS, model, exc, delay)
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    @staticmethod
    def _first_candidate(data: dict[str, Any]) -> dict[str, Any]:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise ValueError(f"Gemini returned no candidates (block={reason})")
        return candidates[0]

    async def generate(
        self,
        parts: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        json_mode: bool = True,
        thinking_budget: int | None = 0,
        max_tokens: int = 2048,
    ) -> str:
        model = model or self.text_model
        gen_config: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen_config["responseMimeType"] = "application/json"
        if (tc := thinking_config(model, thinking_budget)) is not None:
            gen_config["thinkingConfig"] = tc
        payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen_config}
        candidate = self._first_candidate(await self._post(model, payload))
        content_parts = (candidate.get("content") or {}).get("parts") or []
        text = "\n".join(p.get("text", "") for p in content_parts if isinstance(p, dict) and p.get("text")).strip()
        if not text:
            raise ValueError(f"Gemini returned empty text (finish={candidate.get('finishReason')})")
        if candidate.get("finishReason") == "MAX_TOKENS":
            logger.warning("Gemini hit MAX_TOKENS for model %s", model)
        return text

    async def agent_step(
        self,
        contents: list[dict[str, Any]],
        *,
        system: str,
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
        thinking_budget: int | None = 0,
        max_tokens: int = 2048,
    ) -> AgentStep:
        """Один ход агента с function calling: модель либо зовёт инструменты, либо отвечает текстом.

        `contents` — полная история (user/model/functionResponse) в формате Gemini; части ответа
        модели возвращаются как есть (включая thoughtSignature), чтобы их можно было положить в историю."""
        model = model or self.agent_model
        gen_config: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if (tc := thinking_config(model, thinking_budget)) is not None:
            gen_config["thinkingConfig"] = tc
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": [{"functionDeclarations": tools}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": gen_config,
        }
        candidate = self._first_candidate(await self._post(model, payload))
        parts = [p for p in ((candidate.get("content") or {}).get("parts") or []) if isinstance(p, dict)]
        calls: list[tuple[str, dict[str, Any]]] = []
        texts: list[str] = []
        for part in parts:
            call = part.get("functionCall")
            if isinstance(call, dict) and call.get("name"):
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                calls.append((str(call["name"]), args))
            elif part.get("text") and not part.get("thought"):
                texts.append(str(part["text"]))
        finish = str(candidate.get("finishReason") or "")
        if finish == "MAX_TOKENS":
            logger.warning("Gemini agent hit MAX_TOKENS for model %s", model)
        return AgentStep(parts=parts, text="\n".join(texts).strip(), calls=calls, finish=finish)

    async def search(self, query: str, *, lang: str = "ru") -> str:
        """Поиск в интернете через Google Search grounding: модель сама ищет и отвечает по найденному."""
        language = "узбекском (латиница)" if lang == "uz" else "русском"
        payload = {
            "systemInstruction": {"parts": [{"text": f"Найди в интернете и ответь по существу на {language} языке, 2–8 строк, без markdown. "
                                                      "Свежие факты, цифры, даты. Если найти не удалось — так и скажи."}]},
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
        }
        candidate = self._first_candidate(await self._post(self.text_model, payload))
        texts = [str(p.get("text")) for p in (candidate.get("content") or {}).get("parts") or [] if isinstance(p, dict) and p.get("text") and not p.get("thought")]
        return "\n".join(texts).strip()

    async def synthesize(self, text: str, *, voice: str = "Kore") -> bytes | None:
        """Текст → речь (PCM s16le, 24 kHz, mono). None, если TTS-модель недоступна."""
        if not self.tts_model or not text.strip():
            return None
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}},
        }
        candidate = self._first_candidate(await self._post(self.tts_model, payload))
        for part in (candidate.get("content") or {}).get("parts") or []:
            blob = part.get("inlineData") if isinstance(part, dict) else None
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
        return None

    async def generate_json(self, prompt: str, **kwargs: Any) -> Any:
        text = await self.generate([{"text": prompt}], json_mode=True, **kwargs)
        return extract_json(text)

    # ------------------------------------------------------------- nutrition
    @staticmethod
    def _estimate_from_payload(data: dict[str, Any], fallback_desc: str = "Блюдо") -> CalorieEstimate:
        calories = _num(data.get("calories"))
        return CalorieEstimate(
            meal_desc=str(data.get("meal_desc") or fallback_desc).strip() or fallback_desc,
            calories=int(round(calories)) if calories is not None else None,
            protein=_num(data.get("protein")),
            fat=_num(data.get("fat")),
            carbs=_num(data.get("carbs")),
            confidence=_num(data.get("confidence")),
        )

    async def estimate_calories_by_photo(
        self, image_bytes: bytes, mime_type: str = "image/jpeg", *, hint: str | None = None
    ) -> CalorieEstimate:
        prompt = (
            "Определи блюдо на фото и оцени порцию: калории, белки, жиры, углеводы (граммы). "
            "Название блюда — коротко, по-русски. confidence 0..1. "
            'Ответ только JSON: {"meal_desc":"...","calories":0,"protein":0,"fat":0,"carbs":0,"confidence":0.0}'
        )
        if hint:
            prompt += f"\nПодсказка пользователя: {hint}"
        text = await self.generate(
            [{"text": prompt}, {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}}],
            model=self.vision_model,
            temperature=0.1,
        )
        data = extract_json(text)
        if isinstance(data, list) and data:
            data = data[0]
        return self._estimate_from_payload(data if isinstance(data, dict) else {}, fallback_desc="Блюдо")

    async def parse_nutrition_items(self, raw_text: str) -> list[CalorieEstimate]:
        prompt = (
            "Разбери сообщение о еде на отдельные блюда/приёмы пищи и оцени КБЖУ каждого (типичная порция, "
            "если размер не указан). Если размер не назван, но есть слова «наелся», «до отвала», «сытый», «большая», "
            "«много», «to'ydim», «katta» — считай большую порцию (в 1.5–2 раза больше типичной); «немного», «чуть», "
            "«ozgina» — маленькую. meal_desc — короткое название по-русски. "
            "confidence (0..1) — уверенность, что блюдо распознано верно и порция понятна: ≥0.9 если блюдо названо ясно "
            "и объём указан или типичен; ≤0.7 если блюдо неясное, состав/размер неизвестны или это не еда. "
            "Верни только JSON-массив объектов: "
            '[{"meal_desc":"...","calories":0,"protein":0,"fat":0,"carbs":0,"confidence":0.0}]\n\n'
            f"Текст: {raw_text}"
        )
        items: list[CalorieEstimate] = []
        try:
            parsed = await self.generate_json(prompt, temperature=0.1)
        except Exception:
            logger.exception("parse_nutrition_items failed")
            parsed = []
        if isinstance(parsed, dict):
            parsed = [parsed]
        for item in parsed[:8] if isinstance(parsed, list) else []:
            if not isinstance(item, dict):
                continue
            est = self._estimate_from_payload(item, fallback_desc=str(item.get("meal_desc") or "Блюдо"))
            if est.calories is None and est.protein is None and est.fat is None and est.carbs is None:
                continue
            items.append(est)
        return items

    # ------------------------------------------------------------- voice
    async def transcribe_voice(self, file_path: str | Path) -> str:
        audio_b64 = base64.b64encode(Path(file_path).read_bytes()).decode()
        prompt = (
            "Расшифруй аудио дословно. Язык — русский или узбекский (латиница). "
            "Верни только текст без комментариев."
        )
        text = await self.generate(
            [{"text": prompt}, {"inline_data": {"mime_type": "audio/ogg", "data": audio_b64}}],
            model=self.transcribe_model,
            temperature=0.0,
            json_mode=False,
            max_tokens=1024,
        )
        return text.strip()

    # ------------------------------------------------------------- routing
    async def parse_finance_ops(self, raw_text: str, *, today: str | None = None) -> list[dict[str, Any]]:
        """Возвращает список операций:
        income/expense: {"kind","amount","category"(ключ),"note","account"(card|cash)}
        transfer:       {"kind":"transfer","amount","from","to","note","due_date"?}"""
        today_line = f"Сегодня: {today}. " if today else ""
        prompt = (
            "Ты — финансовый ассистент. Разбери сообщение на список операций и верни ТОЛЬКО JSON-массив.\n\n"
            + today_line + "Если у долга (lent/debt) назван срок возврата («вернёт до 5 октября», «через неделю», «до пятницы») — "
            "добавь полю операции due_date (YYYY-MM-DD); иначе поле не добавляй.\n"
            "Счета: \"card\" (карта), \"cash\" (наличные). Виртуальные: \"lent\" (мне должны), \"debt\" (я должен), "
            "\"init\" (долг уже существовал раньше — деньги СЕЙЧАС не двигаются).\n"
            "kind: \"expense\" | \"income\" | \"transfer\".\n"
            "Поля expense/income: amount (число), category (ключ из списка ниже), note (коротко, 1–4 слова, о чём операция), account (card|cash; по умолчанию card).\n"
            "Поля transfer: amount, from, to (card|cash|lent|debt), note.\n"
            "У КАЖДОЙ операции поле confidence (0..1): насколько ты уверен в сумме, типе и категории. "
            "≥0.9 — только если сумма однозначна и категория очевидна; если сумма двусмысленна, категория угадана или фраза неполная — ≤0.7.\n"
            "ДЛЯ ДОЛГОВ (lent/debt) note = ТОЛЬКО имя человека или название банка/организации, кому дал / у кого взял / кто вернул "
            "(например \"Абдулазиз\", \"брат\", \"Хамкорбанк\"). Без слов «долг», «дал», «взял». Если имя не названо — note = null.\n\n"
            f"Категории расходов (category): {cats.prompt_catalog('expense')}.\n"
            f"Категории доходов (category): {cats.prompt_catalog('income')}.\n\n"
            "ПРАВИЛА ДОЛГОВ:\n"
            "- дал в долг / оплатил за друга → transfer from=card|cash to=lent\n"
            "- мне вернули долг → transfer from=lent to=card|cash\n"
            "- взял в долг / занял у кого-то → transfer from=debt to=card|cash\n"
            "- вернул свой долг / погасил кредит → transfer from=card|cash to=debt\n"
            "- снял с карты → transfer card→cash; положил на карту → cash→card\n"
            "- КОНСТАТАЦИЯ существующего долга без действия сейчас («мне должен X 200000», «X должен мне», «я должен банку 3 млн», "
            "«у меня долг перед братом», «уже/давно должен») → transfer from=init to=lent (мне должны) или from=init to=debt (я должен). "
            "Если есть глагол действия (дал, взял, вернул, занял, оплатил) — это обычный перевод с card/cash.\n\n"
            "Суммы: «25к»=25000, «1.5 млн»=1500000, «300 000»=300000. Слова «сум/uzs/сўм» — валюта, не число.\n"
            "В сообщении может быть несколько операций — верни все по порядку. Если ничего нет — [].\n\n"
            "Примеры:\n"
            "«такси 25000, обед 40к» → "
            '[{"kind":"expense","amount":25000,"category":"transport","note":"такси","account":"card","confidence":0.97},'
            '{"kind":"expense","amount":40000,"category":"food","note":"обед","account":"card","confidence":0.95}]\n'
            "«зарплата 5 млн на карту» → "
            '[{"kind":"income","amount":5000000,"category":"salary","note":"зарплата","account":"card","confidence":0.97}]\n'
            "«дал Алишеру в долг 200000 наличными» → "
            '[{"kind":"transfer","amount":200000,"from":"cash","to":"lent","note":"Алишер","confidence":0.95}]\n'
            "«взял в долг у брата 500000» → "
            '[{"kind":"transfer","amount":500000,"from":"debt","to":"card","note":"брат"}]\n'
            "«Абдулазиз вернул 100000 на карту» → "
            '[{"kind":"transfer","amount":100000,"from":"lent","to":"card","note":"Абдулазиз"}]\n'
            "«взял кредит в Хамкорбанке 3 млн» → "
            '[{"kind":"transfer","amount":3000000,"from":"debt","to":"card","note":"Хамкорбанк"}]\n'
            "«дал в долг 200000» → "
            '[{"kind":"transfer","amount":200000,"from":"card","to":"lent","note":null}]\n'
            "«мне должен Абдулазиз 200000» → "
            '[{"kind":"transfer","amount":200000,"from":"init","to":"lent","note":"Абдулазиз"}]\n'
            "«я должен Хамкорбанку 3 млн» → "
            '[{"kind":"transfer","amount":3000000,"from":"init","to":"debt","note":"Хамкорбанк"}]\n'
            "«снял с карты 300000» → "
            '[{"kind":"transfer","amount":300000,"from":"card","to":"cash","note":"снял наличные"}]\n\n'
            f"Сообщение: {raw_text}"
        )
        try:
            parsed = await self.generate_json(prompt, temperature=0.0, max_tokens=1024)
        except Exception:
            logger.exception("parse_finance_ops failed")
            return []
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            return []

        buckets = {"card", "cash", "lent", "debt", "init"}
        result: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            amount = _num(item.get("amount"))
            if amount is None or amount <= 0:
                continue
            kind = str(item.get("kind") or "expense").strip().lower()
            note = _clean_text(item.get("note"), max_len=80)
            confidence = max(0.0, min(1.0, _num(item.get("confidence")) or 0.0))
            if kind == "transfer":
                src = str(item.get("from") or "").strip().lower()
                dst = str(item.get("to") or "").strip().lower()
                if src not in buckets or dst not in buckets or src == dst:
                    continue
                op: dict[str, Any] = {"kind": "transfer", "amount": amount, "from_bucket": src, "to_bucket": dst, "note": note, "confidence": confidence}
                due = _clean_text(item.get("due_date"), max_len=10)
                if due and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) and ("lent" in (src, dst) or "debt" in (src, dst)):
                    op["due_date"] = due
                result.append(op)
                continue
            entry_type = "income" if kind == "income" else "expense"
            account = str(item.get("account") or "card").strip().lower()
            if account not in {"card", "cash"}:
                account = "card"
            result.append(
                {
                    "kind": entry_type,
                    "amount": amount,
                    "category": cats.normalize(item.get("category"), entry_type, note=f"{note or ''} {raw_text}"),
                    "note": note,
                    "bucket": account,
                    "confidence": confidence,
                }
            )
        return result

    async def parse_receipt(self, image_bytes: bytes, mime_type: str = "image/jpeg", *, hint: str | None = None) -> dict[str, Any] | None:
        """Фото чека/квитанции/скриншота оплаты → {"amount","category","note","account","kind"}."""
        prompt = (
            "На фото — чек, квитанция или скриншот оплаты. Извлеки ИТОГОВУЮ сумму к оплате (число, без валюты), "
            "название магазина/получателя (коротко, 1–3 слова) и подбери категорию.\n"
            f"Категории расходов (category): {cats.prompt_catalog('expense')}.\n"
            "account: \"card\" если оплата картой/переводом/через приложение, \"cash\" если наличными; по умолчанию card.\n"
            "Если это не чек и суммы нет — верни {\"amount\": null}.\n"
            'Ответ только JSON: {"amount":0,"category":"other","note":"...","account":"card","is_receipt":true}'
        )
        if hint:
            prompt += f"\nПодсказка пользователя: {hint}"
        text = await self.generate(
            [{"text": prompt}, {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}}],
            model=self.vision_model,
            temperature=0.0,
            max_tokens=512,
        )
        data = extract_json(text)
        if not isinstance(data, dict):
            return None
        amount = _num(data.get("amount"))
        if amount is None or amount <= 0:
            return None
        note = _clean_text(data.get("note"), max_len=60)
        account = str(data.get("account") or "card").strip().lower()
        return {
            "kind": "expense",
            "amount": amount,
            "category": cats.normalize(data.get("category"), "expense", note=f"{note or ''} {hint or ''}"),
            "note": note,
            "bucket": account if account in {"card", "cash"} else "card",
        }

    async def plan_command(self, text: str, context: str) -> dict[str, Any]:
        """«Джарвис»: свободная фраза → структурированная команда."""
        prompt = (
            "Ты — исполнительный ассистент в личном Telegram-боте (финансы, питание, напоминания). "
            "Разбери сообщение пользователя и верни ОДНУ команду в JSON. Не выдумывай данных, которых нет в сообщении.\n\n"
            "Действия (action) и параметры (params):\n"
            "- delete_entry: удалить операцию. params: {kind: expense|income|transfer|lent|debt|any, category: ключ|null, amount: число|null, "
            "note: текст|null, date: today|yesterday|YYYY-MM-DD|null, unnamed: true если «без имени»}\n"
            "- edit_entry: исправить операцию. params: {find: {kind, category, amount, note, date} (что искать), set: {amount, category, note}} "
            "(«не 10000 а 15000» → find.amount=10000, set.amount=15000)\n"
            "- clear_unnamed_debt: убрать сумму «без имени» из долгов. params: {side: lent (мне должны / дал в долг) | debt (я должен)}\n"
            "- set_base: задать текущий остаток счёта. params: {bucket: card|cash|lent|debt, amount}\n"
            "- set_budget: лимит на месяц по категории. params: {category: ключ, amount}\n"
            "- remove_budget: params: {category: ключ|all}\n"
            "- clear_recurring: убрать регулярные платежи. params: {title: текст|all}\n"
            "- pause_recurring / resume_recurring: params: {title: текст|all}\n"
            "- add_reminder: регулярно/однократно присылать текст или ссылку. params: {text, links: [..], time: HH:MM|null, "
            "days: daily|weekdays|weekend|[1..7]|once, date: YYYY-MM-DD|null}\n"
            "- delete_reminder: params: {text: фрагмент|all}\n"
            "- list_reminders: {}\n"
            "- food_advice: что/сколько съесть, совет по питанию. params: {question}\n"
            "- log_food: пользователь СООБЩАЕТ, что съел (записать). params: {text}\n"
            "- question: вопрос о своих данных (сколько потратил, баланс…). params: {question}\n"
            "- none: не команда (обычная операция, вакансия, болтовня)\n\n"
            f"Категории расходов: {cats.prompt_catalog('expense')}. Доходов: {cats.prompt_catalog('income')}.\n"
            "Суммы: «10 тыс»=10000, «700 тыс»=700000, «1.5 млн»=1500000.\n"
            'Ответ только JSON: {"action":"...","params":{...},"confidence":0.0,"reply":"короткая фраза-ответ пользователю на его языке"}\n\n'
            f"КОНТЕКСТ:\n{context}\n\nСООБЩЕНИЕ: {text}"
        )
        data = await self.generate_json(prompt, temperature=0.0, max_tokens=700)
        if not isinstance(data, dict):
            return {"action": "none", "params": {}, "confidence": 0.0, "reply": ""}
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        return {
            "action": str(data.get("action") or "none").strip().lower(),
            "params": params,
            "confidence": max(0.0, min(1.0, _num(data.get("confidence")) or 0.0)),
            "reply": _clean_text(data.get("reply"), max_len=300) or "",
        }

    async def food_advice(self, question: str, context: str, language: str = "ru") -> str:
        lang_name = "узбекском (латиница)" if language == "uz" else "русском"
        prompt = (
            "Ты — личный нутрициолог. По данным ниже дай конкретный, короткий совет: 2–4 варианта, что съесть, "
            "с примерными граммами и ккал, чтобы уложиться в остаток дня по калориям и белку. Учитывай время суток "
            "(ночью — лёгкое, белковое, быстрое в приготовлении). Простые продукты, доступные в Узбекистане. "
            f"Отвечай на {lang_name} языке, без markdown, максимум 8 строк.\n\n"
            f"ДАННЫЕ:\n{context}\n\nВОПРОС: {question}"
        )
        return (await self.generate([{"text": prompt}], temperature=0.5, json_mode=False, max_tokens=600)).strip()

    async def answer_question(self, question: str, context: str, language: str = "ru") -> str:
        lang_name = "узбекском (латиница)" if language == "uz" else "русском"
        prompt = (
            "Ты — личный ассистент пользователя. Ответь на его вопрос по данным ниже коротко и по делу "
            f"(2–5 строк, на {lang_name} языке, без markdown-разметки, суммы с разделителями тысяч).\n"
            "Если данных для точного ответа нет — скажи об этом честно.\n\n"
            f"ДАННЫЕ:\n{context}\n\nВОПРОС: {question}"
        )
        text = await self.generate([{"text": prompt}], temperature=0.3, json_mode=False, max_tokens=700)
        return text.strip()

    async def generate_text(self, prompt: str, *, temperature: float = 0.4, max_tokens: int = 600) -> str:
        return (await self.generate([{"text": prompt}], temperature=temperature, json_mode=False, max_tokens=max_tokens)).strip()

    # ------------------------------------------------------------- vacancy
    async def rewrite_vacancy(self, raw_text: str, *, default_region_tag: str = "#TOSHKENT") -> VacancyData:
        prompt = (
            "Ты — редактор Telegram-канала вакансий по Узбекистану. Из сырого текста вакансии сделай "
            "структурированный пост.\n\n"
            "ЯЗЫК: все текстовые поля — на узбекском языке ЛАТИНИЦЕЙ (o', g', sh, ch). Русский текст и узбекскую "
            "кириллицу переводи на узбекскую латиницу естественно, как пишут носители. Названия компаний, брендов, "
            "адреса, телефоны, @ники, суммы — не переводи и не меняй.\n\n"
            "СТИЛЬ: исправь опечатки и грамматику, сделай формулировки чёткими и понятными, но живыми — как в хороших "
            "вакансиях, без канцелярита и излишней литературности. НЕ выдумывай факты и цифры. НЕ теряй факты: каждая "
            "содержательная деталь исходника должна попасть в пост. Убери рекламу чужих каналов, призывы подписаться, "
            "дисклеймеры и мусорные хештеги.\n\n"
            "СЕКЦИИ — каждый факт клади в САМУЮ ПОДХОДЯЩУЮ секцию:\n"
            "- headline: короткая приглашающая строка с должностью (например «Call-center operatori kerak», "
            "«Sotuvchi-maslahatchi lavozimiga taklif qilamiz»). Не общие слоганы.\n"
            "- intro: 1–2 живых предложения о компании/вакансии, если в исходнике есть о чём; иначе null.\n"
            "- company: название работодателя или null.\n"
            "- region_tag: хештег региона вида #TOSHKENT, #SAMARQAND, #ANDIJON, #FARGONA, #NAMANGAN, #BUXORO, "
            f"#XORAZM, #QASHQADARYO, #SURXONDARYO, #JIZZAX, #SIRDARYO, #NAVOIY, #QORAQALPOGISTON. По умолчанию {default_region_tag}.\n"
            "- address: адрес/ориентир/район или null.\n"
            "- salary: всё про оплату (сумма, диапазон, %, бонусы, KPI, частота выплат) одной строкой или null.\n"
            "- schedule: график, смены, часы, дни, формат (ofis/masofaviy) или null.\n"
            "- requirements: требования к кандидату (возраст, пол, опыт, языки, навыки, образование, документы).\n"
            "- duties: что нужно делать на работе.\n"
            "- benefits: что даёт работодатель (обеды, транспорт, обучение, рост, оформление, форма, жильё).\n"
            "- extra_sections: если факт НЕ подходит ни к одной секции выше — создай отдельную секцию с осмысленным "
            "заголовком на узбекском (например «Sinov muddati», «Ish joyi haqida», «Kimlarga mos keladi», "
            "«Bonuslar», «Hujjatlar»). ЗАПРЕЩЕНО сваливать факты в безымянный раздел «Qo'shimcha ma'lumotlar».\n"
            "- phone: ВСЕ телефоны через « | » (например «+998901112233 | +998935556677») или null.\n"
            "- telegram: @username или ссылка t.me для связи или null.\n"
            "- image_prompt: ОЧЕНЬ короткое описание фона картинки НА РУССКОМ (до 7 слов, без точки): место и люди по профессии, "
            "например «современный колл-центр, улыбающиеся операторы в гарнитурах». Без текста, телефонов, зарплат, компаний.\n\n"
            "Ответ — ТОЛЬКО JSON такого вида:\n"
            '{"headline":"...","intro":null,"company":null,"region_tag":"#TOSHKENT","address":null,"salary":null,'
            '"schedule":null,"requirements":[],"duties":[],"benefits":[],"extra_sections":[{"title":"...","items":["..."]}],'
            '"phone":null,"telegram":null,"image_prompt":"..."}\n\n'
            f"ТЕКСТ ВАКАНСИИ:\n{raw_text}"
        )
        text = await self.generate(
            [{"text": prompt}],
            temperature=0.3,
            json_mode=True,
            thinking_budget=1024,
            max_tokens=4096,
        )
        data = extract_json(text)
        if not isinstance(data, dict):
            raise ValueError("Vacancy response is not a JSON object")

        sections: list[VacancySection] = []
        for raw in data.get("extra_sections") or []:
            if not isinstance(raw, dict):
                continue
            title = _clean_text(raw.get("title"), max_len=80)
            items = _clean_list(raw.get("items"))
            if title and items:
                sections.append(VacancySection(title=title, items=items))

        return VacancyData(
            headline=_clean_text(data.get("headline"), max_len=160) or "",
            intro=_clean_text(data.get("intro"), max_len=500),
            company=_clean_text(data.get("company"), max_len=120),
            region_tag=str(data.get("region_tag") or default_region_tag),
            address=_clean_text(data.get("address"), max_len=240),
            salary=_clean_text(data.get("salary"), max_len=300),
            schedule=_clean_text(data.get("schedule"), max_len=300),
            requirements=_clean_list(data.get("requirements")),
            duties=_clean_list(data.get("duties")),
            benefits=_clean_list(data.get("benefits")),
            extra_sections=sections,
            phone=_clean_text(data.get("phone"), max_len=200),
            telegram=_clean_text(data.get("telegram"), max_len=120),
            image_prompt=_clean_text(data.get("image_prompt"), max_len=900),
        )
