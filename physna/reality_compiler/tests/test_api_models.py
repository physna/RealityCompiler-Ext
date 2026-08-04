# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Asset-state model invariants the pipeline layer relies on."""

import unittest

from physna.reality_compiler.api.models import (
    DEFAULT_WORKING_STATE,
    QUERYABLE_STATES,
    SCENE_REQUIRED_STATE,
    TERMINAL_STATES,
    WORKING_STATES,
    is_queryable,
    is_terminal,
    is_working,
)


class TestStateModel(unittest.TestCase):
    def test_state_sets_are_disjoint_and_consistent(self):
        # A state is either still working or terminal, never both.
        self.assertFalse(WORKING_STATES & TERMINAL_STATES)
        # Everything queryable is terminal, and the scene requirement is both.
        self.assertTrue(QUERYABLE_STATES <= TERMINAL_STATES)
        self.assertIn(SCENE_REQUIRED_STATE, QUERYABLE_STATES)
        # The seed used for interrupted-run reconstruction must poll as working.
        self.assertIn(DEFAULT_WORKING_STATE, WORKING_STATES)

    def test_predicates_match_the_sets(self):
        for s in WORKING_STATES:
            self.assertTrue(is_working(s))
            self.assertFalse(is_terminal(s))
        for s in TERMINAL_STATES:
            self.assertTrue(is_terminal(s))
            self.assertFalse(is_working(s))
        for s in QUERYABLE_STATES:
            self.assertTrue(is_queryable(s))
        self.assertFalse(is_queryable("failed"))

    def test_predicates_tolerate_none_and_unknown(self):
        for fn in (is_working, is_terminal, is_queryable):
            self.assertFalse(fn(None))
            self.assertFalse(fn(""))
            self.assertFalse(fn("some-future-state"))


if __name__ == "__main__":
    unittest.main()
