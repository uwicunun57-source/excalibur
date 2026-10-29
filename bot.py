import os
import time
import json
import sqlite3
import asyncio
import logging
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))
WEBAPP_URL = os.getenv("WEBAPP_URL")
TELEGRAPH_RULES_URL = os.getenv("RULES_URL", "https://telegra.ph/Pravila-Parliament-01-01")
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
router = Router()

COOLDOWN_SECONDS = 15 * 60

def init_db():
    with sqlite3.connect("parliament.db") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_banned INTEGER DEFAULT 0,
                last_link_time INTEGER DEFAULT 0,
                first_seen INTEGER
            )
        """)
        conn.commit()

def log_user_to_file(user_id: int, username: str, full_name: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    log_line = f"[{timestamp}] ID: {user_id} | Username: @{username or 'нет'} | Name: {full_name}\n"
    existing_ids = set()
    if os.path.exists("users_log.txt"):
        with open("users_log.txt", "r", encoding="utf-8") as f:
            existing_ids = {line.split("ID: ")[1].split(" |")[0] for line in f if "ID: " in line}
    if str(user_id) not in existing_ids:
        with open("users_log.txt", "a", encoding="utf-8") as f:
            f.write(log_line)

def upsert_user_db(user_id: int, username: str, full_name: str):
    with sqlite3.connect("parliament.db") as conn:
        now = int(time.time())
        conn.execute("""
            INSERT INTO users (user_id, username, full_name, is_banned, last_link_time, first_seen)
            VALUES (?, ?, ?, 0, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
        """, (user_id, username, full_name, now))
        conn.commit()
    log_user_to_file(user_id, username, full_name)

def get_user_db(user_id: int):
    with sqlite3.connect("parliament.db") as conn:
        cur = conn.execute("SELECT is_banned, last_link_time FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return {"is_banned": False, "last_link_time": 0}
        return {"is_banned": bool(row[0]), "last_link_time": row[1]}

def update_cooldown_db(user_id: int, timestamp: int):
    with sqlite3.connect("parliament.db") as conn:
        conn.execute("UPDATE users SET last_link_time = ? WHERE user_id = ?", (timestamp, user_id))
        conn.commit()

async def check_is_banned(bot: Bot, user_id: int) -> bool:
    user_data = get_user_db(user_id)
    if user_data["is_banned"]:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHAT_ID, user_id=user_id)
        if member.status == "kicked":
            return True
    except TelegramBadRequest:
        pass
    return False

class LobbyStates(StatesGroup):
    step_age = State()
    step_rules = State()
    step_captcha = State()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    user = message.from_user
    upsert_user_db(user.id, user.username, user.full_name)

    if await check_is_banned(bot, user.id):
        await message.answer("⛔ <b>Вы заблокированы в чате Parliament.</b>", parse_mode="HTML")
        await state.clear()
        return

    user_data = get_user_db(user.id)
    now = int(time.time())
    if now - user_data["last_link_time"] < COOLDOWN_SECONDS:
        rem_min = ((COOLDOWN_SECONDS - (now - user_data["last_link_time"])) // 60) + 1
        await message.answer(f"⏳ <b>Вы уже получили персональную ссылку.</b> Снова через {rem_min} мин.", parse_mode="HTML")
        await state.clear()
        return

    await state.set_state(LobbyStates.step_age)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔞 Мне есть 18 лет", callback_data="age_ok"),
            InlineKeyboardButton(text="❌ Мне нет 18", callback_data="age_fail")
        ]
    ])
    await message.answer("🏛 <b>Добро пожаловать в лобби PARLIAMENT.</b>\n\nПодтвердите ваш возраст:", parse_mode="HTML", reply_markup=kb)

@router.callback_query(LobbyStates.step_age, F.data.in_({"age_ok", "age_fail"}))
async def process_age(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "age_fail":
        await state.clear()
        await callback.message.edit_text("⛔ <b>Доступ закрыт.</b>", parse_mode="HTML")
        return

    await state.set_state(LobbyStates.step_rules)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Читать правила (Telegraph)", url=TELEGRAPH_RULES_URL)],
        [InlineKeyboardButton(text="✅ Я ознакомился и согласен", callback_data="rules_accepted")]
    ])
    await callback.message.edit_text("📜 <b>Свод правил PARLIAMENT</b>", parse_mode="HTML", reply_markup=kb)

@router.callback_query(LobbyStates.step_rules, F.data == "rules_accepted")
async def process_rules(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(LobbyStates.step_captcha)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧩 Пройти проверку (Капча)", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await callback.message.edit_text("✔️ <b>Возраст и правила подтверждены.</b> Нажмите кнопку ниже:", parse_mode="HTML", reply_markup=kb)

# --- ЛОВИМ ДАННЫЕ ИЗ WEBAPP БЕЗ СТРОГОЙ ПРОВЕРКИ СОСТОЯНИЯ (НАДЕЖНЕЕ) ---
@router.message(F.web_app_data)
async def process_captcha(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    logging.info(f"Получены данные из WebApp от юзера {user_id}: {message.web_app_data.data}")

    try:
        payload = json.loads(message.web_app_data.data)
    except Exception as e:
        logging.error(f"Ошибка парсинга JSON от WebApp: {e}")
        await message.answer("⚠️ Ошибка чтения данных капчи.")
        return

    if payload.get("status") != "verified":
        await message.answer("❌ Проверка не пройдена.")
        await state.clear()
        return

    if await check_is_banned(bot, user_id):
        await message.answer("⛔ <b>Вы заблокированы.</b>", parse_mode="HTML")
        await state.clear()
        return

    user_data = get_user_db(user_id)
    now = int(time.time())
    if now - user_data["last_link_time"] < COOLDOWN_SECONDS:
        await message.answer("⏳ Кулдаун еще не прошел.")
        await state.clear()
        return

    try:
        expire_timestamp = now + COOLDOWN_SECONDS
        invite = await bot.create_chat_invite_link(
            chat_id=CHAT_ID,
            name=f"User_{user_id}",
            expire_date=expire_timestamp,
            member_limit=1
        )
    except Exception as e:
        logging.error(f"Ошибка создания ссылки: {e}")
        await message.answer("⚠️ Ошибка создания ссылки со стороны Telegram (проверьте права админа у бота в чате).")
        return

    update_cooldown_db(user_id, now)
    await state.clear()

    await message.answer(
        "✅ <b>Проверка пройдена! Доступ открыт.</b>\n\n"
        f"🔗 Ваша одноразовая ссылка:\n{invite.invite_link}\n\n"
        "<i>⚠️ Ссылка сгорит через 15 минут или после 1 входа.</i>",
        parse_mode="HTML"
    )

async def handle_ping(request):
    return web.Response(text="Parliament Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await web_server()
    logging.info("Бот PARLIAMENT запущен через Polling!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
