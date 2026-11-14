
import os
import sqlite3
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DB = "settings.db"


def init_db():
    with sqlite3.connect(DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                min_price REAL DEFAULT 0.30,
                max_price REAL DEFAULT 0.50,
                min_amount INTEGER DEFAULT 100,
                max_amount INTEGER DEFAULT 10000
            )
        """)


def get_settings(user_id):
    with sqlite3.connect(DB) as con:
        con.execute(
            "INSERT OR IGNORE INTO settings (user_id) VALUES (?)",
            (user_id,)
        )
        return con.execute(
            "SELECT min_price, max_price, min_amount, max_amount "
            "FROM settings WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def is_owner(update):
    return update.effective_user.id == OWNER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    await update.message.reply_text(
        "Привет! Я бот для настройки поиска голды Standoff 2.\n\n"
        "/price 0.30 0.50 — диапазон цены за 1 голду\n"
        "/amount 100 10000 — диапазон количества\n"
        "/status — показать настройки"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    try:
        low, high = map(float, context.args)

        if low <= 0 or high < low:
            raise ValueError

        with sqlite3.connect(DB) as con:
            con.execute(
                "UPDATE settings SET min_price=?, max_price=? WHERE user_id=?",
                (low, high, OWNER_ID)
            )

        await update.message.reply_text(
            f"Диапазон цены сохранён: {low:.2f}–{high:.2f} ₽"
        )
    except (ValueError, TypeError):
        await update.message.reply_text(
            "Напиши так: /price 0.30 0.50"
        )


async def amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    try:
        low, high = map(int, context.args)

        if low < 1 or high < low:
            raise ValueError

        with sqlite3.connect(DB) as con:
            con.execute(
                "UPDATE settings SET min_amount=?, max_amount=? WHERE user_id=?",
                (low, high, OWNER_ID)
            )

        await update.message.reply_text(
            f"Диапазон количества сохранён: {low}–{high}"
        )
    except (ValueError, TypeError):
        await update.message.reply_text(
            "Напиши так: /amount 100 10000"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    settings = get_settings(OWNER_ID)
    low_price, high_price, low_amount, high_amount = settings

    await update.message.reply_text(
        "Текущие настройки:\n"
        f"Цена: {low_price:.2f}–{high_price:.2f} ₽\n"
        f"Количество: {low_amount}–{high_amount} голды"
    )


def main():
    if not BOT_TOKEN or not OWNER_ID:
        raise RuntimeError("Не заданы BOT_TOKEN и OWNER_ID")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("amount", amount))
    app.add_handler(CommandHandler("status", status))

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
