
import os
import re
import html
import sqlite3
import logging
import asyncio
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
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

DURATION_LABELS = {v: k for k, v in DURATIONS.items()}

# ---------------- КЛАВИАТУРЫ ----------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🔎 Найти объявления", "🚀 Запустить"],
        ["⛔ Остановить", "⚙️ Настройки"],
        ["⏱ Длительность", "📋 Мои фильтры"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выбери действие в меню",
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    [
        ["💰 Изменить цену", "📦 Изменить количество"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

DURATION_MENU = ReplyKeyboardMarkup(
    [
        ["5 минут", "15 минут"],
        ["30 минут", "1 час"],
        ["2 часа", "6 часов"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

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


# ---------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------------

def allowed_user(user_id):
    return user_id == OWNER_ID


def fmt(value):
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def safe(value):
    return html.escape(str(value or "Не указан"))


def settings_text(user_id, context):
    min_p, max_p, min_a, max_a = get_settings(user_id)

    task = context.application.bot_data.get(MONITOR_KEY)
    running = task is not None and not task.done()

    duration = context.user_data.get("monitor_duration", 900)

    monitor_status = (
        f"🟢 Работает ({DURATIONS[duration]})"
        if running else "⚪ Остановлен"
    )

    return (
        "⚙️ <b>ТВОИ НАСТРОЙКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена СБП за 1 Gold: <b>{fmt(min_p)}–{fmt(max_p)} ₽</b>\n"
        f"📦 Количество: <b>{min_a}–{max_a} Gold</b>\n"
        f"⏱ Длительность: <b>{DURATIONS[duration]}</b>\n"
        f"📡 Мониторинг: <b>{monitor_status}</b>\n"
        "🔄 Проверка: <b>каждые 15 секунд</b>"
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
        cookies={
            "cy": "RUB",
            "locale": "ru",
        },
        timeout=20,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("a.tc-item")

    offers = []

     logger.info("FunPay: карточек найдено: %s", len(rows))

    for row in rows:
        price_element = row.select_one(".tc-price")

          logger.info("FunPay: карточек найдено: %s", len(rows))

    for row in rows:
        price_element = row.select_one(".tc-price")

        if price_element:
            print("DEBUG PRICE:", price_element.get_text(" ", strip=True))
            print("DEBUG HTML:", str(price_element)[:1000])

        amount_element = row.select_one(".tc-amount")

        if not price_element or not amount_element:
            continue

        price_value = price_element.select_one("div")
        if not price_value:
            price_value = price_element

        price_text = price_value.get_text(" ", strip=True)
        unit_element = price_element.select_one(".unit")
        unit_text = (
            unit_element.get_text(" ", strip=True)
            if unit_element else price_element.get_text(" ", strip=True)
        )

        # Не принимаем евро за рубли.
        if "₽" not in unit_text and "RUB" not in unit_text.upper():
            logger.warning(
                "Пропущена цена в неизвестной валюте: %r",
                unit_text,
            )
            continue

        price = parse_number(price_text)
        amount_number = parse_number(
            amount_element.get_text(" ", strip=True)
        )

        if price is None or amount_number is None:
            continue

        href = row.get("href")
        if not href:
            continue

        seller_element = row.select_one(".media-user-name")
        seller = (
            seller_element.get_text(" ", strip=True)
            if seller_element else "Не указан"
        )

        platform_element = (
            row.select_one(".tc-server")
            or row.select_one(".tc-side")
            or row.select_one(".tc-desc-text")
        )

        platform = (
            platform_element.get_text(" ", strip=True)
            if platform_element else "Не указана"
        )

        offers.append({
            "id": href.strip(),
            "price": price,
            "amount": int(amount_number),
            "platform": platform,
            "seller": seller,
            "link": urljoin(CATEGORY_URL, href),
        })

    logger.info(
        "FunPay: обработано рублёвых объявлений: %s",
        len(offers),
    )

   


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

async def send_search_results(message, user_id):
    await message.reply_text(
        "🔎 <b>ИЩУ ОБЪЯВЛЕНИЯ...</b>\n"
        "Проверяю публичную страницу FunPay.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )

    try:
        offers = await asyncio.to_thread(fetch_offers)
        matches = matching_offers(user_id, offers)

    except Exception:
        logger.exception("Ошибка поиска")
        await message.reply_text(
            "⚠️ Не удалось загрузить или обработать объявления FunPay.",
            reply_markup=MAIN_MENU,
        )
        return

    if not matches:
        await message.reply_text(
            "😕 <b>Подходящих объявлений пока нет.</b>\n\n"
            "Попробуй изменить фильтры.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    await message.reply_text(
        f"✅ <b>Найдено объявлений: {len(matches)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )

    for index, offer in enumerate(matches[:20], start=1):
        await message.reply_text(
            offer_message(offer, index),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ---------------- МОНИТОРИНГ ----------------

async def monitor_loop(application, user_id, duration, stop_event):
    started = asyncio.get_running_loop().time()
    deadline = started + duration

    try:
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
                "🔔 Буду сообщать только о новых объявлениях."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
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

                    mark_seen(offer["id"])

                    if matching_offers(user_id, [offer]):
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=offer_message(offer),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )

            except Exception:
                logger.exception("Ошибка проверки FunPay")
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ Не удалось проверить FunPay. "
                        "Попробую ещё раз через 15 секунд."
                    ),
                    reply_markup=MAIN_MENU,
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
            text = "⛔ <b>МОНИТОРИНГ ОСТАНОВЛЕН</b>"
        else:
            text = "🏁 <b>ВРЕМЯ МОНИТОРИНГА ЗАВЕРШИЛОСЬ</b>"

        await application.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )

    except Exception:
        logger.exception("Не удалось начать мониторинг")
        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ Не удалось получить объявления FunPay. "
                "Мониторинг не начался."
            ),
            reply_markup=MAIN_MENU,
        )

    finally:
        application.bot_data.pop(MONITOR_KEY, None)
        application.bot_data.pop(STOP_KEY, None)


async def start_monitor(update, context):
    user_id = update.effective_user.id

    current_task = context.application.bot_data.get(MONITOR_KEY)

    if current_task and not current_task.done():
        await update.message.reply_text(
            "⚠️ Мониторинг уже запущен. Сначала останови его.",
            reply_markup=MAIN_MENU,
        )
        return

    duration = context.user_data.get("monitor_duration", 900)
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

    await update.message.reply_text(
        f"⏳ Подготавливаю мониторинг на {DURATIONS[duration]}...",
        reply_markup=MAIN_MENU,
    )


async def stop_monitor(update, context):
    stop_event = context.application.bot_data.get(STOP_KEY)

    if stop_event:
        stop_event.set()
        await update.message.reply_text(
            "⏳ Останавливаю мониторинг...",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text(
            "Сейчас мониторинг не запущен.",
            reply_markup=MAIN_MENU,
        )


# ---------------- НАСТРОЙКИ ----------------

async def begin_setting(update, context, setting):
    context.user_data["setting"] = setting
    context.user_data.pop("temp_min", None)

    if setting == "price_min":
        text = (
            "💰 Введи минимальную цену СБП за 1 Gold.\n"
            "Например: 0.10\n"
            "Для отмены нажми «⬅️ Главное меню»."
        )
    elif setting == "amount_min":
        text = (
            "📦 Введи минимальное количество Gold.\n"
            "Например: 10\n"
            "Для отмены нажми «⬅️ Главное меню»."
        )
    else:
        return

    await update.message.reply_text(
        text,
        reply_markup=SETTINGS_MENU,
    )


async def receive_setting_value(update, context):
    user_id = update.effective_user.id
    setting = context.user_data.get("setting")

    if not setting:
        return False

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
        return True

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
            return True

        save_setting(user_id, "min_price", min_value)
        save_setting(user_id, "max_price", value)

        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)

        await update.message.reply_text(
            "✅ Диапазон цены сохранён!\n\n"
            + settings_text(user_id, context),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
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
            return True

        save_setting(user_id, "min_amount", min_value)
        save_setting(user_id, "max_amount", value)

        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)

        await update.message.reply_text(
            "✅ Количество сохранено!\n\n"
            + settings_text(user_id, context),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )

    return True


# ---------------- КОМАНДЫ ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    get_settings(user_id)
    context.user_data.setdefault("monitor_duration", 900)

    await update.message.reply_text(
        "🪙 <b>STANDOFF 2 GOLD FINDER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Добро пожаловать!\n\n"
        "🔎 Поиск публичных объявлений FunPay\n"
        "💰 Фильтр по цене СБП\n"
        "📦 Фильтр по количеству Gold\n"
        "🚀 Мониторинг новых объявлений\n\n"
        "Выбери действие в меню под строкой ввода.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    await update.message.reply_text(
        settings_text(user_id, context),
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_user(update.effective_user.id):
        return

    context.user_data.pop("setting", None)
    context.user_data.pop("temp_min", None)

    await update.message.reply_text(
        "Настройка отменена.",
        reply_markup=MAIN_MENU,
    )


# ---------------- ОБРАБОТКА МЕНЮ ----------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not allowed_user(user_id):
        return

    text = update.message.text.strip()

    if text == "🔎 Найти объявления":
        context.user_data.pop("setting", None)
        await send_search_results(update.message, user_id)
        return

    if text == "🚀 Запустить":
        context.user_data.pop("setting", None)
        await start_monitor(update, context)
        return

    if text == "⛔ Остановить":
        context.user_data.pop("setting", None)
        await stop_monitor(update, context)
        return

    if text == "⚙️ Настройки":
        context.user_data.pop("setting", None)
        await update.message.reply_text(
            "Выбери, что хочешь изменить:",
            reply_markup=SETTINGS_MENU,
        )
        return

    if text == "💰 Изменить цену":
        await begin_setting(update, context, "price_min")
        return

    if text == "📦 Изменить количество":
        await begin_setting(update, context, "amount_min")
        return

    if text == "⏱ Длительность":
        context.user_data.pop("setting", None)
        await update.message.reply_text(
            "⏱ Выбери длительность мониторинга:",
            reply_markup=DURATION_MENU,
        )
        return

    if text in DURATION_LABELS:
        context.user_data["monitor_duration"] = DURATION_LABELS[text]
        await update.message.reply_text(
            f"✅ Длительность установлена: <b>{text}</b>\n"
            "Теперь нажми «🚀 Запустить».",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    if text == "📋 Мои фильтры":
        context.user_data.pop("setting", None)
        await update.message.reply_text(
            settings_text(user_id, context),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    if text == "⬅️ Главное меню":
        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)
        await update.message.reply_text(
            "Главное меню:",
            reply_markup=MAIN_MENU,
        )
        return

    handled = await receive_setting_value(update, context)

    if not handled:
        await update.message.reply_text(
            "Выбери действие с помощью кнопок под строкой ввода.",
            reply_markup=MAIN_MENU,
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

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    logger.info("Standoff 2 Gold Finder запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
