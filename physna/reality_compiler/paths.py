# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Filesystem locations for the extension.

All *ephemeral* scratch (converted meshes, extracted scene points, downloaded
scenes, isolated-E57 output, upload staging) lives under a single parent inside
the OS temp dir (:func:`tempfile.gettempdir`), so the host's normal temp
cleanup reclaims everything we write — nothing is left in a bespoke location the
OS won't sweep.

*Persistent* user data (service-account config, saved runs, downloaded scenes
referenced by saved runs) lives under :data:`PERSIST_ROOT` in the home dir
instead (see also ``api.config_store`` / ``api.run_store``) because it must
survive restarts; the resume feature depends on saved runs outliving temp.

Pure Python (no ``omni``), so ``io``/``converters`` and their subprocess helpers
can import it.
"""

from __future__ import annotations

import os
import tempfile

# One parent for every temp scratch dir we create, under the OS temp dir. Kept
# as a single well-known name so all our scratch is co-located and cleanable.
TEMP_ROOT = os.path.join(tempfile.gettempdir(), "physna_reality_compiler")

# Home for anything a saved run record points at — never swept by the OS.
PERSIST_ROOT = os.path.join(os.path.expanduser("~"), ".physna_reality_compiler")


def temp_dir(name: str) -> str:
    """Return (creating if needed) ``<os-temp>/physna_reality_compiler/<name>``."""
    path = os.path.join(TEMP_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


def persistent_dir(name: str) -> str:
    """Return (creating if needed) ``~/.physna_reality_compiler/<name>``."""
    path = os.path.join(PERSIST_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path
