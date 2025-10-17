from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dateutil import parser

from bot.config import Config
from bot.models import ContractContext, PassportData
from bot.states import ContractStates
from bot.utils.counter import build_contract_number, load_and_increment
from bot.utils.documents import generate_documents
from bot.utils.formatting import format_russian_date
from bot.utils.passport import (
    PassportRecognitionError,
    parse_passport_text,
    recognize_passport_image,
)
from bot.utils.registry import get_last_contract, update_user_contract
from bot.utils.schedule import build_payment_schedule

logger = logging.getLogger(__name__)

router = Router()


def get_config(event) -> Config:
    return event.bot._config  # type: ignore[attr-defined]


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Здравствуйте! Пришлите фотографию или скан паспорта — я постараюсь распознать данные автоматически.\n"
        "Если хотите ввести данные вручную, отправьте команду /manual."
    )
    await state.set_state(ContractStates.waiting_for_passport)


@router.message(Command("manual"))
async def cmd_manual(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state != ContractStates.waiting_for_manual_data.state:
        await state.set_state(ContractStates.waiting_for_manual_data)
    await message.answer(
        "Введите паспортные данные в формате:\n"
        "ФИО: Иванов Иван Иванович\n"
        "Серия: 1234\n"
        "Номер: 567890\n"
        "Кем выдан: ОВД района\n"
        "Дата выдачи: 01.01.2020"
    )


async def present_passport_summary(message: Message, state: FSMContext, passport: PassportData) -> None:
    await state.update_data(passport=passport)

    summary_lines = [
        "Проверьте распознанные данные паспорта:",
        f"ФИО: {passport.full_name}",
        f"Серия и номер: {passport.series} {passport.number}",
        f"Кем выдан: {passport.issued_by}",
        f"Дата выдачи: {format_russian_date(passport.issued_date)}",
    ]

    builder = InlineKeyboardBuilder()
    builder.button(text="Всё верно", callback_data="confirm_passport")
    builder.button(text="Изменить", callback_data="reject_passport")
    builder.adjust(2)

    await message.answer("\n".join(summary_lines), reply_markup=builder.as_markup())
    await state.set_state(ContractStates.passport_confirmation)


@router.message(ContractStates.waiting_for_passport, F.photo)
async def handle_passport_photo(message: Message, state: FSMContext) -> None:
    config = get_config(message)
    photo = message.photo[-1]
    passport_dir = config.passports_dir
    passport_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"passport_{message.from_user.id}_{photo.file_unique_id}.jpg"
    destination = passport_dir / file_name
    await message.bot.download(photo.file_id, destination)
    await state.update_data(passport_photo=str(destination))

    await message.answer("Фото получено. Распознаю данные паспорта, пожалуйста, подождите…")

    loop = asyncio.get_running_loop()
    try:
        passport = await loop.run_in_executor(None, recognize_passport_image, destination)
    except PassportRecognitionError as exc:
        logger.warning("Passport OCR failed: %s", exc)
        await state.set_state(ContractStates.waiting_for_manual_data)
        await message.answer(
            "Не удалось распознать паспорт автоматически. Введите данные вручную по образцу или повторите фото."
        )
        return
    except Exception as exc:  # pragma: no cover - unexpected errors
        logger.exception("Unexpected error while processing passport", exc_info=exc)
        await state.set_state(ContractStates.waiting_for_manual_data)
        await message.answer(
            "Произошла ошибка при распознавании паспорта. Попробуйте снова или введите данные вручную."
        )
        return

    passport.photo_path = destination
    await present_passport_summary(message, state, passport)


@router.message(ContractStates.waiting_for_passport)
async def prompt_for_passport(message: Message) -> None:
    await message.answer(
        "Пожалуйста, отправьте фото паспорта или воспользуйтесь командой /manual для ручного ввода."
    )


@router.message(ContractStates.waiting_for_manual_data)
async def handle_manual_passport(message: Message, state: FSMContext) -> None:
    try:
        passport = parse_passport_text(message.text or "")
    except ValueError as exc:
        await message.answer(f"Не удалось прочитать данные: {exc}\nПопробуйте ещё раз по образцу.")
        return

    data = await state.get_data()
    photo_path = data.get("passport_photo")
    if photo_path:
        passport.photo_path = Path(photo_path)

    await present_passport_summary(message, state, passport)


@router.callback_query(ContractStates.passport_confirmation, F.data == "reject_passport")
async def reject_passport(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_reply_markup()
    data = await state.get_data()
    data.pop("passport", None)
    await state.set_data(data)
    await state.set_state(ContractStates.waiting_for_manual_data)
    await callback.message.answer(
        "Хорошо, отправьте паспортные данные ещё раз в формате:\n"
        "ФИО: Иванов Иван Иванович\n"
        "Серия: 1234\n"
        "Номер: 567890\n"
        "Кем выдан: ОВД района\n"
        "Дата выдачи: 01.01.2020"
    )
    await callback.answer()


@router.callback_query(ContractStates.passport_confirmation, F.data == "confirm_passport")
async def confirm_passport(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_reply_markup()
    await state.set_state(ContractStates.waiting_for_amount)
    await callback.message.answer("Данные паспорта подтверждены. Введите сумму оплаты (например, 132000).")
    await callback.answer()


def _clean_amount(text: str) -> Optional[int]:
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return int(digits)


@router.message(ContractStates.waiting_for_amount)
async def handle_amount(message: Message, state: FSMContext) -> None:
    amount = _clean_amount(message.text or "")
    if amount is None or amount <= 0:
        await message.answer("Сумма должна быть положительным числом. Попробуйте ещё раз.")
        return

    await state.update_data(total_amount=amount)
    await state.set_state(ContractStates.waiting_for_first_payment)
    await message.answer("Введите дату первого платежа (например, 25.03.2024).")


@router.message(ContractStates.waiting_for_first_payment)
async def handle_first_payment(message: Message, state: FSMContext) -> None:
    try:
        first_payment_date = parser.parse(message.text or "", dayfirst=True).date()
    except (ValueError, parser.ParserError):
        await message.answer("Не удалось распознать дату. Укажите её в формате ДД.ММ.ГГГГ.")
        return

    data = await state.get_data()
    passport: PassportData = data["passport"]
    total_amount: int = data["total_amount"]
    await state.update_data(first_payment_date=first_payment_date)

    counter = load_and_increment(get_config(message).counter_file)
    counter_str, initials = build_contract_number(counter, passport.full_name)
    contract_number = f"{counter_str}-{initials}" if initials else counter_str

    payments = build_payment_schedule(first_payment_date, total_amount)

    await state.update_data(
        payments=payments,
        contract_number=contract_number,
    )

    summary_lines = [
        "Проверьте данные договора:",
        f"ФИО: {passport.full_name}",
        f"Паспорт: серия {passport.series}, номер {passport.number}",
        f"Кем выдан: {passport.issued_by} {format_russian_date(passport.issued_date)}",
        f"Сумма договора: {total_amount:,.0f} ₽".replace(",", " "),
        f"Дата первого платежа: {format_russian_date(first_payment_date)}",
        f"Номер договора: {contract_number}",
    ]

    builder = InlineKeyboardBuilder()
    builder.button(text="Подтвердить", callback_data="confirm_contract")
    builder.button(text="Изменить", callback_data="cancel_contract")
    builder.adjust(2)

    await message.answer("\n".join(summary_lines), reply_markup=builder.as_markup())
    await state.set_state(ContractStates.confirmation)


@router.callback_query(ContractStates.confirmation, F.data == "cancel_contract")
async def cancel_contract(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.answer("Данные не подтверждены. Начнём заново. Отправьте паспорт или команду /manual.")
    await state.clear()
    await state.set_state(ContractStates.waiting_for_passport)
    await callback.answer()


@router.callback_query(ContractStates.confirmation, F.data == "confirm_contract")
async def confirm_contract(callback: CallbackQuery, state: FSMContext) -> None:
    config = get_config(callback)
    data = await state.get_data()
    passport: PassportData = data["passport"]
    total_amount: int = data["total_amount"]
    first_payment_date: date = data["first_payment_date"]
    payments = data["payments"]
    contract_number: str = data["contract_number"]

    context = ContractContext(
        passport=passport,
        total_amount=total_amount,
        first_payment_date=first_payment_date,
        contract_number=contract_number,
        payments=payments,
    )

    client_dir = config.contracts_root / passport.full_name.replace(" ", "_")
    await generate_documents(context, client_dir)

    update_user_contract(
        config.registry_file,
        callback.from_user.id,
        {
            "client": passport.full_name,
            "contract_number": contract_number,
            "docx_path": str(context.docx_path),
            "pdf_path": str(context.pdf_path),
        },
    )

    await callback.message.answer("✅ Договор готов. Отправляю DOCX и PDF.")

    try:
        await callback.message.answer_document(FSInputFile(context.docx_path))
        await callback.message.answer_document(FSInputFile(context.pdf_path))
    except TelegramBadRequest as exc:
        logger.exception("Failed to send documents: %s", exc)
        await callback.message.answer("Не удалось отправить файлы. Пожалуйста, попробуйте позже.")

    await state.clear()
    await callback.answer()


@router.message(Command("get_contract"))
async def get_last_contract_command(message: Message) -> None:
    config = get_config(message)
    record = get_last_contract(config.registry_file, message.from_user.id)
    if not record:
        await message.answer("Для вас пока нет сгенерированных договоров.")
        return

    docx_path = Path(record["docx_path"])
    pdf_path = Path(record["pdf_path"])
    if not docx_path.exists() or not pdf_path.exists():
        await message.answer("Файлы договора не найдены на сервере. Создайте новый договор.")
        return

    await message.answer("Отправляю последний договор.")
    await message.answer_document(FSInputFile(docx_path))
    await message.answer_document(FSInputFile(pdf_path))
