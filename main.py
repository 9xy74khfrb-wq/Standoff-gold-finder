
import os
import json
import math
import asyncio
import sqlite3
import logging
import urllib.request

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


def db():
    return sqlite3.connect(DB)


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                min_price REAL NOT NULL DEFAULT 0.30,
                max_price REAL NOT NULL DEFAULT 0.50,
                min_amount INTEGER NOT NULL DEFAULT 100,
                max_amount INTEGER NOT NULL DEFAULT 10000
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS checked_offers (
                offer_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def owner(update):
    return (
        update.effective_user is not None
        and update.effective_user.id == OWNER_ID
    )


def get_settings():
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO settings (user_id) VALUES (?)",
            (OWNER_ID,)
        )
        return con.execute(
            """SELECT min_price, max_price, min_amount, max_amount
               FROM settings WHERE user_id=?""",
            (OWNER_ID,)
        ).fetchone()


def get_rate():
    url = "https://api.frankfurter.dev/v2/rate/usd/rub"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "GoldPriceChecker/1.0"}
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    rate = float(data["rate"])
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("Invalid exchange rate")

    return rate, data.get("date", "unknown")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update):
        return

    get_settings()

    await update.message.reply_text(
        "Привет! Я бот для проверки цен Standoff 2 Gold.\n\n"
        "Команды:\n"
        "/price 0.30 0.50 — цена в рублях\n"
        "/amount 100 10000 — количество голды\n"
        "/status — текущие настройки\n"
        "/check 0.0081 1309 — проверка цены в USD\n"
        "/help — помощь"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update):
        return

    await update.message.reply_text(
        "Примеры команд:\n\n"
        "/price 20 50\n"
        "/price 0.30 0.50\n"
        "/amount 100 10000\n"
        "/check 0.0081 1309\n\n"
        "В /check сначала указывается цена за одну голду в USD, "
        "затем количество."
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update):
        return

    try:
        if len(context.args) != 2:
            raise ValueError

        low, high = [
            float(x.replace(",", "."))
            for x in context.args
        ]

        if (
            not math.isfinite(low)
            or not math.isfinite(high)
            or low <= 0
            or high < low
        ):
            raise ValueError

        with db() as con:
            con.execute(
                """UPDATE settings
                   SET min_price=?, max_price=?
                   WHERE user_id=?""",
                (low, high, OWNER_ID)
            )

        await update.message.reply_text(
            f"Цена сохранена: {low:g}–{high:g} ₽ за голду."
        )

    except (ValueError, TypeError):
        await update.message.reply_text(
            "Неверный формат.\nПример: /price 20 50"
        )


async def amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update):
        return

    try:
        if len(context.args) != 2:
            raise ValueError

        low, high = map(int, context.args)

        if low < 1 or high < low:
            raise ValueError

        with db() as con:
            con.execute(
                """UPDATE settings
                   SET min_amount=?, max_amount=?
                   WHERE user_id=?""",
                (low, high, OWNER_ID)
            )

        await update.message.reply_text(
            f"Количество сохранено: {low}–{high} голды."
        )

    except (ValueError, TypeError):
        await update.message.reply_text(
            "Неверный формат.\nПример: /amount 100 10000"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update):
        return

    low_p, high_p, low_a, high_a = get_settings()

    await update.message.reply_text(
        "Твои настройки:\n\n"
        f"Цена: {low_p:g}–{high_p:g} ₽ за голду\n"
        f"Количество: {low_a}–{high_a} голды"
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner(update):
        return

    try:
        if len(context.args) != 2:
            raise ValueError

        usd_price = float(context.args[0].replace(",", "."))
        quantity = int(context.args[1])

        if (
            not math.isfinite(usd_price)
            or usd_price <= 0
            or quantity <= 0
        ):
            raise ValueError

        await update.message.reply_text("Получаю курс доллара...")

        rate, date = await asyncio.to_thread(get_rate)

        rub_price = usd_price * rate
        total_rub = rub_price * quantity

        low_p, high_p, low_a, high_a = get_settings()

        price_ok = low_p <= rub_price <= high_p
        amount_ok = low_a <= quantity <= high_a

        result = (
            "Результат проверки:\n\n"
            f"Цена за голду: ${usd_price:.6f}\n"
            f"Цена в рублях: {rub_price:.4f} ₽\n"
            f"Количество: {quantity}\n"
            f"Общая стоимость: {total_rub:.2f} ₽\n\n"
            f"Курс: 1 USD = {rate:.2f} RUB\n"
            f"Дата курса: {date}\n\n"
            f"Цена подходит: {'Да' if price_ok else 'Нет'}\n"
            f"Количество подходит: {'Да' if amount_ok else 'Нет'}\n"
            f"Итог: {'ПОДХОДИТ' if price_ok and amount_ok else 'НЕ ПОДХОДИТ'}"
        )

        await update.message.reply_text(result)

    except (ValueError, TypeError):
        await update.message.reply_text(
            "Используй:\n/check 0.0081 1309"
        )

    except Exception:
        logging.exception("Ошибка проверки")
        await update.message.reply_text(
            "Не удалось получить курс. Попробуй позже."
        )


def main():
    if not BOT_TOKEN or not OWNER_ID:
        raise RuntimeError("Не заданы BOT_TOKEN и OWNER_ID")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("amount", amount))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("check", check))

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
