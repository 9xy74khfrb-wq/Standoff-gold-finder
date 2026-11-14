
import os
import re
import sqlite3
import logging
import asyncio
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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
logger = logging.getLogger(__name__)

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
        return db.execute(
            """SELECT min_price, max_price, min_amount, max_amount
               FROM settings WHERE user_id = ?""",
            (user_id,)
        ).fetchone()


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


# ---------- МЕНЮ ----------

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
        f"📦 Количество: {min_a}–{max_a} Gold"
    )


# ---------- ПОЛУЧЕНИЕ ОБЪЯВЛЕНИЙ ----------

def parse_number(text):
    if not text:
        return None

    cleaned = text.replace("\xa0", " ").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)

    if not match:
        return None

    try:
        return float(match.group())
    except ValueError:
        return None


def fetch_offers():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    response = requests.get(
        CATEGORY_URL,
        headers=headers,
        timeout=20
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    rows = soup.select("a.tc-item")
    offers = []

    for row in rows:
        price_element = row.select_one(".tc-price")
        amount_element = row.select_one(".tc-amount")

        if not price_element or not amount_element:
            continue

        price = parse_number(price_element.get_text(" ", strip=True))
        amount_text = amount_element.get_text(" ", strip=True)
        amount_number = parse_number(amount_text)

        if price is None or amount_number is None:
            continue

        amount = int(amount_number)

        href = row.get("href")
        if not href:
            continue

        link = urljoin(CATEGORY_URL, href)

        platform_element = (
            row.select_one(".tc-server")
            or row.select_one(".tc-side")
            or row.select_one(".tc-desc-text")
        )

        platform = (
            platform_element.get_text(" ", strip=True)
            if platform_element else "Не указана"
        )

        seller_element = row.select_one(".media-user-name")
        seller = (
            seller_element.get_text(" ", strip=True)
            if seller_element else "Не указан"
        )

        offers.append({
            "price": price,
            "amount": amount,
            "platform": platform,
            "seller": seller,
            "link": link,
        })

    return offers


def find_matching_offers(user_id):
    min_price, max_price, min_amount, max_amount = get_settings(user_id)

    offers = fetch_offers()

    matching = [
        offer for offer in offers
        if min_price <= offer["price"] <= max_price
        and min_amount <= offer["amount"] <= max_amount
    ]

    matching.sort(key=lambda item: item["price"])

    return matching


# ---------- TELEGRAM ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    get_settings(user_id)

    await update.message.reply_text(
        "🪙 Standoff 2 Gold Finder\n\nВыбирай действие:",
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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    await update.message.reply_text(
        settings_text(user_id),
        reply_markup=menu()
    )


async def send_search_results(query, user_id):
    await query.message.reply_text("🔎 Проверяю публичные объявления FunPay...")

    try:
        offers = await asyncio.to_thread(
            find_matching_offers,
            user_id
        )

    except requests.RequestException as error:
        logger.warning("FunPay request error: %s", error)

        await query.message.reply_text(
            "Не удалось загрузить страницу FunPay.\n"
            "Попробуй ещё раз немного позже."
        )
        return

    except Exception:
        logger.exception("Ошибка поиска")

        await query.message.reply_text(
            "Произошла ошибка при обработке объявлений."
        )
        return

    if not offers:
        await query.message.reply_text(
            "Подходящих объявлений не найдено.\n\n"
            "Возможные причины:\n"
            "• Нет предложений по твоему фильтру.\n"
            "• FunPay не показал карточки в публичном ответе.\n"
            "• Изменилась структура страницы.\n\n"
            "Попробуй изменить фильтры или повторить поиск."
        )
        return

    await query.message.reply_text(
        f"✅ Найдено подходящих объявлений: {len(offers)}"
    )

    for index, offer in enumerate(offers, start=1):
        message = (
            f"🪙 Объявление №{index}\n\n"
            f"💰 Цена: {fmt(offer['price'])} ₽ за Gold\n"
            f"📦 Количество: {offer['amount']} Gold\n"
            f"📱 Платформа: {offer['platform']}\n"
            f"👤 Продавец: {offer['seller']}\n\n"
            f"🔗 {offer['link']}"
        )

        await query.message.reply_text(message)


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
            "Например: 0.30\nДля отмены: /cancel"
        )

    elif query.data == "amount":
        context.user_data["setting"] = "amount_min"

        await query.message.reply_text(
            "📦 Введи минимальное количество Gold.\n"
            "Например: 100\nДля отмены: /cancel"
        )

    elif query.data == "search":
        await send_search_results(query, user_id)


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
            "Теперь введи максимальную цену за 1 Gold."
        )

    elif setting == "price_max":
        min_value = context.user_data["temp_min"]

        if value < min_value:
            await update.message.reply_text(
                "Максимальная цена не может быть меньше минимальной."
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
                "Максимальное количество не может быть меньше минимального."
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
