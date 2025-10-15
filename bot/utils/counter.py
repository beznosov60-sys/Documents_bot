from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple


def load_and_increment(counter_file: Path) -> int:
    counter_file.parent.mkdir(parents=True, exist_ok=True)
    if counter_file.exists():
        with counter_file.open("r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError:
                data = {}
    else:
        data = {}
    current = int(data.get("value", 0)) + 1
    data["value"] = current
    with counter_file.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return current


def build_contract_number(counter: int, full_name: str) -> Tuple[str, str]:
    initials = "".join(part[0] for part in full_name.split() if part)
    return f"{counter:05d}", initials
