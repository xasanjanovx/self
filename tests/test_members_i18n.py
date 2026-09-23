"""Доступ по приглашению и английский интерфейс (без сети)."""
from __future__ import annotations

import asyncio
import json

from bot import access, i18n, persona


# ------------------------------------------------------------------ приглашения
def test_invite_code_parsing():
    assert access.invite_code("/start inv_AbC123") == "AbC123"
    assert access.invite_code("/start@flowuzrobot inv_xyz") == "xyz"
    assert access.invite_code("/start") is None
    assert access.invite_code("/start hello") is None
    assert access.invite_code("inv_abc") is None
    assert access.invite_link("flowuzrobot", "abc") == "https://t.me/flowuzrobot?start=inv_abc"


def test_members_are_allowed_but_not_owners():
    from bot.context import settings

    owner = next(iter(settings.allowed_telegram_ids), None)
    access._members.add(555)
    try:
        assert access.is_allowed(555) and access.is_member(555) and not access.is_owner(555)
        assert 555 in access.user_ids()
        if owner is not None:
            assert access.is_owner(owner) and owner in access.user_ids()
    finally:
        access._members.discard(555)
    assert not access.is_member(555)


def test_new_member_language_from_telegram():
    from bot.middlewares import lang_from_telegram

    assert lang_from_telegram("uz") == "uz" and lang_from_telegram("ru") == "ru" and lang_from_telegram("kk") == "ru"
    assert lang_from_telegram("en") == "en" and lang_from_telegram("de") == "en" and lang_from_telegram(None) == "en"


# ------------------------------------------------------------------ перевод интерфейса
def test_numbers_are_masked_so_one_phrase_is_translated_once():
    tpl1, nums1 = i18n.mask("Потрачено 25 000 сум за 3 дня")
    tpl2, nums2 = i18n.mask("Потрачено 1 250 000 сум за 12 дня")
    assert tpl1 == tpl2 == "Потрачено ⟦0⟧ сум за ⟦1⟧ дня"
    assert nums1 == ["25 000", "3"] and nums2 == ["1 250 000", "12"]
    assert i18n.unmask("Spent ⟦0⟧ UZS in ⟦1⟧ days", nums1) == "Spent 25 000 UZS in 3 days"


def test_translate_keeps_jarvis_replies_code_and_latin(monkeypatch):
    """Ответ Джарвиса (KEEP), <pre>, узбекская латиница и цифры не переводятся; одна фраза — один запрос."""
    from bot.context import ai

    calls: list[list[str]] = []

    async def fake_generate(parts, **kw):  # noqa: ANN001
        chunk = json.loads(parts[0]["text"].split("\n\n", 1)[1])
        calls.append(chunk)
        return json.dumps([{"Сегодня ⟦0⟧ задачи": "Today ⟦0⟧ tasks", "<b>Финансы</b>": "<b>Finance</b>"}.get(c, "EN") for c in chunk])

    monkeypatch.setattr(ai, "generate", fake_generate)
    monkeypatch.setattr(i18n, "_mem", {})
    text = ("<b>Финансы</b>\nСегодня 3 задачи\nBugun 3 ta vazifa\n" + i18n.keep("Готово, записала такси")
            + "\n<pre>промпт по-русски</pre>")
    out, button = asyncio.run(i18n.translate_many([text, "Сегодня 7 задачи"]))
    assert out.split("\n")[:3] == ["<b>Finance</b>", "Today 3 tasks", "Bugun 3 ta vazifa"]
    assert "Готово, записала такси" in out and i18n.KEEP not in out and "<pre>промпт по-русски</pre>" in out
    assert button == "Today 7 tasks"
    assert len(calls) == 1 and sorted(calls[0]) == ["<b>Финансы</b>", "Сегодня ⟦0⟧ задачи"]
    # второй раз — из памяти, без запросов
    asyncio.run(i18n.translate_many(["Сегодня 9 задачи"]))
    assert len(calls) == 1


def test_bad_translation_falls_back_to_source(monkeypatch):
    from bot.context import ai

    async def broken(parts, **kw):  # noqa: ANN001
        return json.dumps(["Spent UZS"])  # потеряли ⟦0⟧ — такой перевод не берём

    monkeypatch.setattr(ai, "generate", broken)
    monkeypatch.setattr(i18n, "_mem", {})
    assert asyncio.run(i18n.translate("Потрачено 5 000 сум")) == "Потрачено 5 000 сум"


def test_strip_keep_for_other_languages():
    assert i18n.strip_keep("a" + i18n.keep("b")) == "ab"
    assert i18n.norm("EN") == "en" and i18n.norm("de") == "ru"


# ------------------------------------------------------------------ язык Джарвиса
def test_jarvis_english_is_locked():
    p = persona.Persona.from_row({"lang": "en"})
    assert p.lang == "en"
    rule = persona.lang_rule(p)
    assert "ONLY English" in rule and "Uzbek, Russian" in rule
    assert persona.LANG_CODES["en"] == "en-US"
    from bot.handlers.agent import system_prompt
    from bot.profile import Profile

    prof = Profile(telegram_id=1, lang="ru", tz_name="Asia/Tashkent", currency="UZS", first_name="A", username="a")
    assert "ЯЗЫК ОТВЕТА: всегда английский" in system_prompt(prof, "", reply_lang="en")
    assert "ЯЗЫК ОТВЕТА: всегда русский" in system_prompt(prof, "")


def test_language_keyboards_have_three_languages():
    from bot.keyboards import jarvis_settings_keyboard, language_keyboard

    data = [b.callback_data for row in language_keyboard("en").inline_keyboard for b in row]
    assert {"lang:set:uz", "lang:set:ru", "lang:set:en"} <= set(data)
    kb = jarvis_settings_keyboard("ru", voice="Sulafat", call_lang="en", address="sen", tone="friendly", verbosity="short")
    assert "jarvis:lang:en" in [b.callback_data for row in kb.inline_keyboard for b in row]
