from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def load_registry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_registry(path: Path, registry: Dict[str, Any]) -> None:
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def update_user_contract(
    path: Path,
    user_id: int,
    payload: Dict[str, Any],
) -> None:
    registry = load_registry(path)
    users = registry.setdefault("users", {})
    user_entry = users.setdefault(str(user_id), {})
    history = user_entry.setdefault("contracts", [])
    history.append(payload)
    user_entry["last_contract"] = payload
    save_registry(path, registry)


def get_last_contract(path: Path, user_id: int) -> Optional[Dict[str, Any]]:
    registry = load_registry(path)
    return registry.get("users", {}).get(str(user_id), {}).get("last_contract")
