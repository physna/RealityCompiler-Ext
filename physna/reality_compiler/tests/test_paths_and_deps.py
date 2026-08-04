# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Temp-dir layout (paths.py) and the background pip-install gate (deps.py)."""

import asyncio
import importlib
import os
import tempfile
import time
import unittest

from physna.reality_compiler import deps as deps_module
from physna.reality_compiler import paths


class TestPaths(unittest.TestCase):
    def test_temp_root_lives_in_os_temp(self):
        self.assertEqual(
            paths.TEMP_ROOT,
            os.path.join(tempfile.gettempdir(), "physna_reality_compiler"),
        )

    def test_temp_dir_creates_under_root(self):
        d = paths.temp_dir("unit-test")
        self.assertEqual(d, os.path.join(paths.TEMP_ROOT, "unit-test"))
        self.assertTrue(os.path.isdir(d))


class TestDeps(unittest.TestCase):
    def setUp(self):
        # deps holds module-level install state; reload for a clean slate.
        self.deps = importlib.reload(deps_module)

    def test_ready_is_immediate_when_no_install_started(self):
        self.assertFalse(self.deps.deferred_install_pending())
        asyncio.run(self.deps.ensure_deferred_ready())  # must not hang

    def test_ready_waits_for_a_running_install(self):
        installed = []

        def slow_install(pkg):
            time.sleep(0.2)
            installed.append(pkg)

        self.deps.start_deferred_install(["a", "b"], slow_install)
        self.assertTrue(self.deps.deferred_install_pending())

        t0 = time.monotonic()
        asyncio.run(self.deps.ensure_deferred_ready())
        waited = time.monotonic() - t0

        self.assertGreaterEqual(waited, 0.3)  # actually waited for both
        self.assertEqual(installed, ["a", "b"])
        self.assertFalse(self.deps.deferred_install_pending())

    def test_install_failure_still_releases_waiters(self):
        def broken_install(pkg):
            raise RuntimeError("pip exploded")

        # The install thread must set the done flag even on error, or every
        # later file load would block for the full wait timeout.
        self.deps.start_deferred_install(["a"], broken_install)
        asyncio.run(self.deps.ensure_deferred_ready())
        self.assertFalse(self.deps.deferred_install_pending())

    def test_start_is_idempotent(self):
        calls = []
        self.deps.start_deferred_install(["a"], calls.append)
        self.deps.start_deferred_install(["a"], calls.append)  # ignored
        asyncio.run(self.deps.ensure_deferred_ready())
        self.assertEqual(calls, ["a"])


if __name__ == "__main__":
    unittest.main()
