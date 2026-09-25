from spam_filter import Features, score_message

BAN, SUSPECT = 5, 3

SPAM = [
    "Ищу людей на удаленку, от 1000$ в день, пиши в лс",
    "Нужны люди, без опыта, 15 тыс в день. Подробности в лс",
    "Отправлю бесплатно всем желающим, ссылка в профиле",
    "Скучно одинокой девушке, мои фото в профиле 18+",
    "Заработок на крипте, пассивный доход, t.me/+AbCdEf123",
    "Рабoта на дoму, пиши +в лс",               # латинские o
    "Забирай бесплатный курс по трейдингу https://bit.ly/x",
]
HAM = [
    "Отличный пост, спасибо!",
    "А я вчера скачал этот фильм, так себе",
    "Не согласен, по-моему автор преувеличивает",
    "Сколько это стоит в день в отеле?",
    "Работаю в IT уже 10 лет, подтверждаю",
]


def test_spam_banned_on_first_message():
    for t in SPAM:
        v = score_message(Features(text=t, first_message=True))
        assert v.score >= BAN, (t, v)


def test_ham_passes():
    for t in HAM:
        v = score_message(Features(text=t, first_message=True))
        assert v.score < SUSPECT, (t, v)


def test_inline_keyboard_is_ban():
    assert score_message(Features(text="привет", has_inline_keyboard=True)).score >= BAN


def test_spam_name_and_link():
    v = score_message(Features(text="смотри https://x.ru", display_name="Заработок онлайн", first_message=True))
    assert v.score >= BAN, v


def test_extra_words():
    v = score_message(Features(text="Купи слона", first_message=True), extra_words=["слона"])
    assert v.score >= SUSPECT, v


def test_example_similarity():
    ex = ["Добрый день! Ищу двух человек в команду, обучение бесплатно, всё расскажу в личных сообщениях"]
    variant = "Всем привет! Ищу двух человек в команду, обучение бесплатное, расскажу всё в личных сообщениях"
    assert score_message(Features(text=variant), examples=ex).score >= BAN
    assert score_message(Features(text="Отличный пост, спасибо автору"), examples=ex).score < SUSPECT
