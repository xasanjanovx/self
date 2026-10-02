"""02.10: баланс и расход Google Cloud — остаток кредита с учётом $30 не на API, точные цифры из экспорта биллинга (BigQuery)."""
from __future__ import annotations

import asyncio
import base64
from datetime import date

import pytest

from bot import agent_tools as tools
from bot import billing, gcp_billing
from bot.profile import Profile


def _run(coro):
    return asyncio.run(coro)


def _profile() -> Profile:
    return Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="Тест", username="t")


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(billing, "_state", None)
    monkeypatch.setattr(billing, "_last_check", 0.0)
    for name in ("GCP_BILLING_TABLE", "GCP_SA_FILE", "GCP_SA_JSON", "GCP_OTHER_USD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gcp_billing, "_cache", {"at": 0.0, "data": None})
    monkeypatch.setattr(billing, "_notify", lambda text: None)


def _spend(day: str, vertex: float) -> None:
    st = billing._load()
    st["days"][day] = {"usd": vertex, "vertex_usd": vertex, "calls": 1, "kinds": {}}


# ------------------------------------------------------------------ остаток кредита по счёту бота и его словам
def test_credit_left_counts_30_other_dollars_and_api_via_vertex(fresh):
    _spend("2026-09-29", 0.88)
    _spend("2026-09-30", 0.30)
    c = billing.gcp_credit()
    assert c["credit_total_usd"] == 300 and c["other_cloud_usd"] == 30 and c["api_spent_usd"] == 1.18
    assert c["credit_left_usd"] == round(300 - 30 - 1.18, 2) and "оценка" in c["basis"]


def test_he_names_the_balance_and_later_api_is_subtracted(fresh):
    _spend("2026-09-29", 0.88)
    out = billing.set_gcp(credit_left_usd=268.0)
    assert out["credit_left_usd"] == 268.0 and "назвал" in out["basis"]
    _spend("2099-01-01", 2.0)                      # расход после названной даты вычитается
    assert billing.gcp_credit()["credit_left_usd"] == 266.0
    assert billing.set_gcp(other_usd=45)["other_cloud_usd"] == 45


def test_tool_answers_keep_30_dollars_out_of_api(fresh):
    _spend("2026-09-30", 0.30)
    ctx = tools.ToolContext(profile=_profile(), text="")
    out = _run(tools.run("ai_status", {}, ctx))
    assert out["google_ai"]["credit"]["other_cloud_usd"] == 30 and "НЕ API" in out["answer_rules"] and "ТОЛЬКО расход API" in out["answer_rules"]
    assert out["google_ai"]["credit"]["google_cloud_billing"].startswith("не подключён")
    g = _run(tools.run("gcloud_billing", {}, ctx))
    assert g["credit"]["credit_left_usd"] == out["google_ai"]["credit"]["credit_left_usd"] and "НЕ API" in g["answer_rules"]
    said = _run(tools.run("set_gcloud_info", {"credit_left_usd": 250}, ctx))
    assert said["ok"] and said["credit"]["credit_left_usd"] == 250.0
    assert "error" in _run(tools.run("set_gcloud_info", {}, ctx))


def test_about_text_tells_jes_what_the_30_dollars_are():
    from bot.about import ABOUT_SELF

    assert "gcloud_billing" in ABOUT_SELF and "$30" in ABOUT_SELF and "НЕ API" in ABOUT_SELF


# ------------------------------------------------------------------ экспорт биллинга в BigQuery
ROWS = [
    {"service": "Vertex AI", "day": "2026-10-01", "currency": "USD", "cost": 2.0, "promo": -2.0, "other_credits": 0.0},
    {"service": "Vertex AI", "day": "2026-10-02", "currency": "USD", "cost": 0.5, "promo": -0.5, "other_credits": 0.0},
    {"service": "Cloud Run", "day": "2026-10-02", "currency": "USD", "cost": 30.0, "promo": -30.0, "other_credits": 0.0},
    {"service": "Generative Language API", "day": "2026-10-02", "currency": "USD", "cost": 1.0, "promo": 0.0, "other_credits": 0.0},
]


def test_summary_splits_api_from_other_and_counts_credit():
    s = gcp_billing.summarize(ROWS, date(2026, 10, 2), credit_total_usd=300.0)
    assert s["today"] == {"api_usd": 1.5, "other_usd": 30.0} and s["yesterday"] == {"api_usd": 2.0, "other_usd": 0.0}
    assert s["total"] == {"api_usd": 3.5, "other_usd": 30.0} and s["month"]["api_usd"] == 3.5
    assert s["credit"] == {"total_usd": 300.0, "used_usd": 32.5, "left_usd": 267.5}
    assert s["charged_usd"] == 1.0                      # реально списано с карты: только то, что кредит не покрыл
    assert s["export_since"] == "2026-10-01" and s["services"][0] == {"service": "Cloud Run", "usd": 30.0}


def test_bigquery_rows_are_parsed():
    result = {"schema": {"fields": [{"name": n} for n in ("service", "day", "currency", "cost", "promo", "other_credits")]},
              "rows": [{"f": [{"v": "Vertex AI"}, {"v": "2026-10-02"}, {"v": "USD"}, {"v": "1.25"}, {"v": "-1.25"}, {"v": "0"}]},
                       {"f": [{"v": "bad"}, {"v": None}, {"v": "USD"}, {"v": "x"}, {"v": "0"}, {"v": "0"}]}]}
    assert gcp_billing._rows(result) == [{"service": "Vertex AI", "day": "2026-10-02", "currency": "USD", "cost": 1.25, "promo": -1.25, "other_credits": 0.0}]


def test_table_is_validated_and_query_is_built_from_it(monkeypatch):
    monkeypatch.setenv("GCP_BILLING_TABLE", "my-proj.billing_export.gcp_billing_export_v1_0123")
    assert gcp_billing.table() == "my-proj.billing_export.gcp_billing_export_v1_0123"
    q = gcp_billing.sql(gcp_billing.table(), date(2026, 6, 1))
    assert "`my-proj.billing_export.gcp_billing_export_v1_0123`" in q and "TIMESTAMP('2026-06-01')" in q and "UNNEST(credits)" in q
    monkeypatch.setenv("GCP_BILLING_TABLE", "a.b.c`; DROP TABLE x; --")
    assert gcp_billing.table() == ""
    monkeypatch.setenv("GCP_BILLING_TABLE", "just_a_name")
    assert gcp_billing.table() == ""


def _tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    length = bytes([n]) if n < 0x80 else (bytes([0x81, n]) if n < 0x100 else bytes([0x82, n >> 8, n & 0xFF]))
    return bytes([tag]) + length + content


def test_service_account_key_pkcs8_signs_a_verifiable_jwt():
    import rsa

    pub, priv = rsa.newkeys(1024)
    pkcs1 = priv.save_pkcs1(format="DER")
    algorithm = _tlv(0x30, bytes.fromhex("06092a864886f70d0101010500"))      # rsaEncryption, NULL
    pkcs8 = _tlv(0x30, _tlv(0x02, b"\x00") + algorithm + _tlv(0x04, pkcs1))
    pem = "-----BEGIN PRIVATE KEY-----\n" + base64.encodebytes(pkcs8).decode() + "-----END PRIVATE KEY-----\n"
    key = {"client_email": "jes@proj.iam.gserviceaccount.com", "private_key": pem}
    jwt = gcp_billing.signed_jwt(key, now=1_800_000_000)
    header, claims, signature = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert base64.urlsafe_b64decode(pad(header)) == b'{"alg": "RS256", "typ": "JWT"}'
    assert b"jes@proj.iam.gserviceaccount.com" in base64.urlsafe_b64decode(pad(claims)) and b"bigquery.readonly" in base64.urlsafe_b64decode(pad(claims))
    assert rsa.verify(f"{header}.{claims}".encode(), base64.urlsafe_b64decode(pad(signature)), pub) == "SHA-256"


def test_fetch_is_none_until_configured_and_reports_errors(fresh, monkeypatch):
    assert not gcp_billing.configured() and _run(gcp_billing.fetch()) is None
    monkeypatch.setenv("GCP_BILLING_TABLE", "p.d.t")
    monkeypatch.setenv("GCP_SA_JSON", "{}")                                 # ключ без private_key — не настроено
    assert not gcp_billing.configured()
