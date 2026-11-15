
import os
import re
import html
import sqlite3
import logging
import asyncio
from urllib.parse import urljoin, urlparse

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

DEFAULT_CHECK_INTERVAL = 15
CHECK_INTERVALS = {
    5: "5 секунд",
    10: "10 секунд",
    15: "15 секунд",
    30: "30 секунд",
    60: "1 минута",
    120: "2 минуты",
}

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
INTERVAL_LABELS = {v: k for k, v in CHECK_INTERVALS.items()}


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
        ["🔄 Интервал обновления"],
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

INTERVAL_MENU = ReplyKeyboardMarkup(
    [
        ["5 секунд", "10 секунд"],
        ["15 секунд", "30 секунд"],
        ["1 минута", "2 минуты"],
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

        # Добавляем колонку интервала в старую базу,
        # если её там ещё нет.
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(settings)")
        }

        if "check_interval" not in columns:
            db.execute("""
                ALTER TABLE settings
                ADD COLUMN check_interval INTEGER DEFAULT 15
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


def get_check_interval(user_id):
    with sqlite3.connect(DB_NAME) as db:
        db.execute(
            "INSERT OR IGNORE INTO settings (user_id) VALUES (?)",
            (user_id,),
        )
        result = db.execute(
            "SELECT check_interval FROM settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    return result[0] if result and result[0] else DEFAULT_CHECK_INTERVAL


def save_setting(user_id, field, value):
    allowed = {
        "min_price", "max_price",
        "min_amount", "max_amount",
        "check_interval",
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
    interval = get_check_interval(user_id)

    monitor_status = (
        f"🟢 Работает ({DURATIONS[duration]})"
        if running else "⚪ Остановлен"
    )

    return (
        "⚙️ <b>ТВОИ НАСТРОЙКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена за 1 Gold: <b>{fmt(min_p)}–{fmt(max_p)} ₽</b>\n"
        f"📦 Количество: <b>{min_a}–{max_a} Gold</b>\n"
        f"⏱ Длительность: <b>{DURATIONS[duration]}</b>\n"
        f"🔄 Интервал обновления: <b>{CHECK_INTERVALS.get(interval, str(interval) + ' секунд')}</b>\n"
        f"📡 Мониторинг: <b>{monitor_status}</b>"
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


def fetch_page():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }

    response = requests.get(
        CATEGORY_URL,
        headers=headers,
        cookies={"cy": "rub", "locale": "ru"},
        timeout=25,
    )
    response.raise_for_status()
    return response


def fetch_offers():
    response = fetch_page()
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("a.tc-item")

    offers = []
    skipped_currency = 0

    logger.info("FunPay: карточек найдено: %s", len(rows))

    for row in rows:
        price_element = row.select_one(".tc-price")
        amount_element = row.select_one(".tc-amount")

        if not price_element or not amount_element:
            continue

        price_text = price_element.get_text(" ", strip=True)
        normalized = price_text.lower()

        is_rub = (
            "₽" in price_text
            or "rub" in normalized
            or "руб" in normalized
        )

        if not is_rub:
            skipped_currency += 1
            continue

        price = parse_number(price_text)
        amount = parse_number(
            amount_element.get_text(" ", strip=True)
        )

        if price is None or amount is None:
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

        link = urljoin(CATEGORY_URL, href)
        parsed = urlparse(link)
        offer_id = parsed.path.rstrip("/")

        offers.append({
            "id": offer_id,
            "price": price,
            "amount": int(amount),
            "platform": platform,
            "seller": seller,
            "link": link,
        })

    logger.info(
        "FunPay: рублёвых объявлений: %s; пропущено из-за валюты: %s",
        len(offers),
        skipped_currency,
    )

    if not offers and rows and skipped_currency:
        raise ValueError(
            "FunPay вернул цены не в рублях. "
            "Проверь валюту страницы FunPay."
        )

    return offers


# ---------------- ДИАГНОСТИКА ----------------

async def diagnostic(update, context):
    if not allowed_user(update.effective_user.id):
        return

    try:
        response = await asyncio.to_thread(fetch_page)
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("a.tc-item")

        report = [
            "🔬 ДИАГНОСТИКА FUNPAY",
            f"HTTP: {response.status_code}",
            f"Адрес страницы: {response.url}",
            f"Найдено карточек: {len(rows)}",
            "",
        ]

        for index, row in enumerate(rows[:5], start=1):
            price_element = row.select_one(".tc-price")
            amount_element = row.select_one(".tc-amount")

            price_text = (
                price_element.get_text(" ", strip=True)
                if price_element else "Ценник не найден"
            )
            amount_text = (
                amount_element.get_text(" ", strip=True)
                if amount_element else "Количество не найдено"
            )

            report.append(
                f"Карточка {index}: цена {price_text!r}; "
                f"количество {amount_text!r}"
            )

        await update.message.reply_text("\n".join(report))

    except Exception as error:
        await update.message.reply_text(
            f"Ошибка диагностики: {type(error).__name__}: {error}"
        )


# ---------------- ФИЛЬТРЫ ----------------

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
        else "🪙 <b>НОВОЕ ОБЪЯВЛЕНИЕ FUNPAY</b>"
    )

    return (
        f"{heading}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена: <b>{fmt(offer['price'])} ₽ за 1 Gold</b>\n"
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

    except Exception as error:
        logger.exception("Ошибка поиска")
        await message.reply_text(
            f"⚠️ Ошибка поиска: {type(error).__name__}: {error}",
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

async def monitor_loop(
    application,
    user_id,
    duration,
    interval,
    stop_event,
):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration

    try:
        initial = await asyncio.to_thread(fetch_offers)
        mark_many_seen(initial)

        logger.info(
            "Мониторинг: начальный список содержит %s объявлений",
            len(initial),
        )

        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "🟢 <b>МОНИТОРИНГ ЗАПУЩЕН</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"⏱ Длительность: <b>{DURATIONS[duration]}</b>\n"
                f"🔄 Проверка: каждые <b>{CHECK_INTERVALS[interval]}</b>\n"
                f"📋 Уже существующих объявлений: <b>{len(initial)}</b>\n"
                "🔔 Жду новые объявления."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )

        while loop.time() < deadline and not stop_event.is_set():
            try:
                offers = await asyncio.to_thread(fetch_offers)

                unique_offers = {
                    offer["id"]: offer for offer in offers
                }

                new_offers = [
                    offer for offer in unique_offers.values()
                    if not is_seen(offer["id"])
                ]

                matches = matching_offers(user_id, new_offers)
                matching_ids = {offer["id"] for offer in matches}

                logger.info(
                    "Мониторинг: карточек=%s, новых=%s, "
                    "подходящих по фильтрам=%s",
                    len(offers),
                    len(new_offers),
                    len(matches),
                )

                for offer in new_offers:
                    if stop_event.is_set():
                        break

                    if offer["id"] in matching_ids:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=offer_message(offer),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )

                        logger.info(
                            "Отправлено новое объявление: %s",
                            offer["id"],
                        )

                    mark_seen(offer["id"])

            except Exception:
                logger.exception("Ошибка проверки FunPay")
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ Ошибка проверки FunPay. "
                        f"Повторю попытку через {CHECK_INTERVALS[interval]}."
                    ),
                    reply_markup=MAIN_MENU,
                )

            remaining = deadline - loop.time()
            if remaining <= 0:
                break

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=min(interval, remaining),
                )
            except asyncio.TimeoutError:
                pass

        text = (
            "⛔ <b>МОНИТОРИНГ ОСТАНОВЛЕН</b>"
            if stop_event.is_set()
            else "🏁 <b>ВРЕМЯ МОНИТОРИНГА ЗАВЕРШИЛОСЬ</b>"
        )

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
            text="⚠️ Не удалось начать мониторинг. Проверь диагностику FunPay.",
            reply_markup=MAIN_MENU,
        )

    finally:
        current = application.bot_data.get(MONITOR_KEY)
        if current is asyncio.current_task():
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
    interval = get_check_interval(user_id)
    stop_event = asyncio.Event()

    context.application.bot_data[STOP_KEY] = stop_event

    task = context.application.create_task(
        monitor_loop(
            context.application,
            user_id,
            duration,
            interval,
            stop_event,
        )
    )
    context.application.bot_data[MONITOR_KEY] = task

    await update.message.reply_text(
        f"⏳ Подготавливаю мониторинг на {DURATIONS[duration]}.\n"
        f"Интервал проверки: {CHECK_INTERVALS[interval]}.",
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
            "💰 Введи минимальную цену за 1 Gold.\n"
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

    await update.message.reply_text(text, reply_markup=SETTINGS_MENU)


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
            "Теперь введи максимальную цену за 1 Gold."
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
    get_check_interval(user_id)
    context.user_data.setdefault("monitor_duration", 900)

    await update.message.reply_text(
        "🪙 <b>STANDOFF 2 GOLD FINDER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Добро пожаловать!\n\n"
        "🔎 Поиск публичных объявлений FunPay\n"
        "💰 Фильтр по цене\n"
        "📦 Фильтр по количеству Gold\n"
        "🚀 Мониторинг новых объявлений\n"
        "🔄 Настраиваемый интервал проверки\n\n"
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

    if text == "🔄 Интервал обновления":
        context.user_data.pop("setting", None)
        current = get_check_interval(user_id)
        await update.message.reply_text(
            "🔄 <b>ИНТЕРВАЛ ОБНОВЛЕНИЯ</b>\n\n"
            f"Сейчас: <b>{CHECK_INTERVALS[current]}</b>\n\n"
            "Выбери, как часто проверять FunPay:",
            parse_mode=ParseMode.HTML,
            reply_markup=INTERVAL_MENU,
        )
        return

    if text in INTERVAL_LABELS:
        interval = INTERVAL_LABELS[text]
        save_setting(user_id, "check_interval", interval)
        context.user_data.pop("setting", None)

        await update.message.reply_text(
            f"✅ Интервал обновления установлен: <b>{text}</b>\n\n"
            "Настройка сохранена. При следующем запуске мониторинга "
            "будет использоваться новый интервал.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
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
    app.add_handler(CommandHandler("diagnostic", diagnostic))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    logger.info("Standoff 2 Gold Finder запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
