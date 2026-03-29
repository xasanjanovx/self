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


@dataclass
class VacancyTemplateData:
    titles: list[str]
    region_tag: str
    address: str
    salary: str
    schedule: str
    requirements: list[str]
    benefits: list[str]
    duties: list[str]
    details: list[str]
    phone: str | None
    telegram: str | None
    headline: str | None = None
    company: str | None = None


_VACANCY_REGION_MAP = {
    "tashkent": "#TOSHKENT",
    "toshkent": "#TOSHKENT",
    "ташкент": "#TOSHKENT",
    "ташкенте": "#TOSHKENT",
    "andijon": "#ANDIJON",
    "андижан": "#ANDIJON",
    "andijan": "#ANDIJON",
    "samarqand": "#SAMARQAND",
    "самарканд": "#SAMARQAND",
    "buxoro": "#BUXORO",
    "бухара": "#BUXORO",
    "fergana": "#FARGONA",
    "fargona": "#FARGONA",
    "фаргана": "#FARGONA",
    "namangan": "#NAMANGAN",
    "наманган": "#NAMANGAN",
    "jizzax": "#JIZZAX",
    "джизак": "#JIZZAX",
    "sirdayo": "#SIRDARYO",
    "sirdaryo": "#SIRDARYO",
    "сырдар": "#SIRDARYO",
    "qashqadaryo": "#QASHQADARYO",
    "кашкадар": "#QASHQADARYO",
    "surxondaryo": "#SURXONDARYO",
    "сурхандар": "#SURXONDARYO",
    "xorazm": "#XORAZM",
    "хорезм": "#XORAZM",
    "navoiy": "#NAVOIY",
    "навои": "#NAVOIY",
    "nukus": "#NUKUS",
    "қорақалпоқ": "#QORAQALPOQISTON",
    "каракалпак": "#QORAQALPOQISTON",
}

_VACANCY_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_VACANCY_TELEGRAM_RE = re.compile(r"(https?://t\.me/[A-Za-z0-9_]{3,}|@[A-Za-z0-9_]{3,})", re.IGNORECASE)
_VACANCY_AD_TOKENS = (
    "ishdasiz",
    "join",
    "join our",
    "подпис",
    "subscribe",
    "follow our channel",
    "our channel",
    "telegram channel",
    "obuna",
    "kanal",
    "канал",
    "adminni ogohlantiring",
    "ma'muriyati javobgar emas",
)


def _normalize_text_value(value: Any, default: str = "-", max_len: int = 220) -> str:
    if value is None:
        return default
    text = re.sub(r"\s+", " ", str(value).strip())
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text or default


def _normalize_optional_text(value: Any, *, max_len: int = 220) -> str | None:
    text = _normalize_text_value(value, default="", max_len=max_len).strip()
    if not text or text == "-":
        return None
    return text


