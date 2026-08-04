# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Non-blocking point-cloud loading for the Kit UI thread.

Runs :func:`.loaders.load_point_cloud` on a thread-pool executor so a
large file (multi-hundred-MB scans) doesn't freeze the Omniverse UI while
it parses. File IO releases the GIL, so a thread is sufficient here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import numpy as np

from .. import deps as _deps
from .loaders import load_point_cloud

LoadResult = Tuple[np.ndarray, Optional[np.ndarray], Optional[np.ndarray], Dict]

# Formats whose loader lazy-imports a background-installed dependency. Loading
# one during the startup install window must wait for that install; .npy/.npz
# and ASCII .xyz/.pts need only numpy (always present) and never wait.
_DEFERRED_DEP_EXTS = {".e57", ".las", ".laz", ".ply", ".pcd"}


async def load_point_cloud_async(
    file_path: str,
    color_mode: str = "color",
    on_progress: Optional[Callable[[str], None]] = None,
) -> LoadResult:
    """Load a point-cloud file off the UI thread.

    Returns the same ``(points, colors, intensity, metadata)`` tuple as
    :func:`.loaders.load_point_cloud`. If the format needs a dependency that's
    still installing in the background, waits for that install first.
    """
    if Path(file_path).suffix.lower() in _DEFERRED_DEP_EXTS:
        await _deps.ensure_deferred_ready(on_progress)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: load_point_cloud(file_path, color_mode=color_mode)
    )
