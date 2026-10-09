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
    assert say("JES | AI", "Telegram") == "Звонит Джес в Телеграм"
    assert say("Jarvis") == "Звонит Джес"
    assert say("Мама") == "Звонит мама"
    assert say("Oyijon") == "Звонит мама"
    assert say("+998 90 123 45 67") == "Звонит незнакомый номер"
    assert say("") == "Звонит незнакомый номер"
    assert say("Mashhur bek aka", "WhatsApp") == "Звонит Mashhur Bek aka в Вотсап"      # 09.10: имя — узбекской латиницей, по-узбекски
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
        assert say("+998994995008") == "Звонит Xushnudbek aka"
        assert say("+998 90 123-45-67") == "Звонит мама"      # в книге без +998
        assert say("+998 91 000 00 00") == "Звонит незнакомый номер"
    finally:
        phone._contacts.pop(77, None)


# ------------------------------------------------------------------ 09.10: «имена в контактах узбекские — читай по-узбекски»
def test_uzbek_names_are_voiced_in_uzbek_latin_whatever_the_book_script():
    from bot import names

    assert names.speakable_uz("SIROJIDDIN aka 📱2") == "Sirojiddin aka"
    assert names.speakable_uz("Хусанбой ака") == "Xusanboy aka"             # кириллица в книге — тоже узбекской латиницей
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


# ------------------------------------------------------------------ 09.10: «Звонит» по-русски, имя — Gemini по-узбекски, тем же голосом
def test_announcement_parts_split_russian_frame_and_uzbek_name():
    from bot import phone_live

    parts = phone_live.announcement_parts(77, "Sirojbek aka", "Telegram", "ru")
    assert parts == [("Звонит", "ru-RU"), ("Sirojbek aka", "uz-UZ", "name"), ("в Телеграм", "ru-RU")]
    assert phone_live.announcement_text(77, "Sirojbek aka", "Telegram", "ru") == "Звонит Sirojbek aka в Телеграм"
    assert phone_live.announcement_parts(77, "Хусанбой ака", "", "ru") == [("Звонит", "ru-RU"), ("Xusanboy aka", "uz-UZ", "name")]


def test_kin_and_helper_and_unknown_stay_one_piece():
    from bot import phone_live

    assert phone_live.announcement_parts(77, "Мама", "", "ru") == [("Звонит мама", None)]
    assert phone_live.announcement_parts(77, "JES | AI", "Telegram", "ru") == [("Звонит Джес в Телеграм", None)]
    assert phone_live.announcement_parts(77, "", "", "ru") == [("Звонит незнакомый номер", None)]


def test_uzbek_persona_reads_everything_in_uzbek():
    from bot import phone_live

    parts = phone_live.announcement_parts(77, "Sirojbek aka", "", "uz")
    assert parts and all(p[1] == "uz-UZ" for p in parts) and parts[0][0] == "Sirojbek aka" and parts[0][2] == "name"


def test_synthesize_parts_gives_each_piece_its_language_and_joins_them():
    import asyncio

    from bot.ai import AIService

    seen: list = []

    class Fake(AIService):
        def __init__(self):  # noqa: D107
            pass

        async def speak_stream(self, text, *, voice="Kore", model="m", free=False, language=None):  # noqa: ANN001
            seen.append((text, language, voice))
            yield (b"\x01\x00" * 2400) if language == "ru-RU" else (b"\x02\x00" * 4800)

    pcm = asyncio.run(Fake().synthesize_parts([("Звонит", "ru-RU"), ("Sirojbek aka", "uz-UZ")], voice="Sulafat"))
    assert seen == [("Звонит", "ru-RU", "Sulafat"), ("Sirojbek aka", "uz-UZ", "Sulafat")]
    assert pcm is not None and len(pcm) == 4800 + 3360 + 9600          # кусок, пауза 70 мс, кусок

    # особый кусок (имя): своя озвучка; не вышла — обычная
    seen.clear()

    async def own(text):  # noqa: ANN001
        return b"\x03\x00" * 1000

    pcm = asyncio.run(Fake().synthesize_parts([("Звонит", "ru-RU"), ("Sirojbek aka", "uz-UZ", "name")], voice="Sulafat", special={"name": own}))
    assert seen == [("Звонит", "ru-RU", "Sulafat")] and len(pcm) == 4800 + 3360 + 2000

    async def none(text):  # noqa: ANN001
        return None

    seen.clear()
    asyncio.run(Fake().synthesize_parts([("Sirojbek aka", "uz-UZ", "name")], voice="Sulafat", special={"name": none}))
    assert seen == [("Sirojbek aka", "uz-UZ", "Sulafat")]

    class Broken(Fake):
        async def speak_stream(self, text, *, voice="Kore", model="m", free=False, language=None):  # noqa: ANN001
            if language == "uz-UZ":
                raise RuntimeError("boom")
            yield b"\x01\x00" * 100

    assert asyncio.run(Broken().synthesize_parts([("Звонит", "ru-RU"), ("Sirojbek aka", "uz-UZ")])) is None   # не вышло — обычная озвучка


