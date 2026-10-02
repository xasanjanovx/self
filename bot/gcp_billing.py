"""Расход и остаток кредита Google Cloud — автоматически, из экспорта биллинга в BigQuery (его выбор 02.10).

У Google нет простого API «сколько потрачено / остаток кредита»: точные цифры лежат только в экспорте биллинга в BigQuery
(Billing → Billing export → Standard usage cost). Бот читает таблицу сервисным аккаунтом (роли BigQuery Data Viewer + Job User):
JWT подписываем библиотекой `rsa` (cryptography в образе нет), запрос — REST. Настройка (один раз, в консоли Google Cloud):
  1) Billing → Billing export → BigQuery export → Standard usage cost → проект и набор данных (например billing_export);
  2) IAM → сервисный аккаунт → ключ JSON; роли: BigQuery Data Viewer и BigQuery Job User;
  3) на сервере в .env: GCP_BILLING_TABLE=проект.набор.gcp_billing_export_v1_XXXXXX и GCP_SA_FILE=/app/data/gcp_sa.json
     (или GCP_SA_JSON=<содержимое ключа>).
Данные в экспорте появляются с задержкой в несколько часов и начинаются с даты включения экспорта (export_since в ответе).
Не настроено или не ответило — JES говорит по счёту бота и по тому, что он сам назвал (bot/billing.py: gcp_credit).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
CACHE_S = 1800
# услуги Google Cloud, которые считаем «API» (Gemini и озвучка/распознавание); всё остальное — «другое»
API_SERVICE = re.compile(r"vertex|generative language|gemini|ai platform|speech", re.IGNORECASE)
_TABLE = re.compile(r"^[\w.\-]+$")
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_token: dict[str, Any] = {"value": None, "exp": 0.0}


def table() -> str:
    value = (os.getenv("GCP_BILLING_TABLE") or "").strip().strip("`")
    return value if _TABLE.match(value) and value.count(".") == 2 else ""


def _key() -> dict[str, Any] | None:
    raw = (os.getenv("GCP_SA_JSON") or "").strip()
    path = (os.getenv("GCP_SA_FILE") or "").strip()
    try:
        if not raw and path:
            raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw) if raw else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("private_key") and data.get("client_email") else None


def configured() -> bool:
    return bool(table() and _key())


# ------------------------------------------------------------------ подпись JWT без cryptography
def _der(data: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """Один элемент DER (тег, содержимое, позиция после него)."""
    tag, first = data[pos], data[pos + 1]
    if first < 0x80:
        length, start = first, pos + 2
    else:
        n = first & 0x7F
        length, start = int.from_bytes(data[pos + 2:pos + 2 + n], "big"), pos + 2 + n
    return tag, data[start:start + length], start + length


def private_key(pem: str):  # noqa: ANN201
    """Ключ сервисного аккаунта Google (PKCS#8, «BEGIN PRIVATE KEY») → ключ библиотеки rsa."""
    import rsa

    body = re.sub(r"-----[^-]+-----|\s", "", pem)
    der = base64.b64decode(body)
    if "BEGIN RSA PRIVATE KEY" in pem:
        return rsa.PrivateKey.load_pkcs1(der, format="DER")
    _, seq, _ = _der(der)                 # PrivateKeyInfo ::= SEQUENCE { version, algorithm, privateKey OCTET STRING }
    _, _, pos = _der(seq)                 # version
    _, _, pos = _der(seq, pos)            # algorithm
    tag, octets, _ = _der(seq, pos)
    if tag != 0x04:
        raise ValueError("не PKCS#8")
    return rsa.PrivateKey.load_pkcs1(octets, format="DER")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_jwt(key: dict[str, Any], *, now: float | None = None) -> str:
    import rsa

    iat = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": key["client_email"], "scope": SCOPE, "aud": TOKEN_URL, "iat": iat, "exp": iat + 3600}).encode())
    signing_input = f"{header}.{claims}".encode()
    signature = rsa.sign(signing_input, private_key(str(key["private_key"])), "SHA-256")
    return f"{header}.{claims}.{_b64(signature)}"


async def _access_token(http) -> str | None:  # noqa: ANN001
    if _token["value"] and time.time() < float(_token["exp"]) - 120:
        return str(_token["value"])
    key = _key()
    if key is None:
        return None
    res = await http.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signed_jwt(key)})
    res.raise_for_status()
    data = res.json()
    _token.update(value=data.get("access_token"), exp=time.time() + int(data.get("expires_in") or 3600))
    return str(_token["value"]) if _token["value"] else None


