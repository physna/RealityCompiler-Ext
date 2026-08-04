# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Persist the non-secret routing config across sessions.

Only the routing info (API base, tenant, token URL, scope) is stored here
as plain JSON in the user's home directory — never the client secret,
which lives in the OS credential vault (:mod:`.credentials`). This lets an
operator enter the token endpoint once instead of every launch.
"""

from __future__ import annotations

import json
import os

from .config import ApiConfig

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".physna_reality_compiler")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")


class ConfigStore:
    """Read/write :class:`ApiConfig` to a JSON file (best-effort)."""

    def __init__(self, path: str = _CONFIG_PATH) -> None:
        self._path = path

    def load(self) -> ApiConfig | None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return ApiConfig(
            api_base=str(data.get("api_base", "")).rstrip("/") or ApiConfig().api_base,
            tenant_id=str(data.get("tenant_id", "")) or ApiConfig().tenant_id,
            token_url=str(data.get("token_url", "")),
            scope=str(data.get("scope", "")) or ApiConfig().scope,
        )

    def save(self, config: ApiConfig) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "api_base": config.api_base,
                        "tenant_id": config.tenant_id,
                        "token_url": config.token_url,
                        "scope": config.scope,
                    },
                    fh,
                    indent=2,
                )
        except OSError:
            # Persistence is a convenience; never fail a login over it.
            pass
