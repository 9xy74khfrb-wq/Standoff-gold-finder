#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standoff 2 Gold Finder
Environment:
  BOT_TOKEN - Telegram bot token
  OWNER_ID  - Telegram numeric user ID (optional but recommended)

Install:
  pip install "python-telegram-bot[job-queue]>=21,<22" requests beautifulsoup4
Run:
  python main.py

The bot reads public FunPay listings. Purchases and payments are always handled manually.
"""

import os
import re
import json
import time
import math
import asyncio
import logging
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None

FUNPAY_URL = "https://funpay.com/chips/140/"
DB_PATH = os.getenv("DB_PATH", "gold_finder.sqlite3")
DEFAULT_MIN_PRICE = 0.10
DEFAULT_MAX_PRICE = 0.50
DEFAULT_MIN_AMOUNT = 10
DEFAULT_MAX_AMOUNT = 10000
DEFAULT_INTERVAL = 15
DEFAULT_DURATION_HOURS = 24
REQUEST_TIMEOUT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("gold-finder")

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
})


# ----------------------------- DATABASE ------------------------------------

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            min_price REAL NOT NULL DEFAULT 0.10,
            max_price REAL NOT NULL DEFAULT 0.50,
            min_amount INTEGER NOT NULL DEFAULT 10,
            max_amount INTEGER NOT NULL DEFAULT 10000,
            interval_seconds INTEGER NOT NULL DEFAULT 15,
            monitor_until TEXT,
            monitoring INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS seen_offers (
            user_id INTEGER NOT NULL,
            offer_id TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            PRIMARY KEY(user_id, offer_id)
        );
        CREATE TABLE IF NOT EXISTS access_keys (
            key_hash TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            used_by INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_user(user_id: int) -> None:
    with db() as con:
        con.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))


def get_user(user_id: int) -> sqlite3.Row:
    ensure_user(user_id)
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def set_user_field(user_id: int, field: str, value) -> None:
    allowed = {
        "min_price", "max_price", "min_amount", "max_amount",
        "interval_seconds", "monitor_until", "monitoring",
    }
    if field not in allowed:
        raise ValueError("Unsupported setting")
    ensure_user(user_id)
    with db() as con:
        con.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))


def log_action(user_id: int, action: str) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO history(user_id,action,created_at) VALUES(?,?,?)",
            (user_id, action, now_iso()),
        )


def is_owner(user_id: int) -> bool:
    return OWNER_ID is not None and user_id == OWNER_ID


def has_access(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    with db() as con:
        rows = con.execute(
            "SELECT expires_at FROM access_keys WHERE used_by=?", (user_id,)
        ).fetchall()
    current = datetime.now(timezone.utc)
    return any(datetime.fromisoformat(r["expires_at"]) > current for r in rows)


def mark_seen(user_id: int, offer_id: str) -> bool:
    """Returns True only when this user has not seen the offer before."""
    with db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO seen_offers(user_id,offer_id,seen_at) VALUES(?,?,?)",
            (user_id, offer_id, now_iso()),
        )
        return cur.rowcount == 1


# ----------------------------- PARSING -------------------------------------

def norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_number(value: str) -> Optional[float]:
    """Parse numbers such as '1 234,56', '1.234,56', '61.1 ₽'."""
    if not value:
        return None
    s = value.replace("\xa0", " ").replace("\u202f", " ")
    s = re.sub(r"[^\d,.\- ]", "", s)
    s = s.replace(" ", "")
    if not s or not re.search(r"\d", s):
        return None
    # Russian decimal comma is common. If both separators exist, the last one
    # is treated as the decimal separator.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        n = float(s)
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def parse_amount_from_text(text: str) -> Optional[int]:
    patterns = [
        r"(\d[\d\s\u00a0\u202f]*)\s*(?:gold|голд|голды|золота)",
        r"(?:gold|голд|голды|золота)\s*(\d[\d\s\u00a0\u202f]*)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            n = parse_number(m.group(1))
            if n and n > 0:
                return int(n)
    return None


def first_text(node, selectors: List[str]) -> str:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            value = norm_space(found.get_text(" ", strip=True))
            if value:
                return value
    return ""


def extract_offer_id(node, link: str, title: str, amount: int, total: float) -> str:
    for attr in ("data-id", "data-offer-id", "id"):
        value = node.get(attr)
        if value:
            return str(value)
    match = re.search(r"/(?:lots|offer)/(\d+)", link or "")
    if match:
        return match.group(1)
    raw = f"{link}|{title}|{amount}|{total:.8f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_funpay_html(html: str) -> List[dict]:
    """
    Parse listing cards. On FunPay the amount and total price are separate
    fields; unit price is always calculated as total_price / amount.
    The full lot price is never used as the per-Gold price.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".tc-item")
    if not cards:
        # Fallback for markup variants that retain offer links.
        candidates = soup.select("a[href*='/lots/'], a[href*='/offer/']")
        cards = []
        for a in candidates:
            parent = a.find_parent(class_=re.compile(r"tc-item|offer|lot"))
            if parent and parent not in cards:
                cards.append(parent)

    offers = []
    for card in cards:
        title = first_text(card, [
            ".tc-desc-text", ".tc-desc", ".tc-title", ".offer-title",
            "[class*='desc']", "[class*='title']",
        ])
        amount_text = first_text(card, [
            ".tc-amount", ".tc-count", ".offer-amount", "[class*='amount']",
            "[class*='count']",
        ])
        price_text = first_text(card, [
            ".tc-price", ".tc-price-value", ".offer-price", "[class*='price']",
        ])
        seller = first_text(card, [
            ".media-user-name", ".tc-user .media-user-name", ".offer-user",
            "[class*='seller']", "[class*='user-name']",
        ])
        platform = ""

        # Platform may be present in title, description, or attributes.
        full_text = norm_space(card.get_text(" ", strip=True))
        platform_match = re.search(r"\b(iOS|Android|PC)\b", full_text, re.IGNORECASE)
        if platform_match:
            platform = platform_match.group(1)

        amount = parse_amount_from_text(amount_text)
        if amount is None:
            amount = parse_number(amount_text)
            amount = int(amount) if amount and amount > 0 else None
        if amount is None:
            amount = parse_amount_from_text(title + " " + full_text)

        total_price = parse_number(price_text)
        if total_price is None:
            # Fallback only to an explicitly marked price node; do not parse
            # arbitrary numbers from the whole card as price.
            for node in card.select("[data-price], [data-value]"):
                total_price = parse_number(
                    str(node.get("data-price") or node.get("data-value") or "")
                )
                if total_price is not None:
                    break

        if not amount or amount <= 0 or total_price is None or total_price <= 0:
            continue

        unit_price = total_price / amount
        if not math.isfinite(unit_price) or unit_price <= 0:
            continue

        anchor = card.select_one("a[href]")
        link = anchor.get("href", "") if anchor else ""
        if link.startswith("/"):
            link = "https://funpay.com" + link
        offer_id = extract_offer_id(card, link, title, amount, total_price)

        offers.append({
            "id": offer_id,
            "title": title or "Standoff 2 Gold",
            "amount": int(amount),
            "unit_price": float(unit_price),
            "seller": seller or "Не указан",
            "platform": platform or "Не указана",
            "url": link,
        })

    # Remove duplicates while preserving order.
    unique = {}
    for offer in offers:
        unique[offer["id"]] = offer
    return list(unique.values())


