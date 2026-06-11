"""Tests for vacancy detection (pure function)."""
from bot.vacancy import looks_like_vacancy


def test_detects_clear_vacancy_with_contact():
    text = (
        "Требуется продавец-консультант в магазин. "
        "Зарплата 5 000 000 сум. График 5/2. "
        "Контакт: +998 90 123 45 67"
    )
    assert looks_like_vacancy(text) is True


def test_detects_uzbek_vacancy():
    text = (
        "Sotuvchi kerak. Maosh kelishilgan. "
        "Ish vaqti 9:00-18:00. Aloqa: +998901234567"
    )
    assert looks_like_vacancy(text) is True


def test_rejects_short_greeting():
    assert looks_like_vacancy("привет") is False


def test_rejects_empty():
    assert looks_like_vacancy("") is False


def test_rejects_random_sentence():
    assert looks_like_vacancy("Сегодня хорошая погода и я гулял в парке") is False
