# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""High-level auth/session facade for the extension.

Wraps the config, credential store, token provider and HTTP client into
one object the UI and pipelines talk to.  Owns the login/logout lifecycle
and lazily restores a session from stored credentials on startup.
"""

from __future__ import annotations

import requests

from .auth import AuthError, TokenProvider
from .client import PhysnaClient
from .config import ApiConfig
from .config_store import ConfigStore
from .credentials import CredentialStore, ServiceAccount


class ApiSession:
    """Login state + a ready-to-use :class:`PhysnaClient`."""

    def __init__(
        self,
        config: ApiConfig | None = None,
        credential_store: CredentialStore | None = None,
        config_store: ConfigStore | None = None,
    ) -> None:
        self._config_store = config_store or ConfigStore()
        # Precedence: explicit arg > persisted routing > environment/defaults.
        self._config = config or self._config_store.load() or ApiConfig.from_env()
        self._store = credential_store or CredentialStore()
        self._client: PhysnaClient | None = None
        self._provider: TokenProvider | None = None
        self._account: ServiceAccount | None = None
        self._http = requests.Session()

    # -- config ------------------------------------------------------------

    @property
    def config(self) -> ApiConfig:
        return self._config

    def set_config(self, config: ApiConfig) -> None:
        """Swap routing config, persist it, and invalidate any live client."""
        self._config = config
        self._client = None
        self._config_store.save(config)

    @property
    def credential_store(self) -> CredentialStore:
        return self._store

    # -- state -------------------------------------------------------------

    @property
    def is_logged_in(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> PhysnaClient | None:
        return self._client

    @property
    def client_id(self) -> str:
        return self._account.client_id if self._account else ""

    # -- lifecycle ---------------------------------------------------------

    def restore(self) -> bool:
        """Rebuild a client from stored credentials without a network call.

        Returns True if credentials were found and a client was built.
        The token is fetched lazily on the first API call.
        """
        account = self._store.load()
        if account is None:
            return False
        self._bind(account)
        return True

    def login(
        self,
        client_id: str,
        client_secret: str,
        *,
        persist: bool = True,
        verify: bool = True,
    ) -> None:
        """Authenticate with a service account and (optionally) store it.

        With ``verify=True`` a token is fetched immediately so bad
        credentials fail fast at login rather than on first use.  Raises
        :class:`AuthError` on failure; nothing is persisted in that case.
        """
        account = ServiceAccount(client_id=client_id.strip(), client_secret=client_secret.strip())
        if not account.is_complete():
            raise AuthError("Client id and secret are both required.")

        provider = TokenProvider(self._config, account, session=self._http)
        if verify:
            provider.token()  # raises AuthError on bad creds / config

        if persist:
            self._store.save(account.client_id, account.client_secret)
        self._account = account
        self._provider = provider
        self._client = PhysnaClient(self._config, provider, session=self._http)

    def verify(self) -> None:
        """Confirm the current session's credentials are still valid.

        Forces a fresh token fetch; raises :class:`AuthError` if the
        service account has been revoked/rotated or the endpoint is
        unreachable. Blocking (network) — call it off the UI thread.
        """
        if self._provider is None:
            raise AuthError("No active session to verify.")
        self._provider.invalidate()
        self._provider.token()

    def invalidate_session(self) -> None:
        """Drop the live client (logged-out state) but keep stored creds.

        Used when a restored session fails validation: the user is shown
        the sign-in form, but the vault entry is left intact.
        """
        self._client = None
        self._provider = None
        self._account = None

    def logout(self, *, forget: bool = True) -> None:
        """Drop the live client and (by default) erase stored credentials."""
        self._client = None
        self._provider = None
        self._account = None
        if forget:
            self._store.clear()

    # -- internals ---------------------------------------------------------

    def _bind(self, account: ServiceAccount) -> None:
        provider = TokenProvider(self._config, account, session=self._http)
        self._account = account
        self._provider = provider
        self._client = PhysnaClient(self._config, provider, session=self._http)
