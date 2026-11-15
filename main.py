import os
import re
import html
import time
import sqlite3
import asyncio
import logging
import statistics
import unicodedata
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ============================================================
# FUNPAY STEAL A BRAINROT — PUBLIC MARKET SCANNER
# Покупка и оплата выполняются пользователем вручную на FunPay.
# Не запрашивает логин, пароль, 2FA или cookies аккаунта.
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("brainrot_scanner")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_NAME = os.getenv("DB_NAME", "brainrot_settings.db")

CATEGORY_URL = "https://funpay.com/lots/3503/"
CATEGORY_NAME = "Steal a Brainrot"

# Настройки сканирования. Сканируются только публичные страницы FunPay.
MIN_COMPARABLES = 3
MAX_PAGES = 25
MAX_DETAILS_PER_SCAN = 250
REQUEST_DELAY = 0.25
DEFAULT_DISCOUNT = 40.0
DEFAULT_MIN_PRICE = 0.01
DEFAULT_MAX_PRICE = 100000.0
DEFAULT_INTERVAL = 180
DETAIL_CACHE_TTL = 600

DETAIL_CACHE = {}
MONITORS = {}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🔎 Найти выгодные", "🚀 Запустить мониторинг"],
        ["⛔ Остановить", "⚙️ Настройки"],
        ["📊 Мои параметры", "🧪 Проверить FunPay"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    [
        ["🎯 Порог скидки", "💰 Мин. цена"],
        ["💵 Макс. цена", "🔄 Интервал"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
)

INTERVALS = {
    "1 минута": 60,
    "3 минуты": 180,
    "5 минут": 300,
    "10 минут": 600,
}


# ---------------- DATABASE ----------------

def db():
    return sqlite3.connect(DB_NAME, timeout=30)


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                min_price REAL NOT NULL DEFAULT 0.01,
                max_price REAL NOT NULL DEFAULT 100000,
                discount REAL NOT NULL DEFAULT 40,
                interval INTEGER NOT NULL DEFAULT 180
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                user_id INTEGER NOT NULL,
                offer_id TEXT NOT NULL,
                PRIMARY KEY(user_id, offer_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS access (
                user_id INTEGER PRIMARY KEY
            )
        """)


def ensure_user(user_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))


def get_settings(user_id):
    ensure_user(user_id)
    with db() as conn:
        row = conn.execute(
            "SELECT min_price,max_price,discount,interval FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row


def save_settings(user_id, **values):
    allowed = {"min_price", "max_price", "discount", "interval"}
    if not values or not set(values).issubset(allowed):
        raise ValueError("Недопустимые параметры")
    ensure_user(user_id)
    fields = ", ".join(f"{key}=?" for key in values)
    with db() as conn:
        conn.execute(
            f"UPDATE users SET {fields} WHERE user_id=?",
            (*values.values(), user_id),
        )


def is_authorized(user_id):
    if user_id == OWNER_ID:
        return True
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM access WHERE user_id=?", (user_id,)
        ).fetchone() is not None


def set_authorized(user_id, allowed):
    with db() as conn:
        if allowed:
            conn.execute("INSERT OR IGNORE INTO access(user_id) VALUES(?)", (user_id,))
        else:
            conn.execute("DELETE FROM access WHERE user_id=?", (user_id,))


def seen(user_id, offer_id):
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM seen WHERE user_id=? AND offer_id=?",
            (user_id, str(offer_id)),
        ).fetchone() is not None


def mark_seen(user_id, offer_id):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen(user_id,offer_id) VALUES(?,?)",
            (user_id, str(offer_id)),
        )


# ---------------- FUNPAY PARSER ----------------

SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def number(value):
    if not value:
        return None
    raw = str(value).replace("\xa0", " ").replace(",", ".")
    raw = raw.replace(" ", "")
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def get_offer_id(link):
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    return str(query.get("id", [parsed.path])[0])


def pagination_pages(soup):
    found = set()
    for a in soup.select('a[href*="page="]'):
        value = parse_qs(urlparse(a.get("href", "")).query).get("page", [None])[0]
        try:
            page = int(value)
            if 0 <= page < MAX_PAGES:
                found.add(page)
        except (ValueError, TypeError):
            pass
    return sorted(found)


def page_url(page):
    parsed = urlparse(CATEGORY_URL)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def get_description(soup):
    chunks = []
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        label = clean(heading.get_text(" ", strip=True)).casefold()
        if any(x in label for x in (
            "short description", "detailed description",
            "краткое описание", "подробное описание",
        )):
            parts = []
            for node in heading.find_all_next():
                if node.name and re.fullmatch(r"h[1-6]", node.name):
                    break
                if node.name in ("script", "style", "nav", "footer"):
                    continue
                value = clean(node.get_text(" ", strip=True))
                if value and value not in parts:
                    parts.append(value)
            if parts:
                chunks.append(" ".join(parts[:25]))

    if chunks:
        return " ".join(chunks)

    for selector in (
        ".offer-desc", ".offer-description", ".offer-item-description",
        ".description",
    ):
        node = soup.select_one(selector)
        if node:
            value = clean(node.get_text(" ", strip=True))
            if len(value) >= 8:
                return value
    return ""


def normalize_signature(title, description):
    # Убираем типовые фразы доставки, чтобы они не дробили одинаковые лоты.
    text = unicodedata.normalize("NFKC", f"{title} {description}").casefold()
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(
        r"\b(fast delivery|instant delivery|quick delivery|safe trade|"
        r"быстрая доставка|мгновенная выдача|пишите в чат|пишите продавцу)\b",
        " ", text,
    )
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def fetch_detail(session, link):
    now = time.time()
    cached = DETAIL_CACHE.get(link)
    if cached and now - cached[0] < DETAIL_CACHE_TTL:
        return cached[1]

    response = session.get(link, timeout=20)
    response.raise_for_status()
    description = get_description(BeautifulSoup(response.text, "html.parser"))
    if description:
        DETAIL_CACHE[link] = (now, description)
    return description


def parse_card(row):
    price_node = row.select_one(".tc-price")
    if not price_node:
        return None
    price_text = clean(price_node.get_text(" ", strip=True))
    if not any(mark in price_text.casefold() for mark in ("₽", "руб", "rub")):
        return None
    price = number(price_text)
    if price is None or price <= 0:
        return None

    href = row.get("href")
    if not href:
        return None
    link = urljoin(CATEGORY_URL, href)
    parsed = urlparse(link)
    if parsed.scheme not in ("http", "https") or parsed.netloc not in (
        "funpay.com", "www.funpay.com"
    ):
        return None

    title_node = row.select_one(".tc-desc-text") or row
    seller_node = row.select_one(".media-user-name")
    return {
        "id": get_offer_id(link),
        "title": clean(title_node.get_text(" ", strip=True))[:240],
        "price": price,
        "seller": clean(seller_node.get_text(" ", strip=True)) if seller_node else "Не указан",
        "link": link,
    }


def scan_market():
    """Собирает публичные лоты, читает описания и сравнивает похожие группы."""
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    session.cookies.update({"cy": "rub", "locale": "ru"})

    response = session.get(CATEGORY_URL, timeout=25)
    response.raise_for_status()
    first_soup = BeautifulSoup(response.text, "html.parser")
    pages = [None] + [p for p in pagination_pages(first_soup) if p != 0]

    cards = {}
    for page in pages[:MAX_PAGES]:
        if page is None:
            soup = first_soup
        else:
            page_response = session.get(page_url(page), timeout=25)
            page_response.raise_for_status()
            soup = BeautifulSoup(page_response.text, "html.parser")

        for row in soup.select("a.tc-item"):
            card = parse_card(row)
            if card:
                cards[card["id"]] = card
        time.sleep(REQUEST_DELAY)

    all_cards = list(cards.values())
    detailed = []
    inspected = 0

    for card in all_cards:
        if inspected >= MAX_DETAILS_PER_SCAN:
            break
        try:
            description = fetch_detail(session, card["link"])
            inspected += 1
            if not description:
                continue
            signature = normalize_signature(card["title"], description)
            if len(signature.split()) < 2:
                continue
            card["description"] = description[:1800]
            card["signature"] = signature
            detailed.append(card)
        except requests.RequestException as exc:
            log.warning("Не удалось прочитать лот %s: %s", card["id"], exc)
        time.sleep(REQUEST_DELAY)

    groups = {}
    for item in detailed:
        groups.setdefault(item["signature"], []).append(item)

    results = []
    for group in groups.values():
        if len(group) < MIN_COMPARABLES:
            continue
        prices = [item["price"] for item in group]
        # Медиана устойчивее к единичным аномальным ценам.
        market_price = statistics.median(prices)
        if market_price <= 0:
            continue
        for item in group:
            discount = (market_price - item["price"]) / market_price * 100
            results.append({
                **item,
                "market_price": market_price,
                "discount": discount,
                "sample_size": len(group),
            })

    log.info(
        "Сканирование: карточек=%s, описаний=%s, групп=%s, сравнений=%s",
        len(all_cards), inspected, len(groups), len(results),
    )
    return {
        "offers": results,
        "cards_found": len(all_cards),
        "details_read": inspected,
        "groups_found": len(groups),
    }


# ---------------- TELEGRAM UI ----------------

def money(value):
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def settings_message(user_id):
    min_price, max_price, discount, interval = get_settings(user_id)
    interval_label = next(
        (label for label, seconds in INTERVALS.items() if seconds == interval),
        f"{interval} сек.",
    )
    return (
        "📊 <b>ТВОИ ПАРАМЕТРЫ</b>\n"
        f"🎮 Категория: <b>{CATEGORY_NAME}</b>\n"
        f"💰 Минимальная цена: <b>{money(min_price)} ₽</b>\n"
        f"💵 Максимальная цена: <b>{money(max_price)} ₽</b>\n"
        f"🎯 Минимальная скидка: <b>{discount:.0f}%</b>\n"
        f"🔄 Интервал: <b>{html.escape(interval_label)}</b>\n"
        f"📚 Для оценки нужно минимум: <b>{MIN_COMPARABLES} похожих лота</b>"
    )


def filter_results(user_id, scan):
    min_price, max_price, discount, _ = get_settings(user_id)
    found = [
        item for item in scan["offers"]
        if min_price <= item["price"] <= max_price
        and item["discount"] >= discount
    ]
    found.sort(key=lambda x: (-x["discount"], x["price"]))
    return found


def offer_text(item):
    return (
        "💎 <b>ВОЗМОЖНО ВЫГОДНОЕ ПРЕДЛОЖЕНИЕ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧩 <b>{html.escape(item['title'][:180])}</b>\n"
        f"💰 Цена: <b>{money(item['price'])} ₽</b>\n"
        f"📊 Оценка рынка: <b>{money(item['market_price'])} ₽</b>\n"
        f"📉 Ниже оценки: <b>{item['discount']:.1f}%</b>\n"
        f"🔎 Похожих объявлений: <b>{item['sample_size']}</b>\n"
        f"👤 Продавец: <b>{html.escape(item['seller'])}</b>\n"
        f"📝 Описание: {html.escape(item['description'][:700])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Оценка рассчитана по похожим публичным объявлениям. "
        "Перед покупкой проверь характеристики вручную."
    )


def offer_keyboard(item):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть объявление на FunPay", url=item["link"])
    ]])


async def send_scan(message, user_id):
    await message.reply_text("🔎 Сканирую публичные объявления FunPay…")
    try:
        scan = await asyncio.to_thread(scan_market)
        matches = filter_results(user_id, scan)
    except Exception as exc:
        log.exception("Ошибка сканирования")
        await message.reply_text(
            f"Не удалось завершить сканирование: {type(exc).__name__}: {exc}",
            reply_markup=MAIN_MENU,
        )
        return

    await message.reply_text(
        "📊 <b>ОТЧЁТ СКАНИРОВАНИЯ</b>\n"
        f"Карточек найдено: <b>{scan['cards_found']}</b>\n"
        f"Описаний прочитано: <b>{scan['details_read']}</b>\n"
        f"Групп характеристик: <b>{scan['groups_found']}</b>\n"
        f"Под твои фильтры: <b>{len(matches)}</b>\n\n"
        "Если карточек больше, чем удалось проверить, сканер ограничивает "
        "объём запросов за один проход.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )

    for item in matches[:15]:
        await message.reply_text(
            offer_text(item),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=offer_keyboard(item),
        )


async def notify_loop(app, user_id, stop_event):
    _, _, _, interval = get_settings(user_id)
    try:
        await app.bot.send_message(
            user_id,
            "🟢 Мониторинг запущен. Проверяю публичные объявления.",
            reply_markup=MAIN_MENU,
        )
        while not stop_event.is_set():
            try:
                scan = await asyncio.to_thread(scan_market)
                matches = filter_results(user_id, scan)
                for item in matches:
                    if stop_event.is_set():
                        break
                    if not seen(user_id, item["id"]):
                        await app.bot.send_message(
                            user_id,
                            offer_text(item),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=offer_keyboard(item),
                        )
                        mark_seen(user_id, item["id"])
            except Exception:
                log.exception("Ошибка фонового сканирования")
                await app.bot.send_message(
                    user_id,
                    "⚠️ Ошибка проверки FunPay. Попробую ещё раз позже.",
                )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        MONITORS.pop(user_id, None)
        await app.bot.send_message(user_id, "⛔ Мониторинг остановлен.")


async def start_monitor(update, context):
    user_id = update.effective_user.id
    if user_id in MONITORS and not MONITORS[user_id]["task"].done():
        await update.message.reply_text("Мониторинг уже запущен.")
        return
    stop_event = asyncio.Event()
    task = context.application.create_task(
        notify_loop(context.application, user_id, stop_event)
    )
    MONITORS[user_id] = {"task": task, "stop": stop_event}
    await update.message.reply_text("Запускаю мониторинг…", reply_markup=MAIN_MENU)


async def stop_monitor(update, context):
    entry = MONITORS.get(update.effective_user.id)
    if entry:
        entry["stop"].set()
        await update.message.reply_text("Останавливаю мониторинг…", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Мониторинг сейчас не запущен.", reply_markup=MAIN_MENU)


# ---------------- SETTINGS INPUT ----------------

async def start_setting(update, context, field):
    context.user_data["setting"] = field
    prompts = {
        "discount": "Введи минимальную скидку в процентах (например, 40).",
        "min_price": "Введи минимальную цену в рублях.",
        "max_price": "Введи максимальную цену в рублях.",
        "interval": "Выбери интервал: 1 минута, 3 минуты, 5 минут или 10 минут.",
    }
    await update.message.reply_text(prompts[field], reply_markup=SETTINGS_MENU)


async def handle_setting_value(update, context):
    field = context.user_data.get("setting")
    if not field:
        return False

    raw = update.message.text.strip().replace(",", ".")
    user_id = update.effective_user.id

    if field == "interval":
        if raw not in INTERVALS:
            await update.message.reply_text("Выбери один из предложенных интервалов.")
            return True
        save_settings(user_id, interval=INTERVALS[raw])
    else:
        try:
            value = float(raw)
        except ValueError:
            await update.message.reply_text("Введи число.")
            return True

        if value < 0:
            await update.message.reply_text("Значение не может быть отрицательным.")
            return True

        min_price, max_price, discount, interval = get_settings(user_id)
        if field == "discount":
            if not 1 <= value <= 95:
                await update.message.reply_text("Скидка должна быть от 1 до 95%.")
                return True
            save_settings(user_id, discount=value)
        elif field == "min_price":
            if value <= 0 or value > max_price:
                await update.message.reply_text("Минимальная цена должна быть больше 0 и не выше максимальной.")
                return True
            save_settings(user_id, min_price=value)
        elif field == "max_price":
            if value < min_price:
                await update.message.reply_text("Максимальная цена не может быть меньше минимальной.")
                return True
            save_settings(user_id, max_price=value)

    context.user_data.pop("setting", None)
    await update.message.reply_text(
        "✅ Настройка сохранена.\n\n" + settings_message(user_id),
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )
    return True


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text(
            "Доступ закрыт. Попроси владельца бота выдать доступ."
        )
        return
    ensure_user(user_id)
    await update.message.reply_text(
        "💎 <b>FUNPAY BRAINROT SCANNER</b>\n"
        "Сканирую публичные объявления категории Steal a Brainrot.\n"
        "Сравниваю описание и характеристики; не считаю цену рынка, "
        "если похожих объявлений меньше трёх.\n\n"
        "Покупку и оплату ты выполняешь самостоятельно.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_MENU,
    )


async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /grant TELEGRAM_ID")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    set_authorized(target, True)
    ensure_user(target)
    await update.message.reply_text(f"Доступ выдан: {target}")


async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID or not context.args:
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    set_authorized(target, False)
    if target in MONITORS:
        MONITORS[target]["stop"].set()
    await update.message.reply_text(f"Доступ отозван: {target}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    await update.message.reply_text(
        settings_message(user_id), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    text = update.message.text.strip()
    if await handle_setting_value(update, context):
        return

    if text == "🔎 Найти выгодные":
        await send_scan(update.message, user_id)
    elif text == "🚀 Запустить мониторинг":
        await start_monitor(update, context)
    elif text == "⛔ Остановить":
        await stop_monitor(update, context)
    elif text == "⚙️ Настройки":
        await update.message.reply_text("Выбери параметр:", reply_markup=SETTINGS_MENU)
    elif text == "🎯 Порог скидки":
        await start_setting(update, context, "discount")
    elif text == "💰 Мин. цена":
        await start_setting(update, context, "min_price")
    elif text == "💵 Макс. цена":
        await start_setting(update, context, "max_price")
    elif text == "🔄 Интервал":
        await start_setting(update, context, "interval")
    elif text == "📊 Мои параметры":
        await status(update, context)
    elif text == "🧪 Проверить FunPay":
        await update.message.reply_text("Проверяю доступность категории…")
        try:
            result = await asyncio.to_thread(
                lambda: requests.get(CATEGORY_URL, headers=SESSION_HEADERS, timeout=20)
            )
            await update.message.reply_text(
                f"HTTP: {result.status_code}\nURL: {result.url}",
                reply_markup=MAIN_MENU,
            )
        except Exception as exc:
            await update.message.reply_text(
                f"Проверка не прошла: {type(exc).__name__}: {exc}",
                reply_markup=MAIN_MENU,
            )
    elif text == "⬅️ Главное меню":
        context.user_data.pop("setting", None)
        await update.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Используй кнопки меню.", reply_markup=MAIN_MENU)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Добавь BOT_TOKEN в переменные окружения.")
    if not OWNER_ID:
        raise RuntimeError("Добавь OWNER_ID в переменные окружения.")

    init_db()
    set_authorized(OWNER_ID, True)
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Brainrot scanner started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
