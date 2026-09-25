"""Оценка сообщения на спам. Чистые функции без Telegram, чтобы их можно было тестировать."""
import re
from dataclasses import dataclass, field

# Латиница, похожая на кириллицу: спамеры пишут «рабoта» с латинской o, чтобы обойти фильтры.
LOOKALIKES = str.maketrans({
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "k": "к", "m": "м", "t": "т", "h": "н", "b": "в", "ё": "е",
})
ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­]")

# (название, вес, паттерны). Категория даёт вес один раз, сколько бы паттернов ни совпало.
CATEGORIES = [
    ("работа/заработок", 3, [
        r"заработ", r"подработ", r"удален\w*\s*(работ|занят)", r"удаленк",
        r"работ\w*\s+(на|из)\s+дом", r"на\s+дому", r"пассивн\w*\s+доход",
        r"дополнительн\w*\s+доход", r"ищу\s+(людей|партнер|сотрудник|помощник|ответствен)",
        r"нужны\s+люди", r"набира\w*\s+(людей|команд)", r"набор\s+в\s+команду",
        r"свободн\w*\s+врем", r"без\s+опыта", r"\bвакансия\b",
    ]),
    ("обещание денег", 3, [
        r"\d[\d\s.,]*\s*(\$|₽|руб\w*|р\.|тыс\w*|k|к|usdt?|евро|€)\s*(в|за|/)\s*(день|сутки|неделю|недел|час|месяц|мес)",
        r"\bот\s*\d[\d\s]*\s*(\$|₽|руб|тыс|usdt?|€)",
    ]),
    ("зовёт в личку", 3, [
        r"(пиши|напиши|пишите|напишите|стучи|стучите|жду)\w*\s+(мне\s+)?(в\s+)?(лс|личк|личные|директ|пм)",
        r"\+\s*в\s*лс", r"\bв\s+лс\b", r"подробност\w*\s+в\s+(лс|личк|профил|био|канал)",
        r"(ссылк\w*|фото|видео|все)\s+(в|у\s+меня\s+в)\s+(профил|био|описан)",
        r"(загляни|заходи|смотри|переходи)\w*\s+(в|ко\s+мне\s+в)\s+(профил|канал|био)",
    ]),
    ("скачать/забрать", 3, [
        r"скача\w*\s+(можно\s+)?(тут|здесь|бесплатн|по\s+ссылк|в\s+(профил|био|канал))",
        r"забира(й|йте)\b", r"заберит", r"(отправлю|скину)\s+(тебе|вам|бесплатно|всем)",
        r"бесплатн\w*\s+(курс|доступ|подписк|гайд|файл|верси|бот)",
        r"(получи|получите)\s+(бонус|подарок|доступ|выплат)", r"промокод",
        r"\bжми\b", r"переходи\s+по\s+ссылк",
    ]),
    ("крипта/ставки", 2, [
        r"крипт", r"usdt", r"арбитраж", r"\bp2p\b", r"ставк\w*\s+на\s+спорт", r"казино",
        r"букмекер", r"трейдинг", r"airdrop", r"эйрдроп", r"сигнал\w*\s+(на|по)\s",
    ]),
    ("18+/знакомства", 3, [
        r"\b18\s*\+", r"интим", r"эрот", r"знакомств", r"одинок\w*\s+девушк",
        r"скучно\w*\s+девушк", r"(мои|мое)\s+(фото|видео)\s+в\s+(профил|био)",
    ]),
]
COMPILED = [(name, w, [re.compile(p) for p in pats]) for name, w, pats in CATEGORIES]

LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|tg://)", re.I)
INVITE_RE = re.compile(r"(t\.me|telegram\.me)/(\+|joinchat)", re.I)
MENTION_RE = re.compile(r"(?<!\w)@[a-z0-9_]{4,}", re.I)
WORD_RE = re.compile(r"\w+")


@dataclass
class Features:
    text: str = ""
    display_name: str = ""
    first_message: bool = False
    has_inline_keyboard: bool = False
    is_forward: bool = False
    via_bot: bool = False
    foreign_sender_chat: bool = False
    link_entities: int = 0
    mention_entities: int = 0
    custom_emoji: int = 0


@dataclass
class Verdict:
    score: int = 0
    reasons: list = field(default_factory=list)

    def add(self, points: int, reason: str):
        self.score += points
        self.reasons.append(f"{reason} (+{points})")


def normalize(text: str) -> str:
    return ZERO_WIDTH.sub("", text.lower()).translate(LOOKALIKES)


def has_mixed_script(text: str) -> bool:
    for w in WORD_RE.findall(ZERO_WIDTH.sub("", text.lower())):
        if len(w) >= 3 and re.search(r"[а-яё]", w) and re.search(r"[a-z]", w):
            return True
    return False


def match_categories(text: str) -> list:
    variants = (ZERO_WIDTH.sub("", text.lower()), normalize(text))
    return [(name, w) for name, w, pats in COMPILED
            if any(p.search(v) for p in pats for v in variants)]


def word_set(text: str) -> set:
    return {w for w in WORD_RE.findall(normalize(text)) if len(w) >= 3}


def similarity(a: str, b: str) -> float:
    """Доля общих слов (Жаккар). Порядок слов и мелкие правки не важны."""
    wa, wb = word_set(a), word_set(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


EXAMPLE_THRESHOLD = 0.6


def score_message(f: Features, extra_words=(), examples=()) -> Verdict:
    v = Verdict()
    best = max((similarity(f.text, e) for e in examples), default=0.0)
    if best >= EXAMPLE_THRESHOLD:
        v.add(10, f"похоже на образец спама ({best:.0%})")
    for name, w in match_categories(f.text):
        v.add(w, name)

    norm = normalize(f.text)
    hits = [w for w in extra_words if normalize(w) in norm]
    if hits:
        v.add(3, "стоп-слово: " + ", ".join(hits[:3]))

    if has_mixed_script(f.text):
        v.add(3, "смесь латиницы и кириллицы в слове")

    if INVITE_RE.search(f.text):
        v.add(3, "инвайт-ссылка в чат/канал")
    elif f.link_entities or LINK_RE.search(f.text):
        v.add(2, "ссылка")
    if f.mention_entities or MENTION_RE.search(f.text):
        v.add(2, "упоминание @")

    if f.has_inline_keyboard:
        v.add(5, "кнопки под сообщением")
    if f.is_forward:
        v.add(2, "пересланное сообщение")
    if f.via_bot:
        v.add(2, "отправлено через инлайн-бота")
    if f.foreign_sender_chat:
        v.add(2, "пишет от имени чужого канала")
    if f.custom_emoji >= 3:
        v.add(1, "много премиум-эмодзи")
    if f.display_name and match_categories(f.display_name):
        v.add(2, "спам в имени профиля")
    if f.first_message and v.score > 0:
        v.add(1, "первое сообщение в чате")
    return v
