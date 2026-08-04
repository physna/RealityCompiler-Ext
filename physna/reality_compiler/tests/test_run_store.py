# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""RunStore / RunRecord persistence — pure Python, runs without Kit."""

import os
import shutil
import tempfile
import unittest

import numpy as np

from physna.reality_compiler.api.models import Match
from physna.reality_compiler.api.run_store import RunPart, RunRecord, RunStore


def _record(rid: str = "run-1", complete: bool = True) -> RunRecord:
    return RunRecord(
        id=rid,
        name="Warehouse",
        run_folder="demo/warehouse",
        created_at="2026-07-01T12:00:00",
        tenant_id="tenant",
        api_base="https://api.example/v3",
        scene_asset_id="scene-asset",
        parts=[
            RunPart(
                display_name="pallet",
                source_path="C:/parts/pallet.usd",
                asset_id="part-asset",
                matches=[Match(score=91.5, transform4x4=np.eye(4))],
            )
        ],
        complete=complete,
    )


class TestRunRecord(unittest.TestCase):
    def test_round_trip_preserves_fields(self):
        rec = _record(complete=False)
        loaded = RunRecord.from_dict(rec.to_dict())
        self.assertEqual(loaded.id, rec.id)
        self.assertEqual(loaded.scene_asset_id, rec.scene_asset_id)
        self.assertFalse(loaded.complete)
        self.assertEqual(loaded.total_matches, 1)
        np.testing.assert_allclose(
            loaded.parts[0].matches[0].transform4x4, np.eye(4)
        )

    def test_pre_complete_flag_records_load_as_complete(self):
        # Records saved before the resume feature have no "complete" key —
        # they were all finished runs, so they must default to complete=True.
        d = _record().to_dict()
        del d["complete"]
        self.assertTrue(RunRecord.from_dict(d).complete)


class TestRunStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_runstore_")
        self.store = RunStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_get_delete(self):
        self.store.save(_record("a"))
        got = self.store.get("a")
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "Warehouse")
        self.store.delete("a")
        self.assertIsNone(self.store.get("a"))

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_save_is_atomic_no_tmp_left_behind(self):
        self.store.save(_record("a"))
        self.store.save(_record("a"))  # overwrite goes through the same path
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_list_sorts_newest_first(self):
        old = _record("old")
        old.created_at = "2026-01-01T00:00:00"
        new = _record("new")
        new.created_at = "2026-07-01T00:00:00"
        self.store.save(old)
        self.store.save(new)
        self.assertEqual([r.id for r in self.store.list()], ["new", "old"])


if __name__ == "__main__":
    unittest.main()
