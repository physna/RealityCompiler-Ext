# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Centralised color normalisation for point-cloud loaders.

Every loader funnels its RGB through ``normalise_colors_to_uint8`` so the
rest of the extension gets a canonical ``Nx3 uint8`` array regardless of
the source format's quirks (LAS uint16, E57 0-1/0-255/0-65535, USD
displayColor floats, NPZ palette HSV).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

__all__ = ["normalise_colors_to_uint8"]


def normalise_colors_to_uint8(arr: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """Coerce an RGB array to canonical ``Nx3 uint8`` (0-255).

    Branches on dtype + observed max-value to handle every source format:

    * ``uint8``       — assumed already ``[0, 255]``; returned as-is.
    * ``uint16``      — LAS/LAZ, E57, NPZ palette. Divided by 257.
    * ``float`` ≤ 1.0 — USD ``displayColor`` style. Multiplied by 255.
    * ``float`` ≤ 255 — byte-scaled floats. Cast directly.
    * ``float`` > 255 — uint16-range floats. Divided by 257.

    Non-finite / out-of-range values are clipped to ``[0, 255]``. Returns
    ``None`` for ``None``/empty input; returns ``Nx3`` (extra channels
    dropped).
    """
    if arr is None:
        return None
    arr = np.asarray(arr)
    if arr.size == 0:
        return None

    # Drop alpha or other extra channels — we only consume RGB.
    if arr.ndim == 2 and arr.shape[1] > 3:
        arr = arr[:, :3]
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError(
            f"normalise_colors_to_uint8 expects Nx3 (or NxK, K>=3) array, "
            f"got shape {arr.shape}"
        )

    if arr.dtype == np.uint8:
        return np.ascontiguousarray(arr)

    if np.issubdtype(arr.dtype, np.integer):
        if arr.dtype == np.uint16 or arr.itemsize >= 2:
            # 257 == 0xFFFF / 0xFF; division demotes uint16 to uint8.
            scaled = arr.astype(np.uint32) // 257
            return np.clip(scaled, 0, 255).astype(np.uint8)
        return np.clip(arr, 0, 255).astype(np.uint8)

    if np.issubdtype(arr.dtype, np.floating):
        as_f = arr.astype(np.float64, copy=False)
        finite = np.where(np.isfinite(as_f), as_f, 0.0)
        max_val = float(finite.max()) if finite.size else 0.0
        if max_val <= 1.0:
            scaled = finite * 255.0
        elif max_val <= 255.0:
            scaled = finite
        else:
            scaled = finite / 257.0
        return np.clip(scaled, 0.0, 255.0).astype(np.uint8)

    return np.clip(arr.astype(np.float64), 0, 255).astype(np.uint8)
