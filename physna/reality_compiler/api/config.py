# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Connection configuration for the hosted Physna Scan Search API.

Pure data — no ``omni``/``pxr`` imports — so it can be exercised from a
plain Python interpreter.  Values default to the production Physna stack
and can be overridden from the environment or the extension's settings UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Defaults for the production Physna stack. The tenant ID is intentionally
# NOT defaulted — it is per-organization and every user must supply their
# own.
DEFAULT_API_BASE = "https://app-api.physna.com/v3"
DEFAULT_TOKEN_URL = "https://physna-app.auth.us-east-2.amazoncognito.com/oauth2/token"
DEFAULT_TENANT_ID = ""
DEFAULT_SCOPE = "physna-api/access"


@dataclass(frozen=True)
class ApiConfig:
    """Everything needed to address a tenant on a Physna stack.

    Secrets (client id/secret) are *not* held here — they live in the
    OS credential store (:mod:`.credentials`).  This object only carries
    the non-secret routing information.
    """

    api_base: str = DEFAULT_API_BASE
    tenant_id: str = DEFAULT_TENANT_ID
    token_url: str = DEFAULT_TOKEN_URL
    scope: str = DEFAULT_SCOPE

    @classmethod
    def from_env(cls) -> "ApiConfig":
        """Build config from ``PHYSNA_*`` environment variables.

        Mirrors the variable names used by the reference implementation's
        ``.env`` file so an operator can validate credentials outside Kit.
        """
        return cls(
            api_base=os.environ.get("PHYSNA_API_BASE", DEFAULT_API_BASE).rstrip("/"),
            tenant_id=os.environ.get("PHYSNA_TENANT_ID", DEFAULT_TENANT_ID),
            token_url=os.environ.get("PHYSNA_TOKEN_URL", DEFAULT_TOKEN_URL),
            scope=os.environ.get("PHYSNA_SCOPE", DEFAULT_SCOPE),
        )

    def with_overrides(
        self,
        *,
        api_base: str | None = None,
        tenant_id: str | None = None,
        token_url: str | None = None,
        scope: str | None = None,
    ) -> "ApiConfig":
        """Return a copy with the given fields replaced (empty strings ignored)."""
        return ApiConfig(
            api_base=(api_base or self.api_base).rstrip("/"),
            tenant_id=tenant_id or self.tenant_id,
            token_url=token_url or self.token_url,
            scope=scope or self.scope,
        )

    @property
    def assets_url(self) -> str:
        """Base URL for the tenant's asset collection."""
        return f"{self.api_base}/tenants/{self.tenant_id}/assets"

    def is_addressable(self) -> bool:
        """True when we have enough to *route* a call (token url + tenant)."""
        return bool(self.api_base and self.tenant_id and self.token_url)
