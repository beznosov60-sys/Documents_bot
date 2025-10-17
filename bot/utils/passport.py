from __future__ import annotations

import logging
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List

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


def recognize_passport_image(image_path: Path) -> tuple[PassportData | None, Dict[str, Any]]:
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

    result = _build_passport_from_lines(lines)

    passport: PassportData | None = None
    values = result.get("values", {})
    if all(key in values for key in ("full_name", "series", "number", "issued_by", "issued_date")):
        passport = PassportData(
            full_name=values["full_name"],
            series=values["series"],
            number=values["number"],
            issued_by=values["issued_by"],
            issued_date=values["issued_date"],
        )

    if not result.get("recognized_fields"):
        raise PassportRecognitionError(
            "Не удалось распознать ни одного поля паспорта"
        )

    return passport, result


@lru_cache(maxsize=1)
def _get_ocr_reader() -> "easyocr.Reader":
    try:
        import easyocr
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise PassportRecognitionError(
            "Библиотека easyocr не установлена. Добавьте easyocr в зависимости."
        ) from exc

    return easyocr.Reader(["ru", "en"], gpu=False)


def _build_passport_from_lines(lines: List[str]) -> Dict[str, Any]:
    text = "\n".join(lines)

    result: Dict[str, Any] = {
        "values": {},
        "recognized_fields": [],
        "missing_fields": [],
        "warnings": [],
    }

    values: Dict[str, Any] = result["values"]

    full_name, missing_name_parts = _extract_full_name(lines, text)
    if full_name:
        values["full_name"] = full_name
        result["recognized_fields"].append("ФИО")
        if missing_name_parts:
            result["warnings"].append(
                "ФИО распознано не полностью: отсутствует "
                + ", ".join(missing_name_parts)
            )
    else:
        result["missing_fields"].append("ФИО")

    series, number = _extract_series_number(text)
    if series:
        values["series"] = series
        result["recognized_fields"].append("серия")
    else:
        result["missing_fields"].append("серия")

    if number:
        values["number"] = number
        result["recognized_fields"].append("номер")
    else:
        result["missing_fields"].append("номер")

    issued_by = _extract_issued_by(lines)
    if issued_by:
        values["issued_by"] = issued_by
        result["recognized_fields"].append("кем выдан")
    else:
        result["missing_fields"].append("кем выдан")

    issued_date = _extract_issued_date(text)
    if issued_date:
        values["issued_date"] = issued_date
        result["recognized_fields"].append("дата выдачи")
    else:
        result["missing_fields"].append("дата выдачи")

    division_code = _extract_division_code(text)
    if division_code:
        values["division_code"] = division_code
        result["recognized_fields"].append("код подразделения")

    required_fields = {"ФИО", "серия", "номер", "кем выдан", "дата выдачи"}
    result["missing_fields"] = [
        field for field in result["missing_fields"] if field in required_fields
    ]

    result["recognized_fields"] = list(dict.fromkeys(result["recognized_fields"]))
    result["missing_fields"] = list(dict.fromkeys(result["missing_fields"]))

    return result


def _extract_full_name(lines: List[str], text: str) -> tuple[str | None, List[str]]:
    last = _extract_after_keyword(lines, ("фамил",))
    first = _extract_after_keyword(lines, ("имя",))
    middle = _extract_after_keyword(lines, ("отч",))

    name_parts: List[str] = []
    if last:
        tokens = _split_name_tokens(last)
        if tokens:
            name_parts.append(_normalize_name_word(tokens[0]))
    if first:
        tokens = _split_name_tokens(first)
        if tokens:
            name_parts.append(_normalize_name_word(tokens[0]))
            if not middle and len(tokens) > 1:
                name_parts.append(_normalize_name_word(tokens[1]))
    if middle:
        tokens = _split_name_tokens(middle)
        if tokens:
            name_parts.append(_normalize_name_word(tokens[0]))

    if len(name_parts) >= 2:
        if len(name_parts) >= 3:
            return " ".join(name_parts[:3]), []
        return " ".join(name_parts[:2]), ["отчество"]

    normalized_text = " ".join(line.strip() for line in lines if line.strip())
    name_pattern = re.compile(r"[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+")
    matches = name_pattern.findall(normalized_text)
    for candidate in matches:
        lowered = candidate.lower()
        if any(stop in lowered for stop in ("российск", "федерац", "паспорт")):
            continue
        words = [
            _normalize_name_word(word)
            for word in candidate.split()
            if _is_name_word(word)
        ]
        if len(words) == 3:
            return " ".join(words), []

    sequential = _collect_name_sequence(lines)
    if sequential:
        if len(sequential) == 3:
            return " ".join(_normalize_name_word(word) for word in sequential), []
        if len(sequential) == 2:
            return (
                " ".join(_normalize_name_word(word) for word in sequential),
                ["отчество"],
            )

    return None, []


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


def _extract_division_code(text: str) -> str | None:
    pattern = re.compile(r"\b\d{3}\s*[-–—]\s*\d{3}\b")
    match = pattern.search(text.replace("\n", " "))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group())
    if len(digits) != 6:
        return None
    return f"{digits[:3]}-{digits[3:]}"


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


def _is_name_word(word: str) -> bool:
    return bool(re.fullmatch(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?", word))


def _split_name_tokens(value: str) -> List[str]:
    tokens: List[str] = []
    for word in value.split():
        if not _is_name_word(word):
            continue
        lowered = word.lower()
        if any(stop in lowered for stop in ("россий", "федерац", "паспорт", "мвд")):
            continue
        tokens.append(word)
    return tokens


def _collect_name_sequence(lines: List[str]) -> List[str]:
    best: List[str] = []
    current: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or _contains_digits(stripped):
            current = []
            continue
        tokens = _split_name_tokens(stripped)
        if not tokens:
            current = []
            continue
        if len(tokens) >= 3:
            return tokens[:3]
        if len(tokens) == 2:
            current = tokens[:]
        else:
            current.append(tokens[0])
        if len(current) > 3:
            current = current[-3:]
        if len(current) > len(best):
            best = current[:]
        if len(best) == 3:
            break
    return best
