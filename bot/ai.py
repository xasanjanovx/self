from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings


@dataclass
class CalorieEstimate:
    meal_desc: str
    calories: int | None
    protein: float | None
    fat: float | None
    carbs: float | None
    confidence: float | None
    advice: str | None


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    if cleaned.startswith("{") or cleaned.startswith("["):
        return json.loads(cleaned)

    first_obj = cleaned.find("{")
    last_obj = cleaned.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        return json.loads(cleaned[first_obj : last_obj + 1])

    first_arr = cleaned.find("[")
    last_arr = cleaned.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        return json.loads(cleaned[first_arr : last_arr + 1])

    raise ValueError("JSON not found in model response")


class AIService:
    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.gemini_api_key
        self.text_model = settings.gemini_model
        self.vision_model = settings.gemini_vision_model
        self.transcribe_model = settings.gemini_transcribe_model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    def _generate_content(self, model: str, parts: list[dict[str, Any]], temperature: float = 0.2) -> str:
        url = f"{self.base_url}/{model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature},
        }

        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=120) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError(f"Gemini response has no candidates: {data}")

        content_parts = candidates[0].get("content", {}).get("parts", [])
        texts = [part.get("text", "") for part in content_parts if isinstance(part, dict) and "text" in part]
        text = "\n".join(filter(None, texts)).strip()
        if not text:
            raise ValueError(f"Gemini returned empty text: {data}")

        return text

    def estimate_calories_by_photo(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> CalorieEstimate:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        prompt = (
            "Определи блюдо и приблизительные КБЖУ. "
            "Ответ только JSON без пояснений: "
            '{"meal_desc":"...","calories":0,"protein":0,"fat":0,"carbs":0,"confidence":0.0}'
        )

        text = self._generate_content(
            model=self.vision_model,
            parts=[
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": image_b64}},
            ],
        )
        data = _extract_json(text)

        return CalorieEstimate(
            meal_desc=str(data.get("meal_desc") or "Блюдо"),
            calories=int(data["calories"]) if data.get("calories") is not None else None,
            protein=float(data["protein"]) if data.get("protein") is not None else None,
            fat=float(data["fat"]) if data.get("fat") is not None else None,
            carbs=float(data["carbs"]) if data.get("carbs") is not None else None,
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            advice=None,
        )

    def estimate_calories_by_text(self, food_text: str) -> CalorieEstimate:
        prompt = (
            "Оцени калорийность и КБЖУ по текстовому описанию еды. "
            "Ответ только JSON без пояснений: "
            '{"meal_desc":"...","calories":0,"protein":0,"fat":0,"carbs":0,"confidence":0.0}'
        )

        text = self._generate_content(
            model=self.text_model,
            parts=[{"text": f"{prompt}\n\nОписание: {food_text}"}],
            temperature=0.2,
        )
        data = _extract_json(text)

        return CalorieEstimate(
            meal_desc=str(data.get("meal_desc") or food_text.strip() or "Блюдо"),
            calories=int(data["calories"]) if data.get("calories") is not None else None,
            protein=float(data["protein"]) if data.get("protein") is not None else None,
            fat=float(data["fat"]) if data.get("fat") is not None else None,
            carbs=float(data["carbs"]) if data.get("carbs") is not None else None,
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            advice=None,
        )

    def transcribe_voice(self, file_path: str | Path) -> str:
        file_path = Path(file_path)
        audio_bytes = file_path.read_bytes()

        mime_map = {
            ".ogg": "audio/ogg",
            ".oga": "audio/ogg",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".mp4": "audio/mp4",
            ".webm": "audio/webm",
        }
        mime_type = mime_map.get(file_path.suffix.lower(), "audio/ogg")

        prompt = "Сделай точную транскрибацию аудио. Ответ только текстом без пояснений."
        text = self._generate_content(
            model=self.transcribe_model,
            parts=[
                {"text": prompt},
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(audio_bytes).decode("utf-8"),
                    }
                },
            ],
            temperature=0.0,
        )
        return text.strip()

    def parse_finance_items(self, raw_text: str) -> list[dict[str, Any]]:
        prompt = (
            "Ты извлекаешь финансовые операции из текста. "
            "Верни только JSON-массив. Каждый элемент: "
            '{"type":"income|expense","amount":12345,"category":"еда","note":"обед","bucket":"card|cash|lent|debt"}. '
            "Если не удалось извлечь, верни []"
        )
        normalized: list[dict[str, Any]] = []

        try:
            text = self._generate_content(
                model=self.text_model,
                parts=[{"text": f"{prompt}\n\nТекст: {raw_text}"}],
                temperature=0.0,
            )

            parsed = _extract_json(text)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                for item in parsed:
                    entry_type = str(item.get("type", "expense")).lower().strip()
                    if entry_type not in {"income", "expense"}:
                        entry_type = "expense"

                    amount = item.get("amount")
                    try:
                        amount = float(amount)
                    except Exception:
                        continue

                    if amount <= 0:
                        continue

                    category = str(item.get("category") or ("доход" if entry_type == "income" else "прочее")).strip()
                    note = str(item.get("note") or "").strip() or None
                    bucket = str(item.get("bucket") or "").strip().lower()
                    if bucket not in {"card", "cash", "lent", "debt"}:
                        bucket = self._infer_finance_bucket(f"{category} {note or ''}", entry_type)

                    normalized.append(
                        {
                            "type": entry_type,
                            "amount": amount,
                            "category": category,
                            "note": note,
                            "bucket": bucket,
                        }
                    )
        except Exception:
            normalized = []

        if normalized:
            return normalized
        return self._parse_finance_items_fallback(raw_text)

    def _parse_finance_items_fallback(self, raw_text: str) -> list[dict[str, Any]]:
        chunks = [
            chunk.strip()
            for chunk in raw_text.replace("\n", ",").split(",")
            if chunk.strip()
        ]
        if not chunks:
            chunks = [raw_text.strip()]

        result: list[dict[str, Any]] = []
        pattern = r"(?P<type>доход|income|расход|трата|expense)\s*(?P<amount>\d[\d\s]*)\s*(?P<rest>.*)"

        for chunk in chunks:
            match = re.search(pattern, chunk, flags=re.IGNORECASE)
            if not match:
                continue

            raw_type = match.group("type").lower()
            entry_type = "income" if raw_type in {"доход", "income"} else "expense"

            raw_amount = match.group("amount").replace(" ", "")
            try:
                amount = float(raw_amount)
            except Exception:
                continue
            if amount <= 0:
                continue

            rest = (match.group("rest") or "").strip()
            if not rest:
                category = "доход" if entry_type == "income" else "прочее"
                note = None
            else:
                parts = rest.split(maxsplit=1)
                category = parts[0].strip()
                note = parts[1].strip() if len(parts) > 1 else None
            bucket = self._infer_finance_bucket(chunk, entry_type)

            result.append(
                {
                    "type": entry_type,
                    "amount": amount,
                    "category": category,
                    "note": note,
                    "bucket": bucket,
                }
            )

        return result

    def _infer_finance_bucket(self, text: str, entry_type: str) -> str:
        lower = text.lower()
        if any(token in lower for token in ["нал", "налич"]):
            return "cash"

        if "долг" in lower or "в долг" in lower:
            if any(token in lower for token in ["дал", "одолжил"]):
                return "lent"
            if any(token in lower for token in ["вернули", "получил обратно"]):
                return "lent"
            if any(token in lower for token in ["занял", "взял"]):
                return "debt"
            if any(token in lower for token in ["вернул", "погасил"]):
                return "debt"
            if entry_type == "income":
                return "debt"
            return "lent"

        return "card"

    def build_recommendations(self, context: dict[str, Any]) -> str:
        prompt = (
            "Ты AI-коуч для личного развития. "
            "На основе данных пользователя дай 5 коротких, практичных советов на русском. "
            "Формат: каждая строка начинается с '- '. Без воды."
        )

        return self._generate_content(
            model=self.text_model,
            parts=[{"text": f"{prompt}\n\nДанные: {json.dumps(context, ensure_ascii=False)}"}],
            temperature=0.4,
        ).strip()

    def assistant_reply(self, question: str, context: dict[str, Any]) -> str:
        prompt = (
            "Ты короткий и практичный AI-помощник в Telegram. "
            "Отвечай по-русски, максимум 6 строк, с опорой на данные пользователя. "
            "Если данных мало, скажи что добавить."
        )

        return self._generate_content(
            model=self.text_model,
            parts=[
                {
                    "text": (
                        f"{prompt}\n\n"
                        f"Контекст пользователя: {json.dumps(context, ensure_ascii=False)}\n"
                        f"Вопрос: {question}"
                    )
                }
            ],
            temperature=0.3,
        ).strip()
