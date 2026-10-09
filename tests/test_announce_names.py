"""28.09: «Звонит …» — без модели, имя целиком (было: «JES | AI» → «Джарвис», «Mashhur bek» → «Махурбек»)."""
from bot import names, phone_live


def test_speakable_uzbek_latin_names():
    assert names.speakable("Mashhur bek aka") == "Машхур Бек Ака"
    assert names.speakable("SIROJIDDIN 📱2") == "Сироджиддин"   # узбекское j — «дж»
    assert names.speakable("Xushnudbek") == "Хушнудбек"
    assert names.speakable("Yo'ldosh") == "Юлдош"
    assert names.speakable("G‘ayrat") == "Гайрат"
    assert names.speakable("Elyor new") == "Элёр"
    assert names.speakable("Shohamir Beeline") == "Шохамир"
    assert names.speakable("Iris Cafe Zakaz") == "Ирис Кафе Заказ"
    assert names.speakable("Алишер ака") == "Алишер Ака"
    assert names.speakable("Alisher aka", "uz") == "Alisher Aka"   # узбекский голос читает латиницу сам
    assert names.speakable("Jasur") == "Джасур"
    assert names.speakable("Jo'rabek") == "Джурабек"
    assert names.speakable("Djamshid") == "Джамшид"
    assert names.speakable("Ghayrat") == "Гайрат"
    assert names.speakable("Ўткир ака") == "Уткир Ака"            # узбекская кириллица — русскому голосу понятно
    assert names.speakable("Қодир") == "Кодир"


def test_announcement_text(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    say = lambda name, app="", lang="ru": phone_live.announcement_text(1, name, app, lang)  # noqa: E731
    assert say("JES | AI", "Telegram") == "Звонит Джес в Telegram"
    assert say("Jarvis") == "Звонит Джес"
    assert say("Мама") == "Звонит мама"
    assert say("Oyijon") == "Звонит мама"
    assert say("+998 90 123 45 67") == "Звонит незнакомый номер"
    assert say("") == "Звонит незнакомый номер"
    assert say("Mashhur bek aka", "WhatsApp") == "Звонит Mashhur Bek Aka в WhatsApp"      # 09.10: имя — узбекской латиницей, по-узбекски
    assert say("Alisher", "Telegram", "uz") == "Alisher qo'ng'iroq qilyapti (Telegram)"


def test_speakable_drops_contact_tags():
    assert names.speakable("SIROJIDDIN AKA I") == "Сироджиддин Ака"
    assert names.speakable("Bobur aka Inv") == "Бобур Ака"
    assert names.speakable("I Muxtorjon aka Marhamat") == "Мухторджон Ака Мархамат"
    assert names.speakable("Ali") == "Али"                 # короткое имя — не пометка


def test_announcement_number_from_contacts(tmp_path, monkeypatch):
    """28.09: уведомление о звонке пришло с номером, а человек в книге — называем человека, а не «незнакомый номер»."""
    from bot import phone

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    phone.save_contacts(77, [{"n": "Xushnudbek Aka ISH", "p": ["+998994995008"]}, {"n": "Onam", "p": ["90 123 45 67"]}])
    try:
        say = lambda name: phone_live.announcement_text(77, name, "", "ru")  # noqa: E731
        assert say("+998994995008") == "Звонит Xushnudbek Aka"
        assert say("+998 90 123-45-67") == "Звонит мама"      # в книге без +998
        assert say("+998 91 000 00 00") == "Звонит незнакомый номер"
    finally:
        phone._contacts.pop(77, None)


# ------------------------------------------------------------------ 09.10: «имена в контактах узбекские — читай по-узбекски»
def test_uzbek_names_are_voiced_in_uzbek_latin_whatever_the_book_script():
    from bot import names

    assert names.speakable_uz("SIROJIDDIN aka 📱2") == "Sirojiddin Aka"
    assert names.speakable_uz("Хусанбой ака") == "Xusanboy Aka"             # кириллица в книге — тоже узбекской латиницей
    assert names.speakable_uz("Ғайрат Қодиров") == "Gʻayrat Qodirov"
    assert names.speakable_uz("Onajonim") == "Onajonim"
    assert names.speakable_uz("Mashhur bek Inv") == "Mashhur Bek"           # пометка «Inv» — не имя
    assert names.speakable_uz("") == "" and names.speakable_uz("📱") == ""


def test_fancy_font_names_become_plain_letters():
    from bot import names

    assert names.plain("𝑀𝑎𝑠ℎ𝑥𝑢𝑟𝑏𝑒𝑘") == "Mashxurbek"
    assert names.plain("𝐀𝐝𝐚𝐤𝐡𝐚𝐦𝐨𝐯 🇺🇿") == "Adakhamov"


def test_telegram_display_name_is_plain(monkeypatch):
    from types import SimpleNamespace

    from bot import tg_user

    entity = SimpleNamespace(first_name="𝐀𝐝𝐚𝐤𝐡𝐚𝐦𝐨𝐯 🇺🇿", last_name=None, title=None, username="ad")
    assert tg_user.display_name(entity) == "Adakhamov"
    assert tg_user.display_name(SimpleNamespace(title="Маслаҳатчилар", first_name=None, last_name=None, username=None)) == "Маслаҳатчилар"


def test_old_russian_reading_is_still_available_and_cache_is_new(tmp_path, monkeypatch):
    """ANNOUNCE_NAME_STYLE=ru возвращает прежнее чтение; ключ записи новый (v5) — прежние русские записи не играют."""
    from bot import phone_live

    monkeypatch.setattr(phone_live, "NAME_STYLE", "ru")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert phone_live.announcement_text(77, "Mashhur bek aka", "", "ru") == "Звонит Машхур Бек Ака"
