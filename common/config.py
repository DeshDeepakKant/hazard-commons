"""Config + env loading. Import `settings()` anywhere; it is cached."""
from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@functools.lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    with open(ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def flag(name: str) -> bool:
    return (env(name, "0") or "0").lower() in ("1", "true", "yes")


def is_demo() -> bool:
    return flag("DEMO")


def mock_brain_enabled() -> bool:
    """MOCK_BRAIN is dev-only. It is impossible to enable together with DEMO=1."""
    if not flag("MOCK_BRAIN"):
        return False
    if is_demo():
        raise RuntimeError("MOCK_BRAIN=1 is forbidden when DEMO=1. Unset one of them.")
    return True


def operator_names() -> list[str]:
    return [o["name"] for o in settings()["operators"]]


def path(rel: str) -> Path:
    return ROOT / rel
