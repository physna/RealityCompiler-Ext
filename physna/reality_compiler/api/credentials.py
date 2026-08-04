# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Secure storage for Physna service-account credentials.

The client secret is shown only once at creation time and must never be
written to disk in plaintext.  We store it in the OS credential vault via
``keyring`` (Windows Credential Manager, macOS Keychain, or the Secret
Service on Linux).

The non-secret client id and the routing config are kept out of the
vault deliberately — only the secret is sensitive.  Both are needed to
authenticate, so :class:`CredentialStore` persists the id alongside the
secret for convenience but treats the secret as write-once-read-by-vault.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:  # keyring is installed via extension.toml's pipapi block.
    import keyring  # type: ignore

    _KEYRING_ERR: Exception | None = None
except Exception as exc:  # pragma: no cover - import-time environment issue
    keyring = None  # type: ignore
    _KEYRING_ERR = exc


SERVICE_NAME = "physna.reality_compiler"
_CLIENT_ID_KEY = "client_id"
_CLIENT_SECRET_KEY = "client_secret"


@dataclass(frozen=True)
class ServiceAccount:
    """A resolved client-credentials pair."""

    client_id: str
    # repr=False so the secret never lands in a log line, exception frame dump,
    # or debugger repr.
    client_secret: str = field(repr=False)

    def is_complete(self) -> bool:
        return bool(self.client_id and self.client_secret)


class CredentialStore:
    """Read/write the service-account secret in the OS credential vault."""

    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self._service = service_name

    @property
    def available(self) -> bool:
        """True when a backend is present to store secrets securely."""
        return keyring is not None

    @property
    def backend_error(self) -> Exception | None:
        return _KEYRING_ERR

    # -- write -------------------------------------------------------------

    def save(self, client_id: str, client_secret: str) -> None:
        """Persist the credential pair. Raises if no secure backend exists."""
        if keyring is None:
            raise RuntimeError(
                "No secure credential backend available (keyring failed to "
                f"import: {_KEYRING_ERR!r})."
            )
        keyring.set_password(self._service, _CLIENT_ID_KEY, client_id)
        keyring.set_password(self._service, _CLIENT_SECRET_KEY, client_secret)

    # -- read --------------------------------------------------------------

    def load(self) -> ServiceAccount | None:
        """Return the stored credential pair, or ``None`` if not set."""
        if keyring is None:
            return None
        client_id = keyring.get_password(self._service, _CLIENT_ID_KEY) or ""
        client_secret = keyring.get_password(self._service, _CLIENT_SECRET_KEY) or ""
        if not (client_id and client_secret):
            return None
        return ServiceAccount(client_id=client_id, client_secret=client_secret)

    def is_configured(self) -> bool:
        return self.load() is not None

    # -- delete ------------------------------------------------------------

    def clear(self) -> None:
        """Remove any stored credentials (used on logout)."""
        if keyring is None:
            return
        for key in (_CLIENT_ID_KEY, _CLIENT_SECRET_KEY):
            try:
                keyring.delete_password(self._service, key)
            except Exception:
                # keyring raises when the entry is absent; that's fine.
                pass
