# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Client for the hosted Physna Scan Search API.

Pure-Python (``requests`` + ``keyring`` + ``numpy``) — no ``omni``/``pxr``
imports — so the whole package can be exercised from a plain interpreter
for credential validation and integration tests.

Typical use from the extension::

    session = ApiSession()
    session.restore() or session.login(client_id, client_secret)
    client = session.client
    scene = client.upload_asset("scene.npy", "demo/warehouse/scene.npy")
    part = client.upload_asset("forklift.usd", "demo/warehouse/USD/forklift.usd")
    # ... poll both to `finished` ...
    matches = client.get_scene_matches(part.id, scene.id)
"""

from __future__ import annotations

from .auth import AuthError, TokenProvider
from .client import ApiError, PhysnaClient
from .config import (
    DEFAULT_API_BASE,
    DEFAULT_SCOPE,
    DEFAULT_TENANT_ID,
    DEFAULT_TOKEN_URL,
    ApiConfig,
)
from .config_store import ConfigStore
from .discovery import DiscoveredRun, discover_runs
from .credentials import CredentialStore, ServiceAccount
from .models import (
    DEFAULT_WORKING_STATE,
    QUERYABLE_STATES,
    SCENE_REQUIRED_STATE,
    TERMINAL_STATES,
    TYPE_MODEL,
    TYPE_SCAN,
    WORKING_STATES,
    Asset,
    Match,
    SceneMatches,
    is_queryable,
    is_terminal,
    is_working,
)
from .polling import (
    DEFAULT_POLL_INTERVAL_S,
    PollState,
    is_asset_not_found,
    poll_step,
)
from .run_store import RunPart, RunRecord, RunStore
from .session import ApiSession

__all__ = [
    # session / auth
    "ApiSession",
    "TokenProvider",
    "AuthError",
    # client
    "PhysnaClient",
    "ApiError",
    # config / creds
    "ApiConfig",
    "DEFAULT_API_BASE",
    "DEFAULT_TOKEN_URL",
    "DEFAULT_TENANT_ID",
    "DEFAULT_SCOPE",
    "ConfigStore",
    "CredentialStore",
    "ServiceAccount",
    # models
    "Asset",
    "Match",
    "SceneMatches",
    "WORKING_STATES",
    "TERMINAL_STATES",
    "QUERYABLE_STATES",
    "DEFAULT_WORKING_STATE",
    "SCENE_REQUIRED_STATE",
    "TYPE_SCAN",
    "TYPE_MODEL",
    "is_working",
    "is_terminal",
    "is_queryable",
    # polling
    "PollState",
    "poll_step",
    "is_asset_not_found",
    "DEFAULT_POLL_INTERVAL_S",
    # run persistence
    "RunStore",
    "RunRecord",
    "RunPart",
    # platform discovery
    "DiscoveredRun",
    "discover_runs",
]
