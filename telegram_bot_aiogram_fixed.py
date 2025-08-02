import os
import asyncio
import sqlite3
import json
from typing import Dict, List, Optional, Any, Set

from aiogram import Bot, Dispatcher, F, types
from aiogram import Router
from aiogram.filters import Command

# ---------------------------------------------------------------------------
# Aiogram version compatibility helpers
try:
    from aiogram.utils import executor  # type: ignore
    EXECUTOR_AVAILABLE: bool = True
except Exception:
    executor = None  # type: ignore
    EXECUTOR_AVAILABLE = False

try:
    from aiogram.enums import ParseMode as AiogramParseMode  # type: ignore
    PARSE_MODE_HTML = AiogramParseMode.HTML
    PARSE_MODE_MARKDOWN = AiogramParseMode.MARKDOWN
except Exception:
    PARSE_MODE_HTML = types.ParseMode.HTML  # type: ignore
    PARSE_MODE_MARKDOWN = types.ParseMode.MARKDOWN  # type: ignore

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from run_top_collections_once import (
    OpenSeaClient,
    fetch_page_collections,
    extract_pricing,
    calculate_difference,
)


# -----------------------------------------------------------------------------
# Configuration and Database Setup
#
MAX_PAGES_CODE: int = 2
BOT_TOKEN = "8285697328:AAE8iNKQYsZmbX0IQdybfhxHj4GsdNXKmVM"
ADMIN_IDS: List[int] = [414589178, 2086060667, 212031298]
DB_FILE = "bot_data.db"

