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
# Публичный поиск объявлений FunPay и уведомления в Telegram.
# Покупка и оплата выполняются пользователем самостоятельно.
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
KEY_LIFETIME_SECONDS = 24 * 60 * 60

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
DURATION_LABELS = {v: k for k, v in DURATIONS.items()}
INTERVAL_LABELS = {v: k for k, v in CHECK_INTERVALS.items()}

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


# ---------------- DATABASE ----------------

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
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(settings)")
        }
        if "check_interval" not in columns:
            db.execute(
                "ALTER TABLE settings ADD COLUMN check_interval INTEGER DEFAULT 15"
            )

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
                expires_at REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Совместимость со старой БД, где access_keys могла быть без expires_at.
        key_columns = {
            row[1] for row in db.execute("PRAGMA table_info(access_keys)")
        }
        if "expires_at" not in key_columns:
            db.execute("ALTER TABLE access_keys ADD COLUMN expires_at REAL")
            db.execute(
                "UPDATE access_keys SET expires_at = ? WHERE expires_at IS NULL",
                (time.time(),),
            )

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
        db.execute(
            "INSERT OR IGNORE INTO settings (user_id) VALUES (?)",
            (user_id,),
        )


def get_settings(user_id):
    ensure_settings(user_id)
    with db_connect() as db:
        return db.execute(
            """SELECT min_price, max_price, min_amount, max_amount
               FROM settings WHERE user_id = ?""",
            (user_id,),
        ).fetchone()


def get_check_interval(user_id):
    ensure_settings(user_id)
    with db_connect() as db:
        row = db.execute(
            "SELECT check_interval FROM settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    value = row[0] if row else DEFAULT_CHECK_INTERVAL
    return value if value in CHECK_INTERVALS else DEFAULT_CHECK_INTERVAL


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
            """INSERT INTO action_history (user_id, offer_id, action)
               VALUES (?, ?, ?)""",
            (user_id, str(offer_id or ""), action),
        )


def is_seen(user_id, offer_id):
    with db_connect() as db:
        return db.execute(
            """SELECT 1 FROM seen_offers_v2
               WHERE user_id = ? AND offer_id = ?""",
            (user_id, str(offer_id)),
        ).fetchone() is not None


def mark_seen(user_id, offer_id):
    with db_connect() as db:
        db.execute(
            """INSERT OR IGNORE INTO seen_offers_v2 (user_id, offer_id)
               VALUES (?, ?)""",
            (user_id, str(offer_id)),
        )


def mark_many_seen(user_id, offers):
    with db_connect() as db:
        db.executemany(
            """INSERT OR IGNORE INTO seen_offers_v2 (user_id, offer_id)
               VALUES (?, ?)""",
            [(user_id, str(item["id"])) for item in offers],
        )


# ---------------- FORMATTING ----------------

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
    status_text = (
        f"🟢 Работает ({duration_text})" if running else "⚪ Остановлен"
    )
    return (
        "⚙️ <b>ТВОИ НАСТРОЙКИ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена за 1 Gold: <b>{fmt(min_p)}–{fmt(max_p)} ₽</b>\n"
        f"📦 Количество: <b>{min_a}–{max_a} Gold</b>\n"
        f"⏱ Длительность: <b>{duration_text}</b>\n"
        f"🔄 Интервал: <b>{CHECK_INTERVALS[interval]}</b>\n"
        f"📡 Мониторинг: <b>{status_text}</b>"
    )


# ---------------- FUNPAY PARSER ----------------

def parse_number(value):
    if not value:
        return None
    cleaned = (
        str(value).replace("\xa0", " ")
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
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
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

    # На странице FunPay объявления обычно представлены ссылками tc-item.
    rows = soup.select("a.tc-item")
    if not rows:
        # Запасной селектор на случай небольшого изменения разметки.
        rows = soup.select(".tc-item")

    offers = []
    skipped = 0

    logger.info(
        "FunPay HTTP %s; найдено карточек: %s",
        response.status_code,
        len(rows),
    )

    for row in rows:
        price_element = row.select_one(".tc-price")
        amount_element = row.select_one(".tc-amount")
        if not price_element or not amount_element:
            skipped += 1
            continue

        price_text = price_element.get_text(" ", strip=True)
        normalized = price_text.lower()
        is_rub = (
            "₽" in price_text
            or "rub" in normalized
            or "руб" in normalized
        )
        if not is_rub:
            skipped += 1
            continue

        total_price = parse_number(price_text)
        amount_value = parse_number(amount_element.get_text(" ", strip=True))
        if total_price is None or amount_value is None or amount_value <= 0:
            skipped += 1
            continue

        # В фильтрах хранится цена за 1 Gold, а на карточке может быть
        # указана полная стоимость лота.
        unit_price = total_price / amount_value

        href = row.get("href")
        if not href:
            skipped += 1
            continue

        link = urljoin(CATEGORY_URL, href)
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or parsed.netloc not in (
            "funpay.com", "www.funpay.com"
        ):
            skipped += 1
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
            "price": unit_price,
            "total_price": total_price,
            "amount": int(amount_value),
            "platform": platform,
            "seller": seller,
            "link": link,
        })

    logger.info(
        "FunPay: обработано %s объявлений; пропущено карточек: %s",
        len(offers),
        skipped,
    )
    return offers


