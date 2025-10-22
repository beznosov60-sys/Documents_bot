"""Data models used across the bot package."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(slots=True)
class PassportData:
    """Structured representation of recognised passport information."""

    full_name: str
    series: str
    number: str
    issued_by: str
    issued_date: date


__all__ = ["PassportData"]