def test_tts_cache_key_depends_on_language_only_when_given(tmp_path, monkeypatch):
    from bot import ai as ai_mod

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    plain = ai_mod._tts_cache_path("m", "Sulafat", "Звонит")
    assert plain == ai_mod._tts_cache_path("m", "Sulafat", "Звонит", None)           # прежние записи не теряются
    assert plain != ai_mod._tts_cache_path("m", "Sulafat", "Звонит", "ru-RU")


# ------------------------------------------------------------------ bot/uz_voice.py: имя внутри узбекской фразы, хвост срезан
def _speech(seconds: float, amp: int = 6000):
    import numpy as np

    n = int(24000 * seconds)
    return (np.sin(np.arange(n) * 0.3) * amp).astype(np.int16)


def _silence(seconds: float):
    import numpy as np

    return np.zeros(int(24000 * seconds), dtype=np.int16)


def test_cut_name_stops_at_the_sentence_pause_not_between_name_words():
    import numpy as np

    from bot import uz_voice

    # «Xusanboy [0.1 с] aka [0.6 с пауза] qo'ng'iroq qilyapti»
    pcm = np.concatenate([_silence(0.25), _speech(0.7), _silence(0.1), _speech(0.35), _silence(0.6), _speech(1.2)]).tobytes()
    out = uz_voice.cut_name(pcm)
    assert out is not None
    seconds = len(out) / 2 / 24000
    assert 1.1 <= seconds <= 1.3                     # оба слова имени (0.7 + 0.1 + 0.35 + кромки), без «qo'ng'iroq qilyapti»


def test_cut_name_without_a_real_pause_gives_none():
    import numpy as np

    from bot import uz_voice

    pcm = np.concatenate([_speech(0.7), _silence(0.1), _speech(1.5)]).tobytes()
    assert uz_voice.cut_name(pcm) is None            # паузы нет — хвост надёжно не отрезать: озвучат имя обычным способом
    assert uz_voice.cut_name(b"\x00\x00" * 100) is None