# ---------------- FILTERS AND MESSAGES ----------------

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
        InlineKeyboardButton(
            "Пропустить",
            callback_data=f"skip:{offer_id}",
        ),
    ]])


def offer_message(offer, number=None):
    heading = (
        f"🆕 <b>ОБЪЯВЛЕНИЕ №{number}</b>"
        if number is not None
        else "🪙 <b>НОВОЕ ОБЪЯВЛЕНИЕ FUNPAY</b>"
    )
    total = offer.get(
        "total_price",
        offer["price"] * offer["amount"],
    )
    return (
        f"{heading}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена за 1 Gold: <b>{fmt(offer['price'])} ₽</b>\n"
        f"📦 Количество: <b>{offer['amount']} Gold</b>\n"
        f"💵 Цена всего лота: <b>{fmt(total)} ₽</b>\n"
        f"📱 Платформа: <b>{safe(offer['platform'])}</b>\n"
        f"👤 Продавец: <b>{safe(offer['seller'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
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
        min_p, max_p, min_a, max_a = get_settings(user_id)
        await message.reply_text(
            "😕 Подходящих объявлений пока нет.\n"
            f"Фильтры: {fmt(min_p)}–{fmt(max_p)} ₽ за Gold, "
            f"{min_a}–{max_a} Gold.\n"
            "Попробуй расширить диапазон.",
            reply_markup=MAIN_MENU,
        )
        return

    await message.reply_text(
        f"✅ Найдено подходящих объявлений: {len(matches)}",
        reply_markup=MAIN_MENU,
    )
    for index, offer in enumerate(matches[:20], start=1):
        await message.reply_text(
            offer_message(offer, index),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=offer_keyboard(offer),
        )


# ---------------- DIAGNOSTICS ----------------

async def diagnostic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        return
    try:
        offers = await asyncio.to_thread(fetch_offers)
        matches = matching_offers(user_id, offers)
        min_p, max_p, min_a, max_a = get_settings(user_id)

        examples = offers[:5]
        lines = [
            "🔬 ДИАГНОСТИКА FUNPAY",
            f"Объявлений обработано: {len(offers)}",
            f"Подходит под фильтры: {len(matches)}",
            f"Фильтр цены за Gold: {fmt(min_p)}–{fmt(max_p)} ₽",
            f"Фильтр количества: {min_a}–{max_a} Gold",
            "",
            "Первые объявления:",
        ]
        for item in examples:
            lines.append(
                f"• {fmt(item['price'])} ₽/Gold; "
                f"{item['amount']} Gold; "
                f"всего {fmt(item['total_price'])} ₽"
            )
        if not examples:
            lines.append("Карточки не удалось разобрать.")
        await update.message.reply_text("\n".join(lines))
    except Exception as error:
        logger.exception("Ошибка диагностики")
        await update.message.reply_text(
            f"Ошибка диагностики: {type(error).__name__}: {error}"
        )


async def test_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        return
    await update.message.reply_text(
        "🧪 Проверяю FunPay, фильтры и отправку Telegram...",
        reply_markup=MAIN_MENU,
    )
    try:
        offers = await asyncio.to_thread(fetch_offers)
        matches = matching_offers(user_id, offers)
        min_p, max_p, min_a, max_a = get_settings(user_id)
        report = (
            "🧪 <b>РЕЗУЛЬТАТ ТЕСТА</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📋 Объявлений обработано: <b>{len(offers)}</b>\n"
            f"🎯 Подходит под фильтры: <b>{len(matches)}</b>\n"
            f"💰 Цена за Gold: <b>{fmt(min_p)}–{fmt(max_p)} ₽</b>\n"
            f"📦 Количество: <b>{min_a}–{max_a} Gold</b>\n"
            f"🔄 Интервал: <b>{CHECK_INTERVALS[get_check_interval(user_id)]}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Отправка тестового сообщения проверяется отдельно."
        )
        await update.message.reply_text(
            report,
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="📨 Тестовое уведомление: Telegram может отправлять сообщения.",
        )
    except Exception as error:
        logger.exception("Ошибка теста мониторинга")
        await update.message.reply_text(
            f"🔴 Тест не пройден: {type(error).__name__}: {error}",
            reply_markup=MAIN_MENU,
        )


# ---------------- MONITORING ----------------

async def monitor_loop(application, user_id, duration, interval, stop_event):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration

    try:
        # При первом запуске запоминаем уже существующие лоты, чтобы
        # не рассылать сотни старых объявлений. Новые лоты будут приходить
        # после следующей проверки.
        initial = await asyncio.to_thread(fetch_offers)
        mark_many_seen(user_id, initial)
        initial_matches = matching_offers(user_id, initial)

        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "🟢 <b>МОНИТОРИНГ ЗАПУЩЕН</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"⏱ Длительность: <b>{DURATIONS[duration]}</b>\n"
                f"🔄 Проверка: каждые <b>{CHECK_INTERVALS[interval]}</b>\n"
                f"📋 Объявлений на старте: <b>{len(initial)}</b>\n"
                f"🎯 Сейчас подходит фильтрам: <b>{len(initial_matches)}</b>\n"
                "🔔 Жду новые объявления."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )

        while loop.time() < deadline and not stop_event.is_set():
            try:
                offers = await asyncio.to_thread(fetch_offers)
                unique = {str(item["id"]): item for item in offers}
                new_offers = [
                    item for item in unique.values()
                    if not is_seen(user_id, item["id"])
                ]
                matches = matching_offers(user_id, new_offers)
                matching_ids = {str(item["id"]) for item in matches}

                for offer in new_offers:
                    if stop_event.is_set():
                        break
                    if str(offer["id"]) in matching_ids:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=offer_message(offer),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=offer_keyboard(offer),
                        )
                        log_action(user_id, offer["id"], "notified")
                    mark_seen(user_id, offer["id"])

            except Exception as error:
                logger.exception("Ошибка проверки FunPay")
                try:
                    await application.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "⚠️ Ошибка проверки FunPay: "
                            f"{type(error).__name__}: {error}"
                        ),
                        reply_markup=MAIN_MENU,
                    )
                except Exception:
                    logger.exception("Не удалось отправить сообщение об ошибке")

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
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text="⚠️ Не удалось начать мониторинг. Проверь /diagnostic.",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            logger.exception("Не удалось отправить сообщение о сбое")
    finally:
        tasks = application.bot_data.setdefault(MONITORS_KEY, {})
        stops = application.bot_data.setdefault(STOPS_KEY, {})
        if tasks.get(user_id) is asyncio.current_task():
            tasks.pop(user_id, None)
            stops.pop(user_id, None)


