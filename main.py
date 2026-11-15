import os
import re
import html
import time
import sqlite3
import asyncio
import secrets
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ============================================================
# STANDOFF 2 GOLD FINDER
# Публичный поиск объявлений FunPay. Покупка вручную.
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("standoff_finder")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_NAME = os.getenv("DB_NAME", "settings.db")

CATEGORY_URL = "https://funpay.com/chips/140/"
DEFAULT_INTERVAL = 15
DEFAULT_DURATION = 900
RECENT_SECONDS = 120

INTERVALS = {
    "5 секунд": 5,
    "10 секунд": 10,
    "15 секунд": 15,
    "30 секунд": 30,
    "1 минута": 60,
    "2 минуты": 120,
}
DURATIONS = {
    "5 минут": 300,
    "15 минут": 900,
    "30 минут": 1800,
    "1 час": 3600,
    "2 часа": 7200,
    "6 часов": 21600,
}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🔎 Найти объявления", "🚀 Запустить мониторинг"],
        ["⛔ Остановить", "🧪 Тест мониторинга"],
        ["⚙️ Настройки", "📋 Мои фильтры"],
        ["⏱ Длительность", "❔ Помощь"],
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
    [["5 минут", "15 минут"], ["30 минут", "1 час"], ["2 часа", "6 часов"],
     ["⬅️ Главное меню"]],
    resize_keyboard=True,
)
INTERVAL_MENU = ReplyKeyboardMarkup(
    [["5 секунд", "10 секунд"], ["15 секунд", "30 секунд"],
     ["1 минута", "2 минуты"], ["⬅️ Главное меню"]],
    resize_keyboard=True,
)

MONITORS_KEY = "monitor_tasks"
STOPS_KEY = "monitor_stops"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}


# ---------------- DATABASE ----------------

def db_connect():
    return sqlite3.connect(DB_NAME, timeout=30)