def fetch_offers() -> List[dict]:
    response = HTTP.get(FUNPAY_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return parse_funpay_html(response.text)


def matches(offer: dict, settings: sqlite3.Row) -> bool:
    # IMPORTANT: filter only by calculated price per ONE Gold.
    p = offer["unit_price"]
    amount = offer["amount"]
    return (
        float(settings["min_price"]) <= p <= float(settings["max_price"])
        and int(settings["min_amount"]) <= amount <= int(settings["max_amount"])
    )


# ----------------------------- TELEGRAM UI ---------------------------------

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Проверить объявления", callback_data="scan")],
        [InlineKeyboardButton("⚙️ Фильтры", callback_data="filters"),
         InlineKeyboardButton("📊 Статус", callback_data="status")],
        [InlineKeyboardButton("🚀 Запустить мониторинг", callback_data="monitor"),
         InlineKeyboardButton("⏹ Остановить", callback_data="stop")],
        [InlineKeyboardButton("🧪 Тест сообщения", callback_data="test")],
    ])


def filters_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Цена за Gold", callback_data="setprice")],
        [InlineKeyboardButton("Количество Gold", callback_data="setamount")],
        [InlineKeyboardButton("Интервал", callback_data="setinterval")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="home")],
    ])


def fmt_price(value: float) -> str:
    return f"{value:.3f}".replace(".", ",")


