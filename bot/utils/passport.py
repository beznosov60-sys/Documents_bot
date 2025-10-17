from __future__ import annotations

import logging
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List

from dateutil import parser

from bot.models import PassportData

logger = logging.getLogger(__name__)


FIELD_ALIASES = {
    "full_name": ["фио", "ф.и.о", "фамилия"],
    "series": ["серия"],
    "number": ["номер"],
    "issued_by": ["кем выдан", "кем выдано", "орган"],
    "issued_date": ["дата выдачи", "выдан", "выдана"],
}


class PassportRecognitionError(Exception):
    """Raised when OCR recognition of the passport fails."""


def parse_passport_text(raw_text: str) -> PassportData:
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


def recognize_passport_image(image_path: Path) -> PassportData:
    reader = _get_ocr_reader()
    logger.debug("Running OCR for passport image %s", image_path)

    try:
        raw_lines = reader.readtext(str(image_path), detail=0, paragraph=False)
    except Exception as exc:  # pragma: no cover - third-party error
        raise PassportRecognitionError("Не удалось обработать изображение паспорта") from exc

    lines = [_normalize_text(line) for line in raw_lines if _normalize_text(line)]
    if not lines:
        raise PassportRecognitionError("Текст на изображении не найден")

    logger.debug("OCR lines: %s", lines)

    return _build_passport_from_lines(lines)


@lru_cache(maxsize=1)
def _get_ocr_reader() -> "easyocr.Reader":
    try:
        import easyocr
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise PassportRecognitionError(
            "Библиотека easyocr не установлена. Добавьте easyocr в зависимости."
        ) from exc

    return easyocr.Reader(["ru", "en"], gpu=False)


def _build_passport_from_lines(lines: List[str]) -> PassportData:
    text = "\n".join(lines)

    full_name = _extract_full_name(lines, text)
    series, number = _extract_series_number(text)
    issued_date = _extract_issued_date(text)
    issued_by = _extract_issued_by(lines)

    missing_fields = []
    if not full_name:
        missing_fields.append("ФИО")
    if not series or not number:
        missing_fields.append("серия и номер")
    if not issued_by:
        missing_fields.append("кем выдан")
    if not issued_date:
        missing_fields.append("дата выдачи")

    if missing_fields:
        raise PassportRecognitionError(
            "Не удалось распознать поля: " + ", ".join(missing_fields)
        )

    return PassportData(
        full_name=full_name,
        series=series,
        number=number,
        issued_by=issued_by,
        issued_date=issued_date,
    )


def _extract_full_name(lines: List[str], text: str) -> str | None:
    last = _extract_after_keyword(lines, ("фамил",))
    first = _extract_after_keyword(lines, ("имя",))
    middle = _extract_after_keyword(lines, ("отч",))

    if last and first:
        parts = [_normalize_name_word(last), _normalize_name_word(first)]
        if middle:
            parts.append(_normalize_name_word(middle))
        return " ".join(parts)

    name_pattern = re.compile(r"\b[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}\b")
    for match in name_pattern.finditer(text):
        candidate = match.group().strip()
        if any(stop in candidate.lower() for stop in ("российск", "федерац", "паспорт")):
            continue
        words = candidate.split()
        if 2 <= len(words) <= 3:
            return " ".join(_normalize_name_word(word) for word in words)
    return None


def _extract_series_number(text: str) -> tuple[str | None, str | None]:
    series_number_pattern = re.compile(r"(\d{2}\s?\d{2})\D*(\d{6})")
    match = series_number_pattern.search(text.replace("\n", " "))
    if not match:
        return None, None
    series = re.sub(r"\D", "", match.group(1))
    number = re.sub(r"\D", "", match.group(2))
    if len(series) == 4 and len(number) == 6:
        return series, number
    return None, None


def _extract_issued_date(text: str) -> date | None:
    date_pattern = re.compile(r"(\d{2}[.\s]\d{2}[.\s]\d{4})")
    keywords = ("дата выдачи", "выдан", "выдано", "выдана")

    for keyword in keywords:
        keyword_pattern = re.compile(
            rf"{re.escape(keyword)}[^0-9]{{0,40}}(\d{{2}}[.\s]\d{{2}}[.\s]\d{{4}})",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = keyword_pattern.search(text)
        if match:
            parsed = _parse_date_string(match.group(1))
            if parsed:
                return parsed

    for keyword in keywords:
        keyword_pattern = re.compile(
            rf"(\d{{2}}[.\s]\d{{2}}[.\s]\d{{4}})[^0-9]{{0,40}}{re.escape(keyword)}",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = keyword_pattern.search(text)
        if match:
            parsed = _parse_date_string(match.group(1))
            if parsed:
                return parsed

    lines = text.splitlines()
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if any(keyword in lowered for keyword in keywords):
            neighbourhood = [line]
            if idx + 1 < len(lines):
                neighbourhood.append(lines[idx + 1])
            if idx > 0:
                neighbourhood.append(lines[idx - 1])
            combined = " ".join(neighbourhood)
            match = date_pattern.search(combined)
            if match:
                parsed = _parse_date_string(match.group(1))
                if parsed:
                    return parsed

    matches: list[date] = []
    for raw in date_pattern.findall(text):
        parsed = _parse_date_string(raw)
        if parsed:
            matches.append(parsed)
    unique_matches = set(matches)
    if len(unique_matches) == 1:
        return matches[0]
    return None


def _parse_date_string(value: str) -> date | None:
    digits = re.findall(r"\d+", value)
    if len(digits) != 3:
        return None
    normalized = f"{int(digits[0]):02d}.{int(digits[1]):02d}.{int(digits[2]):04d}"
    try:
        return parser.parse(normalized, dayfirst=True).date()
    except (ValueError, parser.ParserError):
        return None


def _extract_issued_by(lines: List[str]) -> str | None:
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if any(keyword in lowered for keyword in ("выдан", "овд", "уфмс", "мвд", "отдел")):
            collected = [line]
            for tail in lines[idx + 1 : idx + 4]:
                if _looks_like_new_field(tail):
                    break
                collected.append(tail)
            cleaned = " ".join(collected)
            cleaned = re.sub(r"(?i)кем\s+выдан[\s:]*", "", cleaned)
            cleaned = cleaned.strip()
            if cleaned:
                return _normalize_title_phrase(cleaned)
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
        "пол",
        "место рождения",
        "выдан",
        "паспорт",
        "фио",
    )
    if any(keyword in lowered for keyword in keyword_triggers):
        return True

    if re.search(r"\d{2}[.\s]\d{2}[.\s]\d{4}", stripped):
        return True

    digits_only = re.sub(r"\D", "", stripped)
    if len(digits_only) >= 10:
        return True

    if digits_only and len(digits_only) == 6:
        if re.fullmatch(r"[\d\s\-–—]+", stripped):
            return True

    if 5 <= len(stripped) <= 8 and digits_only:
        if re.fullmatch(r"[\d\s\-–—]+", stripped) and 4 <= len(digits_only) <= 6:
            return True

    return False


def _extract_after_keyword(lines: List[str], keywords: Iterable[str]) -> str | None:
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if any(keyword in lowered for keyword in keywords):
            for tail in lines[idx + 1 : idx + 3]:
                if any(keyword in tail.lower() for keyword in keywords):
                    continue
                if _contains_digits(tail):
                    continue
                return tail.strip()
    return None


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


def _contains_digits(text: str) -> bool:
    return any(char.isdigit() for char in text)
