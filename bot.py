import os
import time
import json
import sqlite3
import logging
import asyncio
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove,
    WebAppInfo
)
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))
WEBAPP_URL = os.getenv("WEBAPP_URL")
TELEGRAPH_RULES_URL = os.getenv("RULES_URL", "https://telegra.ph/Pravila-Parliament-01-01")
PORT = int(os.getenv("PORT", 10000))

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://excalibur-6ols.onrender.com")
WEBHOOK_PATH = f"/bot/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
router = Router()
bot_global = Bot(token=BOT_TOKEN)

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
        await message.answer("⛔ <b>Вы заблокированы в чате Parliament.</b>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    user_data = get_user_db(user.id)
    now = int(time.time())
    if now - user_data["last_link_time"] < COOLDOWN_SECONDS:
        rem_min = ((COOLDOWN_SECONDS - (now - user_data["last_link_time"])) // 60) + 1
        await message.answer(f"⏳ <b>Вы уже получили персональную ссылку.</b> Снова через {rem_min} мин.", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    # Шаг 1: Возраст
    await state.set_state(LobbyStates.step_age)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔞 Мне есть 18 лет", callback_data="age_ok"),
            InlineKeyboardButton(text="❌ Мне нет 18", callback_data="age_fail")
        ]
    ])
    await message.answer("🏛 <b>Добро пожаловать в лобби PARLIAMENT.</b>\n\nПодтвердите ваш возраст:", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите вариант:", reply_markup=kb)

@router.callback_query(LobbyStates.step_age, F.data.in_({"age_ok", "age_fail"}))
async def process_age(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "age_fail":
        await state.clear()
        await callback.message.edit_text("⛔ <b>Доступ закрыт. Вам нет 18 лет.</b>", parse_mode="HTML")
        return

    # Шаг 2: Правила
    await state.set_state(LobbyStates.step_rules)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Читать правила (Telegraph)", url=TELEGRAPH_RULES_URL)],
        [InlineKeyboardButton(text="✅ Я ознакомился и согласен", callback_data="rules_accepted")]
    ])
    await callback.message.edit_text("📜 <b>Свод правил PARLIAMENT</b>\n\nОзнакомьтесь с правилами по ссылке ниже:", parse_mode="HTML", reply_markup=kb)

@router.callback_query(LobbyStates.step_rules, F.data == "rules_accepted")
async def process_rules(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(LobbyStates.step_captcha)

    # Шаг 3: Капча
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧩 Пройти проверку (Капча)", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await callback.message.edit_text("✔️ <b>Возраст и правила подтверждены.</b> Нажмите кнопку ниже для прохождения капчи:", parse_mode="HTML", reply_markup=kb)

async def handle_verify_api(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    
    if request.method == "OPTIONS":
        return web.Response(status=200, headers=headers)

    try:
        data = await request.json()
        user_id = int(data.get("user_id"))
    except Exception:
        return web.json_response({"success": False, "error": "Неверные данные"}, status=400, headers=headers)

    if await check_is_banned(bot_global, user_id):
        return web.json_response({"success": False, "error": "Вы заблокированы"}, status=403, headers=headers)

    user_data = get_user_db(user_id)
    now = int(time.time())
    if now - user_data["last_link_time"] < COOLDOWN_SECONDS:
        return web.json_response({"success": False, "error": "Кулдаун не прошел"}, status=429, headers=headers)

    try:
        expire_timestamp = now + COOLDOWN_SECONDS
        invite = await bot_global.create_chat_invite_link(
            chat_id=CHAT_ID,
            name=f"User_{user_id}",
            expire_date=expire_timestamp,
            member_limit=1
        )
    except Exception as e:
        logging.error(f"Ошибка создания ссылки через API: {e}")
        return web.json_response({"success": False, "error": "Ошибка генерации ссылки Telegram"}, status=500, headers=headers)

    update_cooldown_db(user_id, now)

    try:
        await bot_global.send_message(
            chat_id=user_id,
            text=(
                "✅ <b>Проверка успешно пройдена! Доступ открыт.</b>\n\n"
                f"🔗 Ваша одноразовая ссылка:\n{invite.invite_link}\n\n"
                "<i>⚠️ Ссылка сгорит через 15 минут или после 1 входа.</i>"
            ),
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение юзеру {user_id}: {e}")

    return web.json_response({"success": True}, headers=headers)

async def handle_ping(request):
    return web.Response(text="Parliament Bot is running!")

# Фоновый пингер, чтобы Render не засыпал
async def keep_alive():
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(RENDER_EXTERNAL_URL) as resp:
                    pass
            except Exception:
                pass
            await asyncio.sleep(240) # Пингуем каждые 4 минуты

async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Вебхук успешно установлен на: {WEBHOOK_URL}")
    asyncio.create_task(keep_alive())

def main():
    init_db()
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    dp.startup.register(on_startup)

    app = web.Application()
    
    app.router.add_get("/", handle_ping)
    app.router.add_post("/api/verify", handle_verify_api)
    app.router.add_options("/api/verify", handle_verify_api)

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot_global,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot_global)

    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
