"""Tests for pure helpers in bot.ai (no network)."""
import pytest

from bot.ai import _extract_json, _normalize_phone_value, _normalize_region_tag


def test_extract_json_plain_object():
    assert _extract_json('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


def test_extract_json_fenced_block():
    assert _extract_json('```json\n{"x": 10}\n```') == {"x": 10}


def test_extract_json_with_surrounding_text():
    assert _extract_json('Here is the result: {"ok": true} done') == {"ok": True}


def test_extract_json_array():
    assert _extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        _extract_json("no json here at all")


def test_normalize_phone_extracts_uz_number():
    assert _normalize_phone_value("звоните +998 90 123 45 67") == "+998 90 123 45 67"


def test_normalize_phone_none_for_garbage():
    assert _normalize_phone_value("нет телефона") is None


def test_normalize_region_tag_from_explicit_value():
    assert _normalize_region_tag("toshkent", "", "#TOSHKENT") == "#TOSHKENT"


def test_normalize_region_tag_detects_city_in_text():
    assert _normalize_region_tag(None, "Работа в Ташкенте", "#OTHER") == "#TOSHKENT"


def test_normalize_region_tag_fallback_default():
    assert _normalize_region_tag(None, "no city here", "#DEFAULT") == "#DEFAULT"
