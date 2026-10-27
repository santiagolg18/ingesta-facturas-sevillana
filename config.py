"""
Configuración por variables de entorno.

En Render se ponen en el panel del worker. En local se pueden dejar en un
archivo .env junto a este (ver .env.example); nunca se sube al repo.
"""

import os
from pathlib import Path


def _load_dotenv() -> None:
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Falta la variable de entorno {name} (ver .env.example)")
    return value


def optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None