def _normalize_list_value(value: Any, *, max_items: int = 6, max_len: int = 180) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        chunks = re.split(r"[\n;]+", value)
    elif isinstance(value, list):
        chunks = value
    else:
        chunks = [value]

    result: list[str] = []
    seen: set[str] = set()

    for chunk in chunks:
        text = str(chunk or "").strip()
        text = re.sub(r"^[\-*•\u2022]+\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip(" -")
        if not text or text == "-":
            continue
        if len(text) > max_len:
            text = text[: max_len - 1].rstrip() + "…"
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= max_items:
            break

    return result


def _normalize_telegram_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None

    match = re.search(r"t\.me/([A-Za-z0-9_]{3,})", text, flags=re.IGNORECASE)
    if match:
        return f"@{match.group(1)}"

    match = re.search(r"@([A-Za-z0-9_]{3,})", text)
    if match:
        return f"@{match.group(1)}"

    return None


def _extract_phone_from_text(text: str) -> str | None:
    for match in _VACANCY_PHONE_RE.finditer(text or ""):
        raw = re.sub(r"\s+", " ", match.group(0)).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 9:
            continue
        if len(digits) <= 10 and not raw.startswith("+") and not digits.startswith("998"):
            continue
        return raw
    return None


def _normalize_phone_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    return _extract_phone_from_text(text)


def _normalize_region_tag(value: Any, raw_text: str, default_region_tag: str) -> str:
    text = str(value or "").strip()
    if text and text != "-":
        if not text.startswith("#"):
            text = f"#{text}"
        return text.upper().replace(" ", "_")

    lower = raw_text.lower()
    for token, tag in _VACANCY_REGION_MAP.items():
        if token in lower:
            return tag

    hashtag_match = re.search(r"#([A-Za-zА-Яа-яЁё_]+)", raw_text)
    if hashtag_match:
        return f"#{hashtag_match.group(1).upper()}"

    return default_region_tag


def _is_vacancy_ad_line(line: str) -> bool:
    lower = line.lower()
    return any(token in lower for token in _VACANCY_AD_TOKENS)


def _extract_vacancy_details_fallback(raw_text: str) -> list[str]:
    known_prefixes = (
        "hudud",
        "manzil",
        "адрес",
        "location",
        "maosh",
        "зарплат",
        "oklad",
        "salary",
        "ish vaqti",
        "график",
        "schedule",
        "talablar",
        "треб",
        "requirements",
        "qulayliklar",
        "услов",
        "benefit",
        "vazifalar",
        "обязан",
        "duties",
        "aloqa",
        "контакт",
        "telegram",
        "телеграм",
        "kompaniya",
        "компания",
        "ish beruvchi",
        "работодатель",
        "bo'sh ish o'rinlari",
        "bo‘sh ish o'rinlari",
        "kerak",
        "vacancy",
        "vakansiya",
    )

    details: list[str] = []
    seen: set[str] = set()

    for raw_line in raw_text.splitlines():
        line = re.sub(r"^[\-*•\u2022]+\s*", "", raw_line).strip()
        line = re.sub(r"\s+", " ", line)
        if not line or line == "-":
            continue
        if _is_vacancy_ad_line(line):
            continue
        if line.startswith("#"):
            continue

        lower = line.lower()
        if lower in {"...", "—", "-", "━━━━━━━━━━━━━━━"}:
            continue
        if any(lower.startswith(prefix) for prefix in known_prefixes):
            continue
        if ":" in lower:
            key = lower.split(":", 1)[0].strip()
            if any(key.startswith(prefix) for prefix in known_prefixes):
                continue

        if len(line) < 3:
            continue

        normalized = line.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        details.append(line)
        if len(details) >= 12:
            break

    return _normalize_list_value(details, max_items=12, max_len=180)


def _strip_ad_lines(items: list[str]) -> list[str]:
    return [item for item in items if not _is_vacancy_ad_line(item)]


def _vacancy_section_lines(raw_text: str, keywords: tuple[str, ...]) -> list[str]:
    lines = [line.strip() for line in raw_text.splitlines()]
    result: list[str] = []
    collecting = False

    for line in lines:
        clean = re.sub(r"^[\-*•\u2022]+\s*", "", line).strip()
        lower = clean.lower()

        if any(keyword in lower for keyword in keywords):
            collecting = True
            tail = clean.split(":", 1)[1].strip() if ":" in clean else ""
            if tail and tail != "-":
                result.append(tail)
            continue

        if collecting:
            if not clean:
                break
            if ":" in clean and any(
                marker in lower
                for marker in (
                    "talab",
                    "треб",
                    "qulay",
                    "услов",
                    "vazifa",
                    "обязан",
                    "aloqa",
                    "контакт",
                    "telegram",
                    "телеграм",
                    "hudud",
                    "адрес",
                    "manzil",
                    "ish vaqti",
                    "график",
                    "maosh",
                    "зарплат",
                )
            ):
                break
            result.append(clean)

    return _normalize_list_value(result)


def _vacancy_fallback_titles(raw_text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]
    openings = _vacancy_openings_block(raw_text)
    if openings:
        return _normalize_list_value(openings, max_items=20)

    prioritized: list[str] = []

    for line in lines:
        if not line:
            continue
        clean = re.sub(r"^[\-*•\u2022]+\s*", "", line).strip()
        lower = clean.lower()
        if any(token in lower for token in ("вакан", "vakans", "требует", "kerak", "bo'sh ish", "bo‘sh ish")):
            tail = clean.split(":", 1)[1].strip() if ":" in clean else clean
            tail = re.sub(r"^(вакансия|vakansiya|требуется|kerak)\s*", "", tail, flags=re.IGNORECASE).strip(" -")
            if tail:
                prioritized.append(tail)

    if prioritized:
        return _normalize_list_value(prioritized, max_items=20)

    fallback: list[str] = []
    for line in lines:
        clean = re.sub(r"^[\-*•\u2022]+\s*", "", line).strip()
        lower = clean.lower()
        if not clean or clean.startswith("#"):
            continue
        if any(
            token in lower
            for token in (
                "aloqa",
                "контакт",
                "telegram",
                "телеграм",
                "hudud",
                "manzil",
                "адрес",
                "talab",
                "треб",
                "qulay",
                "услов",
                "vazifa",
                "обязан",
                "maosh",
                "зарплат",
                "grafik",
                "график",
                "ish vaqti",
                "📞",
                "💰",
            )
        ):
            continue
        if len(clean) < 3:
            continue
        fallback.append(clean)
        if len(fallback) >= 20:
            break
    return _normalize_list_value(fallback, max_items=20)


def _vacancy_openings_block(raw_text: str) -> list[str]:
    lines = [line.strip() for line in raw_text.splitlines()]
    result: list[str] = []
    collecting = False

    for line in lines:
        clean = re.sub(r"^[\-*•\u2022✅]+\s*", "", line).strip()
        lower = clean.lower()
        if not clean:
            if collecting and result:
                break
            continue

        if any(token in lower for token in ("bo'sh ish o'rinlari", "bo‘sh ish o'rinlari", "вакансии", "bo'sh ish")):
            collecting = True
            tail = clean.split(":", 1)[1].strip() if ":" in clean else ""
            if tail and tail != "-":
                result.append(tail)
            continue

        if not collecting:
            continue

        if ":" in clean and any(
            marker in lower
            for marker in (
                "hudud",
                "manzil",
                "maosh",
                "ish vaqti",
                "talab",
                "qulay",
                "vazifa",
                "aloqa",
                "telegram",
                "kompaniya",
                "ish beruvchi",
                "адрес",
                "зарплат",
                "график",
                "компания",
                "работодатель",
                "контакт",
            )
        ):
            break

        result.append(clean)
        if len(result) >= 20:
            break

    return _normalize_list_value(result, max_items=20, max_len=90)


def _extract_company_fallback(raw_text: str) -> str | None:
    for raw_line in raw_text.splitlines():
        clean = re.sub(r"^[\-*•\u2022]+\s*", "", raw_line).strip()
        clean = re.sub(r"\s+", " ", clean)
        if not clean or _is_vacancy_ad_line(clean):
            continue
        lower = clean.lower()

        if ":" in clean:
            key, value = clean.split(":", 1)
            key_lower = key.strip().lower()
            value = value.strip()
            if value and any(token in key_lower for token in ("kompaniya", "компания", "ish beruvchi", "работодатель", "firma", "фирма")):
                return _normalize_text_value(value, default="-", max_len=120)

        if re.search(r"\b(ooo|ооо|mchj|llc|inc|aj|jsc)\b", lower):
            return _normalize_text_value(clean, default="-", max_len=120)
    return None


def _extract_headline_fallback(
    raw_text: str,
    titles: list[str],
    region_tag: str,
    company: str | None,
) -> str | None:
    section_tokens = (
        "hudud",
        "manzil",
        "maosh",
        "ish vaqti",
        "talab",
        "qulaylik",
        "vazifa",
        "aloqa",
        "telegram",
        "адрес",
        "зарплат",
        "график",
        "контакт",
    )
    headline_tokens = (
        "vakans",
        "вакан",
        "kerak",
        "требуется",
        "ishga",
        "ishga ol",
        "bo'sh ish",
        "bo‘sh ish",
        "lavozim",
    )

    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]

    for line in lines:
        clean = re.sub(r"^[\-*•\u2022]+\s*", "", line).strip(" -")
        lower = clean.lower()
        if not clean or _is_vacancy_ad_line(clean):
            continue
        if len(clean) < 4:
            continue
        if "bo'sh ish o'rinlari" in lower or "bo‘sh ish o'rinlari" in lower:
            continue
        if any(token in lower for token in headline_tokens):
            return _normalize_text_value(clean, default="-", max_len=140)

    for line in lines:
        clean = re.sub(r"^[\-*•\u2022]+\s*", "", line).strip(" -")
        lower = clean.lower()
        if not clean or _is_vacancy_ad_line(clean):
            continue
        if len(clean) < 4:
            continue
        if any(token in lower for token in section_tokens):
            continue
        if "bo'sh ish o'rinlari" in lower or "bo‘sh ish o'rinlari" in lower:
            continue
        if clean.startswith("#"):
            continue
        return _normalize_text_value(clean, default="-", max_len=140)

    first_title = titles[0] if titles else "Xodim"
    region = str(region_tag or "").strip().upper().lstrip("#")
    if region and company:
        return f"{region}ga {first_title} ({company})"
    if region:
        return f"{region}ga {first_title} kerak"
    if company:
        return f"{first_title} ({company})"
    return f"{first_title} kerak"


