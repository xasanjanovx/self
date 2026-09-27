"""28.09: «Звонит …» — без модели, имя целиком (было: «JES | AI» → «Джарвис», «Mashhur bek» → «Махурбек»)."""
from bot import names, phone_live


def test_speakable_uzbek_latin_names():
    assert names.speakable("Mashhur bek aka") == "Машхур Бек Ака"
    assert names.speakable("SIROJIDDIN 📱2") == "Сирожиддин"
    assert names.speakable("Xushnudbek") == "Хушнудбек"
    assert names.speakable("Yo'ldosh") == "Юлдош"
    assert names.speakable("G‘ayrat") == "Гайрат"
    assert names.speakable("Elyor new") == "Элёр"
    assert names.speakable("Shohamir Beeline") == "Шохамир"
    assert names.speakable("Iris Cafe Zakaz") == "Ирис Кафе Заказ"
    assert names.speakable("Алишер ака") == "Алишер Ака"
    assert names.speakable("Alisher aka", "uz") == "Alisher Aka"   # узбекский голос читает латиницу сам


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
    assert names.speakable("SIROJIDDIN AKA I") == "Сирожиддин Ака"
    assert names.speakable("Bobur aka Inv") == "Бобур Ака"
    assert names.speakable("I Muxtorjon aka Marhamat") == "Мухторжон Ака Мархамат"
    assert names.speakable("Ali") == "Али"                 # короткое имя — не пометка
