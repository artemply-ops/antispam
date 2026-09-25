"""Антиспам-бот для комментариев канала «За гранью»."""
import asyncio
import html
import logging
import os
import sqlite3
import time

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

from spam_filter import Features, Verdict, score_message, word_set

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001901682519"))
DISCUSSION_CHAT_ID = int(os.getenv("DISCUSSION_CHAT_ID") or 0)
BAN_SCORE = int(os.getenv("BAN_SCORE", "5"))
SUSPECT_SCORE = int(os.getenv("SUSPECT_SCORE", "3"))
TRUST_AFTER = int(os.getenv("TRUST_AFTER", "3"))
CAS_ENABLED = os.getenv("CAS_ENABLED", "1") == "1"
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
DB_PATH = os.getenv("DB_PATH", "antispam.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("antispam")
router = Router()


# ---------- хранилище ----------
class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, clean INTEGER DEFAULT 0, trusted INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS words(word TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS examples(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS events(ts INTEGER, chat_id INTEGER, actor_id INTEGER, action TEXT,
                                              score INTEGER, reasons TEXT, text TEXT);
        """)

    def clean_count(self, uid):
        row = self.c.execute("SELECT clean, trusted FROM users WHERE id=?", (uid,)).fetchone()
        return row or (0, 0)

    def add_clean(self, uid):
        self.c.execute("INSERT INTO users(id, clean) VALUES(?, 1) ON CONFLICT(id) DO UPDATE SET clean=clean+1", (uid,))
        self.c.execute("UPDATE users SET trusted=1 WHERE id=? AND clean>=?", (uid, TRUST_AFTER))
        self.c.commit()

    def set_trusted(self, uid, value=True):
        self.c.execute("INSERT INTO users(id, trusted) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET trusted=?",
                       (uid, int(value), int(value)))
        self.c.commit()

    def words(self):
        return [r[0] for r in self.c.execute("SELECT word FROM words ORDER BY word")]

    def add_word(self, w):
        self.c.execute("INSERT OR IGNORE INTO words VALUES(?)", (w.lower(),))
        self.c.commit()

    def del_word(self, w):
        n = self.c.execute("DELETE FROM words WHERE word=?", (w.lower(),)).rowcount
        self.c.commit()
        return n

    def examples(self):
        return self.c.execute("SELECT id, text FROM examples ORDER BY id").fetchall()

    def add_example(self, text):
        cur = self.c.execute("INSERT OR IGNORE INTO examples(text) VALUES(?)", (text,))
        self.c.commit()
        return cur.rowcount

    def del_example(self, ex_id):
        n = self.c.execute("DELETE FROM examples WHERE id=?", (ex_id,)).rowcount
        self.c.commit()
        return n

    def event(self, chat_id, actor_id, action, verdict=None, text=""):
        self.c.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                       (int(time.time()), chat_id, actor_id, action,
                        verdict.score if verdict else 0,
                        "; ".join(verdict.reasons) if verdict else "", text[:500]))
        self.c.commit()

    def get_setting(self, key, default: int) -> int:
        row = self.c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row else default

    def set_setting(self, key, value: int):
        self.c.execute("INSERT INTO settings VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=?", (key, value, value))
        self.c.commit()

    def stats(self, since):
        return dict(self.c.execute("SELECT action, COUNT(*) FROM events WHERE ts>=? GROUP BY action", (since,)))


db = DB(DB_PATH)
state = {"chat_id": DISCUSSION_CHAT_ID}
# Значения по умолчанию; всё, что меняется через /settings, хранится в базе и переживает перезапуск
DEFAULTS = {"autoban": int(not DRY_RUN), "autodelete": int(not DRY_RUN), "notify": 1,
            "ban_score": BAN_SCORE, "suspect_score": SUSPECT_SCORE}


def cfg(key: str) -> int:
    return db.get_setting(key, DEFAULTS[key])


def mode_text() -> str:
    if cfg("autoban"):
        return "автобан ВКЛ"
    return "автобан ВЫКЛ, автоудаление " + ("ВКЛ" if cfg("autodelete") else "ВЫКЛ") + " (решаешь кнопками)"

admin_cache: dict = {}
cas_cache: dict = {}


# ---------- помощники ----------
async def admin_ids(bot: Bot, chat_id: int) -> set:
    ids, ts = admin_cache.get(chat_id, (set(), 0))
    if time.time() - ts > 600:
        try:
            ids = {m.user.id for m in await bot.get_chat_administrators(chat_id)}
            admin_cache[chat_id] = (ids, time.time())
        except TelegramBadRequest as e:
            log.warning("не получил список админов: %s", e)
    return ids | {OWNER_ID}


async def cas_banned(uid: int) -> bool:
    """Combot Anti-Spam: открытая база спамеров Telegram. Отправляется только числовой id."""
    if not CAS_ENABLED or uid < 0:
        return False
    if uid in cas_cache:
        return cas_cache[uid]
    res = False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            async with s.get("https://api.cas.chat/check", params={"user_id": uid}) as r:
                res = bool((await r.json(content_type=None)).get("ok"))
    except Exception as e:
        log.debug("CAS недоступен: %s", e)
    cas_cache[uid] = res
    return res


async def ban_actor(bot: Bot, chat_id: int, actor_id: int):
    if actor_id < 0:  # пишет от имени канала
        await bot.ban_chat_sender_chat(chat_id, actor_id)
    else:
        await bot.ban_chat_member(chat_id, actor_id, revoke_messages=True)


async def unban_actor(bot: Bot, chat_id: int, actor_id: int):
    if actor_id < 0:
        await bot.unban_chat_sender_chat(chat_id, actor_id)
    else:
        await bot.unban_chat_member(chat_id, actor_id, only_if_banned=True)


def msg_link(chat_id: int, msg_id: int) -> str:
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{msg_id}"


async def notify(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await bot.send_message(OWNER_ID, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        log.warning("не смог написать владельцу (он нажал /start у бота?): %s", e)


def report(title, name, actor_id, verdict: Verdict, text, link=None) -> str:
    parts = [f"<b>{title}</b>",
             f"Кто: {html.escape(name)} (<code>{actor_id}</code>)",
             f"Баллы: {verdict.score} — " + html.escape("; ".join(verdict.reasons)),
             f"Текст: <i>{html.escape(text[:300]) or '—'}</i>"]
    if link:
        parts.append(f'<a href="{link}">Открыть сообщение</a>')
    return "\n".join(parts)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def decision_kb(chat_id, actor_id, msg_id, deleted: bool) -> InlineKeyboardMarkup:
    row = [btn("🔨 Забанить", f"b:{chat_id}:{actor_id}:{msg_id}")]
    if not deleted:
        row.append(btn("🗑 Удалить", f"d:{chat_id}:{msg_id}"))
    row.append(btn("✅ Не спам", f"ok:{actor_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def features_from(message: Message, actor_name: str, first: bool, foreign_chat: bool) -> Features:
    ents = message.entities or message.caption_entities or []
    return Features(
        text=message.text or message.caption or "",
        display_name=actor_name,
        first_message=first,
        has_inline_keyboard=bool(message.reply_markup and message.reply_markup.inline_keyboard),
        is_forward=message.forward_origin is not None,
        via_bot=message.via_bot is not None,
        foreign_sender_chat=foreign_chat,
        link_entities=sum(e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK) for e in ents),
        mention_entities=sum(e.type in (MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION) for e in ents),
        custom_emoji=sum(e.type == MessageEntityType.CUSTOM_EMOJI for e in ents),
    )


# ---------- команды в группе ----------
@router.message(Command("chatid"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_chatid(message: Message, bot: Bot):
    log.info("/chatid в чате %s «%s»", message.chat.id, message.chat.title)
    if message.from_user and message.from_user.id in await admin_ids(bot, message.chat.id):
        await message.reply(f"ID этого чата: <code>{message.chat.id}</code>")


@router.message(Command("spam"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_spam(message: Message, bot: Bot):
    """Админ отвечает /spam на сообщение: удалить и забанить автора."""
    if not message.from_user or message.from_user.id not in await admin_ids(bot, message.chat.id):
        return
    target = message.reply_to_message
    await message.delete()
    if not target:
        return
    actor_id = target.sender_chat.id if target.sender_chat else target.from_user.id
    await target.delete()
    await ban_actor(bot, message.chat.id, actor_id)
    text = target.text or target.caption or ""
    db.event(message.chat.id, actor_id, "manual_ban", text=text)
    if len(word_set(text)) >= 3:
        db.add_example(text)


# ---------- проверка каждого комментария ----------
@router.message(F.chat.type.in_({"group", "supergroup"}))
@router.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(message: Message, bot: Bot):
    chat_id = message.chat.id
    if state["chat_id"] and chat_id != state["chat_id"]:
        return
    if message.is_automatic_forward:  # пост канала, продублированный в обсуждение
        return
    sc = message.sender_chat
    if sc and sc.id in (CHANNEL_ID, chat_id):
        return
    user = message.from_user
    if not sc and (not user or user.id in await admin_ids(bot, chat_id)):
        return

    actor_id = sc.id if sc else user.id
    actor_name = sc.title if sc else (user.full_name + (f" @{user.username}" if user.username else ""))
    clean, trusted = db.clean_count(actor_id)
    f = features_from(message, actor_name, first=clean == 0, foreign_chat=bool(sc))

    if trusted and not f.has_inline_keyboard:
        return
    verdict = score_message(f, db.words(), [t for _, t in db.examples()])
    if await cas_banned(actor_id):
        verdict.add(10, "в базе спамеров CAS")

    link = msg_link(chat_id, message.message_id)
    msg_id = message.message_id
    if verdict.score >= cfg("ban_score"):
        deleted = banned = False
        errors = []
        if cfg("autodelete") or cfg("autoban"):
            try:
                await message.delete()
                deleted = True
            except TelegramBadRequest as e:
                errors.append(f"удалить: {e.message}")
        if cfg("autoban"):
            try:
                await ban_actor(bot, chat_id, actor_id)
                banned = True
            except TelegramBadRequest as e:
                errors.append(f"забанить: {e.message}")
        action = "ban" if banned else "delete" if deleted else "spam_detected"
        db.event(chat_id, actor_id, action, verdict, f.text)
        log.info("спам от %s, баллы %s, действие %s %s", actor_id, verdict.score, action, errors or "")
        if banned:
            title = "🔨 Спам: удалён, автор забанен"
            kb = InlineKeyboardMarkup(inline_keyboard=[[btn("↩️ Разбанить", f"u:{chat_id}:{actor_id}")]])
        else:
            title = "🗑 Спам удалён, автор НЕ забанен" if deleted else "🚨 Спам! Автобан выключен"
            kb = decision_kb(chat_id, actor_id, msg_id, deleted)
        if errors:
            title += "\n⚠️ Не смог " + "; ".join(errors) + " (проверь права бота)"
        if cfg("notify") or not banned:  # без автобана решение за тобой, поэтому пуш всегда
            await notify(bot, report(title, actor_name, actor_id, verdict, f.text, None if deleted else link), kb)
    elif verdict.score >= cfg("suspect_score"):
        db.event(chat_id, actor_id, "suspect", verdict, f.text)
        await notify(bot, report("⚠️ Подозрительный комментарий", actor_name, actor_id, verdict, f.text, link),
                     decision_kb(chat_id, actor_id, msg_id, deleted=False))
    elif not message.edit_date:
        db.add_clean(actor_id)


# ---------- настройки ----------
TOGGLES = [("autoban", "Автобан"), ("autodelete", "Автоудаление"), ("notify", "Пуш об автобане")]


def settings_view():
    text = ("<b>Настройки антиспама</b>\n\n"
            "<b>Автобан</b> — сразу банить автора спама (заодно удаляются все его сообщения в группе).\n"
            "<b>Автоудаление</b> — сразу удалять спам-сообщение, даже если автобан выключен.\n"
            "<b>Пуш об автобане</b> — присылать отчёт, когда бот забанил сам. "
            "Если автобан выключен, пуш с кнопкой «Забанить» приходит всегда.\n\n"
            f"Спам — от <b>{cfg('ban_score')}</b> баллов, подозрительное — от <b>{cfg('suspect_score')}</b>.")
    rows = [[btn(("✅ " if cfg(k) else "❌ ") + name, f"s:{k}")] for k, name in TOGGLES]
    # Подпись порога отдельной строкой на всю ширину, иначе на телефоне она обрезается
    for key, name in (("ban_score", "🚨 Спам"), ("suspect_score", "⚠️ Подозрение")):
        rows.append([btn(f"{name}: от {cfg(key)} баллов", "s:noop")])
        rows.append([btn("➖ меньше", f"s:{key}:-1"), btn("➕ больше", f"s:{key}:1")])
    rows.append([btn("✔️ Готово", "s:done")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def settings_summary() -> str:
    on = lambda k: "вкл" if cfg(k) else "выкл"
    return ("<b>Настройки сохранены</b>\n"
            f"Автобан: {on('autoban')}, автоудаление: {on('autodelete')}, пуш об автобане: {on('notify')}\n"
            f"Спам от {cfg('ban_score')} баллов, подозрение от {cfg('suspect_score')}\n"
            "Изменить: /settings")


@router.callback_query(F.from_user.id == OWNER_ID, F.data.startswith("s:"))
async def on_settings_button(cb: CallbackQuery):
    _, key, *delta = cb.data.split(":")
    if key == "done":
        await cb.message.edit_text(settings_summary(), reply_markup=None)
        await cb.answer("Готово")
        return
    if key in ("autoban", "autodelete", "notify") and not delta:
        db.set_setting(key, 1 - cfg(key))
    elif key in ("ban_score", "suspect_score") and delta:
        value = min(20, max(1, cfg(key) + int(delta[0])))
        ban = value if key == "ban_score" else cfg("ban_score")
        sus = value if key == "suspect_score" else cfg("suspect_score")
        if sus > ban:
            await cb.answer("Порог подозрения не может быть выше порога спама", show_alert=True)
            return
        db.set_setting(key, value)
    else:
        await cb.answer()
        return
    text, kb = settings_view()
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer("Сохранено")


# ---------- кнопки в уведомлениях ----------
@router.callback_query(F.from_user.id == OWNER_ID)
async def on_button(cb: CallbackQuery, bot: Bot):
    kind, *args = cb.data.split(":")
    try:
        if kind == "b":
            chat_id, actor_id, msg_id = map(int, args)
            try:
                await bot.delete_message(chat_id, msg_id)
            except TelegramBadRequest:
                pass
            await ban_actor(bot, chat_id, actor_id)
            db.event(chat_id, actor_id, "manual_ban")
            done = "🔨 Забанен"
        elif kind == "d":
            chat_id, msg_id = map(int, args)
            await bot.delete_message(chat_id, msg_id)
            db.event(chat_id, 0, "manual_delete")
            done = "🗑 Удалено"
        elif kind == "u":
            chat_id, actor_id = map(int, args)
            await unban_actor(bot, chat_id, actor_id)
            db.set_trusted(actor_id)
            db.event(chat_id, actor_id, "unban")
            done = "↩️ Разбанен и добавлен в доверенные"
        elif kind == "ok":
            db.set_trusted(int(args[0]))
            done = "✅ Добавлен в доверенные"
        else:
            return
    except TelegramBadRequest as e:
        await cb.answer(f"Ошибка: {e.message}", show_alert=True)
        return
    await cb.message.edit_text(cb.message.html_text + f"\n\n<b>{done}</b>", reply_markup=None,
                               disable_web_page_preview=True)
    await cb.answer(done)


# ---------- команды владельца в личке ----------
owner = F.chat.type == "private"


@router.message(Command("start", "help"), owner, F.from_user.id == OWNER_ID)
async def cmd_help(message: Message):
    await message.answer(
        "<b>Антиспам «За гранью»</b>\n"
        f"Чат обсуждения: <code>{state['chat_id'] or 'не найден'}</code>\n"
        f"Режим: {mode_text()}\n\n"
        "/settings — автобан, автоудаление, пуши, пороги\n"
        "/mode live|test — всё включить / всё выключить\n"
        "/stats — статистика\n"
        "/words — свои стоп-слова\n/addword слово — добавить\n/delword слово — удалить\n"
        "/unban id — разбанить\n/trust id — в доверенные\n\n"
        "<b>Образцы спама</b> (похожее считается спамом сразу):\n"
        "перешли мне спам-сообщение или просто пришли его текст — сохраню как образец\n"
        "/examples — список, /delexample N — удалить\n\n"
        "В группе: ответь <code>/spam</code> на сообщение, чтобы удалить, забанить и запомнить как образец.")


@router.message(Command("settings"), owner, F.from_user.id == OWNER_ID)
async def cmd_settings(message: Message):
    text, kb = settings_view()
    await message.answer(text, reply_markup=kb)


@router.message(Command("stats"), owner, F.from_user.id == OWNER_ID)
async def cmd_stats(message: Message):
    names = {"ban": "Забанено авто", "manual_ban": "Забанено вручную", "delete": "Удалено авто без бана",
             "manual_delete": "Удалено вручную", "spam_detected": "Спам без действий",
             "suspect": "Подозрительных", "dry_ban": "Был бы бан (тест)", "unban": "Разбанено"}
    out = []
    for title, since in (("24 часа", time.time() - 86400), ("7 дней", time.time() - 7 * 86400), ("всё время", 0)):
        s = db.stats(int(since))
        out.append(f"<b>{title}</b>: " + (", ".join(f"{names.get(k, k)} {v}" for k, v in s.items()) or "пусто"))
    await message.answer("\n".join(out))


@router.message(Command("mode"), owner, F.from_user.id == OWNER_ID)
async def cmd_mode(message: Message, command: CommandObject):
    if command.args in ("test", "live"):
        for key in ("autoban", "autodelete"):
            db.set_setting(key, int(command.args == "live"))
    await message.answer(f"Режим: {mode_text()}\nТонко — в /settings")


@router.message(Command("words"), owner, F.from_user.id == OWNER_ID)
async def cmd_words(message: Message):
    await message.answer(", ".join(db.words()) or "Своих стоп-слов нет.")


@router.message(Command("addword"), owner, F.from_user.id == OWNER_ID)
async def cmd_addword(message: Message, command: CommandObject):
    if command.args:
        db.add_word(command.args.strip())
        await message.answer(f"Добавлено: {html.escape(command.args.strip())}")


@router.message(Command("delword"), owner, F.from_user.id == OWNER_ID)
async def cmd_delword(message: Message, command: CommandObject):
    n = db.del_word((command.args or "").strip())
    await message.answer("Удалено." if n else "Такого слова нет.")


@router.message(Command("unban", "trust"), owner, F.from_user.id == OWNER_ID)
async def cmd_unban(message: Message, command: CommandObject, bot: Bot):
    try:
        actor_id = int(command.args)
    except (TypeError, ValueError):
        await message.answer("Укажи числовой id, например /unban 123456")
        return
    db.set_trusted(actor_id)
    if command.command == "unban" and state["chat_id"]:
        await unban_actor(bot, state["chat_id"], actor_id)
        db.event(state["chat_id"], actor_id, "unban")
    await message.answer("Готово.")


@router.message(Command("examples"), owner, F.from_user.id == OWNER_ID)
async def cmd_examples(message: Message):
    rows = db.examples()
    if not rows:
        await message.answer("Образцов нет. Перешли мне спам-сообщение.")
        return
    lines = [f"<b>{i}</b>. {html.escape(t[:120])}" for i, t in rows]
    await message.answer(f"Образцов: {len(rows)}\n\n" + "\n".join(lines)[:4000])


@router.message(Command("delexample"), owner, F.from_user.id == OWNER_ID)
async def cmd_delexample(message: Message, command: CommandObject):
    ok = (command.args or "").strip().isdigit() and db.del_example(int(command.args))
    await message.answer("Удалено." if ok else "Укажи номер из /examples")


@router.message(owner, F.from_user.id == OWNER_ID, ~F.text.startswith("/"))
async def on_owner_example(message: Message):
    """Любой текст или пересланное сообщение от владельца в личке становится образцом спама."""
    text = message.text or message.caption or ""
    if len(word_set(text)) < 3:
        await message.answer("Слишком коротко для образца: нужно хотя бы 3 слова. Для отдельных слов есть /addword.")
        return
    added = db.add_example(text)
    await message.answer(f"Сохранил как образец спама. Всего образцов: {len(db.examples())}."
                         if added else "Такой образец уже есть.")


@router.message(F.chat.type == "private")
async def on_stranger(message: Message):
    """Не владелец пишет в личку: подсказать его id (так проще всего узнать OWNER_ID)."""
    u = message.from_user
    log.info("личное сообщение от id=%s @%s", u.id, u.username)
    await message.answer(f"Твой Telegram id: <code>{u.id}</code>\n"
                         "Если ты владелец бота, этот id нужно прописать в OWNER_ID.")


def import_examples_file(path="spam_examples.txt"):
    """Образцы из файла: блоки текста, разделённые строкой ---. Строки с # игнорируются."""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8-sig") as fh:
        body = "\n".join(l for l in fh.read().splitlines() if not l.lstrip().startswith("#"))
    blocks = [b.strip() for b in body.split("\n---") if b.strip()]
    return sum(db.add_example(b) for b in blocks if len(word_set(b)) >= 3)


# ---------- запуск ----------
async def main():
    log.info("импортировано образцов из spam_examples.txt: %s", import_examples_file())
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    me = await bot.get_me()
    if OWNER_ID == me.id:
        log.error("OWNER_ID=%s — это id самого бота. Напиши боту в личку, он ответит твоим id", OWNER_ID)
    if state["chat_id"] >= 0 or state["chat_id"] == CHANNEL_ID:
        if state["chat_id"]:
            log.warning("DISCUSSION_CHAT_ID=%s не похож на группу обсуждения, ищу сам", state["chat_id"])
        state["chat_id"] = 0
    if not state["chat_id"]:
        try:
            state["chat_id"] = (await bot.get_chat(CHANNEL_ID)).linked_chat_id or 0
        except TelegramBadRequest as e:
            log.warning("не смог узнать чат обсуждения канала: %s", e)
    if not state["chat_id"]:
        log.warning("DISCUSSION_CHAT_ID не задан и не найден: проверяю ВСЕ группы, где бот админ. "
                    "Напиши /chatid в группе и пропиши id в .env")
    log.info("чат обсуждения: %s, режим: %s", state["chat_id"], mode_text())
    try:
        await bot.set_my_commands([
            BotCommand(command="settings", description="Автобан, удаление, пуши, пороги"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="mode", description="live — всё вкл, test — всё выкл"),
            BotCommand(command="examples", description="Образцы спама"),
            BotCommand(command="words", description="Стоп-слова"),
            BotCommand(command="help", description="Все команды"),
        ], scope=BotCommandScopeChat(chat_id=OWNER_ID))
    except TelegramBadRequest as e:
        log.warning("не смог выставить меню команд: %s", e)
    await notify(bot, f"Антиспам запущен. Чат: <code>{state['chat_id']}</code>\nРежим: {mode_text()}\n/settings")
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
