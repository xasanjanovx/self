from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


# ---- Retry config for Gemini API ----
_GEMINI_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_GEMINI_MAX_ATTEMPTS = 4
_GEMINI_BASE_DELAY = 1.0   # секунды для экспоненциального backoff
_GEMINI_MAX_DELAY = 16.0


def _gemini_backoff_delay(attempt: int) -> float:
    """Экспоненциальный backoff с лёгким jitter, чтобы не синхронизировать retry."""
    delay = min(_GEMINI_BASE_DELAY * (2 ** (attempt - 1)), _GEMINI_MAX_DELAY)
    return delay + random.uniform(0, delay * 0.25)


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


@dataclass
class InboxIntent:
    module: str
    mode: str
    confidence: float
    cleaned_text: str | None = None


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

    return _normalize_list_value(result, max_items=30, max_len=220)


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
    titles = _strip_ad_lines(_normalize_list_value(data.get("titles"), max_items=25, max_len=120))
    requirements = _strip_ad_lines(_normalize_list_value(data.get("requirements"), max_items=30, max_len=220))
    benefits = _strip_ad_lines(_normalize_list_value(data.get("benefits"), max_items=30, max_len=220))
    duties = _strip_ad_lines(_normalize_list_value(data.get("duties"), max_items=30, max_len=220))
    details = _strip_ad_lines(_normalize_list_value(data.get("details"), max_items=30, max_len=220))

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
        self._client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=20.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def list_available_models(self) -> set[str]:
        """Return model names (short form) that support generateContent."""
        response = self._client.get(self.base_url, headers={"x-goog-api-key": self.api_key})
        response.raise_for_status()
        models = response.json().get("models", []) or []
        names: set[str] = set()
        for item in models:
            methods = item.get("supportedGenerationMethods", []) or []
            if "generateContent" not in methods:
                continue
            name = str(item.get("name") or "").split("/")[-1]
            if name:
                names.add(name)
        return names

    def ensure_models(self) -> None:
        """Self-heal model selection at startup.

        If a configured model is not available for this API key (e.g. an old
        name left in env vars), fall back to the first working alternative so
        AI features keep functioning instead of failing with 404.
        """
        try:
            available = self.list_available_models()
        except Exception as exc:
            logger.warning("Could not list Gemini models, keeping configured ones: %s", exc)
            return
        if not available:
            return

        preferred = [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
            "gemini-2.0-flash",
        ]

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
        logger.info(
            "Gemini models resolved: text=%s vision=%s transcribe=%s",
            self.text_model,
            self.vision_model,
            self.transcribe_model,
        )

    def _estimate_from_payload(self, data: dict[str, Any], fallback_desc: str = "Блюдо") -> CalorieEstimate:
        return CalorieEstimate(
            meal_desc=str(data.get("meal_desc") or fallback_desc).strip() or fallback_desc,
            calories=int(data["calories"]) if data.get("calories") is not None else None,
            protein=float(data["protein"]) if data.get("protein") is not None else None,
            fat=float(data["fat"]) if data.get("fat") is not None else None,
            carbs=float(data["carbs"]) if data.get("carbs") is not None else None,
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            advice=None,
        )

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

        last_exc: Exception | None = None
        for attempt in range(1, _GEMINI_MAX_ATTEMPTS + 1):
            try:
                response = self._client.post(url, headers=headers, json=payload)
                if response.status_code in _GEMINI_RETRY_STATUSES:
                    raise httpx.HTTPStatusError(
                        f"Gemini transient {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                data = response.json()
                break
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= _GEMINI_MAX_ATTEMPTS:
                    logger.error("Gemini call failed after %d attempts (%s): %s", attempt, model, exc)
                    raise
                delay = _gemini_backoff_delay(attempt)
                logger.warning(
                    "Gemini transient error on attempt %d/%d (%s): %s — retrying in %.2fs",
                    attempt, _GEMINI_MAX_ATTEMPTS, model, exc, delay,
                )
                time.sleep(delay)
        else:  # pragma: no cover — break без присвоения data
            raise last_exc or RuntimeError("Gemini failed without exception")

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

        return self._estimate_from_payload(data, fallback_desc="Блюдо")

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

        return self._estimate_from_payload(data, fallback_desc=food_text.strip() or "Блюдо")

    def parse_nutrition_items(self, raw_text: str) -> list[CalorieEstimate]:
        prompt = (
            "Ты разбираешь сообщение о еде на отдельные приемы пищи и оцениваешь КБЖУ. "
            "Если в тексте несколько блюд или приемов пищи, верни массив объектов по каждому элементу. "
            "Если один прием пищи, верни массив из одного объекта. "
            "Ответ только JSON-массив без пояснений. "
            'Формат каждого элемента: {"meal_desc":"...","calories":0,"protein":0,"fat":0,"carbs":0,"confidence":0.0}.'
        )

        parsed_items: list[CalorieEstimate] = []
        try:
            text = self._generate_content(
                model=self.text_model,
                parts=[{"text": f"{prompt}\n\nТекст: {raw_text}"}],
                temperature=0.1,
            )
            parsed = _extract_json(text)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                for item in parsed[:6]:
                    if not isinstance(item, dict):
                        continue
                    estimate = self._estimate_from_payload(item, fallback_desc=str(item.get("meal_desc") or "Блюдо"))
                    if estimate.calories is None and estimate.protein is None and estimate.fat is None and estimate.carbs is None:
                        continue
                    parsed_items.append(estimate)
        except Exception:
            parsed_items = []

        if parsed_items:
            return parsed_items

        fallback_parts = [
            chunk.strip(" .")
            for chunk in re.split(r"[\n,;]+", raw_text)
            if chunk and chunk.strip()
        ]
        if 1 < len(fallback_parts) <= 5:
            estimates: list[CalorieEstimate] = []
            for part in fallback_parts:
                try:
                    estimates.append(self.estimate_calories_by_text(part))
                except Exception:
                    continue
            if estimates:
                return estimates

        return [self.estimate_calories_by_text(raw_text)]

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

    def classify_inbox_intent(self, raw_text: str, *, has_photo: bool = False, has_voice: bool = False) -> InboxIntent:
        prompt = (
            "Определи, к какому модулю Telegram-бота относится входящее сообщение пользователя. "
            "Допустимые module: finance, calorie, vacancy, trainer, report, goals, habits, menu, unknown. "
            "Допустимые mode: process, open, answer, unknown. "
            "process = сообщение уже содержит данные для обработки, "
            "open = пользователь просит открыть/показать раздел, "
            "answer = пользователь задает содержательный вопрос тренеру/аналитике. "
            "Ответ только JSON: "
            '{"module":"unknown","mode":"unknown","confidence":0.0,"cleaned_text":"..."}'
        )

        try:
            text = self._generate_content(
                model=self.text_model,
                parts=[
                    {
                        "text": (
                            f"{prompt}\n\n"
                            f"has_photo={str(has_photo).lower()}, has_voice={str(has_voice).lower()}\n"
                            f"message={raw_text}"
                        )
                    }
                ],
                temperature=0.0,
            )
            data = _extract_json(text)
            module = str(data.get("module") or "unknown").strip().lower()
            mode = str(data.get("mode") or "unknown").strip().lower()
            confidence = float(data.get("confidence") or 0.0)
            cleaned_text = _normalize_optional_text(data.get("cleaned_text"), max_len=1200)
        except Exception:
            module = "unknown"
            mode = "unknown"
            confidence = 0.0
            cleaned_text = None

        if module not in {"finance", "calorie", "vacancy", "trainer", "report", "goals", "habits", "menu", "unknown"}:
            module = "unknown"
        if mode not in {"process", "open", "answer", "unknown"}:
            mode = "unknown"
        confidence = max(0.0, min(1.0, confidence))
        return InboxIntent(module=module, mode=mode, confidence=confidence, cleaned_text=cleaned_text)

    def parse_finance_ops(self, raw_text: str) -> list[dict[str, Any]]:
        """Smart unified finance parser.

        Returns a list of normalized operations ready to store:
          income/expense: {"type","amount","category","note","bucket"(card|cash|lent|debt)}
          transfer:       {"kind":"transfer","amount","from_bucket","to_bucket","category","note"}

        Understands debts, lending, paying for a friend, repayments, account
        transfers and multi-operation sentences. Returns [] if nothing parsed.
        """
        prompt = (
            "Ты — финансовый ассистент. Разбери сообщение на список операций и верни ТОЛЬКО JSON-массив.\n\n"
            "Счета: \"card\" (карта), \"cash\" (наличные).\n"
            "Виртуальные счета: \"lent\" (мне должны / я дал в долг), \"debt\" (я должен / мои долги/кредит).\n\n"
            "Типы (kind):\n"
            "- \"income\": доход. Поля: amount, category, note, account(card|cash).\n"
            "- \"expense\": расход. Поля: amount, category, note, account(card|cash).\n"
            "- \"transfer\": перемещение между счетами. Поля: amount, from, to, category, note.\n\n"
            "ПРАВИЛА ДОЛГОВ (важно):\n"
            "- дал в долг / оплатил за друга / занял кому-то (с карты) → transfer from=card(или cash) to=lent.\n"
            "- мне вернули долг / друг вернул (на наличные) → transfer from=lent to=cash(или card).\n"
            "- я взял в долг / занял у кого-то (на карту) → transfer from=debt to=card(или cash).\n"
            "- я вернул свой долг / погасил кредит (картой) → transfer from=card(или cash) to=debt.\n"
            "- снял с карты / положил на карту → transfer card<->cash.\n\n"
            "В одном сообщении может быть несколько операций — верни все по порядку.\n"
            "Суммы — числа без пробелов. Если счёт не указан — по умолчанию card.\n\n"
            "Примеры:\n"
            "\"расход 25000 еда, доход 300000 зарплата\" -> "
            '[{"kind":"expense","amount":25000,"category":"еда","note":"еда","account":"card"},'
            '{"kind":"income","amount":300000,"category":"зарплата","note":"зарплата","account":"card"}]\n'
            "\"я сам вернул свои долги картой 100000\" -> "
            '[{"kind":"transfer","amount":100000,"from":"card","to":"debt","category":"Погашение долга","note":"вернул свой долг картой"}]\n'
            "\"оплатил за друга картой 50000, а он вернул мне наличными\" -> "
            '[{"kind":"transfer","amount":50000,"from":"card","to":"lent","category":"Оплата за друга","note":"оплатил за друга"},'
            '{"kind":"transfer","amount":50000,"from":"lent","to":"cash","category":"Возврат долга","note":"друг вернул наличными"}]\n'
            "\"снял с карты 200000\" -> "
            '[{"kind":"transfer","amount":200000,"from":"card","to":"cash","category":"Снятие наличных","note":"снял с карты"}]\n'
            "\"взял в долг 500000 на карту\" -> "
            '[{"kind":"transfer","amount":500000,"from":"debt","to":"card","category":"Взял в долг","note":"взял в долг"}]\n'
            "Если ничего не извлечь — верни []."
        )

        try:
            text = self._generate_content(
                model=self.text_model,
                parts=[{"text": f"{prompt}\n\nСообщение: {raw_text}"}],
                temperature=0.0,
            )
            parsed = _extract_json(text)
        except Exception:
            return []

        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            return []

        buckets = {"card", "cash", "lent", "debt"}
        result: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                amount = float(item.get("amount"))
            except Exception:
                continue
            if amount <= 0:
                continue
            kind = str(item.get("kind") or "").strip().lower()
            note = str(item.get("note") or "").strip() or None
            category = str(item.get("category") or "").strip()

            if kind == "transfer":
                from_bucket = str(item.get("from") or "").strip().lower()
                to_bucket = str(item.get("to") or "").strip().lower()
                if from_bucket not in buckets or to_bucket not in buckets or from_bucket == to_bucket:
                    continue
                result.append(
                    {
                        "kind": "transfer",
                        "amount": amount,
                        "from_bucket": from_bucket,
                        "to_bucket": to_bucket,
                        "category": category or "Перевод",
                        "note": note,
                    }
                )
            else:
                entry_type = "income" if kind == "income" else "expense"
                account = str(item.get("account") or "card").strip().lower()
                if account not in {"card", "cash"}:
                    account = "card"
                result.append(
                    {
                        "type": entry_type,
                        "amount": amount,
                        "category": category or ("доход" if entry_type == "income" else "прочее"),
                        "note": note,
                        "bucket": account,
                    }
                )
        return result

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

    def assistant_reply(self, question: str, context: dict[str, Any], language: str = "ru") -> str:
        """Answer a question about the user's own data (finance, nutrition, habits)."""
        lang = "uzbek" if (language or "").strip().lower() == "uz" else "russian"
        prompt = (
            "Ты — персональный ассистент в Telegram-боте по финансам, питанию и привычкам. "
            "Ответь на вопрос пользователя, опираясь ТОЛЬКО на данные из контекста за последние ~30 дней. "
            "Аккуратно считай суммы. Если данных не хватает — честно скажи об этом. "
            f"Пиши на {lang}. Кратко, до 8 строк, с конкретными числами; при необходимости короткий список."
        )
        return self._generate_content(
            model=self.text_model,
            parts=[
                {
                    "text": (
                        f"{prompt}\n\n"
                        f"Данные пользователя: {json.dumps(context, ensure_ascii=False, default=str)}\n"
                        f"Вопрос: {question}"
                    )
                }
            ],
            temperature=0.2,
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
            "Нужно перенести МАКСИМУМ фактов из исходника. Нельзя придумывать факты. "
            "ВАЖНО: сохраняй ВСЕ пункты списков (требования, условия/льготы, обязанности) — НЕ сокращай, "
            "НЕ объединяй и НЕ выбрасывай пункты. Переноси формулировки максимально близко к оригиналу, "
            "только убери лишние эмодзи/маркеры в начале строк. "
            "Вступительный/описательный абзац и важные уточнения положи в details, чтобы ничего не потерять. "
            "Нельзя переносить только рекламу чужого канала, призывы подписаться, ссылки на канал-источник, общие дисклеймеры. "
            "ОБЯЗАТЕЛЬНО сохрани все контакты: телефон и telegram-username. "
            "Если данных нет, используй '-' для строк и [] для списков. "
            "Ответ только JSON без пояснений. "
            'Формат: {"headline":"...","company":"...","titles":["..."],"region_tag":"#TOSHKENT","address":"...","salary":"...",'
            '"schedule":"...","requirements":["..."],"benefits":["..."],"duties":["..."],'
            '"details":["..."],"phone":"+998...","telegram":"@username"}. '
            "headline: цепляющая первая строка по исходнику (без выдумок и без рекламных слоганов канала). "
            "company: название компании/работодателя, если явно указано, иначе '-'. "
            "Поле titles: названия должности/ролей, сколько реально указано в источнике. "
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