def offer_message(offer: dict, number: int) -> str:
    # Deliberately do not display the total price of the lot.
    msg = (
        f"🆕 ОБЪЯВЛЕНИЕ №{number}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена за 1 Gold: {fmt_price(offer['unit_price'])} ₽\n"
        f"📦 Количество: {offer['amount']} Gold\n"
        f"📱 Платформа: {offer['platform']}\n"
        f"👤 Продавец: {offer['seller']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Покупку и оплату ты подтверждаешь самостоятельно."
    )
    if offer.get("url"):
        msg += f"\n🔗 Открыть объявление: {offer['url']}"
    return msg


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    except Exception:
        log.exception("Could not send Telegram message to chat_id=%s", chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    if not has_access(uid):
        await update.message.reply_text(
            "Привет! Для доступа нужен ключ активации.\n"
            "Отправь команду /activate ТВОЙ_КЛЮЧ"
        )
        return
    await update.message.reply_text(
        "🟡 Standoff 2 Gold Finder\n\n"
        "Ищу публичные объявления FunPay и считаю цену именно за 1 Gold.\n"
        "Полная стоимость лота в уведомлениях не показывается.",
        reply_markup=main_keyboard(),
    )


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /activate КЛЮЧ")
        return
    uid = update.effective_user.id
    key_hash = hashlib.sha256(context.args[0].encode()).hexdigest()
    with db() as con:
        row = con.execute(
            "SELECT * FROM access_keys WHERE key_hash=? AND used_by IS NULL",
            (key_hash,),
        ).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            await update.message.reply_text("Ключ недействителен, уже использован или истёк.")
            return
        con.execute("UPDATE access_keys SET used_by=? WHERE key_hash=?", (uid, key_hash))
    ensure_user(uid)
    log_action(uid, "activated")
    await update.message.reply_text("✅ Доступ активирован. Нажми /start.")


async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    key = secrets.token_urlsafe(8)
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    with db() as con:
        con.execute(
            "INSERT INTO access_keys(key_hash,expires_at,created_at) VALUES(?,?,?)",
            (key_hash, expires, now_iso()),
        )
    await update.message.reply_text(f"🔑 Ключ на 24 часа:\n{key}")


async def status_text(user_id: int) -> str:
    s = get_user(user_id)
    until = s["monitor_until"] or "не запущен"
    return (
        "📊 ТЕКУЩИЕ НАСТРОЙКИ\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Цена за 1 Gold: {fmt_price(s['min_price'])}–{fmt_price(s['max_price'])} ₽\n"
        f"📦 Количество: {s['min_amount']}–{s['max_amount']} Gold\n"
        f"🔄 Интервал: {s['interval_seconds']} сек.\n"
        f"🚀 Мониторинг: {'включён' if s['monitoring'] else 'выключен'}\n"
        f"⏳ До: {until}"
    )


async def scan_now(user_id: int, context: ContextTypes.DEFAULT_TYPE, send_results=True):
    settings = get_user(user_id)
    try:
        offers = await asyncio.to_thread(fetch_offers)
    except Exception as exc:
        log.exception("FunPay scan failed")
        await safe_send(context, user_id, f"❌ Не удалось загрузить объявления FunPay: {exc}")
        return
    suitable = [o for o in offers if matches(o, settings)]
    if send_results:
        if not suitable:
            await safe_send(
                context, user_id,
                f"Обработано объявлений: {len(offers)}\n"
                "Подходящих объявлений по фильтрам не найдено."
            )
        else:
            await safe_send(
                context, user_id,
                f"Обработано объявлений: {len(offers)}\n"
                f"Подходит под фильтры: {len(suitable)}"
            )
            for i, offer in enumerate(suitable[:20], 1):
                await safe_send(context, user_id, offer_message(offer, i))
    return offers, suitable


async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    settings = get_user(user_id)
    if not settings["monitoring"]:
        return
    until_raw = settings["monitor_until"]
    if until_raw:
        try:
            if datetime.fromisoformat(until_raw) <= datetime.now(timezone.utc):
                set_user_field(user_id, "monitoring", 0)
                await safe_send(context, user_id, "⏹ Время мониторинга закончилось.")
                return
        except ValueError:
            pass
    try:
        offers = await asyncio.to_thread(fetch_offers)
    except Exception:
        log.exception("Monitor fetch failed")
        return
    for offer in offers:
        if matches(offer, settings) and mark_seen(user_id, offer["id"]):
            await safe_send(context, user_id, offer_message(offer, int(time.time()) % 100000))


async def start_monitoring(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    settings = get_user(user_id)
    try:
        offers = await asyncio.to_thread(fetch_offers)
    except Exception as exc:
        await safe_send(context, user_id, f"❌ Не удалось проверить FunPay: {exc}")
        return
    # Baseline: current listings are marked seen so only newly appearing offers
    # trigger notifications after monitoring starts.
    for offer in offers:
        if matches(offer, settings):
            mark_seen(user_id, offer["id"])
    set_user_field(user_id, "monitoring", 1)
    set_user_field(
        user_id, "monitor_until",
        (datetime.now(timezone.utc) + timedelta(hours=DEFAULT_DURATION_HOURS)).isoformat(),
    )
    for job in context.job_queue.get_jobs_by_name(f"monitor_{user_id}"):
        job.schedule_removal()
    context.job_queue.run_repeating(
        monitor_job,
        interval=int(settings["interval_seconds"]),
        first=int(settings["interval_seconds"]),
        chat_id=user_id,
        name=f"monitor_{user_id}",
    )
    log_action(user_id, "monitor_started")
    await safe_send(
        context, user_id,
        f"🚀 Мониторинг запущен на 24 часа.\n"
        f"Фильтр цены за 1 Gold: {fmt_price(settings['min_price'])}–{fmt_price(settings['max_price'])} ₽.\n"
        "Уже существующие объявления повторно отправляться не будут."
    )


async def stop_monitoring(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    set_user_field(user_id, "monitoring", 0)
    for job in context.job_queue.get_jobs_by_name(f"monitor_{user_id}"):
        job.schedule_removal()
    log_action(user_id, "monitor_stopped")
    await safe_send(context, user_id, "⏹ Мониторинг остановлен.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not has_access(uid):
        await query.edit_message_text("Сначала активируй доступ: /activate КЛЮЧ")
        return
    data = query.data

    if data == "home":
        await query.edit_message_text("Главное меню:", reply_markup=main_keyboard())
    elif data == "filters":
        await query.edit_message_text("⚙️ Настройки фильтров:", reply_markup=filters_keyboard())
    elif data == "status":
        await query.edit_message_text(await status_text(uid), reply_markup=main_keyboard())
    elif data == "scan":
        await query.edit_message_text("🔎 Проверяю публичные объявления FunPay…")
        await scan_now(uid, context)
    elif data == "monitor":
        await query.edit_message_text("Запускаю мониторинг…")
        await start_monitoring(uid, context)
    elif data == "stop":
        await stop_monitoring(uid, context)
    elif data == "test":
        await safe_send(
            context, uid,
            "🧪 Тестовое сообщение: отправка в Telegram работает.\n"
            "Полная стоимость лота в уведомлениях не отображается."
        )
    elif data == "setprice":
        context.user_data["awaiting"] = "price"
        await query.edit_message_text(
            "Отправь две границы цены за 1 Gold через пробел.\n"
            "Например: 0.10 0.50"
        )
    elif data == "setamount":
        context.user_data["awaiting"] = "amount"
        await query.edit_message_text(
            "Отправь минимальное и максимальное количество Gold.\n"
            "Например: 10 10000"
        )
    elif data == "setinterval":
        context.user_data["awaiting"] = "interval"
        await query.edit_message_text("Отправь интервал в секундах (от 15 до 300).")


async def text_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("awaiting")
    if not mode:
        return
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text("Сначала активируй доступ: /activate КЛЮЧ")
        return
    parts = update.message.text.strip().replace(",", ".").split()
    try:
        if mode == "price" and len(parts) == 2:
            low, high = map(float, parts)
            if low < 0 or high <= low:
                raise ValueError
            set_user_field(uid, "min_price", low)
            set_user_field(uid, "max_price", high)
            reply = f"✅ Фильтр цены за 1 Gold: {fmt_price(low)}–{fmt_price(high)} ₽"
        elif mode == "amount" and len(parts) == 2:
            low, high = map(int, parts)
            if low < 1 or high < low:
                raise ValueError
            set_user_field(uid, "min_amount", low)
            set_user_field(uid, "max_amount", high)
            reply = f"✅ Фильтр количества: {low}–{high} Gold"
        elif mode == "interval" and len(parts) == 1:
            interval = int(parts[0])
            if not 15 <= interval <= 300:
                raise ValueError
            set_user_field(uid, "interval_seconds", interval)
            reply = f"✅ Интервал установлен: {interval} сек. Перезапусти мониторинг, чтобы применить."
        else:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Неверный формат или значения. Попробуй ещё раз.")
        return
    context.user_data.pop("awaiting", None)
    await update.message.reply_text(reply, reply_markup=main_keyboard())


async def diagnostic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text("Сначала активируй доступ: /activate КЛЮЧ")
        return
    await update.message.reply_text("🧪 Проверяю загрузку и разбор объявлений…")
    try:
        offers = await asyncio.to_thread(fetch_offers)
        settings = get_user(uid)
        suitable = [o for o in offers if matches(o, settings)]
        await update.message.reply_text(
            "🧪 РЕЗУЛЬТАТ ТЕСТА\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📋 Объявлений обработано: {len(offers)}\n"
            f"🎯 Подходит под фильтры: {len(suitable)}\n"
            f"💰 Цена за Gold: {fmt_price(settings['min_price'])}–{fmt_price(settings['max_price'])} ₽\n"
            f"📦 Количество: {settings['min_amount']}–{settings['max_amount']} Gold\n"
            f"🔄 Интервал: {settings['interval_seconds']} секунд\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Полная стоимость лота не используется как цена за 1 Gold."
        )
    except Exception as exc:
        log.exception("Diagnostic failed")
        await update.message.reply_text(f"❌ Ошибка проверки: {exc}")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT action,created_at FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("История пока пуста.")
        return
    lines = [f"• {r['action']} — {r['created_at']}" for r in rows]
    await update.message.reply_text("📜 Последние действия:\n" + "\n".join(lines))


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    with db() as con:
        rows = con.execute("SELECT user_id,monitoring FROM users ORDER BY user_id").fetchall()
    if not rows:
        await update.message.reply_text("Пользователей пока нет.")
        return
    await update.message.reply_text(
        "👥 Пользователи:\n" +
        "\n".join(f"{r['user_id']} — мониторинг: {'да' if r['monitoring'] else 'нет'}" for r in rows)
    )


async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /revoke USER_ID")
        return
    target = int(context.args[0])
    with db() as con:
        con.execute("UPDATE access_keys SET expires_at=? WHERE used_by=?", (now_iso(), target))
    await update.message.reply_text(f"Доступ пользователя {target} отключён.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Telegram handler error", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("diagnostic", diagnostic))
    app.add_handler(CommandHandler("status", lambda u, c: status_command(u, c)))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(__import__("telegram.ext", fromlist=["MessageHandler"]).MessageHandler(
        __import__("telegram.ext", fromlist=["filters"]).filters.TEXT
        & ~__import__("telegram.ext", fromlist=["filters"]).filters.COMMAND,
        text_settings,
    ))
    app.add_error_handler(error_handler)
    log.info("Standoff 2 Gold Finder started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text("Сначала активируй доступ: /activate КЛЮЧ")
        return
    await update.message.reply_text(await status_text(uid), reply_markup=main_keyboard())


if __name__ == "__main__":
    main()
