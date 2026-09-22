"""Tests for the in-memory live-screen registry (pure logic, no network)."""
from bot import screen


def test_track_screen_sets_screen_and_removes_from_ephemerals():
    chat = 4242
    screen._ephemerals[chat] = [111, 222]
    try:
        screen.track_screen(chat, 222)
        assert screen._screen[chat] == 222
        # the promoted message must no longer be scheduled for deletion
        assert 222 not in screen._ephemerals[chat]
        assert 111 in screen._ephemerals[chat]
    finally:
        screen._ephemerals.pop(chat, None)
        screen._screen.pop(chat, None)


def test_track_screen_ignores_none():
    chat = 4243
    screen.track_screen(chat, None)
    assert chat not in screen._screen


def test_short_agent_replies_are_notices_not_screens():
    from bot.handlers.agent import _is_notice

    assert _is_notice("Звоню 📞", mutated=False)
    assert _is_notice("<b>Готово</b>", mutated=False)
    # запись данных — нужна кнопка «Отменить», значит это не мимолётная реплика
    assert not _is_notice("Записал: такси 25 000", mutated=True)
    # длинный разбор — на экран с кнопками
    assert not _is_notice("Сегодня ты потратил 250 000 сум. " * 6, mutated=False)
    assert not _is_notice("Первая строка\nвторая\nтретья", mutated=False)
    assert not _is_notice("", mutated=False)