async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        return

    tasks = context.application.bot_data.setdefault(MONITORS_KEY, {})
    stops = context.application.bot_data.setdefault(STOPS_KEY, {})
    current = tasks.get(user_id)

    if current and not current.done():
        await update.message.reply_text(
            "⚠️ Мониторинг уже запущен. Сначала останови его.",
            reply_markup=MAIN_MENU,
        )
        return

    duration = context.user_data.get("monitor_duration", 900)
    interval = get_check_interval(user_id)
    stop_event = asyncio.Event()
    stops[user_id] = stop_event

    task = context.application.create_task(
        monitor_loop(
            context.application,
            user_id,
            duration,
            interval,
            stop_event,
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
    event = stops.get(update.effective_user.id)
    if event:
        event.set()
        await update.message.reply_text(
            "⏳ Останавливаю мониторинг...",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text(
            "Сейчас мониторинг не запущен.",
            reply_markup=MAIN_MENU,
        )


# ---------------- SETTINGS ----------------

async def begin_setting(update, context, setting):
    context.user_data["setting"] = setting
    context.user_data.pop("temp_min", None)

    if setting == "price_min":
        prompt = (
            "💰 Введи минимальную цену за 1 Gold.\n"
            "Например: 0.10"
        )
    elif setting == "amount_min":
        prompt = (
            "📦 Введи минимальное количество Gold.\n"
            "Например: 10"
        )
    else:
        return

    await update.message.reply_text(
        prompt + "\nДля отмены нажми «⬅️ Главное меню».",
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

    if setting in ("price_min", "amount_min"):
        context.user_data["temp_min"] = value
        context.user_data["setting"] = (
            "price_max" if setting == "price_min" else "amount_max"
        )
        await update.message.reply_text(
            "Теперь введи максимальное значение."
        )
        return True

    min_value = context.user_data["temp_min"]
    if value < min_value:
        await update.message.reply_text(
            "Максимальное значение не может быть меньше минимального."
        )
        return True

    if setting == "price_max":
        save_setting(user_id, "min_price", min_value)
        save_setting(user_id, "max_price", value)
        label = "Диапазон цены сохранён!"
    else:
        save_setting(user_id, "min_amount", min_value)
        save_setting(user_id, "max_amount", value)
        label = "Количество сохранено!"

    context.user_data.pop("setting", None)
    context.user_data.pop("temp_min", None)
    await update.message.reply_text(
        "✅ " + label + "\n\n" + settings_text(user_id, context),
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )
    return True


# ---------------- ACCESS KEYS AND ADMIN ----------------

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if allowed_user(user_id):
        await update.message.reply_text("У тебя уже есть доступ.")
        return
    if not context.args:
        await update.message.reply_text("Формат: /activate ТВОЙ_КЛЮЧ")
        return

    digest = key_digest(context.args[0].strip())
    now = time.time()

    with db_connect() as db:
        row = db.execute(
            "SELECT expires_at FROM access_keys WHERE key_hash = ?",
            (digest,),
        ).fetchone()

        if not row or row[0] is None or row[0] <= now:
            db.execute(
                "DELETE FROM access_keys WHERE key_hash = ?",
                (digest,),
            )
            await update.message.reply_text(
                "❌ Ключ недействителен, просрочен или уже использован."
            )
            return

        db.execute(
            "DELETE FROM access_keys WHERE key_hash = ?",
            (digest,),
        )
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
    expiry = time.time() + KEY_LIFETIME_SECONDS

    with db_connect() as db:
        db.execute(
            "INSERT INTO access_keys (key_hash, expires_at) VALUES (?, ?)",
            (key_digest(key), expiry),
        )

    await update.message.reply_text(
        f"🔑 Одноразовый ключ:\n<code>{safe(key)}</code>\n"
        "⏳ Он действует 24 часа с момента создания и используется один раз.",
        parse_mode=ParseMode.HTML,
    )


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    with db_connect() as db:
        rows = db.execute(
            """SELECT user_id, activated_at FROM authorized_users
               ORDER BY activated_at DESC"""
        ).fetchall()

    if not rows:
        await update.message.reply_text("Пока нет пользователей с доступом.")
        return

    lines = ["👥 <b>ПОЛЬЗОВАТЕЛИ С ДОСТУПОМ</b>"]
    lines.extend(
        f"• <code>{uid}</code> — {safe(activated)}"
        for uid, activated in rows
    )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


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
        db.execute(
            "DELETE FROM authorized_users WHERE user_id = ?",
            (target,),
        )

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
            """SELECT offer_id, action, created_at FROM action_history
               WHERE user_id = ? ORDER BY id DESC LIMIT 10""",
            (user_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("История действий пока пуста.")
        return

    lines = ["📜 <b>ПОСЛЕДНИЕ ДЕЙСТВИЯ</b>"]
    lines.extend(
        f"• {safe(action)} · объявление <code>{safe(offer_id)}</code> · "
        f"{safe(created)}"
        for offer_id, action, created in rows
    )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def skip_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Объявление пропущено.")
    offer_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    log_action(update.effective_user.id, offer_id, "skip")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------------- START, STATUS AND MENU ----------------

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
        "💰 Фильтр по цене за 1 Gold\n"
        "📦 Фильтр по количеству Gold\n"
        "🚀 Индивидуальный мониторинг\n"
        "🔔 Уведомления о новых подходящих объявлениях\n\n"
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
        "Настройка отменена.",
        reply_markup=MAIN_MENU,
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
            "Выбери, что хочешь изменить:",
            reply_markup=SETTINGS_MENU,
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
            "Настройка применится при следующем запуске мониторинга.",
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


# ---------------- APPLICATION ----------------

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
    app.add_handler(
        CallbackQueryHandler(skip_offer, pattern=r"^skip:")
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    logger.info("Standoff 2 Gold Finder запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
