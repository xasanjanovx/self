"""Вакансии: детект и сборка поста (без сети)."""
from bot.ai import VacancyData, VacancySection
from bot.vacancy import default_image_prompt, finalize, format_vacancy_post, looks_like_vacancy


def test_detects_clear_vacancy_with_contact():
    text = "Требуется продавец-консультант в магазин. Зарплата 5 000 000 сум. График 5/2. Контакт: +998 90 123 45 67"
    assert looks_like_vacancy(text) is True


def test_detects_uzbek_vacancy():
    assert looks_like_vacancy("Sotuvchi kerak. Maosh kelishilgan. Ish vaqti 9:00-18:00. Aloqa: +998901234567") is True


def test_rejects_short_greeting():
    assert looks_like_vacancy("привет") is False


def test_rejects_empty():
    assert looks_like_vacancy("") is False


def test_rejects_random_sentence():
    assert looks_like_vacancy("Сегодня хорошая погода и я гулял в парке") is False


def test_rejects_finance_message():
    assert looks_like_vacancy("зарплата 5 млн на карту") is False


def _data():
    return VacancyData(
        headline="Call-center operatori kerak",
        intro="Katta savdo kompaniyasiga xodim izlaymiz.",
        company="Ishdasiz LLC",
        region_tag="#TOSHKENT",
        address="Chilonzor",
        salary="4 000 000 so'm + bonus",
        schedule="9:00-18:00, 6/1",
        requirements=["18-35 yosh", "Rus tili"],
        duties=["Qo'ng'iroqlarga javob berish"],
        benefits=["Tushlik bepul"],
        extra_sections=[VacancySection(title="Sinov muddati", items=["1 oy"])],
        phone="+998901234567",
        telegram="@hr_ish",
        image_prompt=None,
    )


def test_finalize_fills_phone_telegram_prompt():
    raw = "Aloqa: +998 90 123 45 67, +998 93 555 66 77 https://t.me/hr_ish"
    data = finalize(_data(), raw)
    assert data.phone == "+998 90 123 45 67 | +998 93 555 66 77"
    assert data.telegram == "@hr_ish"
    assert data.image_prompt and "16:9" in data.image_prompt


def test_format_post_contains_sections_and_no_generic_bucket():
    post = format_vacancy_post(finalize(_data(), ""), premium=False)
    assert "Call-center operatori kerak" in post
    assert "Talablar:" in post and "Vazifalar:" in post and "Qulayliklar:" in post
    assert "Sinov muddati:" in post and "• 1 oy" in post
    assert "Qo'shimcha ma'lumotlar" not in post
    assert "ISHDASIZ" in post and "<blockquote>" in post
    assert "#TOSHKENT" in post


def test_default_prompt_is_horizontal():
    assert "16:9" in default_image_prompt("Sotuvchi kerak")