def init_db():
    with db_connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                min_price REAL NOT NULL DEFAULT 0.10,
                max_price REAL NOT NULL DEFAULT 0.50,
                min_amount INTEGER NOT NULL DEFAULT 10,
                max_amount INTEGER NOT NULL DEFAULT 10000,
                check_interval INTEGER NOT NULL DEFAULT 15
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS seen_offers (
                user_id INTEGER NOT NULL,
                offer_id TEXT NOT NULL,
                PRIMARY KEY(user_id, offer_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS access_keys (
                key_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id INTEGER PRIMARY KEY,
                activated_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS action_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                offer_id TEXT,
                action TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)


def ensure_settings(user_id):
    with db_connect() as db:
        db.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (user_id,))


def get_settings(user_id):
    ensure_settings(user_id)
    with db_connect() as db:
        return db.execute(
            """SELECT min_price,max_price,min_amount,max_amount,check_interval
               FROM settings WHERE user_id=?""",
            (user_id,),
        ).fetchone()


def save_setting(user_id, field, value):
    allowed = {"min_price", "max_price", "min_amount", "max_amount", "check_interval"}
    if field not in allowed:
        raise ValueError("Недопустимая настройка")
    ensure_settings(user_id)
    with db_connect() as db:
        db.execute(f"UPDATE settings SET {field}=? WHERE user_id=?", (value, user_id))


def key_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def allowed_user(user_id):
    if user_id == OWNER_ID:
        return True
    now = time.time()
    with db_connect() as db:
        row = db.execute(
            "SELECT expires_at FROM authorized_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return False
        if row[0] <= now:
            db.execute("DELETE FROM authorized_users WHERE user_id=?", (user_id,))
            return False
        return True


def access_expiry(user_id):
    if user_id == OWNER_ID:
        return None
    with db_connect() as db:
        row = db.execute(
            "SELECT expires_at FROM authorized_users WHERE user_id=?", (user_id,)
        ).fetchone()
    return row[0] if row else None


def is_seen(user_id, offer_id):
    with db_connect() as db:
        return db.execute(
            "SELECT 1 FROM seen_offers WHERE user_id=? AND offer_id=?",
            (user_id, str(offer_id)),
        ).fetchone() is not None


def mark_seen(user_id, offer_id):
    with db_connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen_offers(user_id,offer_id) VALUES(?,?)",
            (user_id, str(offer_id)),
        )


def log_action(user_id, offer_id, action):
    with db_connect() as db:
        db.execute(
            "INSERT INTO action_history(user_id,offer_id,action,created_at) VALUES(?,?,?,?)",
            (user_id, str(offer_id or ""), action, time.time()),
        )


# ---------------- FUNPAY PARSER ----------------

def parse_number(value):
    if not value:
        return None
    value = str(value).replace("\xa0", " ").replace(",", ".").replace(" ", "")
    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def parse_published_time(value):
    if not value:
        return None
    value = str(value).strip()
    if re.fullmatch(r"\d{10,13}", value):
        try:
            stamp = int(value)
            return stamp / 1000 if stamp > 10**12 else float(stamp)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    lowered = value.lower().replace("ё", "е")
    if "только что" in lowered or "сейчас" in lowered:
        return time.time()
    match = re.search(
        r"(\d+)\s*(секунд\w*|сек\.?|минут\w*|мин\.?|час\w*|дн\w*|день)\s*назад",
        lowered,
    )
    if match:
        n = int(match.group(1))
        unit = match.group(2)
        factor = 1 if unit.startswith("сек") else 60 if unit.startswith("мин") else 3600 if unit.startswith("час") else 86400
        return time.time() - n * factor
    return None


def extract_published_time(row):
    candidates = []
    for element in row.select("time,[data-time],[data-timestamp],[datetime]"):
        for attr in ("datetime", "data-time", "data-timestamp", "title"):
            if element.get(attr):
                candidates.append(element.get(attr))
        text = element.get_text(" ", strip=True)
        if text:
            candidates.append(text)
    text = row.get_text(" ", strip=True)
    match = re.search(
        r"(только что|\d+\s*(?:секунд\w*|сек\.?|минут\w*|мин\.?|час\w*|дн\w*|день)\s*назад)",
        text.lower().replace("ё", "е"),
    )
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        parsed = parse_published_time(candidate)
        if parsed is not None:
            return parsed
    return None


def fetch_offers():
    response = requests.get(
        CATEGORY_URL,
        headers=HEADERS,
        cookies={"cy": "rub", "locale": "ru"},
        timeout=25,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("a.tc-item")
    offers = []
    skipped_currency = 0

    for row in rows:
        price_el = row.select_one(".tc-price")
        amount_el = row.select_one(".tc-amount")
        if not price_el or not amount_el:
            continue

        price_text = price_el.get_text(" ", strip=True)
        low = price_text.lower()
        if not any(x in low for x in ("₽", "руб", "rub")):
            skipped_currency += 1
            continue

        price = parse_number(price_text)
        amount_value = parse_number(amount_el.get_text(" ", strip=True))
        if price is None or amount_value is None:
            continue

        href = row.get("href")
        if not href:
            continue
        link = urljoin(CATEGORY_URL, href)
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or parsed.netloc not in ("funpay.com", "www.funpay.com"):
            continue

        offer_id = parse_qs(parsed.query).get("id", [parsed.path])[0]
        seller_el = row.select_one(".media-user-name")
        platform_el = row.select_one(".tc-server") or row.select_one(".tc-side") or row.select_one(".tc-desc-text")

        offers.append({
            "id": str(offer_id),
            "price": price,
            "amount": int(amount_value),
            "platform": platform_el.get_text(" ", strip=True) if platform_el else "Не указана",
            "seller": seller_el.get_text(" ", strip=True) if seller_el else "Не указан",
            "link": link,
            "published_at": extract_published_time(row),
        })

    log.info("FunPay: карточек=%d, рублёвых объявлений=%d", len(rows), len(offers))
    if not offers and rows and skipped_currency:
        raise ValueError("FunPay вернул цены не в рублях. Проверь валюту страницы.")
    return offers


# ---------------- SEARCH AND MESSAGES ----------------

def matching_offers(user_id, offers):
    min_price, max_price, min_amount, max_amount, _ = get_settings(user_id)
    result = [
        offer for offer in offers
        if min_price <= offer["price"] <= max_price
        and min_amount <= offer["amount"] <= max_amount
    ]
    return sorted(result, key=lambda item: item["price"])


def fmt(value):
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def safe(value):
    return html.escape(str(value or "Не указан"), quote=True)


def offer_keyboard(offer):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Открыть на FunPay", url=offer["link"]),
        InlineKeyboardButton("Пропустить", callback_data=f"skip:{offer['id'][:40]}"),
    ]])


