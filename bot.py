import asyncio
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime

import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5 минут по умолчанию
DB_PATH = os.getenv("DB_PATH", "rss_bot.db")
FEED_PROXY = os.getenv("FEED_PROXY")  # напр. http://login:pass@host:port — только для запросов к RSS-лентам

FEED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# Хендлеры для feedparser: если задан FEED_PROXY, все запросы к лентам пойдут через него.
# На соединение с Telegram (через aiohttp/aiogram) это не влияет — они не связаны.
FEED_HANDLERS = []
if FEED_PROXY:
    FEED_HANDLERS.append(urllib.request.ProxyHandler({"http": FEED_PROXY, "https": FEED_PROXY}))
    logging.info("Запросы к RSS-лентам будут идти через прокси")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- База данных ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            target TEXT NOT NULL,
            UNIQUE(url, target)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_entries (
            feed_id INTEGER NOT NULL,
            entry_id TEXT NOT NULL,
            PRIMARY KEY (feed_id, entry_id)
        )
    """)
    conn.commit()
    conn.close()


def add_feed(chat_id: int, url: str, target: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO feeds (chat_id, url, target) VALUES (?, ?, ?)", (chat_id, url, target))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_feed(chat_id: int, url: str, target: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM feeds WHERE chat_id = ? AND url = ? AND target = ?", (chat_id, url, target))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def list_feeds(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url, target FROM feeds WHERE chat_id = ?", (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def all_feeds():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, url, target FROM feeds")
    rows = cur.fetchall()
    conn.close()
    return rows


def is_entry_seen(feed_id: int, entry_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen_entries WHERE feed_id = ? AND entry_id = ?", (feed_id, entry_id))
    result = cur.fetchone() is not None
    conn.close()
    return result


def mark_entry_seen(feed_id: int, entry_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO seen_entries (feed_id, entry_id) VALUES (?, ?)", (feed_id, entry_id))
    conn.commit()
    conn.close()


def get_feed_id(url: str, target: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM feeds WHERE url = ? AND target = ?", (url, target))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def mark_all_current_entries_seen(feed_id: int, parsed_feed):
    """Помечает все записи уже спарсенной ленты как виденные, чтобы при подписке
    не улетел весь бэклог, а только новые записи, появившиеся после /add."""
    for entry in parsed_feed.entries:
        entry_id = entry.get("id") or entry.get("link")
        if entry_id:
            mark_entry_seen(feed_id, entry_id)


# ---------- Команды ----------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я RSS-бот.\n\n"
        "Команды:\n"
        "/add <url> — подписаться, новости придут сюда\n"
        "/add <url> <@канал_или_id> — новости будут публиковаться в указанный канал\n"
        "/addmany [@канал_или_id] — добавить сразу несколько лент (каждая с новой строки)\n"
        "/list — список подписок\n"
        "/remove <url> [@канал_или_id] — отписаться\n"
    )


@dp.message(Command("add"))
async def cmd_add(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /add <url> [@канал_или_id]")
        return

    url = parts[1].strip()
    target = parts[2].strip() if len(parts) > 2 else str(message.chat.id)

    try:
        parsed = feedparser.parse(url, request_headers=FEED_HEADERS, handlers=FEED_HANDLERS)
    except Exception as e:
        logging.warning(f"Ошибка парсинга URL {url!r}: {e}")
        await message.answer("Некорректная ссылка на RSS. Проверь, что скопировал только сам URL, без лишних символов.")
        return

    if parsed.bozo and not parsed.entries:
        await message.answer("Не удалось распознать RSS/Atom по этой ссылке. Проверь URL.")
        return

    # Проверяем, что бот реально может писать в target (например, добавлен ли он в канал)
    try:
        await bot.get_chat(target)
    except Exception:
        await message.answer(
            f"Не могу найти чат/канал {target}. Проверь, что бот добавлен туда админом с правом публикации."
        )
        return

    ok = add_feed(message.chat.id, url, target)
    if ok:
        feed_id = get_feed_id(url, target)
        if feed_id is not None:
            mark_all_current_entries_seen(feed_id, parsed)
        where = "сюда" if target == str(message.chat.id) else f"в {target}"
        await message.answer(f"Подписка добавлена: {url}\nНовости будут приходить {where}")
    else:
        await message.answer("Эта лента уже добавлена.")


@dp.message(Command("addmany"))
async def cmd_addmany(message: Message):
    lines = [line.strip() for line in message.text.split("\n") if line.strip()]
    if len(lines) < 2:
        await message.answer(
            "Использование:\n"
            "/addmany [@канал_или_id]\n"
            "https://url1.xml\n"
            "https://url2.xml\n"
            "...\n\n"
            "Первая строка — команда и, опционально, общий канал для всех лент.\n"
            "Каждая следующая строка — отдельная ссылка на RSS."
        )
        return

    # Первая строка: "/addmany" или "/addmany @канал_или_id"
    first_line_parts = lines[0].split(maxsplit=1)
    target = first_line_parts[1].strip() if len(first_line_parts) > 1 else str(message.chat.id)
    urls = lines[1:]

    # Проверяем доступ к target один раз, а не на каждую ленту
    try:
        await bot.get_chat(target)
    except Exception:
        await message.answer(
            f"Не могу найти чат/канал {target}. Проверь, что бот добавлен туда админом с правом публикации."
        )
        return

    added, skipped, failed = [], [], []

    for url in urls:
        try:
            parsed = feedparser.parse(url, request_headers=FEED_HEADERS, handlers=FEED_HANDLERS)
        except Exception as e:
            logging.warning(f"Ошибка парсинга URL {url!r}: {e}")
            failed.append(url)
            continue

        if parsed.bozo and not parsed.entries:
            failed.append(url)
            continue

        ok = add_feed(message.chat.id, url, target)
        if not ok:
            skipped.append(url)
            continue

        feed_id = get_feed_id(url, target)
        if feed_id is not None:
            mark_all_current_entries_seen(feed_id, parsed)
        added.append(url)

        await asyncio.sleep(0.3)  # не долбим все сайты одновременно

    where = "сюда" if target == str(message.chat.id) else f"в {target}"
    lines_out = [f"Готово, новости будут приходить {where}."]
    if added:
        lines_out.append(f"\n✅ Добавлено ({len(added)}):\n" + "\n".join(f"• {u}" for u in added))
    if skipped:
        lines_out.append(f"\n⏭ Уже было добавлено ({len(skipped)}):\n" + "\n".join(f"• {u}" for u in skipped))
    if failed:
        lines_out.append(f"\n⚠️ Не удалось распознать ({len(failed)}):\n" + "\n".join(f"• {u}" for u in failed))

    await message.answer("\n".join(lines_out))


@dp.message(Command("remove"))
async def cmd_remove(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /remove <url> [@канал_или_id]")
        return

    url = parts[1].strip()
    target = parts[2].strip() if len(parts) > 2 else str(message.chat.id)
    ok = remove_feed(message.chat.id, url, target)
    await message.answer("Отписал." if ok else "Такой подписки не найдено.")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    feeds = list_feeds(message.chat.id)
    if not feeds:
        await message.answer("У тебя пока нет подписок. Добавь через /add <url>")
        return
    lines = []
    for url, target in feeds:
        where = "сюда" if target == str(message.chat.id) else target
        lines.append(f"• {url} → {where}")
    await message.answer("Твои подписки:\n" + "\n".join(lines))


# ---------- Фоновая проверка лент ----------

async def check_feeds_loop():
    while True:
        try:
            for feed_id, url, target in all_feeds():
                try:
                    parsed = feedparser.parse(url, request_headers=FEED_HEADERS, handlers=FEED_HANDLERS)
                except Exception as e:
                    logging.warning(f"Не удалось загрузить ленту {url}: {e}")
                    continue

                for entry in reversed(parsed.entries):  # от старых к новым
                    entry_id = entry.get("id") or entry.get("link")
                    if not entry_id:
                        continue
                    if is_entry_seen(feed_id, entry_id):
                        continue

                    title = entry.get("title", "Без заголовка")
                    link = entry.get("link", "")
                    text = f"<b>{title}</b>\n{link}"

                    try:
                        await bot.send_message(target, text, parse_mode="HTML", disable_web_page_preview=False)
                    except Exception as e:
                        logging.warning(f"Не удалось отправить сообщение в {target}: {e}")

                    mark_entry_seen(feed_id, entry_id)
        except Exception as e:
            logging.error(f"Ошибка при проверке лент: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


async def main():
    init_db()
    asyncio.create_task(check_feeds_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN (переменная окружения)")
    asyncio.run(main())
