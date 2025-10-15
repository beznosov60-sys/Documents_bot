from __future__ import annotations

from datetime import date
from babel.dates import format_date
from num2words import num2words


def format_russian_date(value: date) -> str:
    return format_date(value, format="d MMMM y 'г.'", locale="ru")


def amount_to_words(amount: int) -> str:
    words = num2words(amount, lang="ru")
    return words.capitalize()
