import logging
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import cv2
from natasha import DatesExtractor, NamesExtractor
from paddleocr import PaddleOCR
from dateutil import parser

from bot.models import PassportData


logger = logging.getLogger(__name__)


FIELD_ALIASES: Dict[str, Sequence[str]] = {
    "full_name": ("фио", "ф.и.о", "фамилия"),
    "series": ("серия",),
    "number": ("номер",),
    "issued_by": ("кем выдан", "кем выдано", "орган"),
    "issued_date": ("дата выдачи", "выдан", "выдана"),
}


class PassportRecognitionError(Exception):
    """Raised when OCR recognition of the passport fails."""


def parse_passport_text(raw_text: str) -> PassportData:
    """Parse manually entered passport data."""

    cleaned = raw_text.strip()
    if not cleaned:
        raise ValueError("Пустой текст")

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    data: Dict[str, str] = {}

    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
        elif "-" in line:
            key, value = line.split("-", 1)
        else:
            continue
        data[key.strip().lower()] = value.strip()

    mapped: Dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            for key, value in data.items():
                if alias in key:
                    mapped[field] = value
                    break
            if field in mapped:
                break

    missing = [field for field in FIELD_ALIASES if field not in mapped]
    if missing:
        raise ValueError(f"Не удалось определить поля: {', '.join(missing)}")

    series = re.sub(r"\D", "", mapped["series"])
    number = re.sub(r"\D", "", mapped["number"])
    issued_date = parser.parse(mapped["issued_date"], dayfirst=True).date()

    return PassportData(
        full_name=mapped["full_name"],
        series=series,
        number=number,
        issued_by=mapped["issued_by"],
        issued_date=issued_date,
    )


def recognize_passport_image(
    image_path: Path,
) -> Tuple[PassportData | None, Dict[str, str | None]]:
    """Run OCR recognition for the provided passport image."""

    reader = _get_ocr_reader()
    preprocessed = _preprocess_image(image_path)
    logger.debug("Running PaddleOCR for passport image %s", image_path)

    try:
        ocr_pages = reader.ocr(preprocessed, cls=True)
    except Exception as exc:  # pragma: no cover - third-party error
        raise PassportRecognitionError(
            "Не удалось обработать изображение паспорта"
        ) from exc

    lines = _extract_text_lines(ocr_pages)
    if not lines:
        raise PassportRecognitionError("Текст на изображении не найден")

    normalized_lines = [_normalize_text(line) for line in lines if _normalize_text(line)]
    text = "\n".join(normalized_lines)

    full_name = _extract_full_name(text)
    series = _extract_series(text)
    number = _extract_number(text)
    issued_by = _extract_issued_by(normalized_lines)
    issued_date_str, issued_date_obj = _extract_issued_date(text)
    division_code = _extract_division_code(text)

    result = {
        "ФИО": full_name,
        "Серия": series,
        "Номер": number,
        "Кем выдан": issued_by,
        "Дата": issued_date_str,
        "Код подразделения": division_code,
    }

    passport: PassportData | None = None
    if all([full_name, series, number, issued_by, issued_date_obj]):
        passport = PassportData(
            full_name=full_name,
            series=series,
            number=number,
            issued_by=issued_by,
            issued_date=issued_date_obj,
        )

    return passport, result


@lru_cache(maxsize=1)
def _get_ocr_reader() -> PaddleOCR:
    try:
        return PaddleOCR(lang="ru", use_angle_cls=True, show_log=False)
    except Exception as exc:  # pragma: no cover - optional dependency
        raise PassportRecognitionError(
            "Библиотека PaddleOCR не установлена. Добавьте paddleocr в зависимости."
        ) from exc


@lru_cache(maxsize=1)
def _get_names_extractor() -> NamesExtractor:
    return NamesExtractor()


@lru_cache(maxsize=1)
def _get_dates_extractor() -> DatesExtractor:
    return DatesExtractor()


def _preprocess_image(image_path: Path) -> "cv2.Mat":
    image = cv2.imread(str(image_path))
    if image is None:
        raise PassportRecognitionError("Не удалось открыть изображение паспорта")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, thresh = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    return cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR)


def _extract_text_lines(ocr_pages: Sequence[Sequence[Sequence[object]]]) -> List[str]:
    lines: List[str] = []
    for page in ocr_pages:
        for item in page:
            if not item or len(item) < 2:
                continue
            text_block = item[1]
            if isinstance(text_block, (list, tuple)) and text_block:
                text = text_block[0]
            else:
                text = text_block
            if text:
                lines.append(str(text))
    logger.debug("OCR lines: %s", lines)
    return lines


