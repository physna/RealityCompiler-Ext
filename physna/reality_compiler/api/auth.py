# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""OAuth2 client-credentials token provider (Appendix A of the API guide).

Exchanges a service account's client id/secret for a short-lived bearer
access token at the tenant's Cognito token endpoint, and caches it in
memory until shortly before it expires.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests

from .config import ApiConfig
from .credentials import ServiceAccount


class AuthError(RuntimeError):
    """Raised when a token cannot be obtained (bad creds, network, config)."""


def _is_secure_url(url: str) -> bool:
    """True for ``https://`` URLs, or ``http://`` to a loopback host (dev)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme == "https":
        return True
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")


class TokenProvider:
    """Fetches and caches a client-credentials access token.

    Thread-safe: the request layer may call :meth:`token` from a worker
    thread while the UI thread triggers a refresh.  The token is cached
    for its lifetime minus a safety skew so callers never hand the server
    an about-to-expire token.
    """

    # Refresh this many seconds before the server-reported expiry.
    _EXPIRY_SKEW_S = 60.0

    def __init__(
        self,
        config: ApiConfig,
        account: ServiceAccount,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._config = config
        self._account = account
        self._session = session or requests.Session()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-authenticates."""
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    def token(self, *, monotonic: "callable[[], float]" = time.monotonic) -> str:
        """Return a valid access token, refreshing if needed."""
        with self._lock:
            now = monotonic()
            if self._access_token and now < self._expires_at:
                return self._access_token
            self._access_token, lifetime = self._request_token()
            self._expires_at = now + max(0.0, lifetime - self._EXPIRY_SKEW_S)
            return self._access_token

    # -- internals ---------------------------------------------------------

    def _request_token(self) -> tuple[str, float]:
        if not self._config.token_url:
            raise AuthError("No token endpoint configured (PHYSNA_TOKEN_URL).")
        if not self._account.is_complete():
            raise AuthError("Service account is missing a client id or secret.")
        # The client secret is sent as HTTP Basic auth below - never over plain
        # HTTP (it would be exfiltratable in cleartext).
        if not _is_secure_url(self._config.token_url):
            raise AuthError(
                "Token URL must be https:// (refusing to send the client secret "
                "over an insecure connection)."
            )

        try:
            resp = self._session.post(
                self._config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "scope": self._config.scope,
                },
                auth=(self._account.client_id, self._account.client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise AuthError(f"Token request failed: {exc}") from exc

        if resp.status_code != 200:
            # Don't echo the raw server body into the exception/log/UI - some
            # auth servers reflect request parameters back.
            raise AuthError(
                f"Token endpoint returned HTTP {resp.status_code}."
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise AuthError("Token endpoint returned a non-JSON body.") from exc

        access = body.get("access_token")
        if not access:
            raise AuthError("Token response contained no access_token.")
        # Cognito returns expires_in (seconds); default to one hour.
        lifetime = float(body.get("expires_in", 3600))
        return access, lifetime
