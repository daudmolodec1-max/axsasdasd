# language: Python 3.11+, file: admin_bot.py
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime
import random

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- НАСТРОЙКИ ---
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "9260782657:AAHDtIG1ZQVCaZ-2N2aF1wX_43jTzDBD-kM")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "960992616"))
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "-1004358051534"))
LOG_THREAD_ID = int(os.getenv("LOG_THREAD_ID", "1245"))
LOG_THREAD_ID_TEMP = int(os.getenv("LOG_THREAD_ID_TEMP", "1249"))

# --- БАЗА ДАННЫХ ---
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
conn = sqlite3.connect(os.path.join(DATA_DIR, "bot_data.db"), check_same_thread=False)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, uses_left INTEGER, days INTEGER)")
cur.execute("CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, channel_link TEXT)")
cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (MAIN_ADMIN_ID,))
conn.commit()

conn_users = sqlite3.connect(os.path.join(DATA_DIR, "users.db"), check_same_thread=False)
cur_users = conn_users.cursor()

bot = Bot(ADMIN_BOT_TOKEN)
dp = Dispatcher()
log = logging.getLogger("admin")

def is_admin(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return cur.fetchone() is not None

def get_setting(key: str) -> str:
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else ""

def set_setting(key: str, value: str):
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

async def log_sub_grant(user_id: int, dur_text: str):
    try:
        chat = await bot.get_chat(user_id)
        username = f"@{chat.username}" if chat.username else f"ID {user_id}"
    except Exception: username = f"ID {user_id}"
    
    text = f"Пользователь {username} получил подписку\nВремя: {datetime.now().strftime('%d.%m.%Y %H:%M')}\nСрок: {dur_text}\nБот - @exit12_bot"
    try: await bot.send_message(LOG_CHAT_ID, text, message_thread_id=LOG_THREAD_ID)
    except Exception: pass

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

# --- ГЛАВНОЕ МЕНЮ ---
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

# --- ВРЕМЕННЫЙ ДОСТУП ---
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

# --- ПРОМОКОДЫ ---
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

# --- ПОИСК И ВЫДАЧА ПОДПИСОК ---
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

# --- ВЫДАЧА ПО ID И РАССЫЛКА ---
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

# --- КАНАЛЫ И АДМИНЫ ---
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

# --- СТАРТ ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())