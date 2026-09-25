"""Настройка .env: спрашивает токен скрыто, проверяет его и права бота, пишет .env.
Токен никуда не выводится и уходит только в api.telegram.org."""
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CHANNEL_ID = "-1001901682519"
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")


def api(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)
    except urllib.error.URLError as e:
        sys.exit(f"Нет связи с api.telegram.org: {e.reason}. Возможно, Telegram закрыт в этой сети.")


def main():
    owner_id = input("Твой числовой Telegram id (узнать у @userinfobot): ").strip()
    if not owner_id.isdigit():
        sys.exit("id должен состоять из цифр")
    print("Вставь токен от BotFather (ввод скрыт: вставь правой кнопкой мыши или Ctrl+V и нажми Enter)")
    token = getpass.getpass("Токен: ").strip()
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        sys.exit("Это не похоже на токен. Формат: 123456789:AAE...")

    me = api(token, "getMe")
    if not me.get("ok"):
        sys.exit(f"Telegram не принял токен: {me.get('description')}")
    bot = me["result"]
    print(f"OK, бот @{bot['username']}")

    group_id = ""
    ch = api(token, "getChat", chat_id=CHANNEL_ID)
    if ch.get("ok"):
        print(f"Канал: {ch['result'].get('title')}")
        group_id = str(ch["result"].get("linked_chat_id") or "")
        if not group_id:
            print("! У канала не найдена группа обсуждения (комментарии включены?)")
    else:
        print(f"! Бот не видит канал ({ch.get('description')}). Не страшно, если он админ в группе.")
        group_id = input("Введи id группы обсуждения (узнать: /chatid в группе после запуска) или Enter, чтобы пропустить: ").strip()

    if group_id:
        m = api(token, "getChatMember", chat_id=group_id, user_id=bot["id"])
        if not m.get("ok"):
            print(f"! Не смог проверить бота в группе {group_id}: {m.get('description')}")
        else:
            r = m["result"]
            if r["status"] != "administrator":
                print(f"! В группе обсуждения бот не админ (статус: {r['status']}). Банить не сможет.")
            else:
                miss = [n for k, n in (("can_delete_messages", "удаление сообщений"),
                                       ("can_restrict_members", "блокировка пользователей")) if not r.get(k)]
                print("! Не хватает прав: " + ", ".join(miss) if miss else "OK, бот админ группы обсуждения, права есть")

    if os.path.exists(ENV_PATH) and input(".env уже есть. Перезаписать? (y/n): ").strip().lower() != "y":
        sys.exit("Оставил старый .env")
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write(f"""BOT_TOKEN={token}
OWNER_ID={owner_id}
CHANNEL_ID={CHANNEL_ID}
DISCUSSION_CHAT_ID={group_id}
DRY_RUN=1
BAN_SCORE=5
SUSPECT_SCORE=3
TRUST_AFTER=3
CAS_ENABLED=1
DB_PATH=antispam.db
""")
    print(f"\n.env записан. Дальше:\n 1) Напиши @{bot['username']} /start в личку\n 2) Запусти: python bot.py")


if __name__ == "__main__":
    main()
