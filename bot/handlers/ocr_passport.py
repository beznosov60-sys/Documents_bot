import html
from pathlib import Path
from typing import List

from aiogram import Router, types, F
from aiogram.types import PhotoSize, FSInputFile
from bot.services.ocr_engine import process_passport_image, preprocess_image

router = Router()
UPLOADS_DIR = Path("uploads"); UPLOADS_DIR.mkdir(exist_ok=True)

@router.message(F.document & (F.document.mime_type.contains("image") | F.document.mime_type == "application/pdf"))
async def handle_passport_document(message: types.Message) -> None:
    doc = message.document
    file = await message.bot.get_file(doc.file_id)
    ext = Path(doc.file_name or "scan").suffix or ".jpg"
    file_path = UPLOADS_DIR / f"{doc.file_unique_id}{ext}"
    await message.bot.download(file, destination=file_path)
    await _run_ocr_and_reply(message, file_path)

@router.message(F.photo)
async def handle_passport_photo(message: types.Message) -> None:
    assert message.photo is not None
    photos: List[PhotoSize] = message.photo
    photo = photos[-1]
    file = await message.bot.get_file(photo.file_id)
    file_path = UPLOADS_DIR / f"{photo.file_unique_id}.jpg"
    await message.bot.download(file, destination=file_path)
    await _run_ocr_and_reply(message, file_path)

async def _run_ocr_and_reply(message: types.Message, file_path: Path) -> None:
    await message.answer("🔍 Распознаю данные паспорта…")
    clean_path = preprocess_image(file_path)
    if clean_path.exists():
        await message.answer_photo(FSInputFile(str(clean_path)), caption="🧼 Предобработка для OCR")

    data = await process_passport_image(file_path)
    if not data:
        await message.answer("😕 Не удалось распознать. Отправь оригинал как *файл* (не фото) и ровный разворот.", parse_mode=None)
        return

    lines = ["✅ Результат распознавания:"]
    if data.get("fio"): lines.append(f"ФИО: {data['fio']}")
    if data.get("series") or data.get("number"):
        lines.append(f"Серия/номер: {data.get('series','?')} {data.get('number','?')}")
    if data.get("birth_date"): lines.append(f"Дата рождения: {data['birth_date']}")
    if data.get("issue_date"): lines.append(f"Дата выдачи: {data['issue_date']}")
    if data.get("department_code"): lines.append(f"Код подразделения: {data['department_code']}")

    # debug-хвост (укороченный)
    lines.append("\n— debug —")
    lines.append(f"dates: {data.get('dates')}")
    lines.append(f"variant: {data.get('variant')}")
    lines.append(f"raw_text:\n{(data.get('raw_text') or '')[:700]}")

    await message.answer("\n".join(lines), parse_mode=None)
