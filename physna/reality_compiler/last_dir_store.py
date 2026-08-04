# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Persists last-opened directories for file picker dialogs.

Stores per-category paths (scene, instance, batch) in a JSON file so file
pickers re-open where the user last browsed.
"""

import json
from pathlib import Path

from .logger import get_logger

_log = get_logger("physna.reality_compiler.last_dir_store")

# Category keys
SCENE = "scene"
INSTANCE = "instance"
BATCH = "batch"

# Same dot-dir as the config/run stores (api.config_store / api.run_store), so
# all persistent extension data lives in one place.
_STORE_DIR = Path.home() / ".physna_reality_compiler"
_STORE_FILE = _STORE_DIR / "last_dirs.json"
# Pre-unification location (hyphens); read once as a fallback so remembered
# directories survive the move. Writes always go to the new file.
_LEGACY_STORE_FILE = Path.home() / ".physna-reality-compiler" / "last_dirs.json"


def get_last_dir(category: str) -> str:
    """Return the last-used directory for *category*, or user home."""
    data = _load()
    return data.get(category) or str(Path.home())


def set_last_dir(category: str, file_or_dir_path: str) -> None:
    """Save the directory of *file_or_dir_path* for *category*.

    If *file_or_dir_path* is a file, its parent directory is stored.
    """
    p = Path(file_or_dir_path)
    directory = str(p if p.is_dir() else p.parent)
    data = _load()
    if data.get(category) == directory:
        return  # no change
    data[category] = directory
    _save(data)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _load() -> dict:
    for path in (_STORE_FILE, _LEGACY_STORE_FILE):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.debug("Could not read last-dir store %s: %s", path, exc)
    return {}


def _save(data: dict) -> None:
    try:
        _STORE_DIR.mkdir(parents=True, exist_ok=True)
        _STORE_FILE.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        _log.debug("Could not write last-dir store: %s", exc)
