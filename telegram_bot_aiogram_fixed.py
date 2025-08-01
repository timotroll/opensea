import os
import re
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
        "awaiting": None,
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
admin_states: Dict[int, str] = {}
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
# Control panel helpers
#
control_panel_messages: Dict[int, int] = {}


def build_control_panel_text(uid: int) -> str:
    cfg = user_settings[uid]
    max_price_display = "∞" if cfg["price_max"] == float("inf") else f"{cfg['price_max']:.0f}"
    monitoring = "🟢 Мониторинг включен" if cfg.get("monitoring") else "🔴 Мониторинг выключен"
    return (
        f"{monitoring}\n"
        f"Страницы: {cfg['pages']}/{admin_settings['max_pages']}\n"
        f"Диапазон цен: {cfg['price_min']:.0f}-{max_price_display}$\n"
        f"Порог разрыва: {cfg['diff_max']:.2f}%\n"
        f"Исключений: {len(cfg['excluded'])}"
    )


def build_control_panel_keyboard(uid: int) -> InlineKeyboardMarkup:
    cfg = user_settings[uid]
    buttons: List[List[InlineKeyboardButton]] = []
    if cfg.get("monitoring"):
        buttons.append([InlineKeyboardButton(text="⏹ Остановить", callback_data="monitor:stop")])
    else:
        buttons.append([InlineKeyboardButton(text="▶️ Запустить", callback_data="monitor:start")])
    buttons.append([
        InlineKeyboardButton(text="📄 Страницы", callback_data="set:pages"),
        InlineKeyboardButton(text="💲 Цена", callback_data="set:price"),
    ])
    buttons.append([
        InlineKeyboardButton(text="📉 Разрыв", callback_data="set:diff"),
    ])
    if uid in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_or_update_panel(uid: int, bot: Bot) -> None:
    ensure_user_settings(uid)
    text = build_control_panel_text(uid)
    keyboard = build_control_panel_keyboard(uid)
    msg_id = control_panel_messages.get(uid)
    if msg_id:
        try:
            await bot.edit_message_text(text, uid, msg_id, reply_markup=keyboard)
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, text, reply_markup=keyboard)
    control_panel_messages[uid] = sent.message_id

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
            "awaiting": None,
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
        lines.append(
            f"{i+1}. <a href='{link}'>{name}</a>\n"
            f"   💵 Цена: ${price_str}\n"
            f"   🧾 Floor: {floor_str} ETH\n"
            f"   🤝 Offer: {offer_str} ETH\n"
            f"   📉 Разрыв: {diff_str}%\n"
        )
    return "\n".join(lines)


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


