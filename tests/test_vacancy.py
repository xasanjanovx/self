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
    assert data.phone == "+998901234567 | +998935556677"
    assert data.telegram == "@hr_ish"
    prompt = data.image_prompt
    # полный промпт: вся вакансия сверху, задача на горизонтальный баннер снизу
    assert prompt and prompt.startswith("VAKANSIYA")
    assert "Lavozim: Call-center operatori kerak" in prompt and "Toshkent" in prompt
    assert "Talablar: 18-35 yosh; Rus tili" in prompt and "Sinov muddati: 1 oy" in prompt
    assert "+998901234567" in prompt and "@hr_ish" in prompt
    task = prompt[prompt.index("ЗАДАЧА"):]
    assert "ГОРИЗОНТАЛЬНЫЙ баннер 16:9" in task and "ТОЛЬКО самое важное" in task
    assert "зарплата" in task and "@ishdasiz" in task and "сочный, современный" in task


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


# ------------------------------------------------------------------ постер для автоподбора
def test_headline_is_split_in_two_lines_for_the_two_colour_title():
    from bot.vacancy import split_headline

    assert split_headline("Kredit menejeri kerak") == ("KREDIT", "MENEJERI KERAK")
    assert split_headline("Sotuv operatorlarini ishga taklif qilamiz!") == ("SOTUV OPERATORLARINI", "ISHGA TAKLIF QILAMIZ")
    assert split_headline("Barista") == ("BARISTA", "")


def test_age_badge_from_requirements():
    from bot.vacancy import age_badge

    data = _data()
    data.requirements = ["18-35 yosh", "Rus tili"]
    assert age_badge(data) == "18–35 yosh"
    data.requirements = ["20 yoshdan yuqori"]
    assert age_badge(data) == "20+ yosh"
    data.requirements = ["Rus tili"]
    assert age_badge(data) is None


def test_designs_are_many_and_complete():
    from bot.vacancy import DESIGNS

    assert len(DESIGNS) >= 10 and len({d["id"] for d in DESIGNS}) == len(DESIGNS)
    assert all(d["style"] and d["layout"] and d["photo"] for d in DESIGNS)


def test_design_follows_the_profession_but_never_repeats_the_recent_ones():
    from bot.vacancy import pick_design

    data = _data()
    data.headline = "Shifokor-stomatolog kerak"
    assert pick_design(data, seed="x")["id"] == "clean_teal"
    data.headline = "Donarchi va ofitsiant kerak"
    assert pick_design(data, seed="x")["id"] == "fastfood_red"
    assert pick_design(data, recent=["fastfood_red"], seed="x")["id"] != "fastfood_red"        # недавний не берём, даже если подходит
    data.headline = "Operator kerak"
    seen = []
    for i in range(12):
        seen.insert(0, pick_design(data, recent=seen, seed=str(i))["id"])
    assert all(seen[i] not in seen[i + 1:i + 7] for i in range(len(seen) - 1))                   # в окне из 6 дизайнов повторов нет


def test_same_seed_same_design_and_different_seeds_spread():
    from bot.vacancy import pick_design

    data = _data()
    data.headline = "Xodim kerak"
    assert pick_design(data, seed="a")["id"] == pick_design(data, seed="a")["id"]
    assert len({pick_design(data, seed=str(i))["id"] for i in range(40)}) >= 6


def test_poster_prompt_quotes_exact_texts_and_reserves_the_logo_corner():
    from bot.vacancy import build_poster_prompt, pretty_phone

    assert pretty_phone("+998901234567 | +998935556677") == "+998 90 123 45 67"
    prompt = build_poster_prompt(_data(), scene="кафе", design="neon_green")
    assert "LIME-GREEN" in prompt and "STYLE:" in prompt
    assert '"CALL-CENTER"' in prompt and "\"4 000 000 so'm + bonus\"" in prompt and '"+998 90 123 45 67"' in prompt and '"@hr_ish"' in prompt
    assert "RESERVED ZONE" in prompt and "bottom-left" in prompt
    assert "Qulayliklar" not in prompt and '"Tushlik bepul"' in prompt             # преимущества — подписями к иконкам


def test_cut_words_never_leaves_an_ellipsis_or_half_a_word():
    from bot.vacancy import cut_words

    assert cut_words("Yotoq joy ishxona hisobidan", 14) == "Yotoq joy"
    assert cut_words("Kompaniya tomonidan qo'shimcha", 20) == "Kompaniya tomonidan"
    assert cut_words("Short", 20) == "Short"
    assert cut_words(None, 10) is None
    assert "…" not in (cut_words("a" * 50 + " " + "b" * 50, 60) or "")


def test_poster_uses_the_short_texts_and_never_draws_an_ellipsis():
    from bot.vacancy import build_poster_prompt

    data = _data()
    data.headline = "Bolalar kiyim do'koniga sotuvchi-konsultant qizlarni taklif qilamiz"
    data.short_title = "Sotuvchi-konsultant kerak"
    data.salary = "3 000 000 - 5 000 000 so'm (oz vaqtida to'lanadi, bonuslar bilan)"
    data.short_salary = "3–5 mln so'm"
    data.short_schedule = "15:00–22:00"
    data.short_place = "Mirzo Ulug'bek"
    data.short_perks = ["Tushlik bepul", "Rasmiy ish"]
    prompt = build_poster_prompt(data, scene="do'kon", design="gold_black")
    for exact in ('"SOTUVCHI-KONSULTANT"', '"KERAK"', "\"3–5 mln so'm\"", '"15:00–22:00"', "\"Mirzo Ulug'bek\"",
                  '"Tushlik bepul"', '"Rasmiy ish"'):
        assert exact in prompt, exact
    assert "…" not in prompt and "workplace" not in prompt and "qizlarni" not in prompt


def test_without_short_texts_the_full_ones_are_cut_at_word_boundaries():
    from bot.vacancy import build_poster_prompt

    data = _data()
    data.benefits = ["Yotoq joy ishxona hisobidan", "3 mahal ovqat ishxona hisobidan"]
    data.schedule = "08:00 dan 17:00 gacha (doimiy aloqada bo'lish vaqti 07:00–22:00)"
    prompt = build_poster_prompt(data, scene="seh")
    assert "…" not in prompt and '"Yotoq joy ishxona"' in prompt and "doimiy" not in prompt and "hisobid" not in prompt


def test_prompt_forbids_extra_elements_and_literal_plus_signs():
    from bot.vacancy import build_poster_prompt

    data = _data()
    data.requirements = ["Rus tili"]                      # возраста нет — бейджа в плакате быть не должно
    prompt = build_poster_prompt(data, scene="ofis", design="worker_left_dark")
    assert "ONLY THE LISTED ELEMENTS" in prompt and "badge" in prompt.lower()
    assert "Round accent-coloured badge" not in prompt     # элемента-бейджа в списке текстов нет
    assert "icon + " not in prompt and "followed by the text" in prompt
