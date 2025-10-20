from aiogram import Router, types
from aiogram.filters import Command

router = Router()

@router.message(Command("start"))
async def start_cmd(message: types.Message):
    text = (
        "👋 Привет! Я бот для распознавания данных паспорта.\n\n"
        "📸 Отправь фото или скан паспорта — я попробую извлечь данные."
    )
    await message.answer(text)
