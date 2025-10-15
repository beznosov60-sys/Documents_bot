from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List


@dataclass
class PassportData:
    full_name: str
    series: str
    number: str
    issued_by: str
    issued_date: date
    photo_path: Path | None = None


@dataclass
class Payment:
    month_index: int
    due_date: date
    amount: int


@dataclass
class ContractContext:
    passport: PassportData
    total_amount: int
    first_payment_date: date
    contract_number: str
    payments: List[Payment] = field(default_factory=list)
    contract_dir: Path | None = None
    docx_path: Path | None = None
    pdf_path: Path | None = None