def _extract_full_name(text: str) -> str | None:
    extractor = _get_names_extractor()
    matches = extractor(text)
    for match in matches:
        fact = match.fact
        parts = [fact.last, fact.first, fact.middle]
        cleaned = [
            _normalize_name_word(part)
            for part in parts
            if isinstance(part, str) and part.strip()
        ]
        if cleaned:
            return " ".join(cleaned)
    return None


def _extract_series(text: str) -> str | None:
    pattern = re.compile(
        r"сер(?:ия|\.|\b)[\s:–—-]*([0-9]{2}\s?[0-9]{2})",
        flags=re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) == 4:
            return digits

    candidates = re.findall(r"\b\d{4}\b", text)
    for candidate in candidates:
        if len(candidate) == 4:
            return candidate
    return None


def _extract_number(text: str) -> str | None:
    pattern = re.compile(r"номер[\s:–—-]*([0-9]{6})", flags=re.IGNORECASE)
    match = pattern.search(text)
    if match:
        return re.sub(r"\D", "", match.group(1))

    candidates = re.findall(r"\b\d{6}\b", text)
    for candidate in candidates:
        if len(candidate) == 6:
            return candidate
    return None


def _extract_issued_date(text: str) -> Tuple[str | None, date | None]:
    extractor = _get_dates_extractor()
    matches = extractor(text)
    for match in matches:
        fact = match.fact
        year = _safe_int(getattr(fact, "year", None))
        month = _safe_int(getattr(fact, "month", None))
        day = _safe_int(getattr(fact, "day", None))
        if year and month and day:
            try:
                parsed = date(year, month, day)
            except ValueError:
                continue
            return parsed.strftime("%d.%m.%Y"), parsed

    fallback_pattern = re.compile(r"\b(\d{2}[.\s]\d{2}[.\s]\d{4})\b")
    for raw in fallback_pattern.findall(text):
        parsed = _parse_date_string(raw)
        if parsed:
            return parsed.strftime("%d.%m.%Y"), parsed

    return None, None


def _extract_division_code(text: str) -> str | None:
    pattern = re.compile(r"\b\d{3}\s*[-–—]\s*\d{3}\b")
    match = pattern.search(text.replace("\n", " "))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group())
    if len(digits) != 6:
        return None
    return f"{digits[:3]}-{digits[3:]}"


def _extract_issued_by(lines: List[str]) -> str | None:
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if "кем" in lowered and "выдан" in lowered:
            collected = [re.sub(r"(?i)кем\s+выдан[\s:]*", "", line).strip()]
            for tail in lines[idx + 1 : idx + 4]:
                if _looks_like_new_field(tail):
                    break
                cleaned = tail.strip()
                if cleaned:
                    collected.append(cleaned)
            cleaned_text = " ".join(collected).strip()
            if cleaned_text:
                return _normalize_title_phrase(cleaned_text)
    return None


def _looks_like_new_field(line: str) -> bool:
    stripped = line.strip()
    lowered = stripped.lower()
    keyword_triggers = (
        "серия",
        "номер",
        "дата",
        "код подразделения",
        "фамил",
        "имя",
        "отч",
        "место рождения",
        "выдан",
        "фио",
    )
    if any(keyword in lowered for keyword in keyword_triggers):
        return True

    if re.match(r"^паспорт(?:\b|[\s:,-])", lowered):
        return True

    if re.search(r"\d{2}[.\s]\d{2}[.\s]\d{4}", stripped):
        return True

    digits_only = re.sub(r"\D", "", stripped)
    if len(digits_only) >= 10:
        return True

    if re.fullmatch(r"\d{3}\s*[-–—]\s*\d{3}", stripped):
        return True

    return False


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_name_word(word: str) -> str:
    cleaned = word.strip()
    if len(cleaned) <= 2:
        return cleaned.upper()
    return cleaned.capitalize()


def _normalize_title_phrase(text: str) -> str:
    words = []
    for word in text.split():
        if word.isupper() and len(word) <= 3:
            words.append(word.upper())
        elif len(word) <= 2:
            words.append(word.upper())
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _parse_date_string(value: str) -> date | None:
    digits = re.findall(r"\d+", value)
    if len(digits) != 3:
        return None
    normalized = f"{int(digits[0]):02d}.{int(digits[1]):02d}.{int(digits[2]):04d}"
    try:
        return parser.parse(normalized, dayfirst=True).date()
    except (ValueError, parser.ParserError):
        return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "PassportRecognitionError",
    "parse_passport_text",
    "recognize_passport_image",
]

