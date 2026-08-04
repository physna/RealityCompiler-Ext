# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
import unittest

import physna.reality_compiler

# Kit-only smoke test; skipped cleanly when discovered by a bare interpreter
# (the pure-Python tests alongside this file run anywhere).
try:
    import omni.kit.test
except ImportError:
    omni = None


@unittest.skipIf(omni is None, "requires Kit (omni.kit.test)")
class Test(
    omni.kit.test.AsyncTestCaseFailOnLogError if omni else unittest.TestCase
):
    """Smoke test: the extension module imports cleanly inside Kit."""

    async def test_import(self):
        self.assertIsNotNone(physna.reality_compiler)
