
import os
import re
import html
import sqlite3
import logging
import asyncio
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- НАСТРОЙКИ ----------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DB_NAME = "settings.db"
CATEGORY_URL = "https://funpay.com/chips/140/"

CHECK_INTERVAL = 15
MONITOR_KEY = "monitor_task"
STOP_KEY = "monitor_stop"

DURATIONS = {
    300: "5 минут",
    900: "15 минут",
    1800: "30 минут",
    3600: "1 час",
    7200: "2 часа",
    21600: "6 часов",
}


# ---------------- БАЗА ДАННЫХ ----------------

def init_db():
    with sqlite3.connect(DB_NAME) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                min_price REAL DEFAULT 0.10,
                max_price REAL DEFAULT 0.50,
                min_amount INTEGER DEFAULT 10,
                max_amount INTEGER DEFAULT 10000
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS seen_offers (
                offer_id TEXT PRIMARY KEY,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_settings(user_id):
    with sqlite3.connect(DB_NAME) as db:
        db.execute(
            "INSERT OR IGNORE INTO settings (user_id) VALUES (?)",
            (user_id,),
        )
        return db.execute(
            """
            SELECT min_price, max_price, min_amount, max_amount
            FROM settings WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()


def save_setting(user_id, field, value):
    allowed = {
        "min_price",
        "max_price",
        "min_amount",
        "max_amount",
    }

    if field not in allowed:
        return

    with sqlite3.connect(DB_NAME) as db:
        db.execute(
            f"UPDATE settings SET {field} = ? WHERE user_id = ?",
            (value, user_id),
        )


def is_seen(offer_id):
    with sqlite3.connect(DB_NAME) as db:
        return db.execute(
            "SELECT 1 FROM seen_offers WHERE offer_id = ?",
            (offer_id,),
        ).fetchone() is not None


def mark_seen(offer_id):
    with sqlite3.connect(DB_NAME) as db:
        db.execute(
            "INSERT OR IGNORE INTO seen_offers (offer_id) VALUES (?)",
            (offer_id,),
        )


def mark_many_seen(offers):
    with sqlite3.connect(DB_NAME) as db:
        db.executemany(
            "INSERT OR IGNORE INTO seen_offers (offer_id) VALUES (?)",
            [(offer["id"],) for offer in offers],
        )


# ---------------- ДОСТУП И ФОРМАТИРОВАНИЕ ----------------

def allowed_user(user_id):
    return user_id == OWNER_ID


def fmt(value):
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def safe(value):
    return html.escape(str(value or "Не указан"))


def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔎 Найти объявления",
                callback_data="search",
            )
        ],
        [
            InlineKeyboardButton(
                "🚀 Запустить мониторинг",
                callback_data="durations",
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Настроить цену",
                callback_data="price",
            ),
            InlineKeyboardButton(
                "📦 Настроить Gold",
                callback_data="amount",
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ Мои фильтры",
                callback_data="status",
            )
        ],
    ])


def duration_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 минут", callback_data="monitor:300"),
            InlineKeyboardButton("15 минут", callback_data="monitor:900"),
        ],
        [
            InlineKeyboardButton("30 минут", callback_data="monitor:1800"),
            InlineKeyboardButton("1 час", callback_data="monitor:3600"),
        ],
        [
            InlineKeyboardButton("2 часа", callback_data="monitor:7200"),
            InlineKeyboardButton("6 часов", callback_data="monitor:21600"),
        ],
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        ],
    ])


def stop_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⛔ Остановить мониторинг",
                callback_data="stop",
            )
        ]
    ])


def settings_text(user_id):
    min_p, max_p, min_a, max_a = get_settings(user_id)

    return (
        "⚙️ <b>ТВОИ ФИЛЬТРЫ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена СБП за 1 Gold: <b>{fmt(min_p)}–{fmt(max_p)} ₽</b>\n"
        f"📦 Количество: <b>{min_a}–{max_a} Gold</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔄 Частота проверки: <b>15 секунд</b>"
    )


# ---------------- ПАРСИНГ FUNPAY ----------------

def parse_number(text):
    if not text:
        return None

    cleaned = (
        text.replace("\xa0", " ")
        .replace(",", ".")
        .replace(" ", "")
    )

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
        timeout=20,
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

        price = parse_number(
            price_element.get_text(" ", strip=True)
        )
        amount_number = parse_number(
            amount_element.get_text(" ", strip=True)
        )

        if price is None or amount_number is None:
            continue

        amount = int(amount_number)

        href = row.get("href")
        if not href:
            continue

        link = urljoin(CATEGORY_URL, href)

        # Используем ссылку как уникальный идентификатор объявления.
        offer_id = href.strip()

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
            "id": offer_id,
            "price": price,
            "amount": amount,
            "platform": platform,
            "seller": seller,
            "link": link,
        })

    return offers


def matching_offers(user_id, offers):
    min_price, max_price, min_amount, max_amount = get_settings(user_id)

    result = [
        offer for offer in offers
        if min_price <= offer["price"] <= max_price
        and min_amount <= offer["amount"] <= max_amount
    ]

    result.sort(key=lambda item: item["price"])
    return result


def offer_message(offer, number=None):
    heading = (
        f"🆕 <b>НОВОЕ ОБЪЯВЛЕНИЕ №{number}</b>"
        if number is not None
        else "🪙 <b>ОБЪЯВЛЕНИЕ FUNPAY</b>"
    )

    return (
        f"{heading}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена СБП: <b>{fmt(offer['price'])} ₽ за 1 Gold</b>\n"
        f"📦 Количество: <b>{offer['amount']} Gold</b>\n"
        f"📱 Платформа: <b>{safe(offer['platform'])}</b>\n"
        f"👤 Продавец: <b>{safe(offer['seller'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f'🔗 <a href="{safe(offer["link"])}">Открыть объявление</a>'
    )


# ---------------- ПОИСК ----------------

async def send_search_results(query, user_id):
    await query.message.reply_text(
        "🔎 <b>ИЩУ ОБЪЯВЛЕНИЯ...</b>\n"
        "Проверяю публичную страницу FunPay.",
        parse_mode=ParseMode.HTML,
    )

    try:
        offers = await asyncio.to_thread(fetch_offers)
        matches = matching_offers(user_id, offers)

    except requests.RequestException:
        logger.exception("Ошибка запроса FunPay")
        await query.message.reply_text(
            "⚠️ Не удалось загрузить FunPay. Попробуй позже."
        )
        return

    except Exception:
        logger.exception("Ошибка поиска")
        await query.message.reply_text(
            "⚠️ Ошибка при обработке объявлений."
        )
        return

    if not matches:
        await query.message.reply_text(
            "😕 <b>Подходящих объявлений пока нет.</b>\n\n"
            "Попробуй изменить фильтры.",
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )
        return

    await query.message.reply_text(
        f"✅ <b>Найдено объявлений: {len(matches)}</b>",
        parse_mode=ParseMode.HTML,
    )

    for index, offer in enumerate(matches[:20], start=1):
        await query.message.reply_text(
            offer_message(offer, index),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ---------------- МОНИТОРИНГ ----------------

async def monitor_loop(application, user_id, duration, stop_event):
    started = asyncio.get_running_loop().time()
    deadline = started + duration

    try:
        # Первый запрос — базовый список.
        # Старые объявления не отправляем.
        initial = await asyncio.to_thread(fetch_offers)
        mark_many_seen(initial)

        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "🟢 <b>МОНИТОРИНГ ЗАПУЩЕН</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"⏱ Длительность: <b>{DURATIONS[duration]}</b>\n"
                "🔄 Проверка: каждые <b>15 секунд</b>\n"
                "💰 Фильтр: <b>цена СБП</b>\n"
                "🔔 Буду сообщать только о новых объявлениях.\n"
                "━━━━━━━━━━━━━━━━━━"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=stop_menu(),
        )

        while (
            asyncio.get_running_loop().time() < deadline
            and not stop_event.is_set()
        ):
            try:
                offers = await asyncio.to_thread(fetch_offers)

                for offer in offers:
                    if is_seen(offer["id"]):
                        continue

                    # Сначала сохраняем ID, чтобы не повторить
                    # уведомление при следующем цикле.
                    mark_seen(offer["id"])

                    matches = matching_offers(user_id, [offer])

                    if matches:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=offer_message(offer),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )

            except requests.RequestException:
                logger.warning(
                    "Временная ошибка запроса FunPay",
                    exc_info=True,
                )
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ FunPay временно не ответил. "
                        "Повторю проверку через 15 секунд."
                    ),
                )

            except Exception:
                logger.exception("Ошибка мониторинга")
                await application.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ Ошибка проверки. Попробую продолжить.",
                )

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=min(CHECK_INTERVAL, remaining),
                )
            except asyncio.TimeoutError:
                pass

        if stop_event.is_set():
            message = "⛔ <b>МОНИТОРИНГ ОСТАНОВЛЕН</b>"
        else:
            message = "🏁 <b>ВРЕМЯ МОНИТОРИНГА ЗАВЕРШИЛОСЬ</b>"

        await application.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except requests.RequestException:
        logger.exception("Не удалось начать мониторинг")
        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ Не удалось получить публичные объявления FunPay. "
                "Мониторинг не начался."
            ),
            reply_markup=menu(),
        )

    finally:
        application.bot_data.pop(MONITOR_KEY, None)
        application.bot_data.pop(STOP_KEY, None)


# ---------------- TELEGRAM КОМАНДЫ ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    get_settings(user_id)

    await update.message.reply_text(
        "🪙 <b>STANDOFF 2 GOLD FINDER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Добро пожаловать!\n\n"
        "🔎 Поиск объявлений FunPay\n"
        "💰 Фильтр по цене СБП\n"
        "📦 Фильтр по количеству Gold\n"
        "🚀 Автоматический мониторинг\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Выбери действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    await update.message.reply_text(
        settings_text(user_id),
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_user(update.effective_user.id):
        return

    context.user_data.pop("setting", None)
    context.user_data.pop("temp_min", None)

    await update.message.reply_text(
        "Настройка отменена.",
        reply_markup=menu(),
    )


# ---------------- КНОПКИ ----------------

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
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    elif query.data == "back":
        await query.message.reply_text(
            "Главное меню:",
            reply_markup=menu(),
        )

    elif query.data == "price":
        context.user_data["setting"] = "price_min"
        await query.message.reply_text(
            "💰 Введи минимальную цену СБП за 1 Gold.\n"
            "Например: 0.10\n"
            "Для отмены: /cancel"
        )

    elif query.data == "amount":
        context.user_data["setting"] = "amount_min"
        await query.message.reply_text(
            "📦 Введи минимальное количество Gold.\n"
            "Например: 10\n"
            "Для отмены: /cancel"
        )

    elif query.data == "search":
        await send_search_results(query, user_id)

    elif query.data == "durations":
        await query.message.reply_text(
            "🚀 <b>ВЫБЕРИ ВРЕМЯ МОНИТОРИНГА</b>\n\n"
            "Бот будет проверять новые объявления каждые 15 секунд.",
            parse_mode=ParseMode.HTML,
            reply_markup=duration_menu(),
        )

    elif query.data.startswith("monitor:"):
        current_task = context.application.bot_data.get(MONITOR_KEY)

        if current_task and not current_task.done():
            await query.message.reply_text(
                "⚠️ Мониторинг уже запущен. Сначала останови его."
            )
            return

        duration = int(query.data.split(":")[1])

        stop_event = asyncio.Event()
        context.application.bot_data[STOP_KEY] = stop_event

        task = context.application.create_task(
            monitor_loop(
                context.application,
                user_id,
                duration,
                stop_event,
            )
        )
        context.application.bot_data[MONITOR_KEY] = task

    elif query.data == "stop":
        stop_event = context.application.bot_data.get(STOP_KEY)

        if stop_event:
            stop_event.set()
            await query.message.reply_text(
                "⏳ Останавливаю мониторинг..."
            )
        else:
            await query.message.reply_text(
                "Сейчас мониторинг не запущен.",
                reply_markup=menu(),
            )


# ---------------- ВВОД ФИЛЬТРОВ ----------------

async def receive_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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
            "Теперь введи максимальную цену СБП за 1 Gold."
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
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
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
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ---------------- ЗАПУСК ----------------

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
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_value,
        )
    )

    logger.info("Standoff 2 Gold Finder запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
