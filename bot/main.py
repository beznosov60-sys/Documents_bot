import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))


import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from dotenv import load_dotenv

from bot.handlers import start, ocr_passport

load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN_BOT")
if not TOKEN:
    raise ValueError("⚠️ Переменная TOKEN_BOT не найдена в .env")

# Новая схема (aiogram 3.7+)
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

# Регистрируем хэндлеры
dp.include_router(start.router)
dp.include_router(ocr_passport.router)

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск бота")
    ])

async def main():
    await set_commands()
    logging.info("🤖 Bot started.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
