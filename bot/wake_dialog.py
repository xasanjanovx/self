"""Ход утреннего звонка-подъёма — кодом, а не моделью.

06.10 он сказал: «будильник очень много раз спрашивает, слышу я или нет; надо три вопроса, как раньше». В логах 05.10 и 06.10
весь разговор — «Шеф, вы меня слышите?»: таймер тишины каждые 8 секунд толкал слабую модель, а порядок вопросов она сама не держала.
На пробе с настоящей моделью выяснилось и другое: она шутит («ха-ха, ну ты даёшь»), одобряет неверный ответ, выдумывает время до такбира.
Поэтому разговором управляет этот модуль, а модель делает только одно — оценивает его ответ (узкий JSON-вызов):
  • этапы: приветствие → вопросы дня по одному → «вы встали?» → конец; реакции, следующий вопрос, повтор — готовыми фразами;
  • тишина (его выбор «сразу вопрос»): вопрос → повтор → мотивация и «скажите хотя бы “не знаю”» → «слышите ли» (ОДИН раз за
    звонок) → дальше по кругу «до такбира N минут + мотивация» и повтор вопроса;
  • умнее: «который час / сколько до такбира» — точный ответ по часам; «встал, отключайся» — один раз «ещё минуту, осталось N вопроса»,
    второй раз отпускаем; «ещё поспать» — один раз «намаз лучше сна», второй раз откладываем не больше чем на 5 минут;
    меньше вопросов, когда мало времени до такбира; повторный звонок продолжает с того вопроса, где остановились; ошибки возвращаются
    в следующие дни (islam_quiz.note_result); «встал» принимаем только на ясное слово (одно «да» — переспрашиваем «скажите “встал”»).
Здесь чистая логика без звонков и сети — тесты в tests/test_wake_dialog.py; с голосом соединяет bot/cheap_voice.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import islam_quiz
from . import wake as wake_mod
from .wake import MOTIVATION

LANGS = ("ru", "uz", "en")
QUIZ_VERDICTS = ("correct", "wrong", "unknown", "other")
FINAL_VERDICTS = ("up", "not_yet", "other")
SNOOZE_MAX_MIN = 5


def questions_for(minutes_left: int | None, total: int = islam_quiz.QUESTIONS_PER_DAY) -> int:
    """Сколько вопросов успеем: до такбира много времени — все, мало — меньше (но хотя бы один)."""
    if minutes_left is None or minutes_left >= 12:
        return total
    if minutes_left >= 6:
        return min(total, 2)
    return min(total, 1)


def minutes_text(n: int, lang: str) -> str:
    n = max(0, int(n))
    if lang == "uz":
        return f"{n} daqiqa"
    if lang == "en":
        return f"{n} minute" + ("" if n == 1 else "s")
    last2, last = n % 100, n % 10
    word = "минут" if 11 <= last2 <= 14 or last == 0 or last >= 5 else ("минута" if last == 1 else "минуты")
    return f"{n} {word}"


def questions_text(n: int, lang: str) -> str:
    n = max(0, int(n))
    if lang == "uz":
        return f"{n} ta savol"
    if lang == "en":
        return f"{n} question" + ("" if n == 1 else "s")
    last2, last = n % 100, n % 10
    return f"{n} " + ("вопрос" if last == 1 and last2 != 11 else "вопроса" if 2 <= last <= 4 and not 12 <= last2 <= 14 else "вопросов")


# ------------------------------------------------------------------ что он сказал
def _norm(text: str) -> str:
    return str(text or "").lower().replace("ё", "е")


def has_words(text: str) -> bool:
    return bool(re.search(r"\w{2,}", str(text or "")))


_NOT_UP_RE = re.compile(r"\bне\s+(?:встал[аи]?|проснул[а-я]*|хочу|могу)|\b(нет|сплю|лежу|еще|минут[уы]?|потом)\b|yo'?q|\bhali\b|\bemas\b")
_UP_STRICT_RE = re.compile(r"\b(встал[аи]?|проснул[а-я]*|на ногах|поднял[а-я]*|turdim|uyg'?ondim|i'?m up)\b")
# «встал, отключайся» / «хватит, я встал, давай всё» — торопит, а вопросы ещё не закончились. Только вместе с ЯСНЫМ «встал»
# (сонное «да-да, хватит» подъёмом не считается)
_HURRY_RE = re.compile(r"отключ[а-я]*|положи[а-я]* трубку|хватит|давай все|все,? давай|без вопросов|не надо вопрос[а-я]*|некогда|спешу|тороплюсь|"
                       r"достаточно|все,? пока|ладно,? пока|\bпока[.!? ]*$|bo'?ldi|yetar|xayr|vaqtim yo'?q")
_TIME_RE = re.compile(r"который час|сколько (?:сейчас )?(?:времени|на часах)|сколько (?:еще )?(?:до|осталось)|какое время|soat nech|vaqt qancha|necha bo'?ldi")
# «ещё минутку / полежу / дай поспать / отстань» — просит поспать (но не «потом Умар» — это ответ на вопрос)
_SLEEP_ASK_RE = re.compile(r"еще (?:минут|немного|чуть|пять|десять|\d)|полежу|дай(?:те)? (?:поспать|минут|еще)|поспать|подожди|погоди|отстань|не хочу|"
                           r"спать хочу|хочу спать|сплю|yana (?:bir|besh|o'n|\d)|keyinroq|uxlay|uxlab")
_HEAR_RE = re.compile(r"^\W*(?:алло|ало|але|алле|эй|ау|allo|alo)\W*$|(?:вы|ты) (?:меня )?слыш|вас слышно|eshityapsiz|eshitasiz")


def is_hurry(text: str) -> bool:
    t = _norm(text).replace("не сплю", "встал")
    return bool(_HURRY_RE.search(t)) and bool(_UP_STRICT_RE.search(t)) and not _NOT_UP_RE.search(t)


def heard_means_up(text: str) -> bool:
    """Запасная проверка «встал» (если модель-оценщик недоступна): есть ясное «встал / проснулся / на ногах» и нет «не встал / ещё / нет»."""
    t = _norm(text).replace("не сплю", "встал")
    return bool(_UP_STRICT_RE.search(t)) and not _NOT_UP_RE.search(t)


# ------------------------------------------------------------------ оценка ответа моделью (единственное, что делает модель)
def quiz_prompt(q: islam_quiz.Q, heard: str) -> str:
    return (
        "Ты проверяешь устный ответ на исламский вопрос. Ответ получен распознаванием речи: слова могут быть искажены или записаны по "
        "звучанию (в том числе арабские) — понимай по смыслу. Верно, если названо главное; полное имя, арабский текст, порядок слов и "
        "мелкие детали не нужны; если в вопросе несколько частей — верно, когда названо главное или большинство.\n"
        f"Вопрос: {q.q}\nПравильный ответ: {q.a}\n" + (f"Арабский текст (если ответ — дуа или аят): {q.ar}\n" if q.ar else "")
        + f"Его ответ: «{heard}»\n"
        'Верни JSON {"verdict": "..."}: "correct" — по смыслу верно; "wrong" — отвечает, но неверно; "unknown" — ПРЯМО говорит, что не знает, '
        'не помнит или сдаётся; "other" — это вообще не ответ на вопрос: «да», «угу», «проснулся», «я встал», «алло», «что?», мычание, вздохи, '
        'просьба, встречный вопрос, жалоба, шум, другая тема.')


def final_prompt(heard: str) -> str:
    return (
        "Человека разбудили звонком и спросили: «Вы встали? Не ляжете обратно?». Его ответ (распознавание речи): "
        f"«{heard}».\n"
        'Верни JSON {"verdict": "..."}: "up" — ясно говорит, что УЖЕ встал, проснулся, на ногах (в прошедшем времени: «встал», «уже на ногах»); '
        '"not_yet" — ещё лежит, «встаю», «сейчас встану», «уже почти», просит подождать, или это сонное «да / ага / угу» без ясных слов; '
        '"other" — не по теме или непонятно.')


def parse_verdict(data: Any, allowed: tuple[str, ...]) -> str:
    """Ответ модели-оценщика → один из allowed; всё остальное — пусто (оценщик не ответил как надо)."""
    value = data.get("verdict") if isinstance(data, dict) else data
    value = str(value or "").strip().lower()
    return value if value in allowed else ""


# ------------------------------------------------------------------ тексты (язык разговора; вопросы из банка — русские)
_T: dict[str, dict] = {
    "ru": {
        "greet": "Доброе утро, {t}! Проснулись?",
        "ord": ("Первый вопрос", "Второй вопрос", "Третий вопрос"), "last": "Последний вопрос", "one": "Вопрос",
        "ask": "{o}: {q}", "ask_wake": "{T}, давайте разбудим голову. {o}: {q}", "review": "Повторим то, что не получилось. ",
        "repeat": "Повторю, {t}. {q}", "dunno": "Ответьте хотя бы «не знаю», {t}.",
        "hear": "{T}, вы меня слышите? Скажите хоть слово.", "hear_yes": "Да, {t}, слышу вас.",
        "final": "{T}, вы встали? Не ляжете обратно?", "final_q": "Вы встали? Не ляжете обратно?",
        "final_again": "{T}, скажите «встал», когда будете на ногах.",
        "left": "До такбира {n}.", "unclear": "Не разобрала.",
        "right": "Правильный ответ: {a}", "arabic": "По-арабски так: {ar}.",
        "correct": ("Верно, {t}.", "Правильно!", "Молодец, {t}!"), "wrong": "Не совсем, {t}.", "unknown": "Ничего страшного, {t}.",
        "unsure": "Принято.", "react": ("Хорошо, {t}.", "Отлично, {t}."),
        "persuade": "{T}, намаз лучше сна.", "snooze_ok": "Хорошо, {t}, позвоню через {m}. Только не засните.",
        "great": "Отлично, {t}!", "wait": "Ещё минуту, {t}, осталось {k}.",
        "time": "Сейчас {h}.", "time_left": "Сейчас {h}, до такбира {n}.",
        "bye": "До такбира {n}. Пусть Аллах примет ваш намаз!", "bye_late": "Пусть Аллах примет ваш намаз!",
    },
    "uz": {
        "greet": "Xayrli tong, {t}! Uyg'ondingizmi?",
        "ord": ("Birinchi savol", "Ikkinchi savol", "Uchinchi savol"), "last": "Oxirgi savol", "one": "Savol",
        "ask": "{o}: {q}", "ask_wake": "{T}, miyani uyg'otamiz. {o}: {q}", "review": "Chiqmay qolganini takrorlaymiz. ",
        "repeat": "Takrorlayman, {t}. {q}", "dunno": "Hech bo'lmasa «bilmayman» deng, {t}.",
        "hear": "{T}, meni eshityapsizmi? Bir so'z ayting.", "hear_yes": "Ha, {t}, eshityapman.",
        "final": "{T}, turdingizmi? Qayta yotib qolmaysizmi?", "final_q": "Turdingizmi? Qayta yotib qolmaysizmi?",
        "final_again": "{T}, oyoqqa turganingizda «turdim» deng.",
        "left": "Takbirgacha {n} qoldi.", "unclear": "Tushunmadim.",
        "right": "To'g'ri javob: {a}", "arabic": "Arabchasi: {ar}.",
        "correct": ("To'g'ri, {t}.", "Barakalla!", "Qoyil, {t}!"), "wrong": "Unchalik emas, {t}.", "unknown": "Hechqisi yo'q, {t}.",
        "unsure": "Qabul qildim.", "react": ("Yaxshi, {t}.", "Ajoyib, {t}."),
        "persuade": "{T}, namoz uyqudan yaxshiroq.", "snooze_ok": "Yaxshi, {t}, {m}dan keyin qo'ng'iroq qilaman. Faqat uxlab qolmang.",
        "great": "Ajoyib, {t}!", "wait": "Bir daqiqa, {t}, {k} qoldi.",
        "time": "Hozir {h}.", "time_left": "Hozir {h}, takbirgacha {n} qoldi.",
        "bye": "Takbirgacha {n} qoldi. Alloh namozingizni qabul qilsin!", "bye_late": "Alloh namozingizni qabul qilsin!",
    },
    "en": {
        "greet": "Good morning, {t}! Are you up?",
        "ord": ("First question", "Second question", "Third question"), "last": "Last question", "one": "Question",
        "ask": "{o}: {q}", "ask_wake": "{T}, let's wake the mind up. {o}: {q}", "review": "Let's repeat the one that didn't work out. ",
        "repeat": "Once more, {t}. {q}", "dunno": "At least say \"I don't know\", {t}.",
        "hear": "{T}, can you hear me? Say a word.", "hear_yes": "Yes, {t}, I hear you.",
        "final": "{T}, are you up? You won't lie back down?", "final_q": "Are you up? You won't lie back down?",
        "final_again": "{T}, say \"I'm up\" once you're on your feet.",
        "left": "{n} until takbir.", "unclear": "I didn't catch that.",
        "right": "The right answer: {a}", "arabic": "In Arabic: {ar}.",
        "correct": ("Correct, {t}.", "Right!", "Well done, {t}!"), "wrong": "Not quite, {t}.", "unknown": "No worries, {t}.",
        "unsure": "Noted.", "react": ("Good, {t}.", "Great, {t}."),
        "persuade": "{T}, prayer is better than sleep.", "snooze_ok": "Alright, {t}, I'll call again in {m}. Just don't fall asleep.",
        "great": "Great, {t}!", "wait": "One more minute, {t}, {k} left.",
        "time": "It's {h}.", "time_left": "It's {h}, {n} until takbir.",
        "bye": "{n} until takbir. May Allah accept your prayer!", "bye_late": "May Allah accept your prayer!",
    },
}


@dataclass
class Move:
    """Что делать с его словами: сказать (say), ещё нужна оценка ответа моделью (grade), подтвердить подъём (confirm) или отложить (snooze)."""
    say: str = ""
    grade: str = ""        # "quiz" | "final" — нужна оценка ответа моделью (flow.answer / flow.final_answer)
    confirm: bool = False
    snooze: int = 0        # минут


@dataclass
class WakeFlow:
    lang: str = "ru"
    title: str = "сэр"
    quiz: list[islam_quiz.Q] = field(default_factory=list)   # сегодняшние вопросы (не больше, чем успеем до такбира)
    takbir_at: datetime | None = None
    tz: Any = None                        # часовой пояс для «который час»
    attempt: int = 1                      # какой это звонок за утро
    review: frozenset[str] = frozenset()  # вопросы-повторения вчерашних ошибок
    idx: int = 0                          # текущий вопрос (с него начинаем: повторный звонок продолжает)
    stage: str = "greet"                  # greet | quiz | final | done
    heard_user: bool = False
    nudges: int = 0                       # паузы подряд без его речи
    total_nudges: int = 0
    hear_used: bool = False               # «вы меня слышите?» — не чаще раза за звонок
    hurries: int = 0                      # сколько раз он торопил фразой «встал, отключайся»
    sleep_asks: int = 0                   # сколько раз просил ещё поспать
    said: int = 0                         # сколько реакций уже сказано (чтобы фразы чередовались)
    now: Callable[[], datetime] | None = None
    on_result: Callable[[str, bool], None] | None = None   # (id вопроса, верно ли)
    on_progress: Callable[[int], None] | None = None       # сколько вопросов пройдено

    def __post_init__(self) -> None:
        self.lang = self.lang if self.lang in LANGS else "ru"
        self.idx = max(0, min(self.idx, len(self.quiz)))

    # --- основа
    @property
    def T(self) -> dict:  # noqa: N802
        return _T[self.lang]

    def t(self, key: str, **kw: Any) -> str:
        """Фраза с обращением: {t} — «сэр» (внутри предложения), {T} — «Сэр» (в начале)."""
        return self.T[key].format(t=self.title, T=self.title[:1].upper() + self.title[1:], **kw)

    def pick(self, key: str) -> str:
        """Фраза из набора (верно: «Верно» / «Правильно» / «Молодец») — по очереди, без случайности между перезапусками."""
        self.said += 1
        options = self.T[key]
        return options[self.said % len(options)].format(t=self.title, T=self.title[:1].upper() + self.title[1:])

    def current(self) -> islam_quiz.Q | None:
        return self.quiz[self.idx] if self.stage in {"greet", "quiz"} and self.idx < len(self.quiz) else None

    def greeting(self) -> str:
        return self.t("greet")

    def _now(self) -> datetime:
        now = self.now() if self.now else datetime.now(timezone.utc)
        tz = self.tz or (self.takbir_at.tzinfo if self.takbir_at else None)
        return now.astimezone(tz) if tz else now

    def minutes_left(self) -> int | None:
        if self.takbir_at is None:
            return None
        return int((self.takbir_at - self._now()).total_seconds() // 60)

    def time_text(self) -> str:
        """«Сейчас 05:12, до такбира 17 минут.» — по часам, а не по догадке модели."""
        left, h = self.minutes_left(), f"{self._now():%H:%M}"
        if left is not None and left > 0:
            return self.t("time_left", h=h, n=minutes_text(left, self.lang))
        return self.t("time", h=h)

    def _left_text(self) -> str:
        left = self.minutes_left()
        return self.t("left", n=minutes_text(left, self.lang)) if left is not None and left > 0 else ""

    def _motivation(self, k: int = 0) -> str:
        phrases = MOTIVATION.get(self.lang, MOTIVATION["ru"])
        return phrases[(self.attempt + self.total_nudges + k) % len(phrases)]

    def _ordinal(self, i: int) -> str:
        n = len(self.quiz)
        if n == 1:
            return self.T["one"]
        return self.T["last"] if i == n - 1 else self.T["ord"][min(i, len(self.T["ord"]) - 1)]

    def _ask_text(self, i: int, *, wake: bool = False) -> str:
        q = self.quiz[i]
        body = self.t("ask_wake" if wake else "ask", o=self._ordinal(i), q=q.q)
        return (self.T["review"] if q.id in self.review else "") + body

    # --- вопросы
    def _begin_questions(self, *, silent: bool = True) -> str:
        """После приветствия (он ответил или молчит) — вопрос дня, а если вопросов не осталось, сразу «встали ли»."""
        if self.idx < len(self.quiz):
            self.stage = "quiz"
            return self._ask_text(self.idx, wake=silent)
        self.stage = "final"
        return self.t("final" if silent else "final_q")

    def _again(self) -> str:
        """Что мы ждём от него сейчас — сказать заново (повтор вопроса / «вы встали?»); на приветствии — сам первый вопрос."""
        if self.stage == "greet":
            return self._begin_questions(silent=False)
        if self.stage == "final":
            return self.t("final")
        return self.t("repeat", q=self.quiz[self.idx].q)

    # --- тишина
    def silence_limit(self) -> float:
        """Сколько секунд тишины ждём до следующей реплики. Сразу после приветствия — коротко (он сонный: быстрее вопрос);
        уже разговаривал — дольше (мог просто думать над ответом)."""
        base = 6.0 if self.stage == "greet" else (9.0 if self.nudges == 0 else 11.0)
        return base * (1.4 if self.heard_user else 1.0)

    def nudge(self) -> str:
        """Реплика в тишину. «Вы меня слышите?» — максимум один раз за звонок, остальное — вопрос, мотивация, время до такбира."""
        k = self.nudges
        self.nudges += 1
        self.total_nudges += 1
        if self.stage == "greet":
            self.nudges = 0   # первая реплика в тишину — сам вопрос; лестница (повтор → мотивация → …) начинается после него
            return self._begin_questions()
        if k == 0:
            left = self._left_text() if self.attempt >= 3 else ""   # третий звонок за утро — сразу про время
            return f"{left} {self._again()}".strip()
        if k == 1:
            tail = self.t("final_again") if self.stage == "final" else self.t("dunno")
            return f"{self._motivation(k)} {tail}"
        if k == 2 and not self.hear_used:
            self.hear_used = True
            return self.t("hear")
        if k % 2 == 1:
            return f"{self._left_text()} {self._motivation(k)}".strip()
        return self._again()

    def hear_reply(self) -> str:
        """Он сам спросил «алло / вы меня слышите»: подтверждаем и возвращаем к делу — это не считается нашим «слышите ли»."""
        self.heard_user = True
        self.nudges = 0
        return f"{self.t('hear_yes')} {self._again()}".strip()

    # --- его слова
    def route(self, heard: str) -> Move:
        """Разбор его слов без модели: «алло», «который час», «встал, отключайся», «ещё поспать», ответ на приветствие.
        Move.grade — дальше нужна оценка ответа моделью."""
        self.heard_user = True
        self.nudges = 0
        t = _norm(heard)
        if self.stage == "done":
            return Move()
        if _HEAR_RE.search(t):
            return Move(say=self.hear_reply())
        if _TIME_RE.search(t):
            return Move(say=f"{self.time_text()} {self._again()}".strip())
        if self.stage in {"greet", "quiz"} and is_hurry(heard):
            return self.hurry()
        if _SLEEP_ASK_RE.search(t) or (self.stage == "greet" and _NOT_UP_RE.search(t)):
            return self.sleep_ask(heard)
        if self.stage == "greet":
            return Move(say=f"{self.pick('react')} {self._begin_questions(silent=False)}")
        return Move(grade="quiz" if self.stage == "quiz" else "final")

    def hurry(self) -> Move:
        """Он торопит («встал, отключайся»), а вопросы ещё не закончились. Первый раз — «Ещё минуту, осталось N вопроса» и дальше по
        вопросам; настаивает второй раз — отпускаем (подтверждаем подъём)."""
        self.hurries += 1
        if self.hurries >= 2:
            self.stage = "done"
            return Move(say=self.closing(), confirm=True)
        lead = self.t("wait", k=questions_text(max(1, len(self.quiz) - self.idx), self.lang))
        return Move(say=f"{lead} {self._again()}".strip())

    def sleep_ask(self, heard: str) -> Move:
        """«Ещё поспать»: первый раз — «намаз лучше сна» и вопрос; второй раз — откладываем звонок (не больше SNOOZE_MAX_MIN минут)."""
        self.sleep_asks += 1
        if self.sleep_asks >= 2:
            minutes = max(1, min(SNOOZE_MAX_MIN, wake_mod.snooze_minutes(heard) or SNOOZE_MAX_MIN))
            return Move(say=self.t("snooze_ok", m=minutes_text(minutes, self.lang)), snooze=minutes)
        return Move(say=f"{self.t('persuade')} {self._again()}".strip())

    def _right_answer(self, q: islam_quiz.Q) -> str:
        said = self.t("right", a=q.a)
        return f"{said} {self.T['arabic'].format(ar=q.ar)}" if q.ar else said

    def answer(self, verdict: str) -> Move:
        """Оценка ответа на текущий вопрос (correct / wrong / unknown / other; пусто — оценщик не ответил). Этап двигается здесь."""
        q = self.current()
        if q is None:
            return Move()
        if verdict == "other":
            return Move(say=f"{self.t('unclear')} {self._again()}")   # не ответ — тот же вопрос, ничего не засчитываем
        if verdict in {"correct", "wrong", "unknown"} and self.on_result:
            self.on_result(q.id, verdict == "correct")
        if verdict == "correct":
            reaction = self.pick("correct")
        elif verdict == "wrong":
            reaction = f"{self.t('wrong')} {self._right_answer(q)}"
        elif verdict == "unknown":
            reaction = f"{self.t('unknown')} {self._right_answer(q)}"
        else:   # оценщик недоступен: правильный ответ всё равно звучит, а ошибкой не считается
            reaction = f"{self.T['unsure']} {self._right_answer(q)}"
        self.idx += 1
        if self.on_progress:
            self.on_progress(self.idx)
        if self.idx < len(self.quiz):
            follow = self._ask_text(self.idx)
        else:
            self.stage = "final"
            follow = self.t("final_q")
        return Move(say=f"{reaction} {follow}")

    def final_answer(self, verdict: str, heard: str = "") -> Move:
        """Оценка ответа на «вы встали?» (up / not_yet / other; пусто — оценщик недоступен: тогда только ясное «встал» по словам)."""
        if verdict == "up" or (not verdict and heard_means_up(heard)):
            self.stage = "done"
            return Move(say=self.closing(), confirm=True)
        return Move(say=self.t("final_again"))

    # --- подтверждение подъёма
    def closing(self) -> str:
        return f"{self.t('great')} {self.farewell()}"

    def farewell(self) -> str:
        left = self.minutes_left()
        if left is not None and left > 0:
            return self.t("bye", n=minutes_text(left, self.lang))
        return self.t("bye_late")


def build(*, lang: str, title: str, day_quiz: list[islam_quiz.Q], minutes_left: int | None, takbir_at: datetime | None = None, tz: Any = None,
          attempt: int = 1, done: int = 0, review_ids: frozenset[str] = frozenset(),
          on_result: Callable[[str, bool], None] | None = None, on_progress: Callable[[int], None] | None = None) -> WakeFlow:
    """Поток для этого звонка: сколько вопросов успеем и с какого начать (done — сколько он уже прошёл в прошлых звонках утра)."""
    quiz = list(day_quiz)[:questions_for(minutes_left, len(day_quiz) or islam_quiz.QUESTIONS_PER_DAY)]
    return WakeFlow(lang=lang, title=title, quiz=quiz, takbir_at=takbir_at, tz=tz, attempt=attempt, review=frozenset(review_ids),
                    idx=done, on_result=on_result, on_progress=on_progress)


__all__ = ["WakeFlow", "Move", "build", "questions_for", "minutes_text", "questions_text", "quiz_prompt", "final_prompt", "parse_verdict",
           "heard_means_up", "is_hurry", "has_words", "QUIZ_VERDICTS", "FINAL_VERDICTS"]
