# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""HTTP client for the Physna Scan Search API.

Thin, synchronous wrapper over ``requests`` covering the four calls the
workflow needs:

* ``upload_asset``      -> ``POST   /tenants/{tid}/assets``
* ``get_asset``         -> ``GET    /tenants/{tid}/assets/{id}``
* ``get_scene_matches`` -> ``GET    /tenants/{tid}/assets/{part}/scene-matches/{scene}``
* ``delete_asset``      -> ``DELETE /tenants/{tid}/assets/{id}``

Synchronous by design — no ``omni`` imports.  Kit callers run these on a
thread-pool executor so the UI loop keeps pumping; see the pipelines.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping

import requests

from .auth import TokenProvider
from .config import ApiConfig
from .models import Asset, SceneMatches


class ApiError(RuntimeError):
    """A non-success HTTP response from the API."""

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        snippet = (body or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        detail = f"{status_code}: {message}"
        if snippet:
            detail = f"{detail} — {snippet}"
        super().__init__(detail)
        self.status_code = status_code
        self.body = body


class PhysnaClient:
    """Authenticated client bound to one tenant."""

    def __init__(
        self,
        config: ApiConfig,
        token_provider: TokenProvider,
        *,
        session: requests.Session | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._config = config
        self._auth = token_provider
        self._session = session or requests.Session()
        self._timeout = timeout

    @property
    def config(self) -> ApiConfig:
        return self._config

    # -- public API --------------------------------------------------------

    def upload_asset(
        self,
        file_path: str,
        asset_path: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        create_missing_folders: bool = True,
    ) -> Asset:
        """Upload a file and return the created (``indexing``) asset.

        ``asset_path`` is the logical tenant path, e.g.
        ``scan-search-demo/warehouse/USD/forklift.usd``.  Scene and parts
        must share a parent folder for matching to run automatically.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)

        data = {
            "path": asset_path,
            "createMissingFolders": "true" if create_missing_folders else "false",
        }
        if metadata:
            data["metadata"] = json.dumps(dict(metadata))

        with open(file_path, "rb") as fh:
            files = {"file": (os.path.basename(file_path), fh)}
            resp = self._request(
                "POST", self._config.assets_url, data=data, files=files
            )
        return Asset.from_json(resp.json())

    def get_asset(self, asset_id: str) -> Asset:
        """Fetch current state for an asset (used for polling)."""
        url = f"{self._config.assets_url}/{asset_id}"
        resp = self._request("GET", url)
        return Asset.from_json(resp.json())

    def list_assets(
        self, page: int = 1, page_size: int = 200
    ) -> tuple[list[Asset], dict]:
        """One page of the tenant's assets; returns ``(assets, pageData)``."""
        url = f"{self._config.assets_url}?pageSize={page_size}&page={page}"
        resp = self._request("GET", url)
        body = resp.json()
        assets = [Asset.from_json(a) for a in body.get("assets", [])]
        return assets, body.get("pageData", {})

    def list_all_assets(self, page_size: int = 200) -> list[Asset]:
        """Every asset in the tenant, walking pagination."""
        out: list[Asset] = []
        page = 1
        while True:
            assets, page_data = self.list_assets(page=page, page_size=page_size)
            out.extend(assets)
            last = int(page_data.get("lastPage", page) or page)
            if page >= last or not assets:
                break
            page += 1
        return out

    def download_asset(
        self,
        asset_id: str,
        on_progress: "Callable[[int, int], None] | None" = None,
        dest_path: "str | None" = None,
    ) -> "bytes | None":
        """Download an asset's original file (GET /assets/{id}/file).

        With ``dest_path`` the body streams straight to that file and the
        return value is ``None`` — a multi-GB scan never has to fit in RAM.
        Without it the bytes are returned. When ``on_progress`` is given it is
        invoked as ``on_progress(bytes_so_far, total_bytes)`` — ``total`` is 0
        when the server sends no ``Content-Length``. Runs synchronously on the
        caller's thread (the pipelines call it on an executor), so the callback
        must be cheap and thread-safe.
        """
        url = f"{self._config.assets_url}/{asset_id}/file"
        if on_progress is None and dest_path is None:
            resp = self._request("GET", url)
            return resp.content

        resp = self._request("GET", url, stream=True)
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0

        def emit(n: int) -> None:
            if on_progress is not None:
                try:
                    on_progress(n, total)
                except Exception:
                    pass

        emit(0)
        if dest_path is not None:
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    emit(done)
            return None

        chunks: list[bytes] = []
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if not chunk:
                continue
            chunks.append(chunk)
            done += len(chunk)
            emit(done)
        return b"".join(chunks)

    def get_scene_matches(self, part_asset_id: str, scene_asset_id: str) -> SceneMatches:
        """Read placements of ``part`` inside ``scene``."""
        url = (
            f"{self._config.assets_url}/{part_asset_id}"
            f"/scene-matches/{scene_asset_id}"
        )
        resp = self._request("GET", url)
        return SceneMatches.from_json(resp.json())

    def delete_asset(self, asset_id: str) -> None:
        """Permanently delete an asset and its derived data (204 expected)."""
        url = f"{self._config.assets_url}/{asset_id}"
        self._request("DELETE", url, expected=(200, 204))

    # -- internals ---------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: Any = None,
        files: Any = None,
        expected: tuple[int, ...] = (200,),
        stream: bool = False,
        _retry_on_auth: bool = True,
    ) -> requests.Response:
        headers = {"Authorization": f"Bearer {self._auth.token()}"}
        try:
            resp = self._session.request(
                method,
                url,
                data=data,
                files=files,
                headers=headers,
                timeout=self._timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise ApiError(0, f"network error: {exc}") from exc

        # A stale token yields 401; refresh once and retry.  File uploads
        # can't be retried transparently (the stream is consumed), so we
        # only retry idempotent calls without a file body.
        if resp.status_code == 401 and _retry_on_auth and files is None:
            self._auth.invalidate()
            return self._request(
                method,
                url,
                data=data,
                files=files,
                expected=expected,
                stream=stream,
                _retry_on_auth=False,
            )

        if resp.status_code not in expected:
            raise ApiError(resp.status_code, resp.reason or "request failed", resp.text)
        return resp