# Initialize SQLite connection
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Create tables if they don't exist
c.execute("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY
)""")

c.execute("""
CREATE TABLE IF NOT EXISTS user_settings(
    user_id INTEGER PRIMARY KEY,
    pages INTEGER,
    price_min REAL,
    price_max REAL,
    diff_max REAL,
    excluded TEXT,
    monitoring INTEGER
)""")

c.execute("""
CREATE TABLE IF NOT EXISTS admin_settings(
    key TEXT PRIMARY KEY,
    value TEXT
)""")

# Ensure default admin max_pages exists
c.execute("INSERT OR IGNORE INTO admin_settings(key, value) VALUES (?,?)", 
          ("max_pages", str(MAX_PAGES_CODE)))
conn.commit()

# Load allowed users from DB
allowed_users: Set[int] = set(ADMIN_IDS)
c.execute("SELECT id FROM users")
for row in c.fetchall():
    uid = row["id"]
    if uid not in ADMIN_IDS:
        allowed_users.add(uid)

# Load user settings
user_settings: Dict[int, Dict[str, Any]] = {}
c.execute("SELECT * FROM user_settings")
for row in c.fetchall():
    user_settings[row["user_id"]] = {
        "pages": row["pages"],
        "price_min": row["price_min"],
        "price_max": row["price_max"],
        "diff_max": row["diff_max"],
        "excluded": set(json.loads(row["excluded"])),
        "monitoring": bool(row["monitoring"]),
    }

# Load admin settings
admin_settings: Dict[str, Any] = {}
c.execute("SELECT value FROM admin_settings WHERE key=?", ("max_pages",))
row = c.fetchone()
admin_settings["max_pages"] = int(row["value"]) if row else MAX_PAGES_CODE

# Persistence helpers
def persist_user_settings(user_id: int) -> None:
    cfg = user_settings[user_id]
    c.execute(
        "INSERT OR REPLACE INTO user_settings(user_id, pages, price_min, price_max, diff_max, excluded, monitoring)"
        " VALUES (?,?,?,?,?,?,?)",
        (user_id, cfg["pages"], cfg["price_min"], cfg["price_max"], cfg["diff_max"], 
         json.dumps(list(cfg["excluded"])), int(cfg["monitoring"]))
    )
    conn.commit()


def persist_admin_settings() -> None:
    c.execute(
        "INSERT OR REPLACE INTO admin_settings(key, value) VALUES (?,?)",
        ("max_pages", str(admin_settings["max_pages"]))
    )
    conn.commit()

# Router for aiogram
router: Router = Router()

# State tracking variables
user_deal_messages: Dict[int, Dict[str, int]] = {}
last_deal_data: Dict[int, Dict[str, Dict[str, Any]]] = {}
last_deal_text: Dict[int, Dict[str, str]] = {}
monitor_task: Optional[asyncio.Task] = None
menu_messages: Dict[int, int] = {}

# Глобальный экземпляр бота
bot_instance: Optional[Bot] = None


# -----------------------------------------------------------------------------
# Menu Builders and Helpers
#
def build_main_menu(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Create main menu text and keyboard for a user"""
    ensure_user_settings(uid)
    cfg = user_settings[uid]
    max_price = (
        "∞" if cfg["price_max"] == float("inf") else f"{cfg['price_max']:.0f}"
    )
    text = (
        "<b>Главное меню</b>\n"
        f"Мониторинг: {'✅' if cfg['monitoring'] else '❌'}\n"
        f"Страницы: {cfg['pages']} / {admin_settings['max_pages']}\n"
        f"Цена: {cfg['price_min']:.0f}-{max_price}$\n"
        f"Разрыв: {cfg['diff_max']:.2f}%\n"
        f"Исключений: {len(cfg['excluded'])}"
    )
    buttons = [
        [
            InlineKeyboardButton(
                text="▶️ Старт" if not cfg["monitoring"] else "⏹ Стоп",
                callback_data="toggle_monitor",
            )
        ],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu")],
    ]
    if uid in ADMIN_IDS:
        buttons.append(
            [InlineKeyboardButton(text="🛠 Админ", callback_data="admin_menu")]
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return text, keyboard


def build_settings_menu(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Create settings submenu"""
    cfg = user_settings[uid]
    max_price = (
        "∞" if cfg["price_max"] == float("inf") else f"{cfg['price_max']:.0f}"
    )
    text = (
        "<b>Настройки</b>\n"
        f"Страницы: {cfg['pages']} / {admin_settings['max_pages']}\n"
        f"Цена: {cfg['price_min']:.0f}-{max_price}$\n"
        f"Разрыв: {cfg['diff_max']:.2f}%\n"
        f"Исключены: {len(cfg['excluded'])}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton("📄 Страницы", callback_data="set_pages"),
                InlineKeyboardButton("💰 Цена", callback_data="set_price"),
            ],
            [InlineKeyboardButton("📉 Разрыв", callback_data="set_diff")],
            [InlineKeyboardButton("🚫 Исключения", callback_data="set_excluded")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


def build_admin_menu(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Create admin submenu"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton("➕ Добавить", callback_data="admin_adduser"),
                InlineKeyboardButton("➖ Удалить", callback_data="admin_removeuser"),
            ],
            [InlineKeyboardButton("📋 Список", callback_data="admin_listusers")],
            [InlineKeyboardButton("📄 Лимит страниц", callback_data="admin_setmaxpages")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
        ]
    )
    text = "<b>Админ меню</b>"
    return text, keyboard


async def refresh_menu_for_user(bot: Bot, uid: int) -> None:
    """Delete previous menu message and send a new one at the bottom"""
    text, keyboard = build_main_menu(uid)
    old_id = menu_messages.get(uid)
    if old_id is not None:
        try:
            await bot.delete_message(uid, old_id)
        except Exception:
            pass
    sent = await bot.send_message(
        uid, text, parse_mode=PARSE_MODE_HTML, reply_markup=keyboard
    )
    menu_messages[uid] = sent.message_id

# -----------------------------------------------------------------------------
# Helper Functions
#
def ensure_user_settings(user_id: int) -> None:
    """Initialize default settings for a user if not already present"""
    if user_id not in user_settings:
        user_settings[user_id] = {
            "pages": 1,
            "price_min": 0.0,
            "price_max": float("inf"),
            "diff_max": 2.0,
            "excluded": set(),
            "monitoring": False,
        }
        # Persist new settings to database
        persist_user_settings(user_id)


def load_cursors() -> List[Optional[str]]:
    """Read pagination cursors from cursor.txt or return [None]"""
    cursors: List[Optional[str]] = [None]
    try:
        with open("cursor.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    cursors.append(line)
    except FileNotFoundError:
        pass
    return cursors


def get_message_args(message: types.Message) -> str:
    """Extract command arguments from message"""
    if hasattr(message, "get_args"):
        try:
            return message.get_args()
        except Exception:
            pass
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            return parts[1].strip()
    return ""


async def fetch_deals(pages: int) -> List[Dict[str, Any]]:
    """Fetch and assemble deal information"""
    cursors = load_cursors()
    tasks: List[asyncio.Task] = []
    for i in range(pages):
        cursor = cursors[i] if i < len(cursors) else None

        async def process(cursor_val: Optional[str] = cursor) -> List[Dict[str, Any]]:
            page_client = OpenSeaClient()
            try:
                items = await fetch_page_collections(page_client, cursor_val, limit=100)
            except Exception:
                return []
            results: List[Dict[str, Any]] = []
            for item in items:
                name = item.get("name") or item.get("slug") or "Unknown Collection"
                slug = item.get("slug")
                link = f"https://opensea.io/collection/{slug}" if slug else None
                pricing = extract_pricing(item)
                diff = calculate_difference(pricing["eth_floor"], pricing["eth_offer"])
                results.append(
                    {
                        "collection": name,
                        "slug": slug,
                        "price": pricing["usd_floor"],
                        "list": pricing["eth_floor"],
                        "offer": pricing["eth_offer"],
                        "difference_percent": diff,
                        "link": link,
                    }
                )
            return results

        tasks.append(asyncio.create_task(process()))
    aggregated: List[Dict[str, Any]] = []
    for page_result in await asyncio.gather(*tasks):
        aggregated.extend(page_result)
    return aggregated


def filter_deals(
    deals: List[Dict[str, Any]],
    price_min: float,
    price_max: float,
    diff_max: float,
    excluded: Set[str],
) -> List[Dict[str, Any]]:
    """Filter deals based on user settings"""
    filtered: List[Dict[str, Any]] = []
    for d in deals:
        slug = d.get("slug") or ""
        if slug in excluded:
            continue
        price = d.get("price")
        diff = d.get("difference_percent")
        if price is None or diff is None:
            continue
        if price < price_min or price > price_max:
            continue
        if diff > diff_max:
            continue
        filtered.append(d)
    filtered.sort(key=lambda x: x.get("difference_percent", float("inf")))
    return filtered


def format_deals(deals: List[Dict[str, Any]]) -> str:
    """Format multiple deals into a message string"""
    if not deals:
        return "📭 На данный момент подходящих сделок нет."
    lines: List[str] = []
    for i, d in enumerate(deals[:50]):
        name = d.get("collection", "Unknown")
        price = d.get("price")
        floor = d.get("list")
        offer = d.get("offer")
        diff = d.get("difference_percent")
        link = d.get("link")
        price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "?"
        floor_str = f"{floor:.4f}" if isinstance(floor, (int, float)) else "?"
        offer_str = f"{offer:.4f}" if isinstance(offer, (int, float)) else "?"
        diff_str = f"{diff:.2f}" if isinstance(diff, (int, float)) else "?"
        title = f"<a href='{link}'>{name}</a>" if link else name
        lines.append(
            f"{i+1}. {title}\n"
            f"   💵 Цена: ${price_str}\n"
            f"   🧾 Floor: {floor_str} ETH\n"
            f"   🤝 Offer: {offer_str} ETH\n"
            f"   📉 Разрыв: {diff_str}%"
        )
    return "\n\n".join(lines)


def format_deal(deal: Dict[str, Any]) -> str:
    """Format a single deal for Telegram message"""
    name = deal.get("collection", "Unknown")
    price = deal.get("price")
    floor = deal.get("list")
    offer = deal.get("offer")
    diff = deal.get("difference_percent")
    link = deal.get("link")

    price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "?"
    floor_str = f"{floor:.4f}" if isinstance(floor, (int, float)) else "?"
    offer_str = f"{offer:.4f}" if isinstance(offer, (int, float)) else "?"
    diff_str = f"{diff:.2f}" if isinstance(diff, (int, float)) else "?"

    title = f"<a href='{link}'>{name}</a>" if link else name

    return (
        f"{title}\n"
        f"   💵 Цена: ${price_str}\n"
        f"   🧾 Floor: {floor_str} ETH\n"
        f"   🤝 Offer: {offer_str} ETH\n"
        f"   📉 Разрыв: {diff_str}%"
    )


# -----------------------------------------------------------------------------
# Monitoring Functions
#
async def global_monitor_loop(bot: Bot) -> None:
    """Background task for monitoring deals"""
    global monitor_task
    while True:
        active_users = [uid for uid, cfg in user_settings.items() if cfg.get("monitoring")]
        if not active_users:
            monitor_task = None
            break
        max_pages = max(user_settings[uid].get("pages", 1) for uid in active_users)
        try:
            raw_deals = await fetch_deals(max_pages)
            print(f"Парсинг {max_pages} страниц выполнен, получено {len(raw_deals)} коллекций.")
        except Exception as exc:
            print(f"Ошибка при парсинге {max_pages} страниц: {exc}")
            raw_deals = []
        
        for uid in active_users:
            cfg = user_settings[uid]
            deals = filter_deals(
                raw_deals,
                cfg.get("price_min", 0.0),
                cfg.get("price_max", float("inf")),
                cfg.get("diff_max", 2.0),
                cfg.get("excluded", set()),
            )
            print(f"Пользователь {uid}: обновлено {len(deals)} сделок.")

            # Build current deals mapping
            current_deals: Dict[str, Dict[str, Any]] = {}
            for d in deals:
                key = d.get("slug") or d.get("link") or d.get("collection")
                if key is None:
                    continue
                current_deals[str(key)] = d

            # Ensure per-user state dictionaries exist
            if uid not in user_deal_messages:
                user_deal_messages[uid] = {}
            if uid not in last_deal_data:
                last_deal_data[uid] = {}
            if uid not in last_deal_text:
                last_deal_text[uid] = {}
            updated = False

            # Remove outdated deals
            for key in list(user_deal_messages[uid].keys()):
                if key not in current_deals:
                    message_id = user_deal_messages[uid].pop(key)
                    try:
                        await bot.delete_message(uid, message_id)
                        updated = True
                    except Exception as exc:
                        print(f"Ошибка при удалении сообщения для пользователя {uid}: {exc}")
                    last_deal_data[uid].pop(key, None)
                    last_deal_text[uid].pop(key, None)

            # Process current deals
            for key, deal in current_deals.items():
                text = format_deal(deal)
                # New deal
                if key not in user_deal_messages[uid]:
                    kb = None
                    slug = deal.get("slug")
                    if slug:
                        kb = InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton("🚫 Исключить", callback_data=f"exclude:{slug}")]]
                        )
                    try:
                        sent = await bot.send_message(
                            uid,
                            text,
                            parse_mode=PARSE_MODE_HTML,
                            disable_web_page_preview=True,
                            reply_markup=kb,
                        )
                        user_deal_messages[uid][key] = sent.message_id
                        last_deal_data[uid][key] = deal
                        last_deal_text[uid][key] = text
                        updated = True
                    except Exception as exc:
                        print(f"Ошибка при отправке сделки для пользователя {uid}: {exc}")
                    continue

                # Existing deal - check for changes
                prev_text = last_deal_text[uid].get(key)
                if prev_text == text:
                    continue

                message_id = user_deal_messages[uid][key]
                try:
                    kb = None
                    slug = deal.get("slug")
                    if slug:
                        kb = InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton("🚫 Исключить", callback_data=f"exclude:{slug}")]]
                        )
                    await bot.edit_message_text(
                        text,
                        chat_id=uid,
                        message_id=message_id,
                        parse_mode=PARSE_MODE_HTML,
                        disable_web_page_preview=True,
                        reply_markup=kb,
                    )
                    last_deal_data[uid][key] = deal
                    last_deal_text[uid][key] = text
                    updated = True
                except Exception as exc:
                    print(f"Ошибка при обновлении сделки для пользователя {uid}: {exc}")
                    user_deal_messages[uid].pop(key, None)
                    last_deal_data[uid].pop(key, None)
                    last_deal_text[uid].pop(key, None)

            if updated:
                try:
                    await refresh_menu_for_user(bot, uid)
                except Exception:
                    pass

        await asyncio.sleep(1)
    
    # Cleanup when monitoring stops
    for uid in list(user_deal_messages.keys()):
        user_deal_messages.pop(uid, None)
        last_deal_data.pop(uid, None)
        last_deal_text.pop(uid, None)


async def start_monitoring(user_id: int) -> None:
    global monitor_task, bot_instance
    ensure_user_settings(user_id)
    if user_settings[user_id].get("monitoring"):
        return
    user_settings[user_id]["monitoring"] = True
    persist_user_settings(user_id)
    if monitor_task is None and bot_instance is not None:
        monitor_task = asyncio.create_task(global_monitor_loop(bot_instance))


async def stop_monitoring(user_id: int) -> None:
    """Disable monitoring for a user"""
    cfg = user_settings.get(user_id)
    if cfg is None:
        return
    cfg["monitoring"] = False
    persist_user_settings(user_id)


# -----------------------------------------------------------------------------
# Callback Handlers
#

@router.callback_query(F.data == "toggle_monitor")
async def cb_toggle_monitor(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in allowed_users:
        await call.answer("Нет доступа", show_alert=True)
        return
    ensure_user_settings(uid)
    if user_settings[uid].get("monitoring"):
        await stop_monitoring(uid)
        await call.answer("Мониторинг остановлен")
    else:
        await start_monitoring(uid)
        await call.answer("Мониторинг запущен")
    await refresh_menu_for_user(call.message.bot, uid)


@router.callback_query(F.data == "settings_menu")
async def cb_settings_menu(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in allowed_users:
        await call.answer("Нет доступа", show_alert=True)
        return
    text, kb = build_settings_menu(uid)
    await call.message.edit_text(text, reply_markup=kb, parse_mode=PARSE_MODE_HTML)
    await call.answer()


@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    text, kb = build_admin_menu(uid)
    await call.message.edit_text(text, reply_markup=kb, parse_mode=PARSE_MODE_HTML)
    await call.answer()


@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in allowed_users:
        await call.answer()
        return
    await refresh_menu_for_user(call.message.bot, uid)
    await call.answer()


@router.callback_query(F.data == "set_pages")
async def cb_set_pages(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    buttons = [
        [InlineKeyboardButton(str(i), callback_data=f"pages:{i}")]
        for i in range(1, admin_settings["max_pages"] + 1)
    ]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("Выберите количество страниц:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("pages:"))
async def cb_pages_select(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    n = int(call.data.split(":")[1])
    user_settings[uid]["pages"] = n
    persist_user_settings(uid)
    await call.answer("Обновлено")
    await call.message.delete()
    await refresh_menu_for_user(call.message.bot, uid)

@router.callback_query(F.data == "set_price")
async def cb_set_price(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    values = [0, 50, 100, 200, 500, 1000]
    buttons = [[InlineKeyboardButton(str(v), callback_data=f"price_min:{v}") for v in values]]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("Выберите минимальную цену ($):", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("price_min:"))
async def cb_price_min(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    val = int(call.data.split(":")[1])
    user_settings[uid]["price_min"] = val
    values = [0, 50, 100, 200, 500, 1000, "∞"]
    buttons = []
    row = []
    for v in values:
        cb = "price_max:inf" if v == "∞" else f"price_max:{v}"
        row.append(InlineKeyboardButton(str(v), callback_data=cb))
    buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("Выберите максимальную цену ($):", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("price_max:"))
async def cb_price_max(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    val = call.data.split(":")[1]
    user_settings[uid]["price_max"] = float("inf") if val == "inf" else int(val)
    persist_user_settings(uid)
    await call.answer("Диапазон обновлён")
    await call.message.delete()
    await refresh_menu_for_user(call.message.bot, uid)
@router.callback_query(F.data == "set_diff")
async def cb_set_diff(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    options = [1, 2, 3, 5, 10]
    buttons = [[InlineKeyboardButton(f"{v}%", callback_data=f"diff:{v}") for v in options]]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("Выберите максимальный разрыв:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("diff:"))
async def cb_diff_select(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    val = float(call.data.split(":")[1])
    user_settings[uid]["diff_max"] = val
    persist_user_settings(uid)
    await call.answer("Порог обновлён")
    await call.message.delete()
    await refresh_menu_for_user(call.message.bot, uid)
@router.callback_query(F.data == "set_excluded")
async def cb_set_excluded(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    excluded = sorted(user_settings[uid]["excluded"])
    buttons = [[InlineKeyboardButton(slug, callback_data=f"unexclude:{slug}")] for slug in excluded]
    if excluded:
        buttons.append([InlineKeyboardButton("🧹 Очистить", callback_data="clear_excluded")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "Исключённые коллекции:" if excluded else "Исключённые коллекции: (пусто)"
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("unexclude:"))
async def cb_unexclude(call: CallbackQuery) -> None:
    uid = call.from_user.id
    slug = call.data.split(":")[1]
    ensure_user_settings(uid)
    user_settings[uid]["excluded"].discard(slug)
    persist_user_settings(uid)
    await call.answer("Удалено")
    await cb_set_excluded(call)
    await refresh_menu_for_user(call.message.bot, uid)

@router.callback_query(F.data == "clear_excluded")
async def cb_clear_excluded(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    user_settings[uid]["excluded"].clear()
    persist_user_settings(uid)
    await call.answer("Очищено")
    await cb_set_excluded(call)
    await refresh_menu_for_user(call.message.bot, uid)


@router.callback_query(F.data.startswith("exclude:"))
async def cb_exclude_from_deal(call: CallbackQuery) -> None:
    uid = call.from_user.id
    slug = call.data.split(":")[1]
    ensure_user_settings(uid)
    user_settings[uid]["excluded"].add(slug)
    persist_user_settings(uid)
    await call.answer("Добавлено в исключения")
    try:
        await call.message.delete()
    except Exception:
        pass
    await refresh_menu_for_user(call.message.bot, uid)
@router.callback_query(F.data == "admin_adduser")
async def cb_admin_adduser(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    c.execute("SELECT id FROM users")
    all_users = {row["id"] for row in c.fetchall()}
    candidates = sorted(all_users - allowed_users)
    if not candidates:
        await call.answer("Нет новых пользователей", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(str(u), callback_data=f"admin_adduser:{u}")] for u in candidates]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("Добавить пользователя:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("admin_adduser:"))
async def cb_admin_adduser_select(call: CallbackQuery) -> None:
    admin_id = call.from_user.id
    if admin_id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    user_id = int(call.data.split(":")[1])
    allowed_users.add(user_id)
    c.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
    conn.commit()
    await call.answer("Пользователь добавлен")
    await call.message.delete()
    await refresh_menu_for_user(call.message.bot, admin_id)
@router.callback_query(F.data == "admin_removeuser")
async def cb_admin_removeuser(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    removable = sorted(u for u in allowed_users if u not in ADMIN_IDS)
    if not removable:
        await call.answer("Некого удалять", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(str(u), callback_data=f"admin_removeuser:{u}")] for u in removable]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("Удалить пользователя:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("admin_removeuser:"))
async def cb_admin_removeuser_select(call: CallbackQuery) -> None:
    admin_id = call.from_user.id
    if admin_id not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    user_id = int(call.data.split(":")[1])
    allowed_users.discard(user_id)
    await call.answer("Удалён")
    await call.message.delete()
    await refresh_menu_for_user(call.message.bot, admin_id)
@router.callback_query(F.data == "admin_listusers")
async def cb_admin_listusers(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    c.execute("SELECT id FROM users")
    rows = c.fetchall()
    users = [str(r["id"]) for r in rows]
    text = "Разрешённые пользователи:\n" + "\n".join(users) if users else "Нет пользователей"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]]
    )
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "admin_setmaxpages")
async def cb_admin_setmaxpages(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(str(i), callback_data=f"setmax:{i}")] for i in range(1, MAX_PAGES_CODE + 1)]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("Выберите глобальный лимит страниц:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("setmax:"))
async def cb_admin_setmaxpages_select(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    value = int(call.data.split(":")[1])
    admin_settings["max_pages"] = value
    persist_admin_settings()
    await call.answer("Обновлено")
    await call.message.delete()
    await refresh_menu_for_user(call.message.bot, uid)


async def main() -> None:
    """Run the bot"""
    global bot_instance
    bot_instance = Bot(BOT_TOKEN, parse_mode=PARSE_MODE_HTML)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot_instance)


# -----------------------------------------------------------------------------
# Command Handlers
#


@router.message(Command("start"))
async def handle_start(message: types.Message) -> None:
    """Handle /start command and show intro"""
    uid = message.from_user.id
    if uid in ADMIN_IDS:
        allowed_users.add(uid)
    if uid not in allowed_users:
        await message.reply(
            "🚫 У вас нет доступа. Попросите администратора добавить вас.",
        )
        return

    c.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (uid,))
    conn.commit()
    ensure_user_settings(uid)

    intro = (
        "👋 <b>Добро пожаловать!</b>\n"
        "Бот ищет выгодные сделки на OpenSea.\n"
        "Используйте кнопки ниже для запуска мониторинга и настройки фильтров."
    )
    await message.answer(intro, parse_mode=PARSE_MODE_HTML)
    await refresh_menu_for_user(message.bot, uid)


if __name__ == "__main__":
    asyncio.run(main())