def test_say_name_records_takes_and_keeps_the_one_that_sounds_right(tmp_path, monkeypatch):
    import asyncio

    import numpy as np

    from bot import uz_voice

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    said: list = []
    takes = [np.concatenate([_silence(0.2), _speech(0.8, amp=a), _silence(0.5), _speech(1.0)]).tobytes() for a in (2000, 4000, 6000)]
    heard = {2000: "Dilsha take", 4000: "Dilshod aka", 6000: "Dilshot oka"}      # что «услышала» расшифровка в каждом дубле

    class FakeAI:
        def __init__(self):  # noqa: D107
            self.n = 0

        async def speak_stream(self, text, *, voice="Kore", model="m", free=False, language=None):  # noqa: ANN001
            said.append((text, language, voice))
            self.n += 1
            yield takes[(self.n - 1) % 3]

        async def generate(self, parts, **kw):  # noqa: ANN001, ANN003
            import base64
            import io
            import wave

            with wave.open(io.BytesIO(base64.b64decode(parts[1]["inline_data"]["data"]))) as w:
                frames = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            import json

            return json.dumps({"heard": heard[int(frames.max() // 2000 * 2000)] if frames.max() >= 2000 else "", "aka": "neutral"})

    ai = FakeAI()
    pcm = asyncio.run(uz_voice.say_name(ai, "Dilshod aka", voice="Sulafat"))
    assert said == [("Dilshod aka. Qo'ng'iroq qilyapti.", "uz-UZ", "Sulafat")] * uz_voice.TAKES        # несколько дублей
    assert pcm is not None and int(np.frombuffer(pcm, dtype=np.int16).max()) > 3500          # выбран второй (расшифровался как «Dilshod aka»)
    assert 0.8 <= len(pcm) / 2 / 24000 <= 1.0
    again = asyncio.run(uz_voice.say_name(ai, "Dilshod aka", voice="Sulafat"))
    assert again == pcm and len(said) == uz_voice.TAKES                                                   # второй раз — с диска, без модели


def test_similarity_tolerates_spelling_variants_but_not_lost_syllables():
    from bot import uz_voice

    assert uz_voice.similarity("Xusanboy aka", "Husanboy aka") > 0.95
    assert uz_voice.similarity("Gʻayrat", "g'ayrat") == 1.0
    assert uz_voice.similarity("Dilshod aka", "Dilshod aka") == 1.0
    assert uz_voice.similarity("Dilsha take", "Dilshod aka") < uz_voice.similarity("Dilshot aka", "Dilshod aka")
    assert uz_voice.similarity("", "Jasur") == 0.0


def test_say_name_failures_return_none_so_the_plain_voice_takes_over(tmp_path, monkeypatch):
    import asyncio

    from bot import uz_voice

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    class Broken:
        async def speak_stream(self, text, *, voice="Kore", model="m", free=False, language=None):  # noqa: ANN001
            raise RuntimeError("503")
            yield b""

    assert asyncio.run(uz_voice.say_name(Broken(), "Jasur", voice="Sulafat")) is None


def test_similarity_ignores_quotes_around_what_the_listener_wrote():
    from bot import uz_voice

    assert uz_voice.similarity('"Dilshod aka"', "Dilshod aka") == 1.0
    assert uz_voice.similarity("«Jasur»", "Jasur") == 1.0


def test_cut_name_keeps_all_words_when_there_is_a_pause_inside_a_long_name():
    """09.10: «Komiljon aka Jalaquduq» — после «aka» пауза 0.3 с; по первой паузе терялось «Jalaquduq», и оно читалось отдельно как «Yalaquduq»."""
    import numpy as np

    from bot import uz_voice

    pcm = np.concatenate([_silence(0.25), _speech(0.7), _silence(0.1), _speech(0.35), _silence(0.32), _speech(0.8),   # Komiljon aka | Jalaquduq
                          _silence(0.6), _speech(0.45), _silence(0.13), _speech(0.55)]).tobytes()                          # qo'ng'iroq qilyapti
    out = uz_voice.cut_name(pcm)
    assert out is not None
    seconds = len(out) / 2 / 24000
    assert 2.2 <= seconds <= 2.5                      # три слова имени (0.7 + 0.1 + 0.35 + 0.32 + 0.8 = 2.27 + кромки), без хвоста фразы


def test_voice_text_has_no_comma_and_respells_j_in_later_words():
    from bot import uz_voice

    # запятая после «aka» делала из него обращение (зовут человека) — её нет; «J» второго слова пишем «Dj» ([dʒ])
    assert uz_voice.voice_text("Komiljon aka Jalaquduq") == "Komiljon aka Djalaquduq"
    assert uz_voice.voice_text("Jahongir aka Itpark") == "Jahongir aka Itpark"       # первое слово — как есть
    assert uz_voice.voice_text("Xusanboy aka") == "Xusanboy aka"
    assert uz_voice.voice_text("Nafisa opa Axb") == "Nafisa opa Axb"
    assert uz_voice.voice_text("Bobur aka Jyoti") == "Bobur aka Djyoti"



def test_takes_where_aka_sounds_like_a_call_out_lose_to_neutral_ones(tmp_path, monkeypatch):
    import asyncio
    import json

    import numpy as np

    from bot import uz_voice

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    amps = (2000, 4000, 6000)
    takes = [np.concatenate([_silence(0.2), _speech(0.8, amp=a), _silence(0.5), _speech(1.0)]).tobytes() for a in amps]
    verdicts = {2000: ("Akmal aka", "vocative"), 4000: ("Akmal aka", "vocative"), 6000: ("Akmal aka", "neutral")}   # как слышно и как прозвучало «aka»

    class FakeAI:
        n = 0

        async def speak_stream(self, text, *, voice="Kore", model="m", free=False, language=None):  # noqa: ANN001
            self.n += 1
            yield takes[(self.n - 1) % 3]

        async def generate(self, parts, **kw):  # noqa: ANN001, ANN003
            import base64
            import io
            import wave

            with wave.open(io.BytesIO(base64.b64decode(parts[1]["inline_data"]["data"]))) as w:
                frames = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            heard, aka = verdicts[int(round(frames.max() / 2000)) * 2000]
            return json.dumps({"heard": heard, "aka": aka})

    pcm = asyncio.run(uz_voice.say_name(FakeAI(), "Akmal aka", voice="Sulafat", takes=3))
    assert pcm is not None and int(np.frombuffer(pcm, dtype=np.int16).max()) > 5000      # выбран третий дубль: «aka» — часть имени, не обращение
    assert uz_voice.score("Akmal aka", True, "Akmal aka") < uz_voice.score("Akmal aka", False, "Akmal aka")


def test_call_time_announce_is_quick_when_the_name_is_not_ready_and_final_when_it_is(tmp_path, monkeypatch):
    import asyncio

    from bot import phone_live, services, uz_voice
    from bot.context import ai
    from bot.persona import Persona

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    calls: list = []

    async def persona(u):  # noqa: ANN001
        return Persona(lang="ru")

    async def parts(self_or_parts, *a, special=None, **k):  # noqa: ANN001, ANN002, ANN003
        pcm = await special["name"]("Sirojbek aka")
        return b"\x01\x00" * 2400 + (pcm or b"")

    async def quick(ai_, name, *, voice):  # noqa: ANN001
        calls.append(("quick", name))
        return b"\x02\x00" * 4800

    async def best(ai_, name, *, voice, takes=5):  # noqa: ANN001
        calls.append(("best", name))
        return b"\x03\x00" * 4800

    monkeypatch.setattr(services, "persona", persona)
    monkeypatch.setattr(ai, "synthesize_parts", parts, raising=False)
    monkeypatch.setattr(uz_voice, "quick_name", quick)
    monkeypatch.setattr(uz_voice, "say_name", best)
    ready = {"v": False}
    monkeypatch.setattr(uz_voice, "cached", lambda name, voice: ready["v"])

    async def run_quick():
        out = await phone_live.announce(77, "Sirojbek aka", "Telegram", quick=True)
        await asyncio.sleep(0.05)               # фоновая запись лучшего дубля
        return out

    out = asyncio.run(run_quick())
    assert out["final"] is False and out["wav"]                                  # быстрый черновик: приложение его не запомнит
    assert ("quick", "Sirojbek aka") in calls and ("best", "Sirojbek aka") in calls           # лучший дубль записывается в фоне
    assert not list((tmp_path / "announce").glob("*.json"))                      # черновик не кэшируется и на сервере

    calls.clear()
    ready["v"] = True                                                            # лучший дубль готов (фон или заранее)
    out = asyncio.run(phone_live.announce(77, "Sirojbek aka", "Telegram", quick=True))
    assert out["final"] is True and [c[0] for c in calls] == ["best"]            # из готового: быстро и окончательно
    assert list((tmp_path / "announce").glob("*.json"))


def test_prefetch_covers_every_contact_once_at_a_time(tmp_path, monkeypatch):
    import asyncio

    from bot import phone, phone_live

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    phone.save_contacts(77, [{"n": "Shuxrat Aka", "p": ["+998901234567"]}, {"n": "Muxriddin", "p": ["+998901234568"]}, {"n": "Taksi", "p": ["112"]}])
    asked: list = []

    async def fake_announce(uid, name, app="", *, quick=False):  # noqa: ANN001
        asked.append((name, app, quick))
        return {"wav": "x"}

    async def no_pieces(uid):  # noqa: ANN001
        return None

    monkeypatch.setattr(phone_live, "announce", fake_announce)
    monkeypatch.setattr(phone_live, "_prefetch_pieces", no_pieces)
    monkeypatch.setattr(phone_live, "PREFETCH_GAP_S", 0)
    done = asyncio.run(phone_live.prefetch_announcements(77))
    names = [n for n, _a, _q in asked]
    assert {"Shuxrat Aka", "Muxriddin", "Taksi", ""} <= set(names) and done == len(names)
    assert all(q is False for _n, _a, q in asked)                                # заранее — лучшее качество, не быстрый черновик
    assert phone_live._prefetching is False
