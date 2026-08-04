# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""``poll_step`` error semantics: transient errors retry; only repeated
"asset not found" 404s mean deleted."""

import unittest

from physna.reality_compiler.api.client import ApiError
from physna.reality_compiler.api.models import (
    Asset,
    DEFAULT_WORKING_STATE,
    SCENE_REQUIRED_STATE,
    TYPE_MODEL,
)
from physna.reality_compiler.api.polling import (
    MISSING_CONFIRMATIONS,
    PollState,
    is_asset_not_found,
    poll_step,
)

_GONE_BODY = '{"message":"Asset not found","traceId":"t"}'


class _FakeClient:
    """get_asset stub: each id maps to a list of scripted responses, one per
    call — a state string returns an Asset, an ApiError is raised."""

    def __init__(self, table):
        self._table = {k: list(v) for k, v in table.items()}

    def get_asset(self, asset_id):
        script = self._table[asset_id]
        value = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(value, ApiError):
            raise value
        return Asset(id=asset_id, state=value, type=TYPE_MODEL)


def _gone_404():
    return ApiError(404, "Not Found", _GONE_BODY)


class TestIsAssetNotFound(unittest.TestCase):
    def test_matches_all_deleted_asset_bodies(self):
        for msg in ("Asset not found", "Model asset not found",
                    "Scene asset not found"):
            self.assertTrue(
                is_asset_not_found(ApiError(404, "Not Found", f'{{"message":"{msg}"}}'))
            )

    def test_rejects_other_errors(self):
        # Wrong status, wrong body (proxy page / lazy scene-match 404), or
        # not an ApiError at all.
        self.assertFalse(is_asset_not_found(ApiError(500, "boom", _GONE_BODY)))
        self.assertFalse(is_asset_not_found(ApiError(404, "Not Found", "<html>")))
        self.assertFalse(
            is_asset_not_found(
                ApiError(404, "Not Found", '{"message":"has not been computed"}')
            )
        )
        self.assertFalse(is_asset_not_found(RuntimeError("nope")))


class TestPollStep(unittest.TestCase):
    def test_terminal_moves_to_resolved(self):
        poll = PollState.for_ids(["a"])
        poll_step(_FakeClient({"a": [SCENE_REQUIRED_STATE]}), poll)
        self.assertTrue(poll.done)
        self.assertEqual(set(poll.resolved), {"a"})
        self.assertFalse(poll.missing)

    def test_working_stays_pending(self):
        poll = PollState.for_ids(["a"])
        poll_step(_FakeClient({"a": [DEFAULT_WORKING_STATE]}), poll)
        self.assertFalse(poll.done)
        self.assertEqual(poll.pending, {"a"})

    def test_transient_error_stays_pending(self):
        # A 5xx (or any non-deleted failure) must be retried, never treated
        # as gone — no matter how many rounds it persists.
        client = _FakeClient({"a": [ApiError(503, "unavailable")]})
        poll = PollState.for_ids(["a"])
        for _ in range(MISSING_CONFIRMATIONS + 1):
            poll_step(client, poll)
        self.assertEqual(poll.pending, {"a"})
        self.assertFalse(poll.missing)

    def test_single_not_found_is_not_missing_yet(self):
        # Destructive callers act on `missing`; one observation isn't proof.
        poll = PollState.for_ids(["a"])
        poll_step(_FakeClient({"a": [_gone_404()]}), poll)
        self.assertEqual(poll.pending, {"a"})
        self.assertFalse(poll.missing)
        self.assertEqual(poll.miss_counts, {"a": 1})

    def test_repeated_not_found_confirms_missing(self):
        client = _FakeClient({"a": [_gone_404()]})
        poll = PollState.for_ids(["a"])
        for _ in range(MISSING_CONFIRMATIONS):
            poll_step(client, poll)
        self.assertEqual(poll.missing, {"a"})
        self.assertFalse(poll.pending)
        self.assertTrue(poll.done)  # nothing left worth retrying

    def test_reappearing_asset_resets_the_count(self):
        # 404 -> exists -> 404 must NOT count as two consecutive not-founds.
        client = _FakeClient(
            {"a": [_gone_404(), DEFAULT_WORKING_STATE, _gone_404(),
                   DEFAULT_WORKING_STATE]}
        )
        poll = PollState.for_ids(["a"])
        for _ in range(3):
            poll_step(client, poll)
        self.assertFalse(poll.missing)
        self.assertEqual(poll.pending, {"a"})

    def test_non_asset_404_never_confirms(self):
        # A 404 without the API's "asset not found" body (proxy error page)
        # keeps retrying like any transient error.
        client = _FakeClient({"a": [ApiError(404, "Not Found", "<html>oops")]})
        poll = PollState.for_ids(["a"])
        for _ in range(MISSING_CONFIRMATIONS + 1):
            poll_step(client, poll)
        self.assertEqual(poll.pending, {"a"})
        self.assertFalse(poll.missing)

    def test_mixed_batch(self):
        client = _FakeClient(
            {
                "done": [SCENE_REQUIRED_STATE],
                "working": [DEFAULT_WORKING_STATE],
                "gone": [_gone_404()],
                "flaky": [ApiError(500, "boom")],
            }
        )
        poll = PollState.for_ids(["done", "working", "gone", "flaky"])
        for _ in range(MISSING_CONFIRMATIONS):
            poll_step(client, poll)
        self.assertEqual(set(poll.resolved), {"done"})
        self.assertEqual(poll.missing, {"gone"})
        self.assertEqual(poll.pending, {"working", "flaky"})


if __name__ == "__main__":
    unittest.main()
