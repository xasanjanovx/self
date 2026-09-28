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
    assert say("Mashhur bek aka", "WhatsApp") == "Звонит Машхур Бек Ака в WhatsApp"
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
        assert say("+998994995008") == "Звонит Хушнудбек Ака"
        assert say("+998 90 123-45-67") == "Звонит мама"      # в книге без +998
        assert say("+998 91 000 00 00") == "Звонит незнакомый номер"
    finally:
        phone._contacts.pop(77, None)
