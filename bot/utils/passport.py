from __future__ import annotations

import re
from datetime import date
from typing import Dict

from dateutil import parser

from bot.models import PassportData


FIELD_ALIASES = {
    "full_name": ["фио", "ф.и.о", "фамилия"],
    "series": ["серия"],
    "number": ["номер"],
    "issued_by": ["кем выдан", "кем выдано", "орган"],
    "issued_date": ["дата выдачи", "выдан", "выдана"],
}


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