def _extract_vacancy_fallback(raw_text: str, default_region_tag: str) -> VacancyTemplateData:
    salary = "-"
    schedule = "-"
    address = "-"

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines:
        lower = line.lower()
        if salary == "-" and any(token in lower for token in ("зарплат", "оклад", "maosh", "ish haqi", "salary", "оплата")):
            salary = line.split(":", 1)[1].strip() if ":" in line else line
        if schedule == "-" and any(token in lower for token in ("график", "смен", "ish vaqti", "рабоч", "schedule")):
            schedule = line.split(":", 1)[1].strip() if ":" in line else line
        if address == "-" and any(token in lower for token in ("адрес", "manzil", "локац", "location")):
            address = line.split(":", 1)[1].strip() if ":" in line else line

    phone = _extract_phone_from_text(raw_text)

    telegram = None
    telegram_match = _VACANCY_TELEGRAM_RE.search(raw_text)
    if telegram_match:
        telegram = _normalize_telegram_value(telegram_match.group(0))

    details = _extract_vacancy_details_fallback(raw_text)
    titles = _strip_ad_lines(_vacancy_fallback_titles(raw_text))
    region_tag = _normalize_region_tag(None, raw_text, default_region_tag)
    company = _extract_company_fallback(raw_text)
    headline = _extract_headline_fallback(raw_text, titles, region_tag, company)

    return VacancyTemplateData(
        titles=titles,
        region_tag=region_tag,
        address=_normalize_text_value(address),
        salary=_normalize_text_value(salary),
        schedule=_normalize_text_value(schedule),
        requirements=_strip_ad_lines(_vacancy_section_lines(raw_text, ("talab", "треб", "requirements"))),
        benefits=_strip_ad_lines(_vacancy_section_lines(raw_text, ("qulay", "услов", "benefit"))),
        duties=_strip_ad_lines(_vacancy_section_lines(raw_text, ("vazifa", "обязан", "duties"))),
        details=_strip_ad_lines(details),
        phone=phone,
        telegram=telegram,
        headline=headline,
        company=company,
    )


