"""Чистые помощники bot.ai / bot.vacancy (без сети)."""
import pytest

from bot.ai import extract_json
from bot.vacancy import build_contact_url, extract_phones, normalize_region_tag, username_from_telegram


def test_extract_json_plain_object():
    assert extract_json('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


def test_extract_json_fenced_block():
    assert extract_json('```json\n{"x": 10}\n```') == {"x": 10}


def test_extract_json_with_surrounding_text():
    assert extract_json('Here is the result: {"ok": true} done') == {"ok": True}


def test_extract_json_array():
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


def test_extract_phones_uz_number():
    assert extract_phones("звоните +998901234567") == ["+998901234567"]


def test_extract_phones_dedupes_and_ignores_salary():
    text = "Maosh 5 000 000 so'm. Tel: 901234567, +998901234567, +998935556677"
    assert extract_phones(text) == ["+998901234567", "+998935556677"]


def test_extract_phones_none_for_garbage():
    assert extract_phones("нет телефона") == []


def test_region_tag_from_explicit_value():
    assert normalize_region_tag("toshkent", "") == "#TOSHKENT"


def test_region_tag_detects_city_in_text():
    assert normalize_region_tag(None, "Работа в Ташкенте", "#OTHER") == "#TOSHKENT"


def test_region_tag_fallback_default():
    assert normalize_region_tag(None, "no city here", "#DEFAULT") == "#DEFAULT"


def test_username_and_contact_url():
    assert username_from_telegram("https://t.me/ish_hr") == "ish_hr"
    assert username_from_telegram("@ish_hr") == "ish_hr"
    assert build_contact_url("@ish_hr").startswith("tg://resolve?domain=ish_hr&text=")
    assert build_contact_url(None) is None