# ------------------------------------------------------------------ запрос и свод
def sql(tbl: str, since: date) -> str:
    """Суточные суммы по услугам: cost — расход по прайсу, promo — кредиты-акции (отрицательные, там же «Free Trial»), other_credits — прочие скидки."""
    return (
        "SELECT service.description AS service, FORMAT_DATE('%F', DATE(usage_start_time, 'Asia/Tashkent')) AS day, "
        "IFNULL(MAX(currency), 'USD') AS currency, SUM(cost) AS cost, "
        "SUM((SELECT IFNULL(SUM(c.amount), 0) FROM UNNEST(credits) c WHERE c.type = 'PROMOTION')) AS promo, "
        "SUM((SELECT IFNULL(SUM(c.amount), 0) FROM UNNEST(credits) c WHERE c.type != 'PROMOTION')) AS other_credits "
        f"FROM `{tbl}` WHERE usage_start_time >= TIMESTAMP('{since.isoformat()}') GROUP BY service, day"
    )


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    names = [f["name"] for f in (result.get("schema") or {}).get("fields") or []]
    out = []
    for r in result.get("rows") or []:
        values = [c.get("v") for c in r.get("f") or []]
        row = dict(zip(names, values))
        try:
            out.append({"service": str(row.get("service") or "—"), "day": str(row["day"]), "currency": str(row.get("currency") or "USD"),
                        "cost": float(row.get("cost") or 0), "promo": float(row.get("promo") or 0), "other_credits": float(row.get("other_credits") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def summarize(rows: list[dict[str, Any]], today: date, *, credit_total_usd: float = 300.0) -> dict[str, Any]:
    """Чистый свод строк экспорта: расход на API и на другое (сегодня, вчера, месяц, всего), кредит и сколько осталось."""
    def split(pred) -> dict[str, float]:  # noqa: ANN001
        api = sum(r["cost"] for r in rows if pred(r["day"]) and API_SERVICE.search(r["service"]))
        other = sum(r["cost"] for r in rows if pred(r["day"]) and not API_SERVICE.search(r["service"]))
        return {"api_usd": round(api, 2), "other_usd": round(other, 2)}

    iso = today.isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    month = iso[:7]
    promo_used = -sum(r["promo"] for r in rows)       # кредиты в экспорте отрицательные
    by_service: dict[str, float] = {}
    for r in rows:
        by_service[r["service"]] = by_service.get(r["service"], 0.0) + r["cost"]
    out = {
        "today": split(lambda d: d == iso), "yesterday": split(lambda d: d == yesterday),
        "month": split(lambda d: d.startswith(month)), "total": split(lambda d: True),
        "credit": {"total_usd": credit_total_usd, "used_usd": round(promo_used, 2), "left_usd": round(credit_total_usd - promo_used, 2)},
        "charged_usd": round(sum(r["cost"] + r["promo"] + r["other_credits"] for r in rows), 2),   # что реально списано с карты
        "export_since": min((r["day"] for r in rows), default=None),
        "services": [{"service": s, "usd": round(v, 2)} for s, v in sorted(by_service.items(), key=lambda kv: -kv[1]) if abs(v) >= 0.005][:8],
        "currency": next((r["currency"] for r in rows), "USD"),
    }
    return out


async def fetch(*, force: bool = False) -> dict[str, Any] | None:
    """Свод из BigQuery (кэш 30 минут). None — не настроено; {"error": …} — не ответило."""
    if not configured():
        return None
    if not force and _cache["data"] is not None and time.time() - float(_cache["at"]) < CACHE_S:
        return _cache["data"]
    import httpx

    from . import gcloud

    key = _key() or {}
    project = table().split(".")[0]
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            token = await _access_token(http)
            if not token:
                return {"error": "нет ключа сервисного аккаунта"}
            since = datetime.now(timezone.utc).date() - timedelta(days=120)
            res = await http.post(f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries",
                                  headers={"Authorization": f"Bearer {token}"},
                                  json={"query": sql(table(), since), "useLegacySql": False, "timeoutMs": 25000, "maxResults": 5000})
            res.raise_for_status()
            result = res.json()
        if not result.get("jobComplete", True):
            return {"error": "BigQuery ещё считает — спросите чуть позже"}
        today = datetime.now(timezone(timedelta(hours=5))).date()
        data = {**summarize(_rows(result), today, credit_total_usd=gcloud.TRIAL_USD), "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                "table": table(), "account": key.get("client_email")}
    except Exception as exc:
        logger.warning("gcp billing: не вышло", exc_info=True)
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    _cache.update(at=time.time(), data=data)
    return data


__all__ = ["configured", "fetch", "summarize", "sql", "signed_jwt", "private_key", "table"]