def offer_message(offer, number=None, recent=False):
    if recent:
        heading = "🕑 <b>ОБЪЯВЛЕНИЕ ЗА ПОСЛЕДНИЕ 2 МИНУТЫ</b>"
    elif number is not None:
        heading = f"🆕 <b>ОБЪЯВЛЕНИЕ №{number}</b>"
    else:
        heading = "🪙 <b>НОВОЕ ОБЪЯВЛЕНИЕ FUNPAY</b>"
    return (
        f"{heading}\n━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена: <b>{fmt(offer['price'])} ₽ за 1 Gold</b>\n"
        f"📦 Количество: <b>{offer['amount']} Gold</b>\n"
        f"📱 Платформа: <b>{safe(offer['platform'])}</b>\n"
        f"👤 Продавец: <b>{safe(offer['seller'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Нажми кнопку, чтобы открыть объявление. Покупку и оплату выполняй самостоятельно."
    )


def settings_text(user_id, context):
    min_p, max_p, min_a, max_a, interval = get_settings(user_id)
    duration = context.user_data.get("monitor_duration", DEFAULT_DURATION)
    running = context.application.bot_data.setdefault(MONITORS_KEY, {}).get(user_id)
    is_running = bool(running and not running.done())
    expiry = access_expiry(user_id)
    access_line = "👑 Владелец" if user_id == OWNER_ID else (
        "🔐 Доступ до " + datetime.fromtimestamp(expiry, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        if expiry else "🔒 Нет активного доступа"
    )
    duration_label = next((k for k, v in DURATIONS.items() if v == duration), "15 минут")
    interval_label = next((k for k, v in INTERVALS.items() if v == interval), f"{interval} сек.")
    return (
        "⚙️ <b>ТВОИ НАСТРОЙКИ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена за 1 Gold: <b>{fmt(min_p)}–{fmt(max_p)} ₽</b>\n"
        f"📦 Количество: <b>{min_a}–{max_a} Gold</b>\n"
        f"⏱ Длительность: <b>{duration_label}</b>\n"
        f"🔄 Интервал: <b>{interval_label}</b>\n"
        f"📡 Мониторинг: <b>{'🟢 Работает' if is_running else '⚪ Остановлен'}</b>\n"
        f"{access_line}"
    )


async def send_search_results(message, user_id):
    await message.reply_text("🔎 Ищу объявления на публичной странице FunPay…", reply_markup=MAIN_MENU)
    try:
        offers = await asyncio.to_thread(fetch_offers)
        matches = matching_offers(user_id, offers)
    except Exception as error:
        log.exception("Ошибка поиска")
        await message.reply_text(
            f"⚠️ Не удалось выполнить поиск: {type(error).__name__}: {error}",
            reply_markup=MAIN_MENU,
        )
        return
    if not matches:
        await message.reply_text(
            "😕 Подходящих объявлений нет. Попробуй изменить фильтры.",
            reply_markup=MAIN_MENU,
        )
        return
    await message.reply_text(f"✅ Найдено объявлений: {len(matches)}", reply_markup=MAIN_MENU)
    for index, offer in enumerate(matches[:20], start=1):
        await message.reply_text(
            offer_message(offer, index),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=offer_keyboard(offer),
        )


# ---------------- MONITORING ----------------

async def monitor_loop(application, user_id, duration, interval, stop_event):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    try:
        await application.bot.send_message(
            user_id,
            f"🟢 Мониторинг запущен на {duration // 60} мин. Проверка каждые {interval} сек.",
            reply_markup=MAIN_MENU,
        )
        while not stop_event.is_set() and loop.time() < deadline:
            try:
                offers = await asyncio.to_thread(fetch_offers)
                matches = matching_offers(user_id, offers)
                for offer in matches:
                    if stop_event.is_set():
                        break
                    if not is_seen(user_id, offer["id"]):
                        await application.bot.send_message(
                            user_id,
                            offer_message(offer),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=offer_keyboard(offer),
                        )
                        mark_seen(user_id, offer["id"])
                        log_action(user_id, offer["id"], "notified")
            except Exception:
                log.exception("Ошибка проверки FunPay")
                await application.bot.send_message(
                    user_id, "⚠️ Ошибка проверки FunPay. Повторю попытку позже."
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=min(interval, remaining))
            except asyncio.TimeoutError:
                pass
        text = "⛔ Мониторинг остановлен." if stop_event.is_set() else "🏁 Время мониторинга закончилось."
        await application.bot.send_message(user_id, text, reply_markup=MAIN_MENU)
    finally:
        tasks = application.bot_data.setdefault(MONITORS_KEY, {})
        stops = application.bot_data.setdefault(STOPS_KEY, {})
        if tasks.get(user_id) is asyncio.current_task():
            tasks.pop(user_id, None)
            stops.pop(user_id, None)


async def start_monitor(update, context):
    user_id = update.effective_user.id
    tasks = context.application.bot_data.setdefault(MONITORS_KEY, {})
    stops = context.application.bot_data.setdefault(STOPS_KEY, {})
    current = tasks.get(user_id)
    if current and not current.done():
        await update.message.reply_text("⚠️ Мониторинг уже запущен.", reply_markup=MAIN_MENU)
        return
    duration = context.user_data.get("monitor_duration", DEFAULT_DURATION)
    interval = get_settings(user_id)[4]
    stop_event = asyncio.Event()
    stops[user_id] = stop_event
    tasks[user_id] = context.application.create_task(
        monitor_loop(context.application, user_id, duration, interval, stop_event)
    )
    await update.message.reply_text("⏳ Запускаю мониторинг…", reply_markup=MAIN_MENU)


async def stop_monitor(update, context):
    stop_event = context.application.bot_data.setdefault(STOPS_KEY, {}).get(update.effective_user.id)
    if stop_event:
        stop_event.set()
        await update.message.reply_text("⏳ Останавливаю мониторинг…", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Мониторинг сейчас не запущен.", reply_markup=MAIN_MENU)


async def test_monitor(update, context):
    user_id = update.effective_user.id
    await update.message.reply_text("🧪 Проверяю FunPay и твои фильтры…")
    try:
        offers = await asyncio.to_thread(fetch_offers)
        matches = matching_offers(user_id, offers)
        recent = [x for x in matches if x.get("published_at") and 0 <= time.time() - x["published_at"] <= RECENT_SECONDS]
        await update.message.reply_text(
            f"✅ FunPay доступен.\n"
            f"Объявлений в рублях: {len(offers)}\n"
            f"Подходят под фильтры: {len(matches)}\n"
            f"Опубликовано примерно за 2 минуты: {len(recent)}",
            reply_markup=MAIN_MENU,
        )
    except Exception as exc:
        await update.message.reply_text(
            f"⚠️ Проверка не прошла: {type(exc).__name__}: {exc}",
            reply_markup=MAIN_MENU,
        )


# ---------------- SETTINGS UI ----------------

async def begin_setting(update, context, setting):
    context.user_data["setting"] = setting
    context.user_data.pop("temp_min", None)
    prompts = {
        "price_min": "💰 Введи минимальную цену за 1 Gold (например, 0.10).",
        "amount_min": "📦 Введи минимальное количество Gold (например, 10).",
    }
    await update.message.reply_text(
        prompts[setting] + "\nДля отмены нажми «⬅️ Главное меню».",
        reply_markup=SETTINGS_MENU,
    )


async def receive_setting_value(update, context):
    setting = context.user_data.get("setting")
    if not setting:
        return False
    user_id = update.effective_user.id
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
        minimum = context.user_data["temp_min"]
        if value < minimum:
            await update.message.reply_text("Максимальная цена не может быть меньше минимальной.")
            return True
        save_setting(user_id, "min_price", minimum)
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
        minimum = context.user_data["temp_min"]
        if value < minimum:
            await update.message.reply_text("Максимальное количество не может быть меньше минимального.")
            return True
        save_setting(user_id, "min_amount", minimum)
        save_setting(user_id, "max_amount", value)
        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)
        await update.message.reply_text(
            "✅ Количество сохранено!\n\n" + settings_text(user_id, context),
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
    return True


# ---------------- ACCESS KEYS / ADMIN ----------------

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if allowed_user(user_id):
        await update.message.reply_text("У тебя уже есть активный доступ.", reply_markup=MAIN_MENU)
        return
    if not context.args:
        await update.message.reply_text("Формат: /activate ТВОЙ_КЛЮЧ")
        return

    digest = key_digest(context.args[0].strip())
    with db_connect() as db:
        row = db.execute(
            "SELECT expires_at FROM access_keys WHERE key_hash=?", (digest,)
        ).fetchone()
        if not row:
            await update.message.reply_text("❌ Ключ неверный или уже использован.")
            return
        expires_at = float(row[0])
        if expires_at <= time.time():
            db.execute("DELETE FROM access_keys WHERE key_hash=?", (digest,))
            await update.message.reply_text("⌛ Срок действия ключа закончился. Попроси новый.")
            return
        db.execute("DELETE FROM access_keys WHERE key_hash=?", (digest,))
        db.execute(
            "INSERT OR REPLACE INTO authorized_users(user_id,activated_at,expires_at) VALUES(?,?,?)",
            (user_id, time.time(), expires_at),
        )

    ensure_settings(user_id)
    expiry_text = datetime.fromtimestamp(expires_at, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    await update.message.reply_text(
        f"✅ <b>ДОСТУП АКТИВИРОВАН</b>\nДействует до: <b>{expiry_text}</b>\nНажми /start.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    key = secrets.token_urlsafe(12)
    created = time.time()
    expires = created + 24 * 60 * 60
    with db_connect() as db:
        db.execute(
            "INSERT INTO access_keys(key_hash,expires_at,created_at) VALUES(?,?,?)",
            (key_digest(key), expires, created),
        )
    expiry_text = datetime.fromtimestamp(expires, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    await update.message.reply_text(
        "🔑 <b>НОВЫЙ КЛЮЧ СОЗДАН</b>\n"
        f"<code>{safe(key)}</code>\n\n"
        "⏳ Ключ действует 24 часа с момента создания.\n"
        f"⌛ Истекает: <b>{expiry_text}</b>\n"
        "Ключ одноразовый. Пользователь активирует его командой /activate.",
        parse_mode=ParseMode.HTML,
    )


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    with db_connect() as db:
        rows = db.execute(
            "SELECT user_id,expires_at FROM authorized_users ORDER BY expires_at DESC"
        ).fetchall()
    now = time.time()
    lines = ["👥 <b>ПОЛЬЗОВАТЕЛИ С ДОСТУПОМ</b>"]
    for uid, expires in rows:
        state = "активен" if expires > now else "истёк"
        exp_text = datetime.fromtimestamp(expires, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        lines.append(f"• <code>{uid}</code> — {state}, до {exp_text}")
    if len(lines) == 1:
        lines.append("Пока никому не выдан доступ.")
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
        db.execute("DELETE FROM authorized_users WHERE user_id=?", (target,))
    stop = context.application.bot_data.setdefault(STOPS_KEY, {}).get(target)
    if stop:
        stop.set()
    await update.message.reply_text(f"Доступ пользователя {target} отозван.")


async def action_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        return
    with db_connect() as db:
        rows = db.execute(
            "SELECT offer_id,action,created_at FROM action_history WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("История действий пока пуста.")
        return
    lines = ["📜 <b>ПОСЛЕДНИЕ УВЕДОМЛЕНИЯ</b>"]
    for offer_id, action, created in rows:
        stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%d.%m %H:%M UTC")
        lines.append(f"• {safe(action)} · лот <code>{safe(offer_id)}</code> · {stamp}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------- COMMANDS AND MENU ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        await update.message.reply_text(
            "🔐 <b>ДОСТУП ЗАКРЫТ</b>\n"
            "Попроси администратора выдать ключ на 24 часа.\n"
            "Когда получишь ключ, введи: /activate ТВОЙ_КЛЮЧ",
            parse_mode=ParseMode.HTML,
        )
        return
    ensure_settings(user_id)
    context.user_data.setdefault("monitor_duration", DEFAULT_DURATION)
    await update.message.reply_text(
        "🪙 <b>STANDOFF 2 GOLD FINDER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Выбери действие в меню:\n\n"
        "🔎 <b>Найти объявления</b> — разовый поиск.\n"
        "🚀 <b>Запустить мониторинг</b> — получать новые предложения.\n"
        "⚙️ <b>Настройки</b> — изменить цену и количество Gold.\n"
        "⏱ <b>Длительность</b> — выбрать время мониторинга.\n"
        "📋 <b>Мои фильтры</b> — посмотреть настройки и срок доступа.\n\n"
        "🔐 Доступ пользователей выдаётся по одноразовому ключу на 24 часа.\n"
        "Покупку и оплату выполняй самостоятельно.",
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
    await update.message.reply_text("Настройка отменена.", reply_markup=MAIN_MENU)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❔ <b>ПОМОЩЬ</b>\n\n"
        "1. Нажми «🔎 Найти объявления» для разового поиска.\n"
        "2. Настрой диапазон цены и количество Gold.\n"
        "3. Выбери длительность и интервал мониторинга.\n"
        "4. Нажми «🚀 Запустить мониторинг».\n"
        "5. Чтобы завершить раньше, нажми «⛔ Остановить».\n\n"
        "Администратор: /genkey — создать ключ на 24 часа; "
        "/users — список пользователей; /revoke ID — отозвать доступ.\n"
        "Пользователь: /activate КЛЮЧ — активировать ключ."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def skip_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Объявление пропущено.")
    offer_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    log_action(update.effective_user.id, offer_id, "пропущено")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not allowed_user(user_id):
        return
    text = update.message.text.strip()

    if text == "⬅️ Главное меню":
        context.user_data.pop("setting", None)
        context.user_data.pop("temp_min", None)
        await update.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
        return

    if await receive_setting_value(update, context):
        return

    if text == "🔎 Найти объявления":
        context.user_data.pop("setting", None)
        await send_search_results(update.message, user_id)
    elif text == "🚀 Запустить мониторинг":
        context.user_data.pop("setting", None)
        await start_monitor(update, context)
    elif text == "⛔ Остановить":
        await stop_monitor(update, context)
    elif text == "🧪 Тест мониторинга":
        await test_monitor(update, context)
    elif text == "⚙️ Настройки":
        context.user_data.pop("setting", None)
        await update.message.reply_text("Выбери, что изменить:", reply_markup=SETTINGS_MENU)
    elif text == "💰 Изменить цену":
        await begin_setting(update, context, "price_min")
    elif text == "📦 Изменить количество":
        await begin_setting(update, context, "amount_min")
    elif text == "🔄 Интервал обновления":
        current = get_settings(user_id)[4]
        label = next((k for k, v in INTERVALS.items() if v == current), "15 секунд")
        await update.message.reply_text(
            f"Сейчас интервал: <b>{label}</b>\nВыбери новый интервал:",
            parse_mode=ParseMode.HTML,
            reply_markup=INTERVAL_MENU,
        )
        context.user_data["choosing_interval"] = True
    elif text in INTERVALS:
        if not context.user_data.get("choosing_interval"):
            await update.message.reply_text("Открой «Настройки» → «Интервал обновления».")
            return
        save_setting(user_id, "check_interval", INTERVALS[text])
        context.user_data.pop("choosing_interval", None)
        await update.message.reply_text(
            f"✅ Интервал установлен: {text}. Новое значение применится при следующем запуске.",
            reply_markup=MAIN_MENU,
        )
    elif text == "⏱ Длительность":
        await update.message.reply_text("Выбери длительность мониторинга:", reply_markup=DURATION_MENU)
    elif text in DURATIONS:
        context.user_data["monitor_duration"] = DURATIONS[text]
        await update.message.reply_text(
            f"✅ Длительность установлена: {text}. Теперь нажми «🚀 Запустить мониторинг».",
            reply_markup=MAIN_MENU,
        )
    elif text == "📋 Мои фильтры":
        await status(update, context)
    elif text == "❔ Помощь":
        await help_command(update, context)
    else:
        await update.message.reply_text("Выбери действие с помощью кнопок меню.", reply_markup=MAIN_MENU)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Добавь BOT_TOKEN в переменные окружения.")
    if not OWNER_ID:
        raise RuntimeError("Добавь OWNER_ID в переменные окружения.")

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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(skip_offer, pattern=r"^skip:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Standoff 2 Gold Finder запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
