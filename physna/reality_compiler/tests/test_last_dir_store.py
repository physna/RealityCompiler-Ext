# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Remembered file-picker directories, including the legacy-location fallback."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from physna.reality_compiler import last_dir_store as lds


class TestLastDirStore(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rc_lastdir_"))
        self.store_dir = self.root / "store"
        self.store_file = self.store_dir / "last_dirs.json"
        self.legacy_file = self.root / "legacy" / "last_dirs.json"
        patches = [
            mock.patch.object(lds, "_STORE_DIR", self.store_dir),
            mock.patch.object(lds, "_STORE_FILE", self.store_file),
            mock.patch.object(lds, "_LEGACY_STORE_FILE", self.legacy_file),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_get_defaults_to_home_when_unset(self):
        self.assertEqual(lds.get_last_dir(lds.SCENE), str(Path.home()))

    def test_set_stores_a_files_parent_dir(self):
        target = self.root / "scans"
        target.mkdir()
        f = target / "scan.ply"
        f.write_text("x")
        lds.set_last_dir(lds.SCENE, str(f))
        self.assertEqual(lds.get_last_dir(lds.SCENE), str(target))

    def test_categories_are_independent(self):
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        lds.set_last_dir(lds.SCENE, str(self.root / "a"))
        lds.set_last_dir(lds.BATCH, str(self.root / "b"))
        self.assertEqual(lds.get_last_dir(lds.SCENE), str(self.root / "a"))
        self.assertEqual(lds.get_last_dir(lds.BATCH), str(self.root / "b"))

    def test_reads_legacy_location_when_new_file_absent(self):
        # Pre-unification installs wrote ~/.physna-reality-compiler (hyphens);
        # remembered dirs must survive the move to the underscore dot-dir.
        self.legacy_file.parent.mkdir(parents=True)
        self.legacy_file.write_text('{"scene": "C:/somewhere"}', encoding="utf-8")
        self.assertEqual(lds.get_last_dir(lds.SCENE), "C:/somewhere")

    def test_new_location_wins_over_legacy(self):
        self.legacy_file.parent.mkdir(parents=True)
        self.legacy_file.write_text('{"scene": "C:/old"}', encoding="utf-8")
        self.store_dir.mkdir(parents=True)
        self.store_file.write_text('{"scene": "C:/new"}', encoding="utf-8")
        self.assertEqual(lds.get_last_dir(lds.SCENE), "C:/new")


if __name__ == "__main__":
    unittest.main()