def extract_slug(text: str) -> Optional[str]:
    """Extract slug from OpenSea URL"""
    if not text:
        return None
    match = re.search(r"opensea\.io/collection/([\w\-]+)", text)
    return match.group(1) if match else None


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
                    try:
                        sent = await bot.send_message(
                            uid,
                            text,
                            parse_mode=PARSE_MODE_HTML,
                            disable_web_page_preview=True,
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
                    await bot.edit_message_text(
                        text,
                        chat_id=uid,
                        message_id=message_id,
                        parse_mode=PARSE_MODE_HTML,
                        disable_web_page_preview=True,
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
    if bot_instance is not None:
        await send_or_update_panel(user_id, bot_instance)


async def stop_monitoring(user_id: int) -> None:
    """Disable monitoring for a user"""
    cfg = user_settings.get(user_id)
    if cfg is None:
        return
    cfg["monitoring"] = False
    persist_user_settings(user_id)
    if bot_instance is not None:
        await send_or_update_panel(user_id, bot_instance)


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
    user_settings[uid]["awaiting"] = "pages"
    await call.message.answer(
        f"Введите число страниц (1-{admin_settings['max_pages']}):"
    )
    await call.answer()


@router.callback_query(F.data == "set_price")
async def cb_set_price(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    user_settings[uid]["awaiting"] = "price"
    await call.message.answer("Введите мин и макс цену через пробел (макс=0 без лимита):")
    await call.answer()


@router.callback_query(F.data == "set_diff")
async def cb_set_diff(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    user_settings[uid]["awaiting"] = "diff"
    await call.message.answer("Введите максимальный процент разрыва:")
    await call.answer()


@router.callback_query(F.data == "set_excluded")
async def cb_set_excluded(call: CallbackQuery) -> None:
    uid = call.from_user.id
    ensure_user_settings(uid)
    user_settings[uid]["awaiting"] = "exclude"
    await call.message.answer("Отправьте слаг коллекции для исключения или 'clear' для очистки:")
    await call.answer()


@router.callback_query(F.data == "admin_adduser")
async def cb_admin_adduser(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    user_settings[uid]["awaiting"] = "admin_adduser"
    await call.message.answer("Введите ID пользователя для добавления:")
    await call.answer()


@router.callback_query(F.data == "admin_removeuser")
async def cb_admin_removeuser(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    user_settings[uid]["awaiting"] = "admin_removeuser"
    await call.message.answer("Введите ID пользователя для удаления:")
    await call.answer()


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
    await call.message.answer(text)
    await call.answer()


@router.callback_query(F.data == "admin_setmaxpages")
async def cb_admin_setmaxpages(call: CallbackQuery) -> None:
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("Нет прав", show_alert=True)
        return
    user_settings[uid]["awaiting"] = "admin_setmaxpages"
    await call.message.answer(
        f"Введите новый глобальный лимит страниц (1-{MAX_PAGES_CODE}):"
    )
    await call.answer()


# -----------------------------------------------------------------------------
# Command Handlers
#
@router.message(Command("start"))
async def handle_start(message: types.Message) -> None:
    """Handle /start command"""
    uid = message.from_user.id
    if uid in ADMIN_IDS:
        allowed_users.add(uid)
    if uid not in allowed_users:
        await message.reply(
            "🚫 У вас нет доступа. Попросите администратора добавить вас."
        )
        return

    # Persist new user if needed
    c.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (uid,))
    conn.commit()

    ensure_user_settings(uid)



@router.message(Command("settings"))
async def handle_settings_cmd(message: types.Message) -> None:
    """Show current user settings"""
    uid = message.from_user.id
    if uid not in allowed_users:
        await message.reply("🚫 Нет доступа")
        return
    ensure_user_settings(uid)
    cfg = user_settings[uid]
    max_price_display = "∞" if cfg['price_max'] == float('inf') else f"{cfg['price_max']:.0f}"
    excl_count = len(cfg['excluded'])
    monitoring = "включён" if cfg.get("monitoring") else "выключен"
    await message.reply(
        f"Настройки:\n"
        f"Страницы: {cfg['pages']} из {admin_settings['max_pages']}\n"
        f"Диапазон цен: {cfg['price_min']:.0f}-{max_price_display}$\n"
        f"Порог разрыва: {cfg['diff_max']:.2f}%\n"
        f"Исключений: {excl_count}\n"
        f"Мониторинг: {monitoring}",
    )



@router.message(Command("pages"))
async def handle_pages_cmd(message: types.Message) -> None:
    """Set number of pages to monitor"""
    uid = message.from_user.id
    if uid not in allowed_users:
        await message.reply("🚫 Нет доступа")
        return
    ensure_user_settings(uid)
    args = get_message_args(message).split()
    if not args:
        await message.reply(
            f"Текущее количество страниц: {user_settings[uid]['pages']}."
            f" Введите `/pages <N>`, где N от 1 до {admin_settings['max_pages']}.",
            parse_mode=None,
        )
        await refresh_menu_for_user(message.bot, uid)
        return
    try:
        n = int(args[0])
    except ValueError:
        await message.reply("Неверный формат. Укажите число.")
        return
    n = max(1, min(n, admin_settings['max_pages']))
    user_settings[uid]['pages'] = n
    persist_user_settings(uid)
    await message.reply(f"Количество страниц установлено на {n}.")



@router.message(Command("price"))
async def handle_price_cmd(message: types.Message) -> None:
    """Set price range filter"""
    uid = message.from_user.id
    if uid not in allowed_users:
        await message.reply("🚫 Нет доступа")
        return
    ensure_user_settings(uid)
    args = get_message_args(message).split()
    if len(args) < 2:
        await message.reply(
            "Использование: /price <мин> <макс>. Если макс = 0, ограничение отсутствует.", parse_mode=None
        )
        await refresh_menu_for_user(message.bot, uid)
        return
    try:
        min_val = float(args[0].replace(",", "."))
        max_val = float(args[1].replace(",", "."))
    except ValueError:
        await message.reply("Неверный формат. Укажите два числа.")
        await refresh_menu_for_user(message.bot, uid)
        return
    if min_val < 0:
        await message.reply("Минимальная цена не может быть отрицательной.")
        await refresh_menu_for_user(message.bot, uid)
        return
    user_settings[uid]['price_min'] = min_val
    if max_val <= 0:
        user_settings[uid]['price_max'] = float('inf')
    else:
        user_settings[uid]['price_max'] = max_val
    persist_user_settings(uid)
    await message.reply("Диапазон цен обновлён.")



@router.message(Command("diff"))
async def handle_diff_cmd(message: types.Message) -> None:
    """Set max difference percentage"""
    uid = message.from_user.id
    if uid not in allowed_users:
        await message.reply("🚫 Нет доступа")
        return
    ensure_user_settings(uid)
    args = get_message_args(message).split()
    if not args:
        await message.reply("Использование: /diff <процент>.", parse_mode=None)
        await refresh_menu_for_user(message.bot, uid)
        return
    try:
        val = float(args[0].replace(",", "."))
    except ValueError:
        await message.reply("Неверный формат. Укажите число.")
        await refresh_menu_for_user(message.bot, uid)
        return
    if val <= 0:
        await message.reply("Порог должен быть положительным.")
        await refresh_menu_for_user(message.bot, uid)
        return
    user_settings[uid]['diff_max'] = val
    persist_user_settings(uid)
    await message.reply(f"Порог разрыва установлен на {val:.2f}%.")



@router.message(Command("exclude"))
async def handle_exclude_cmd(message: types.Message) -> None:
    """Manage excluded collections"""
    uid = message.from_user.id
    if uid not in allowed_users:
        await message.reply("🚫 Нет доступа")
        return
    ensure_user_settings(uid)
    args = get_message_args(message).split()
    if not args:
        await message.reply(
            "Использование: /exclude add <slug|url> или /exclude clear или /exclude list.", parse_mode=None
        )
        await refresh_menu_for_user(message.bot, uid)
        return
    sub = args[0].lower()
    if sub == "add" and len(args) >= 2:
        slug = extract_slug(args[1]) or args[1]
        user_settings[uid]['excluded'].add(slug)
        persist_user_settings(uid)
        await message.reply(f"Коллекция '{slug}' исключена.")

        return
    if sub == "clear":
        user_settings[uid]['excluded'].clear()
        persist_user_settings(uid)
        await message.reply("Список исключений очищен.")

        return
    if sub == "list":
        excl = user_settings[uid]['excluded']
        if excl:
            await message.reply("Исключены: " + ", ".join(excl))
        else:
            await message.reply("Список исключений пуст.")
        await refresh_menu_for_user(message.bot, uid)
        return
    await message.reply("Неверная команда исключения. Используйте add/clear/list.")
    await refresh_menu_for_user(message.bot, uid)


@router.message(Command("monitor"))
async def handle_monitor_cmd(message: types.Message) -> None:
    """Start/stop monitoring"""
    uid = message.from_user.id
    if uid not in allowed_users:
        await message.reply("🚫 Нет доступа")
        return
    ensure_user_settings(uid)
    args = get_message_args(message).split()
    if not args:
        status = "включён" if user_settings[uid].get("monitoring") else "выключен"
        await message.reply(
            f"Мониторинг сейчас {status}. Используйте `/monitor start` или `/monitor stop`.",
            parse_mode=PARSE_MODE_MARKDOWN,
        )

        return
    sub = args[0].lower()
    if sub == "start":
        await start_monitoring(uid)
        await message.reply("Мониторинг запущен.")

        return
    if sub == "stop":
        await stop_monitoring(uid)
        await message.reply("Мониторинг остановлен.")

        return
    await message.reply("Неверная команда. Используйте start или stop.")
    await refresh_menu_for_user(message.bot, uid)


@router.callback_query()
async def handle_callbacks(query: CallbackQuery) -> None:
    uid = query.from_user.id
    if uid not in allowed_users:
        await query.answer("Нет доступа", show_alert=True)
        return
    data = query.data or ""
    if data == "monitor:start":
        await start_monitoring(uid)
        await query.answer("Мониторинг запущен")
        await send_or_update_panel(uid, query.message.bot)
    elif data == "monitor:stop":
        await stop_monitoring(uid)
        await query.answer("Мониторинг остановлен")
        await send_or_update_panel(uid, query.message.bot)
    elif data == "set:pages":
        user_settings[uid]['awaiting'] = "pages"
        await query.message.answer("Введите количество страниц:")
        await query.answer()
    elif data == "set:price":
        user_settings[uid]['awaiting'] = "price"
        await query.message.answer("Введите диапазон цен <мин> <макс> (макс=0 без лимита):")
        await query.answer()
    elif data == "set:diff":
        user_settings[uid]['awaiting'] = "diff"
        await query.message.answer("Введите максимальный разрыв в %:")
        await query.answer()
    elif data == "admin:menu":
        if uid in ADMIN_IDS:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Лимит страниц", callback_data="admin:maxpages")],
                [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin:listusers")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")],
            ])
            await query.message.edit_text("Админ-панель", reply_markup=kb)
        await query.answer()
    elif data == "admin:back":
        await send_or_update_panel(uid, query.message.bot)
        await query.answer()
    elif data == "admin:maxpages":
        if uid in ADMIN_IDS:
            user_settings[uid]['awaiting'] = "admin_max_pages"
            await query.message.answer("Введите новый лимит страниц:")
        await query.answer()
    elif data == "admin:listusers":
        if uid in ADMIN_IDS:
            c.execute("SELECT id FROM users")
            rows = c.fetchall()
            users = [str(r['id']) for r in rows] or ["Нет пользователей"]
            await query.message.answer("Пользователи:\n" + "\n".join(users))
        await query.answer()
    else:
        await query.answer()


@router.message()
async def handle_awaiting_input(message: types.Message) -> None:
    uid = message.from_user.id
    if uid not in allowed_users:
        return
    state = user_settings.get(uid, {}).get("awaiting")
    if not state:
        return
    text = message.text or ""
    try:
        if state == "pages":
            n = int(text)
            n = max(1, min(n, admin_settings['max_pages']))
            user_settings[uid]['pages'] = n
            persist_user_settings(uid)
            await message.reply(f"Количество страниц установлено на {n}.")
        elif state == "price":
            parts = text.split()
            if len(parts) != 2:
                raise ValueError
            min_val = float(parts[0].replace(",", "."))
            max_val = float(parts[1].replace(",", "."))
            if min_val < 0:
                raise ValueError
            user_settings[uid]['price_min'] = min_val
            user_settings[uid]['price_max'] = float('inf') if max_val <= 0 else max_val
            persist_user_settings(uid)
            await message.reply("Диапазон цен обновлён.")
        elif state == "diff":
            val = float(text.replace(",", "."))
            if val <= 0:
                raise ValueError
            user_settings[uid]['diff_max'] = val
            persist_user_settings(uid)
            await message.reply(f"Порог разрыва установлен на {val:.2f}%.")
        elif state == "admin_max_pages":
            if uid not in ADMIN_IDS:
                return
            val = int(text)
            val = max(1, min(val, MAX_PAGES_CODE))
            admin_settings['max_pages'] = val
            persist_admin_settings()
            for u, cfg in user_settings.items():
                if cfg['pages'] > val:
                    cfg['pages'] = val
                    persist_user_settings(u)
            await message.reply(f"Новый лимит страниц: {val}.")
        else:
            return
    except Exception:
        await message.reply("Неверный ввод.")
    user_settings[uid]['awaiting'] = None
    await send_or_update_panel(uid, message.bot)


@router.message(Command("help"))
async def handle_help_cmd(message: types.Message) -> None:
    """Show help information"""
    uid = message.from_user.id
    if uid in ADMIN_IDS:
        text = (
            "Доступные команды пользователя:\n"
            "/pages <N> — установить число страниц.\n"
            "/price <мин> <макс> — диапазон цен.\n"
            "/diff <процент> — порог разрыва.\n"
            "/exclude add|clear|list — управление исключениями.\n"
            "/settings — показать ваши настройки.\n"
            "/monitor start|stop — мониторинг.\n"
            "\n"
            "Команды администратора:\n"
            "/adduser <id> — добавить пользователя.\n"
            "/removeuser <id> — удалить пользователя.\n"
            "/listusers — показать всех разрешённых пользователей.\n"
            "/setmaxpages <N> — установить глобальный лимит страниц."
        )
    elif uid in allowed_users:
        text = (
            "Доступные команды пользователя:\n"
            "/pages <N> — установить число страниц.\n"
            "/price <мин> <макс> — диапазон цен.\n"
            "/diff <процент> — порог разрыва.\n"
            "/exclude add|clear|list — управление исключениями.\n"
            "/settings — показать ваши настройки.\n"
            "/monitor start|stop — мониторинг."
        )
    else:
        await message.reply("🚫 У вас нет доступа.")
        await refresh_menu_for_user(message.bot, uid)
        return
    await message.reply(text, parse_mode=None)
    await refresh_menu_for_user(message.bot, uid)


@router.message(Command("adduser"))
async def handle_adduser_cmd(message: types.Message) -> None:
    """Add a new user (admin only)"""
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await message.reply("🚫 Нет прав администратора")
        await refresh_menu_for_user(message.bot, uid)
        return
    args = get_message_args(message).split()
    if not args:
        await message.reply("Использование: /adduser <id>.", parse_mode=None)
        await refresh_menu_for_user(message.bot, uid)
        return
    try:
        new_id = int(args[0])
    except ValueError:
        await message.reply("ID должен быть числом.")
        await refresh_menu_for_user(message.bot, uid)
        return
    
    # Persist to DB
    c.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (new_id,))
    conn.commit()
    
    allowed_users.add(new_id)
    ensure_user_settings(new_id)
    await message.reply(f"Пользователь {new_id} добавлен.")
    await refresh_menu_for_user(message.bot, uid)


@router.message(Command("removeuser"))
async def handle_removeuser_cmd(message: types.Message) -> None:
    """Remove a user (admin only)"""
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await message.reply("🚫 Нет прав администратора")
        await refresh_menu_for_user(message.bot, uid)
        return
    args = get_message_args(message).split()
    if not args:
        await message.reply("Использование: /removeuser <id>.", parse_mode=None)
        await refresh_menu_for_user(message.bot, uid)
        return
    try:
        rem_id = int(args[0])
    except ValueError:
        await message.reply("ID должен быть числом.")
        await refresh_menu_for_user(message.bot, uid)
        return
    if rem_id in ADMIN_IDS:
        await message.reply("Нельзя удалить администратора.")
        await refresh_menu_for_user(message.bot, uid)
        return
    
    # Remove from DB
    c.execute("DELETE FROM users WHERE id=?", (rem_id,))
    c.execute("DELETE FROM user_settings WHERE user_id=?", (rem_id,))
    conn.commit()
    
    allowed_users.discard(rem_id)
    user_settings.pop(rem_id, None)
    await stop_monitoring(rem_id)
    await message.reply(f"Пользователь {rem_id} удалён.")
    await refresh_menu_for_user(message.bot, uid)


@router.message(Command("listusers"))
async def handle_listusers_cmd(message: types.Message) -> None:
    """List all allowed users (admin only)"""
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await message.reply("🚫 Нет прав администратора")
        await refresh_menu_for_user(message.bot, uid)
        return
    
    c.execute("SELECT id FROM users")
    rows = c.fetchall()
    users = [str(r["id"]) for r in rows]
    
    if not users:
        await message.reply("Нет зарегистрированных пользователей.")
        await refresh_menu_for_user(message.bot, uid)
        return

    await message.reply("Разрешённые пользователи:\n" + "\n".join(users))
    await refresh_menu_for_user(message.bot, uid)


@router.message(Command("setmaxpages"))
async def handle_setmaxpages_cmd(message: types.Message) -> None:
    """Set global page limit (admin only)"""
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await message.reply("🚫 Нет прав администратора")
        await refresh_menu_for_user(message.bot, uid)
        return
    args = get_message_args(message).split()
    if not args:
        await message.reply(
            f"Текущий лимит страниц: {admin_settings['max_pages']}. "
            f"Использование: /setmaxpages <N>, где N ≤ {MAX_PAGES_CODE}. ({MAX_PAGES_CODE}00 коллекций)", parse_mode=None
        )
        await refresh_menu_for_user(message.bot, uid)
        return
    try:
        val = int(args[0])
    except ValueError:
        await message.reply("Введите число.")
        await refresh_menu_for_user(message.bot, uid)
        return
    val = max(1, min(val, MAX_PAGES_CODE))
    admin_settings['max_pages'] = val
    persist_admin_settings()
    
    # Update users exceeding new max
    for u, cfg in user_settings.items():
        if cfg['pages'] > val:
            cfg['pages'] = val
            persist_user_settings(u)

    await message.reply(f"Новый лимит страниц: {val}.")
    await refresh_menu_for_user(message.bot, uid)


@router.message()
async def handle_text_auto_exclude(message: types.Message) -> None:
    """Process free text inputs for awaiting states or auto-exclude"""
    uid = message.from_user.id
    if (uid not in allowed_users or not message.text or message.text.startswith("/")
            or user_settings.get(uid, {}).get("awaiting")):
        return
    ensure_user_settings(uid)
    cfg = user_settings[uid]
    text = message.text.strip()
    state = cfg.get("awaiting")

    if state == "pages":
        try:
            n = int(text)
            n = max(1, min(n, admin_settings["max_pages"]))
            cfg["pages"] = n
            persist_user_settings(uid)
            await message.reply(f"Количество страниц установлено на {n}.")
        except ValueError:
            await message.reply("Неверный формат. Укажите число.")
        cfg["awaiting"] = None
        await refresh_menu_for_user(message.bot, uid)
        return

    if state == "price":
        parts = text.replace(",", ".").split()
        if len(parts) >= 2:
            try:
                min_val = float(parts[0])
                max_val = float(parts[1])
                if min_val < 0:
                    await message.reply("Минимальная цена не может быть отрицательной.")
                else:
                    cfg["price_min"] = min_val
                    cfg["price_max"] = float("inf") if max_val <= 0 else max_val
                    persist_user_settings(uid)
                    await message.reply("Диапазон цен обновлён.")
            except ValueError:
                await message.reply("Неверный формат. Укажите два числа.")
        else:
            await message.reply("Укажите два числа.")
        cfg["awaiting"] = None
        await refresh_menu_for_user(message.bot, uid)
        return

    if state == "diff":
        try:
            val = float(text.replace(",", "."))
            if val <= 0:
                await message.reply("Порог должен быть положительным.")
            else:
                cfg["diff_max"] = val
                persist_user_settings(uid)
                await message.reply(f"Порог разрыва установлен на {val:.2f}%.")
        except ValueError:
            await message.reply("Неверный формат. Укажите число.")
        cfg["awaiting"] = None
        await refresh_menu_for_user(message.bot, uid)
        return

    if state == "exclude":
        if text.lower() == "clear":
            cfg["excluded"].clear()
            persist_user_settings(uid)
            await message.reply("Список исключений очищен.")
        else:
            slug = extract_slug(text) or text
            cfg["excluded"].add(slug)
            persist_user_settings(uid)
            await message.reply(f"Коллекция '{slug}' исключена.")
        cfg["awaiting"] = None
        await refresh_menu_for_user(message.bot, uid)
        return

    if state == "admin_adduser":
        try:
            new_id = int(text)
            c.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (new_id,))
            conn.commit()
            allowed_users.add(new_id)
            ensure_user_settings(new_id)
            await message.reply(f"Пользователь {new_id} добавлен.")
        except ValueError:
            await message.reply("ID должен быть числом.")
        cfg["awaiting"] = None
        await refresh_menu_for_user(message.bot, uid)
        return

    if state == "admin_removeuser":
        try:
            rem_id = int(text)
            if rem_id in ADMIN_IDS:
                await message.reply("Нельзя удалить администратора.")
            else:
                c.execute("DELETE FROM users WHERE id=?", (rem_id,))
                c.execute("DELETE FROM user_settings WHERE user_id=?", (rem_id,))
                conn.commit()
                allowed_users.discard(rem_id)
                user_settings.pop(rem_id, None)
                await stop_monitoring(rem_id)
                await message.reply(f"Пользователь {rem_id} удалён.")
        except ValueError:
            await message.reply("ID должен быть числом.")
        cfg["awaiting"] = None
        await refresh_menu_for_user(message.bot, uid)
        return

    if state == "admin_setmaxpages":
        try:
            val = int(text)
            val = max(1, min(val, MAX_PAGES_CODE))
            admin_settings["max_pages"] = val
            persist_admin_settings()
            for u, cfg_u in user_settings.items():
                if cfg_u["pages"] > val:
                    cfg_u["pages"] = val
                    persist_user_settings(u)
            await message.reply(f"Новый лимит страниц: {val}.")
        except ValueError:
            await message.reply("Введите число.")
        cfg["awaiting"] = None
        await refresh_menu_for_user(message.bot, uid)
        return

    # Default auto-exclude behavior
    slug = extract_slug(text)
    if slug:
        cfg["excluded"].add(slug)
        persist_user_settings(uid)
        await message.reply(f"Коллекция '{slug}' добавлена в исключения.")



# -----------------------------------------------------------------------------
# Main Bot Setup
#
def main() -> None:
    global bot_instance  # Добавлено для доступа к глобальной переменной
    
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Please set your Telegram bot token via the TELEGRAM_BOT_TOKEN environment "
            "variable or replace BOT_TOKEN in telegram_bot_aiogram.py."
        )
    
    # Create bot instance
    try:
        from aiogram.client.bot import DefaultBotProperties  # type: ignore
        bot_instance = Bot(  # Исправлено: сохраняем в глобальную переменную
            token=BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=PARSE_MODE_HTML),
        )
    except Exception:
        bot_instance = Bot(token=BOT_TOKEN, parse_mode=PARSE_MODE_HTML)
    
    # Create dispatcher
    dp = Dispatcher()
    dp.startup.register(lambda *args, **kwargs: print("Aiogram bot started"))
    dp.include_router(router)

    # Start polling
    asyncio.run(dp.start_polling(bot_instance))  # Исправлено: передаем bot_instance


if __name__ == "__main__":
    main()