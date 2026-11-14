
import os
import sqlite3
import logging
import json
import urllib.request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_NAME = "settings.db"

CATEGORY_URL = "https://funpay.com/chips/140/"


# ---------- БАЗА ДАННЫХ ----------

def init_db():
    with sqlite3.connect(DB_NAME) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                min_price REAL DEFAULT 0.30,
                max_price REAL DEFAULT 0.50,
                min_amount INTEGER DEFAULT 100,
                max_amount INTEGER DEFAULT 10000
            )
        """)


def get_settings(user_id):
    with sqlite3.connect(DB_NAME) as db:
        db.execute(
            "INSERT OR IGNORE INTO settings (user_id) VALUES (?)",
            (user_id,)
        )
        row = db.execute(
            """SELECT min_price, max_price, min_amount, max_amount
               FROM settings WHERE user_id = ?""",
            (user_id,)
        ).fetchone()

    return row


def save_setting(user_id, field, value):
    allowed = {
        "min_price", "max_price",
        "min_amount", "max_amount"
    }

    if field not in allowed:
        return

    with sqlite3.connect(DB_NAME) as db:
        db.execute(
            f"UPDATE settings SET {field} = ? WHERE user_id = ?",
            (value, user_id)
        )


# ---------- ВСПОМОГАТЕЛЬНОЕ ----------

def allowed_user(user_id):
    return user_id == OWNER_ID


def fmt(value):
    return f"{value:.2f}".replace(".", ",")


def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔎 Найти объявления",
            callback_data="search"
        )],
        [InlineKeyboardButton(
            "💰 Изменить диапазон цены",
            callback_data="price"
        )],
        [InlineKeyboardButton(
            "📦 Изменить количество Gold",
            callback_data="amount"
        )],
        [InlineKeyboardButton(
            "⚙️ Мои фильтры",
            callback_data="status"
        )],
    ])


def settings_text(user_id):
    min_p, max_p, min_a, max_a = get_settings(user_id)

    return (
        "⚙️ Твои фильтры:\n\n"
        f"💰 Цена за 1 Gold: {fmt(min_p)}–{fmt(max_p)} ₽\n"
        f"📦 Количество: {min_a}–{max_a} Gold\n\n"
        "Измени настройки кнопками ниже."
    )


# ---------- КУРС USD → RUB ----------

def get_usd_rub():
    url = "https://open.er-api.com/v6/latest/USD"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "GoldFinderBot/1.0"}
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("result") != "success":
        raise ValueError("Не удалось получить курс")

    return float(data["rates"]["RUB"])


def usd_to_rub(price_usd):
    return price_usd * get_usd_rub()


# ---------- КОМАНДЫ ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    get_settings(user_id)

    await update.message.reply_text(
        "🪙 Standoff 2 Gold Finder\n\n"
        "Выбирай действие в меню:",
        reply_markup=menu()
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    await update.message.reply_text(
        settings_text(user_id),
        reply_markup=menu()
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_user(update.effective_user.id):
        return

    context.user_data.pop("setting", None)
    context.user_data.pop("temp_min", None)

    await update.message.reply_text(
        "Настройка отменена.",
        reply_markup=menu()
    )


# ---------- КНОПКИ ----------

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not allowed_user(user_id):
        await query.answer()
        return

    await query.answer()

    if query.data == "status":
        await query.message.reply_text(
            settings_text(user_id),
            reply_markup=menu()
        )

    elif query.data == "price":
        context.user_data["setting"] = "price_min"

        await query.message.reply_text(
            "💰 Введи минимальную цену за 1 Gold в рублях.\n"
            "Например: 0.30\n\n"
            "Для отмены отправь /cancel"
        )

    elif query.data == "amount":
        context.user_data["setting"] = "amount_min"

        await query.message.reply_text(
            "📦 Введи минимальное количество Gold.\n"
            "Например: 100\n\n"
            "Для отмены отправь /cancel"
        )

    elif query.data == "search":
        await query.message.reply_text(
            "🔎 Фильтры поиска:\n\n"
            + settings_text(user_id)
            + "\n\n"
            "Публичная категория FunPay:\n"
            + CATEGORY_URL
            + "\n\n"
            "Автоматический поиск пока не подключён: "
            "нужен разрешённый источник данных FunPay. "
            "Бот не будет показывать выдуманные объявления."
        )


# ---------- ВВОД ФИЛЬТРОВ ----------

async def receive_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    setting = context.user_data.get("setting")

    if not setting:
        return

    raw = update.message.text.strip().replace(",", ".")

    try:
        if setting.startswith("price"):
            value = float(raw)
            if value <= 0:
                raise ValueError

        else:
            value = int(raw)
            if value < 1:
                raise ValueError

    except ValueError:
        await update.message.reply_text(
            "❌ Введи корректное положительное число."
        )
        return

    if setting == "price_min":
        context.user_data["temp_min"] = value
        context.user_data["setting"] = "price_max"

        await update.message.reply_text(
            "Теперь введи максимальную цену за 1 Gold в рублях."
        )

    elif setting == "price_max":
        min_value = context.user_data["temp_min"]

        if value < min_value:
            await update.message.reply_text(
                "Максимальная цена не может быть меньше минимальной. "
                "Введи её ещё раз."
            )
            return

        save_setting(user_id, "min_price", min_value)
        save_setting(user_id, "max_price", value)

        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)

        await update.message.reply_text(
            "✅ Диапазон цены сохранён!\n\n"
            + settings_text(user_id),
            reply_markup=menu()
        )

    elif setting == "amount_min":
        context.user_data["temp_min"] = value
        context.user_data["setting"] = "amount_max"

        await update.message.reply_text(
            "Теперь введи максимальное количество Gold."
        )

    elif setting == "amount_max":
        min_value = context.user_data["temp_min"]

        if value < min_value:
            await update.message.reply_text(
                "Максимальное количество не может быть меньше минимального. "
                "Введи ещё раз."
            )
            return

        save_setting(user_id, "min_amount", min_value)
        save_setting(user_id, "max_amount", value)

        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)

        await update.message.reply_text(
            "✅ Количество сохранено!\n\n"
            + settings_text(user_id),
            reply_markup=menu()
        )


# ---------- ЗАПУСК ----------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    if not OWNER_ID:
        raise RuntimeError("Не задан OWNER_ID")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_value)
    )

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
