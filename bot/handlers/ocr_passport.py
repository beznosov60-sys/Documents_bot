import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import List

from aiogram import F, Router, types
from aiogram.types import FSInputFile, PhotoSize

from bot.utils.passport import (
    PassportRecognitionError,
    recognize_passport_image,
)


logger = logging.getLogger(__name__)

router = Router()
UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)


@router.message(
    F.document
    & (F.document.mime_type.contains("image") | (F.document.mime_type == "application/pdf"))
)
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

    loop = asyncio.get_running_loop()
    try:
        recognize = partial(recognize_passport_image, file_path, return_debug=True)
        passport, data = await loop.run_in_executor(None, recognize)
    except PassportRecognitionError as exc:
        logger.warning("Passport OCR failed: %s", exc)
        await message.answer(
            "😕 Не удалось распознать паспорт. Убедитесь, что фото чёткое, и попробуйте снова."
        )
        return
    except Exception as exc:  # pragma: no cover - unexpected errors
        logger.exception("Unexpected error while processing passport", exc_info=exc)
        await message.answer(
            "⚠️ Произошла непредвиденная ошибка во время распознавания. Попробуйте повторить попытку."
        )
        return

    if passport:
        logger.info("Passport OCR succeeded for %s", file_path)
    else:
        logger.info("Passport OCR returned partial data for %s", file_path)

    debug_path = data.pop("debug_image", None)
    if isinstance(debug_path, Path) and debug_path.exists():
        await message.answer_photo(
            FSInputFile(str(debug_path)), caption="🧼 Изображение после предобработки"
        )

    blocks = data.get("blocks", {})
    personal = blocks.get("personal", {})
    numbers = blocks.get("document_numbers", {})
    issue = blocks.get("issue", {})

    lines = ["✅ Результат распознавания паспорта:"]

    full_name = personal.get("full_name")
    if full_name:
        lines.append(f"• ФИО: {full_name}")

    series = numbers.get("series")
    number = numbers.get("number")
    if series or number:
        if series and len(series) == 4:
            series = f"{series[:2]} {series[2:]}"
        lines.append(f"• Серия и номер: {series or '—'} {number or '—'}")

    issued_by = issue.get("issued_by")
    if issued_by:
        lines.append(f"• Кем выдан: {issued_by}")

    issued_date = issue.get("issued_date")
    if issued_date:
        lines.append(f"• Дата выдачи: {issued_date}")

    division_code = issue.get("division_code")
    if division_code:
        lines.append(f"• Код подразделения: {division_code}")

    missing = [
        label
        for label, value in (
            ("ФИО", full_name),
            ("Серия", series),
            ("Номер", number),
            ("Кем выдан", issued_by),
            ("Дата", issued_date),
        )
        if not value
    ]
    if missing:
        lines.append(
            "⚠️ Распознан частично. Проверьте данные вручную: " + ", ".join(missing)
        )

    raw_preview = data.get("raw_text")
    if raw_preview:
        lines.append("\n📝 Распознанный текст:")
        lines.append("```")
        lines.append((raw_preview[:700]).strip())
        lines.append("```")

    await message.answer("\n".join(lines), parse_mode="Markdown")
