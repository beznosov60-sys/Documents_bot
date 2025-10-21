import logging
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import cv2
import numpy as np
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
    *,
    return_debug: bool = False,
) -> Tuple[PassportData | None, Dict[str, Any]]:
    """Run OCR recognition for the provided passport image using PaddleOCR."""

    reader = _get_ocr_reader()
    preprocessed, debug_path = _preprocess_image(image_path, save_debug=return_debug)
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

    result: Dict[str, Any] = {
        "ФИО": full_name,
        "Серия": series,
        "Номер": number,
        "Кем выдан": issued_by,
        "Дата": issued_date_str,
        "Код подразделения": division_code,
        "raw_text": text,
        "raw_lines": normalized_lines,
        "blocks": {
            "personal": {
                "full_name": full_name,
            },
            "document_numbers": {
                "series": series,
                "number": number,
            },
            "issue": {
                "issued_by": issued_by,
                "issued_date": issued_date_str,
                "division_code": division_code,
            },
        },
    }

    if debug_path is not None:
        result["debug_image"] = debug_path

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


def _preprocess_image(image_path: Path, save_debug: bool = False) -> Tuple["cv2.Mat", Path | None]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise PassportRecognitionError("Не удалось открыть изображение паспорта")

    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l_channel = clahe.apply(l_channel)
    enhanced = cv2.cvtColor(cv2.merge((l_channel, a_channel, b_channel)), cv2.COLOR_LAB2BGR)

    gray = cv2.cvtColor(enhanced, cv2.COLOR_BGR2GRAY)

    # подавление бликов и переотражений
    _, glare_mask = cv2.threshold(gray, 220, 255, cv2.THRESH_BINARY)
    if np.count_nonzero(glare_mask) > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        glare_mask = cv2.dilate(glare_mask, kernel, iterations=1)
        enhanced = cv2.inpaint(enhanced, glare_mask, 5, cv2.INPAINT_TELEA)
        gray = cv2.cvtColor(enhanced, cv2.COLOR_BGR2GRAY)
        logger.debug("Glare suppressed for %s", image_path)

    denoised = cv2.bilateralFilter(gray, 9, 75, 75)
    normalized = cv2.normalize(denoised, None, 0, 255, cv2.NORM_MINMAX)

    adaptive = cv2.adaptiveThreshold(
        normalized,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        10,
    )
    closed = cv2.morphologyEx(
        adaptive,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )

    preprocessed = cv2.cvtColor(closed, cv2.COLOR_GRAY2BGR)

    debug_path: Path | None = None
    if save_debug:
        debug_path = image_path.with_name(f"{image_path.stem}_preprocessed.png")
        cv2.imwrite(str(debug_path), preprocessed)

    return preprocessed, debug_path


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

    caps = re.findall(r"\b[А-ЯЁ]{3,}\b", text)
    if len(caps) >= 2:
        fam = caps[0].title()
        first = caps[1].title() if len(caps) > 1 else ""
        middle = caps[2].title() if len(caps) > 2 else ""
        return " ".join([x for x in [fam, first, middle] if x])

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