def _normalize_vacancy_payload(payload: Any, raw_text: str, default_region_tag: str) -> VacancyTemplateData:
    data = payload if isinstance(payload, dict) else {}
    titles = _strip_ad_lines(_normalize_list_value(data.get("titles"), max_items=20, max_len=90))
    requirements = _strip_ad_lines(_normalize_list_value(data.get("requirements"), max_items=6, max_len=160))
    benefits = _strip_ad_lines(_normalize_list_value(data.get("benefits"), max_items=6, max_len=160))
    duties = _strip_ad_lines(_normalize_list_value(data.get("duties"), max_items=6, max_len=160))
    details = _strip_ad_lines(_normalize_list_value(data.get("details"), max_items=20, max_len=180))

    return VacancyTemplateData(
        titles=titles,
        region_tag=_normalize_region_tag(data.get("region_tag"), raw_text, default_region_tag),
        address=_normalize_text_value(data.get("address")),
        salary=_normalize_text_value(data.get("salary")),
        schedule=_normalize_text_value(data.get("schedule")),
        requirements=requirements,
        benefits=benefits,
        duties=duties,
        details=details,
        phone=_normalize_phone_value(data.get("phone")),
        telegram=_normalize_telegram_value(data.get("telegram")),
        headline=_normalize_optional_text(data.get("headline"), max_len=140),
        company=_normalize_optional_text(data.get("company"), max_len=120),
    )

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

    def trainer_reply(self, question: str, context: dict[str, Any], language: str = "ru") -> str:
        lang = "uzbek" if (language or "").strip().lower() == "uz" else "russian"
        prompt = (
            "Ты персональный фитнес-тренер в Telegram. "
            "Дай безопасный и практичный ответ: структура тренировки, повторения/подходы, отдых, "
            "вариант для новичка и короткое предупреждение по технике. "
            f"Пиши на {lang}. Формат: до 8 строк, четко и без воды."
        )

        return self._generate_content(
            model=self.text_model,
            parts=[
                {
                    "text": (
                        f"{prompt}\n\n"
                        f"Контекст пользователя: {json.dumps(context, ensure_ascii=False)}\n"
                        f"Запрос: {question}"
                    )
                }
            ],
            temperature=0.3,
        ).strip()

    def extract_vacancy_template_data(
        self,
        raw_text: str,
        *,
        default_region_tag: str = "#TOSHKENT",
    ) -> VacancyTemplateData:
        prompt = (
            "Ты извлекаешь данные из текста вакансии для Telegram-шаблона. "
            "Текст может быть на русском или узбекском, с шумом, эмодзи, пересланным оформлением или без структуры. "
            "Нужно перенести максимум фактов из исходника. Нельзя придумывать факты. "
            "Нельзя переносить рекламу чужого канала, призывы подписаться, ссылки на канал-источник, общие дисклеймеры. "
            "Если данных нет, используй '-' для строк и [] для списков. "
            "Ответ только JSON без пояснений. "
            'Формат: {"headline":"...","company":"...","titles":["..."],"region_tag":"#TOSHKENT","address":"...","salary":"...",'
            '"schedule":"...","requirements":["..."],"benefits":["..."],"duties":["..."],'
            '"details":["..."],"phone":"+998...","telegram":"@username"}. '
            "headline: цепляющая первая строка по исходнику (без выдумок и без рекламных слоганов канала). "
            "company: название компании/работодателя, если явно указано, иначе '-'. "
            "Поле titles: 1-20 названий должности/ролей, сколько реально указано в источнике. "
            "region_tag: только uppercase hashtag вида #TOSHKENT или #ANDIJON."
        )

        parsed: Any = {}
        try:
            response_text = self._generate_content(
                model=self.text_model,
                parts=[{"text": f"{prompt}\n\nТекст вакансии:\n{raw_text}"}],
                temperature=0.0,
            )
            parsed = _extract_json(response_text)
        except Exception:
            parsed = {}

        ai_data = _normalize_vacancy_payload(parsed, raw_text, default_region_tag)
        fallback = _extract_vacancy_fallback(raw_text, default_region_tag)

        return VacancyTemplateData(
            titles=ai_data.titles or fallback.titles,
            region_tag=ai_data.region_tag or fallback.region_tag,
            address=ai_data.address if ai_data.address != "-" else fallback.address,
            salary=ai_data.salary if ai_data.salary != "-" else fallback.salary,
            schedule=ai_data.schedule if ai_data.schedule != "-" else fallback.schedule,
            requirements=ai_data.requirements or fallback.requirements,
            benefits=ai_data.benefits or fallback.benefits,
            duties=ai_data.duties or fallback.duties,
            details=ai_data.details or fallback.details,
            phone=ai_data.phone or fallback.phone,
            telegram=ai_data.telegram or fallback.telegram,
            headline=ai_data.headline or fallback.headline,
            company=ai_data.company or fallback.company,
        )
