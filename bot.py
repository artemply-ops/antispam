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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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

    def stats(self, since):
        return dict(self.c.execute("SELECT action, COUNT(*) FROM events WHERE ts>=? GROUP BY action", (since,)))


db = DB(DB_PATH)
state = {"chat_id": DISCUSSION_CHAT_ID, "dry_run": DRY_RUN}
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
    if verdict.score >= BAN_SCORE:
        if state["dry_run"]:
            db.event(chat_id, actor_id, "dry_ban", verdict, f.text)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔨 Забанить", callback_data=f"b:{chat_id}:{actor_id}:{message.message_id}"),
                InlineKeyboardButton(text="✅ Не спам", callback_data=f"ok:{actor_id}")]])
            await notify(bot, report("[тестовый режим] Был бы бан", actor_name, actor_id, verdict, f.text, link), kb)
            return
        try:
            await message.delete()
            await ban_actor(bot, chat_id, actor_id)
        except TelegramBadRequest as e:
            log.error("не смог забанить %s: %s (у бота есть права админа?)", actor_id, e)
            return
        db.event(chat_id, actor_id, "ban", verdict, f.text)
        log.info("бан %s, баллы %s", actor_id, verdict.score)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Разбанить", callback_data=f"u:{chat_id}:{actor_id}")]])
        await notify(bot, report("🔨 Забанен", actor_name, actor_id, verdict, f.text), kb)
    elif verdict.score >= SUSPECT_SCORE:
        db.event(chat_id, actor_id, "suspect", verdict, f.text)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔨 Забанить", callback_data=f"b:{chat_id}:{actor_id}:{message.message_id}"),
            InlineKeyboardButton(text="✅ Не спам", callback_data=f"ok:{actor_id}")]])
        await notify(bot, report("⚠️ Подозрительный комментарий", actor_name, actor_id, verdict, f.text, link), kb)
    elif not message.edit_date:
        db.add_clean(actor_id)


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
        f"Режим: {'ТЕСТОВЫЙ (только сообщаю)' if state['dry_run'] else 'БОЕВОЙ (баню)'}\n"
        f"Порог бана: {BAN_SCORE}, подозрение: {SUSPECT_SCORE}\n\n"
        "/stats — статистика\n/mode test|live — режим\n"
        "/words — свои стоп-слова\n/addword слово — добавить\n/delword слово — удалить\n"
        "/unban id — разбанить\n/trust id — в доверенные\n\n"
        "<b>Образцы спама</b> (похожее банится сразу):\n"
        "перешли мне спам-сообщение или просто пришли его текст — сохраню как образец\n"
        "/examples — список, /delexample N — удалить\n\n"
        "В группе: ответь <code>/spam</code> на сообщение, чтобы удалить, забанить и запомнить как образец.")


@router.message(Command("stats"), owner, F.from_user.id == OWNER_ID)
async def cmd_stats(message: Message):
    names = {"ban": "Забанено авто", "manual_ban": "Забанено вручную", "suspect": "Подозрительных",
             "dry_ban": "Был бы бан (тест)", "unban": "Разбанено"}
    out = []
    for title, since in (("24 часа", time.time() - 86400), ("7 дней", time.time() - 7 * 86400), ("всё время", 0)):
        s = db.stats(int(since))
        out.append(f"<b>{title}</b>: " + (", ".join(f"{names.get(k, k)} {v}" for k, v in s.items()) or "пусто"))
    await message.answer("\n".join(out))


@router.message(Command("mode"), owner, F.from_user.id == OWNER_ID)
async def cmd_mode(message: Message, command: CommandObject):
    if command.args in ("test", "live"):
        state["dry_run"] = command.args == "test"
    await message.answer("Режим: " + ("тестовый" if state["dry_run"] else "боевой") +
                         "\n(после перезапуска берётся DRY_RUN из .env)")


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
    log.info("чат обсуждения: %s, режим: %s", state["chat_id"], "тест" if state["dry_run"] else "боевой")
    await notify(bot, f"Антиспам запущен. Чат: <code>{state['chat_id']}</code>, "
                      f"режим: {'тестовый' if state['dry_run'] else 'боевой'}. /help")
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
