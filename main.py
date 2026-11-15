import os
import re
import html
import sqlite3
import logging
import asyncio
import time
import secrets
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from telegram import (
    Update,
    ReplyKeyboardMarkup,
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

# ============================================================
# STANDOFF 2 GOLD FINDER
# Поиск публичных объявлений FunPay и уведомления в Telegram.
# Покупка выполняется пользователем вручную на FunPay.
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DB_NAME = os.getenv("DB_NAME", "settings.db")
CATEGORY_URL = "https://funpay.com/chips/140/"
DEFAULT_CHECK_INTERVAL = 15
RECENT_SECONDS = 120

CHECK_INTERVALS = {
    5: "5 секунд",
    10: "10 секунд",
    15: "15 секунд",
    30: "30 секунд",
    60: "1 минута",
    120: "2 минуты",
}
DURATIONS = {
    300: "5 минут",
    900: "15 минут",
    1800: "30 минут",
    3600: "1 час",
    7200: "2 часа",
    21600: "6 часов",
}
DURATION_LABELS = {label: seconds for seconds, label in DURATIONS.items()}
INTERVAL_LABELS = {label: seconds for seconds, label in CHECK_INTERVALS.items()}

MONITORS_KEY = "monitor_tasks"
STOPS_KEY = "monitor_stops"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🔎 Найти объявления", "🚀 Запустить"],
        ["⛔ Остановить", "🧪 Тест мониторинга"],
        ["⚙️ Настройки", "📋 Мои фильтры"],
        ["⏱ Длительность"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выбери действие",
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
)
INTERVAL_MENU = ReplyKeyboardMarkup(
    [
        ["5 секунд", "10 секунд"],
        ["15 секунд", "30 секунд"],
        ["1 минута", "2 минуты"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
)


# ---------------- БАЗА ДАННЫХ ----------------

def db_connect():
    return sqlite3.connect(DB_NAME, timeout=30)


def init_db():
    with db_connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                min_price REAL DEFAULT 0.10,
                max_price REAL DEFAULT 0.50,
                min_amount INTEGER DEFAULT 10,
                max_amount INTEGER DEFAULT 10000,
                check_interval INTEGER DEFAULT 15
            )
        """)

        columns = {row[1] for row in db.execute("PRAGMA table_info(settings)")}
        if "check_interval" not in columns:
            db.execute(
                "ALTER TABLE settings ADD COLUMN check_interval INTEGER DEFAULT 15"
            )

        # Отдельная таблица v2, чтобы не конфликтовать со старой схемой seen_offers.
        db.execute("""
            CREATE TABLE IF NOT EXISTS seen_offers_v2 (
                user_id INTEGER NOT NULL,
                offer_id TEXT NOT NULL,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, offer_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS access_keys (
                key_hash TEXT PRIMARY KEY,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id INTEGER PRIMARY KEY,
                activated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS action_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                offer_id TEXT,
                action TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def ensure_settings(user_id):
    with db_connect() as db:
        db.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))


def get_settings(user_id):
    ensure_settings(user_id)
    with db_connect() as db:
        return db.execute(
            """
            SELECT min_price, max_price, min_amount, max_amount
            FROM settings WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()


def get_check_interval(user_id):
    ensure_settings(user_id)
    with db_connect() as db:
        row = db.execute(
            "SELECT check_interval FROM settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    interval = row[0] if row else DEFAULT_CHECK_INTERVAL
    return interval if interval in CHECK_INTERVALS else DEFAULT_CHECK_INTERVAL


def save_setting(user_id, field, value):
    allowed = {
        "min_price", "max_price", "min_amount",
        "max_amount", "check_interval",
    }
    if field not in allowed:
        raise ValueError("Недопустимое поле настройки")
    ensure_settings(user_id)
    with db_connect() as db:
        db.execute(
            f"UPDATE settings SET {field} = ? WHERE user_id = ?",
            (value, user_id),
        )


def allowed_user(user_id):
    if user_id == OWNER_ID:
        return True
    with db_connect() as db:
        return db.execute(
            "SELECT 1 FROM authorized_users WHERE user_id = ?",
            (user_id,),
        ).fetchone() is not None


def key_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def log_action(user_id, offer_id, action):
    with db_connect() as db:
        db.execute(
            "INSERT INTO action_history (user_id, offer_id, action) VALUES (?, ?, ?)",
            (user_id, str(offer_id or ""), action),
        )


def is_seen(user_id, offer_id):
    with db_connect() as db:
        return db.execute(
            "SELECT 1 FROM seen_offers_v2 WHERE user_id = ? AND offer_id = ?",
            (user_id, offer_id),
        ).fetchone() is not None


def mark_seen(user_id, offer_id):
    with db_connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen_offers_v2 (user_id, offer_id) VALUES (?, ?)",
            (user_id, offer_id),
        )


def mark_many_seen(user_id, offers):
    with db_connect() as db:
        db.executemany(
            "INSERT OR IGNORE INTO seen_offers_v2 (user_id, offer_id) VALUES (?, ?)",
            [(user_id, offer["id"]) for offer in offers],
        )


# ---------------- ФОРМАТИРОВАНИЕ ----------------

def fmt(value):
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def safe(value):
    return html.escape(str(value or "Не указан"), quote=True)


def settings_text(user_id, context):
    min_p, max_p, min_a, max_a = get_settings(user_id)
    tasks = context.application.bot_data.setdefault(MONITORS_KEY, {})
    task = tasks.get(user_id)
    running = task is not None and not task.done()
    duration = context.user_data.get("monitor_duration", 900)
    interval = get_check_interval(user_id)
    duration_text = DURATIONS.get(duration, DURATIONS[900])
    status_text = f"🟢 Работает ({duration_text})" if running else "⚪ Остановлен"

    return (
        "⚙️ <b>ТВОИ НАСТРОЙКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена за 1 Gold: <b>{fmt(min_p)}–{fmt(max_p)} ₽</b>\n"
        f"📦 Количество: <b>{min_a}–{max_a} Gold</b>\n"
        f"⏱ Длительность: <b>{duration_text}</b>\n"
        f"🔄 Интервал: <b>{CHECK_INTERVALS[interval]}</b>\n"
        f"📡 Мониторинг: <b>{status_text}</b>"
    )


# ---------------- ВРЕМЯ ПУБЛИКАЦИИ ----------------

def parse_published_time(value):
    if not value:
        return None
    value = str(value).strip()

    if re.fullmatch(r"\d{10,13}", value):
        try:
            number = int(value)
            if number > 10**12:
                number /= 1000
            return float(number)
        except (ValueError, OverflowError):
            return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        pass

    for date_format in (
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y, %H:%M",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(value, date_format)
            parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue

    normalized = value.lower().replace("ё", "е")
    if "только что" in normalized or "сейчас" in normalized:
        return time.time()

    match = re.search(
        r"(\d+)\s*(секунд\w*|сек\.?|минут\w*|мин\.?|час\w*|дн\w*|день)\s*назад",
        normalized,
    )
    if match:
        number = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("сек"):
            seconds = number
        elif unit.startswith("мин"):
            seconds = number * 60
        elif unit.startswith("час"):
            seconds = number * 3600
        else:
            seconds = number * 86400
        return time.time() - seconds
    return None


def extract_published_time(row):
    candidates = []
    for attribute in (
        "data-time", "data-timestamp", "data-created-at",
        "data-date", "datetime", "title",
    ):
        value = row.get(attribute)
        if value:
            candidates.append(value)

    for element in row.select(
        "time, [data-time], [data-timestamp], [data-created-at], [datetime]"
    ):
        for attribute in (
            "datetime", "data-time", "data-timestamp",
            "data-created-at", "title",
        ):
            value = element.get(attribute)
            if value:
                candidates.append(value)
        text_value = element.get_text(" ", strip=True)
        if text_value:
            candidates.append(text_value)

    card_text = row.get_text(" ", strip=True)
    match = re.search(
        r"(только что|\d+\s*(?:секунд\w*|сек\.?|минут\w*|мин\.?|"
        r"час\w*|дн\w*|день)\s*назад)",
        card_text.lower().replace("ё", "е"),
    )
    if match:
        candidates.append(match.group(1))

    for candidate in candidates:
        timestamp = parse_published_time(candidate)
        if timestamp is not None:
            return timestamp
    return None


def is_published_recently(offer, seconds=RECENT_SECONDS):
    published_at = offer.get("published_at")
    if published_at is None:
        return False
    age = time.time() - published_at
    return 0 <= age <= seconds


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
        amount = parse_number(amount_element.get_text(" ", strip=True))
        if price is None or amount is None:
            continue

        href = row.get("href")
        if not href:
            continue
        link = urljoin(CATEGORY_URL, href)
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or parsed.netloc != "funpay.com":
            continue

        query = parse_qs(parsed.query)
        offer_id = (
            query.get("id", [None])[0]
            or parsed.path.rstrip("/") + (
                "?" + parsed.query if parsed.query else ""
            )
        )

        seller_element = row.select_one(".media-user-name")
        seller = (
            seller_element.get_text(" ", strip=True)
            if seller_element else "Не указан"
        )

        # Получаем ссылку на профиль из ссылки вокруг ника продавца.
        seller_link = None
        if seller_element:
            seller_anchor = (
                seller_element
                if seller_element.name == "a"
                else seller_element.find_parent("a")
            )

            if seller_anchor and seller_anchor.get("href"):
                candidate = urljoin(CATEGORY_URL, seller_anchor["href"])
                parsed_seller = urlparse(candidate)

                if (
                    parsed_seller.scheme in ("http", "https")
                    and parsed_seller.netloc.lower() in (
                        "funpay.com",
                        "www.funpay.com",
                    )
                    and parsed_seller.path.startswith("/users/")
                ):
                    seller_link = candidate

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
            "id": str(offer_id),
            "price": price,
            "amount": int(amount),
            "platform": platform,
            "seller": seller,
            "seller_link": seller_link,
            "link": link,
            "published_at": extract_published_time(row),
        })

    logger.info(
        "FunPay: рублёвых объявлений: %s; пропущено из-за валюты: %s",
        len(offers),
        skipped_currency,
    )
    if not offers and rows and skipped_currency:
        raise ValueError(
            "FunPay вернул цены не в рублях. Проверь валюту страницы FunPay."
        )
    return offers


# ---------------- ФИЛЬТРЫ И СООБЩЕНИЯ ----------------

def matching_offers(user_id, offers):
    min_price, max_price, min_amount, max_amount = get_settings(user_id)
    result = [
        offer for offer in offers
        if min_price <= offer["price"] <= max_price
        and min_amount <= offer["amount"] <= max_amount
    ]
    result.sort(key=lambda item: item["price"])
    return result


def offer_keyboard(offer):
    offer_id = str(offer["id"])[:40]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Открыть на FunPay", url=offer["link"]),
        InlineKeyboardButton("Пропустить", callback_data=f"skip:{offer_id}"),
    ]])


def offer_message(offer, number=None, recent=False):
    if recent:
        heading = "🕑 <b>ОБЪЯВЛЕНИЕ ЗА ПОСЛЕДНИЕ 2 МИНУТЫ</b>"
    elif number is not None:
        heading = f"🆕 <b>ОБЪЯВЛЕНИЕ №{number}</b>"
    else:
        heading = "🪙 <b>НОВОЕ ОБЪЯВЛЕНИЕ FUNPAY</b>"

    seller_name = safe(offer["seller"])
    seller_link = offer.get("seller_link")

    if seller_link:
        seller_text = (
            f'<a href="{html.escape(seller_link, quote=True)}">'
            f'{seller_name}</a>'
        )
    else:
        seller_text = seller_name

    return (
        f"{heading}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена: <b>{fmt(offer['price'])} ₽ за 1 Gold</b>\n"
        f"📦 Количество: <b>{offer['amount']} Gold</b>\n"
        f"📱 Платформа: <b>{safe(offer['platform'])}</b>\n"
        f"👤 Продавец: {seller_text}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Нажми кнопку, чтобы открыть объявление на FunPay.\n"
        "Покупку и оплату ты подтверждаешь самостоятельно."
    )


async def send_search_results(message, user_id):
    await message.reply_text(
        "🔎 ИЩУ ОБЪЯВЛЕНИЯ...\nПроверяю публичную страницу FunPay.",
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
            "😕 Подходящих объявлений пока нет. Попробуй изменить фильтры.",
            reply_markup=MAIN_MENU,
        )
        return

    await message.reply_text(
        f"✅ Найдено объявлений: {len(matches)}",
        reply_markup=MAIN_MENU,
    )
    for index, offer in enumerate(matches[:20], start=1):
        await message.reply_text(
            offer_message(offer, index),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=offer_keyboard(offer),
        )


# ---------------- ДИАГНОСТИКА И ТЕСТ ----------------

async def diagnostic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_user(update.effective_user.id):
        return
    try:
        response = await asyncio.to_thread(fetch_page)
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("a.tc-item")
        report = [
            "🔬 ДИАГНОСТИКА FUNPAY",
            f"HTTP: {response.status_code}",
            f"Адрес: {response.url}",
            f"Карточек найдено: {len(rows)}",
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
            published_at = extract_published_time(row)
            report.append(
                f"Карточка {index}: цена {price_text!r}; "
                f"количество {amount_text!r}; "
                f"время: {published_at or 'не найдено'}"
            )
        await update.message.reply_text("\n".join(report))
    except Exception as error:
        await update.message.reply_text(
            f"Ошибка диагностики: {type(error).__name__}: {error}"
        )


async def test_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "🧪 Проверяю мониторинг, фильтры и отправку Telegram...",
        reply_markup=MAIN_MENU,
    )
    try:
        offers = await asyncio.to_thread(fetch_offers)
        matches = matching_offers(user_id, offers)
        recent = [offer for offer in offers if is_published_recently(offer)]
        interval = get_check_interval(user_id)
        report = (
            "🧪 <b>РЕЗУЛЬТАТ ТЕСТА</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🟢 FunPay: страница доступна\n"
            f"📋 Объявлений найдено: <b>{len(offers)}</b>\n"
            f"🎯 Подходит под фильтры: <b>{len(matches)}</b>\n"
            f"🕑 Опубликовано за 2 минуты: <b>{len(recent)}</b>\n"
            f"🕒 Время публикации найдено у: "
            f"<b>{sum(1 for item in offers if item.get('published_at') is not None)}</b>\n"
            f"🔄 Интервал: <b>{CHECK_INTERVALS[interval]}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🟢 Получение объявлений работает.\n"
            "🟢 Фильтры проверены."
        )
        await update.message.reply_text(
            report, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="📨 Тестовое уведомление: отправка Telegram работает!",
        )
    except Exception as error:
        logger.exception("Ошибка теста мониторинга")
        await update.message.reply_text(
            f"🔴 Тест не пройден: {type(error).__name__}: {error}",
            reply_markup=MAIN_MENU,
        )


# ---------------- МОНИТОРИНГ ----------------

async def monitor_loop(application, user_id, duration, interval, stop_event):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration

    try:
        initial = await asyncio.to_thread(fetch_offers)
        recent_initial = [
            offer for offer in initial
            if is_published_recently(offer) and not is_seen(user_id, offer["id"])
        ]
        mark_many_seen(user_id, initial)
        recent_matches = matching_offers(user_id, recent_initial)

        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "🟢 <b>МОНИТОРИНГ ЗАПУЩЕН</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"⏱ Длительность: <b>{DURATIONS[duration]}</b>\n"
                f"🔄 Проверка: каждые <b>{CHECK_INTERVALS[interval]}</b>\n"
                f"📋 Уже существующих объявлений: <b>{len(initial)}</b>\n"
                f"🕑 Недавних объявлений: <b>{len(recent_initial)}</b>\n"
                "🔔 Проверяю новые объявления."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )

        for offer in recent_matches:
            if stop_event.is_set():
                break
            await application.bot.send_message(
                chat_id=user_id,
                text=offer_message(offer, recent=True),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=offer_keyboard(offer),
            )

        while loop.time() < deadline and not stop_event.is_set():
            try:
                offers = await asyncio.to_thread(fetch_offers)
                unique_offers = {offer["id"]: offer for offer in offers}
                new_offers = [
                    offer for offer in unique_offers.values()
                    if not is_seen(user_id, offer["id"])
                ]
                matches = matching_offers(user_id, new_offers)
                matching_ids = {offer["id"] for offer in matches}

                for offer in new_offers:
                    if stop_event.is_set():
                        break
                    if offer["id"] in matching_ids:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=offer_message(offer),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=offer_keyboard(offer),
                        )
                        log_action(user_id, offer["id"], "notified")
                    mark_seen(user_id, offer["id"])

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

        final_text = (
            "⛔ <b>МОНИТОРИНГ ОСТАНОВЛЕН</b>"
            if stop_event.is_set()
            else "🏁 <b>ВРЕМЯ МОНИТОРИНГА ЗАВЕРШИЛОСЬ</b>"
        )
        await application.bot.send_message(
            chat_id=user_id,
            text=final_text,
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )

    except Exception:
        logger.exception("Не удалось начать мониторинг")
        await application.bot.send_message(
            chat_id=user_id,
            text="⚠️ Не удалось начать мониторинг. Проверь /diagnostic.",
            reply_markup=MAIN_MENU,
        )
    finally:
        tasks = application.bot_data.setdefault(MONITORS_KEY, {})
        stops = application.bot_data.setdefault(STOPS_KEY, {})
        if tasks.get(user_id) is asyncio.current_task():
            tasks.pop(user_id, None)
            stops.pop(user_id, None)


async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = context.application.bot_data.setdefault(MONITORS_KEY, {})
    stops = context.application.bot_data.setdefault(STOPS_KEY, {})
    current_task = tasks.get(user_id)

    if current_task and not current_task.done():
        await update.message.reply_text(
            "⚠️ Твой мониторинг уже запущен. Сначала останови его.",
            reply_markup=MAIN_MENU,
        )
        return

    duration = context.user_data.get("monitor_duration", 900)
    interval = get_check_interval(user_id)
    stop_event = asyncio.Event()
    stops[user_id] = stop_event

    task = context.application.create_task(
        monitor_loop(
            context.application, user_id, duration, interval, stop_event
        )
    )
    tasks[user_id] = task
    await update.message.reply_text(
        f"⏳ Запускаю мониторинг на {DURATIONS[duration]}. "
        f"Интервал: {CHECK_INTERVALS[interval]}.",
        reply_markup=MAIN_MENU,
    )


async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stops = context.application.bot_data.setdefault(STOPS_KEY, {})
    stop_event = stops.get(update.effective_user.id)
    if stop_event:
        stop_event.set()
        await update.message.reply_text(
            "⏳ Останавливаю твой мониторинг...",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text(
            "Сейчас твой мониторинг не запущен.",
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
        await update.message.reply_text("❌ Введи корректное положительное число.")
        return True

    if setting == "price_min":
        context.user_data["temp_min"] = value
        context.user_data["setting"] = "price_max"
        await update.message.reply_text("Теперь введи максимальную цену за 1 Gold.")
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
            "✅ Диапазон цены сохранён!\n\n" + settings_text(user_id, context),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
    elif setting == "amount_min":
        context.user_data["temp_min"] = value
        context.user_data["setting"] = "amount_max"
        await update.message.reply_text("Теперь введи максимальное количество Gold.")
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
            "✅ Количество сохранено!\n\n" + settings_text(user_id, context),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
    return True


# ---------------- ДОСТУП И АДМИНИСТРИРОВАНИЕ ----------------

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if allowed_user(user_id):
        await update.message.reply_text("У тебя уже есть доступ.")
        return
    if not context.args:
        await update.message.reply_text("Формат: /activate ТВОЙ_КЛЮЧ")
        return

    digest = key_digest(context.args[0].strip())
    with db_connect() as db:
        exists = db.execute(
            "SELECT 1 FROM access_keys WHERE key_hash = ?", (digest,)
        ).fetchone()
        if not exists:
            await update.message.reply_text(
                "❌ Ключ недействителен или уже использован."
            )
            return
        db.execute("DELETE FROM access_keys WHERE key_hash = ?", (digest,))
        db.execute(
            "INSERT OR IGNORE INTO authorized_users (user_id) VALUES (?)",
            (user_id,),
        )

    ensure_settings(user_id)
    await update.message.reply_text(
        "✅ Доступ активирован! Теперь нажми /start.",
        reply_markup=MAIN_MENU,
    )


async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    key = secrets.token_urlsafe(9)
    with db_connect() as db:
        db.execute(
            "INSERT INTO access_keys (key_hash) VALUES (?)",
            (key_digest(key),),
        )
    await update.message.reply_text(
        f"🔑 Одноразовый ключ доступа:\n<code>{safe(key)}</code>\n"
        "Отправь его только тому пользователю, которому выдаёшь доступ.",
        parse_mode=ParseMode.HTML,
    )


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    with db_connect() as db:
        rows = db.execute(
            "SELECT user_id, activated_at FROM authorized_users "
            "ORDER BY activated_at DESC"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Пока нет активированных пользователей.")
        return
    lines = ["👥 <b>ПОЛЬЗОВАТЕЛИ С ДОСТУПОМ</b>"]
    lines.extend(
        f"• <code>{uid}</code> — {safe(activated)}"
        for uid, activated in rows
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def revoke_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /revoke TELEGRAM_ID")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID должен быть числом.")
        return
    if target == OWNER_ID:
        await update.message.reply_text("Нельзя отзывать доступ у владельца.")
        return
    with db_connect() as db:
        db.execute("DELETE FROM authorized_users WHERE user_id = ?", (target,))
    stops = context.application.bot_data.setdefault(STOPS_KEY, {})
    if target in stops:
        stops[target].set()
    await update.message.reply_text(f"Доступ для {target} отозван.")


async def action_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        return
    with db_connect() as db:
        rows = db.execute(
            "SELECT offer_id, action, created_at FROM action_history "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("История действий пока пуста.")
        return
    lines = ["📜 <b>ПОСЛЕДНИЕ ДЕЙСТВИЯ</b>"]
    lines.extend(
        f"• {safe(action)} · объявление <code>{safe(offer_id)}</code> · {safe(created)}"
        for offer_id, action, created in rows
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def skip_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Объявление пропущено.")
    offer_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    log_action(update.effective_user.id, offer_id, "skip")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------------- ОСНОВНЫЕ КОМАНДЫ И МЕНЮ ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        await update.message.reply_text(
            "🔐 Доступ закрыт.\n"
            "Для активации введи: /activate КЛЮЧ\n"
            "Ключ можно получить у администратора."
        )
        return

    ensure_settings(user_id)
    context.user_data.setdefault("monitor_duration", 900)
    await update.message.reply_text(
        "🪙 <b>STANDOFF 2 GOLD FINDER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 Поиск публичных объявлений FunPay\n"
        "💰 Фильтр по цене\n"
        "📦 Фильтр по количеству Gold\n"
        "🚀 Индивидуальный мониторинг\n"
        "🕑 Поиск недавно опубликованных объявлений\n"
        "🔔 Уведомления с кнопкой перехода на FunPay\n\n"
        "Покупку и оплату ты подтверждаешь самостоятельно.",
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
        "Настройка отменена.", reply_markup=MAIN_MENU
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        return

    text_value = update.message.text.strip()

    if text_value == "🔎 Найти объявления":
        context.user_data.pop("setting", None)
        await send_search_results(update.message, user_id)
        return
    if text_value == "🚀 Запустить":
        context.user_data.pop("setting", None)
        await start_monitor(update, context)
        return
    if text_value == "⛔ Остановить":
        context.user_data.pop("setting", None)
        await stop_monitor(update, context)
        return
    if text_value == "🧪 Тест мониторинга":
        context.user_data.pop("setting", None)
        await test_monitor(update, context)
        return
    if text_value == "⚙️ Настройки":
        context.user_data.pop("setting", None)
        await update.message.reply_text(
            "Выбери, что хочешь изменить:", reply_markup=SETTINGS_MENU
        )
        return
    if text_value == "💰 Изменить цену":
        await begin_setting(update, context, "price_min")
        return
    if text_value == "📦 Изменить количество":
        await begin_setting(update, context, "amount_min")
        return
    if text_value == "🔄 Интервал обновления":
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
    if text_value in INTERVAL_LABELS:
        interval = INTERVAL_LABELS[text_value]
        save_setting(user_id, "check_interval", interval)
        context.user_data.pop("setting", None)
        await update.message.reply_text(
            f"✅ Интервал установлен: <b>{text_value}</b>\n"
            "Новая настройка применится при следующем запуске мониторинга.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return
    if text_value == "⏱ Длительность":
        context.user_data.pop("setting", None)
        await update.message.reply_text(
            "⏱ Выбери длительность мониторинга:",
            reply_markup=DURATION_MENU,
        )
        return
    if text_value in DURATION_LABELS:
        context.user_data["monitor_duration"] = DURATION_LABELS[text_value]
        await update.message.reply_text(
            f"✅ Длительность установлена: <b>{text_value}</b>\n"
            "Теперь нажми «🚀 Запустить».",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return
    if text_value == "📋 Мои фильтры":
        context.user_data.pop("setting", None)
        await update.message.reply_text(
            settings_text(user_id, context),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return
    if text_value == "⬅️ Главное меню":
        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)
        await update.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
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
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")
    if not OWNER_ID:
        raise RuntimeError("Не задан OWNER_ID в переменных окружения")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("revoke", revoke_user))
    app.add_handler(CommandHandler("history", action_history))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("diagnostic", diagnostic))
    app.add_handler(CallbackQueryHandler(skip_offer, pattern=r"^skip:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    logger.info("Standoff 2 Gold Finder запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
