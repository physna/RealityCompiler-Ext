# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""PhysnaClient transport behavior."""

import tempfile
import unittest
from pathlib import Path

import requests

from physna.reality_compiler.api.client import ApiError, PhysnaClient
from physna.reality_compiler.api.config import ApiConfig


class _TokenProvider:
    def token(self):
        return "token"

    def invalidate(self):
        pass


class _JsonResponse:
    status_code = 200
    reason = "OK"
    text = ""

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FlakyUploadSession:
    def __init__(self, exc):
        self._exc = exc
        self.upload_bodies = []

    def request(self, method, url, *, data=None, files=None, headers=None, timeout=None, stream=False):
        file_obj = files["file"][1]
        self.upload_bodies.append(file_obj.read())
        if len(self.upload_bodies) == 1:
            raise self._exc
        return _JsonResponse(
            {
                "id": "asset-1",
                "path": data["path"],
                "type": "model",
                "state": "indexing",
            }
        )


class _AlwaysFailSession:
    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise self._exc


def _config():
    return ApiConfig(
        api_base="https://dev2-api.physna.com/v3",
        tenant_id="tenant",
        token_url="https://auth.example/token",
        scope="scope",
    )


class TestPhysnaClientRetries(unittest.TestCase):
    def test_upload_rewinds_file_after_retryable_ssl_error(self):
        session = _FlakyUploadSession(requests.exceptions.SSLError("tls eof"))
        client = PhysnaClient(
            _config(),
            _TokenProvider(),
            session=session,
            network_retries=1,
            retry_backoff_s=0,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scene.npy"
            path.write_bytes(b"scene-bytes")

            asset = client.upload_asset(str(path), "runs/demo/scene.npy")

        self.assertEqual(asset.id, "asset-1")
        self.assertEqual(session.upload_bodies, [b"scene-bytes", b"scene-bytes"])

    def test_retryable_network_error_reports_attempt_count(self):
        session = _AlwaysFailSession(requests.exceptions.SSLError("tls eof"))
        client = PhysnaClient(
            _config(),
            _TokenProvider(),
            session=session,
            network_retries=2,
            retry_backoff_s=0,
        )

        with self.assertRaises(ApiError) as cm:
            client.get_asset("asset-1")

        self.assertEqual(session.calls, 3)
        self.assertIn("network error after 3 attempts", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
