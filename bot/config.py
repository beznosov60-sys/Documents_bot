from __future__ import annotations

# Требуемые сторонние библиотеки: aiogram, jinja2, python-docx, reportlab, num2words, Babel, python-dateutil, aiofiles, easyocr, python-dotenv

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    bot_token: str
    storage_path: Path
    contracts_root: Path
    counter_file: Path
    registry_file: Path
    passports_dir: Path


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN") or os.getenv("TOKEN_BOT")
    if not token:
        raise RuntimeError("BOT_TOKEN (или TOKEN_BOT) environment variable is required")

    base_path = Path(__file__).resolve().parent.parent
    storage_path = base_path / "data"
    contracts_root = base_path / "contracts"
    counter_file = storage_path / "counter.json"
    registry_file = storage_path / "registry.json"
    passports_dir = storage_path / "passports"

    storage_path.mkdir(parents=True, exist_ok=True)
    contracts_root.mkdir(parents=True, exist_ok=True)
    passports_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        bot_token=token,
        storage_path=storage_path,
        contracts_root=contracts_root,
        counter_file=counter_file,
        registry_file=registry_file,
        passports_dir=passports_dir,
    )
