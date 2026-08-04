# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Point cloud extraction from USD prims."""

from __future__ import annotations

import numpy as np
from typing import Optional, Tuple

import omni.usd
import omni.kit.app
from pxr import UsdGeom, Usd, Vt

from ..logger import get_logger
from . import transforms as _xform

_log = get_logger("physna.reality_compiler.scene.point_extraction")


def _vt_vec3_to_numpy(vt_array) -> np.ndarray:
    """Convert a Vt.Vec3fArray (or similar) to an (N, 3) float32 numpy array.

    Avoids forcing ``dtype`` during ``np.array()`` so that pxr's buffer
    protocol (which may expose a structured dtype) is handled correctly.
    """
    pts = np.array(vt_array)
    if pts.dtype.names is not None:
        pts = np.column_stack([pts[n] for n in pts.dtype.names])
    if pts.ndim == 1:
        pts = pts.reshape(-1, 3)
    return np.ascontiguousarray(pts, dtype=np.float32)


def _numpy_to_vt_vec3f(arr: np.ndarray) -> Vt.Vec3fArray:
    """Bulk-convert an Nx3 numpy array to Vt.Vec3fArray (C++ path, no Python loop)."""
    pts = np.ascontiguousarray(arr[:, :3], dtype=np.float32)
    if hasattr(Vt.Vec3fArray, "FromNumpy"):
        return Vt.Vec3fArray.FromNumpy(pts)
    return Vt.Vec3fArray(pts.tolist())


async def extract_point_cloud(
    prim_path: str, track_actual_prim: bool = False
) -> Optional[Tuple[np.ndarray, Optional[np.ndarray], Optional[str]]]:
    """Extract point cloud data from a USD prim (async with UI updates).

    Args:
        prim_path: Path to the prim to extract from.
        track_actual_prim: If ``True``, returns the actual Points prim
            path used (useful when a parent Xform is selected).

    Returns:
        ``(points, colors, actual_prim_path)`` or ``None``.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None

    prim = stage.GetPrimAtPath(prim_path)
    if not prim.IsValid():
        _log.warning("Invalid prim path: %s", prim_path)
        return None

    app = omni.kit.app.get_app()
    await app.next_update_async()

    # --- Try Points prim ---
    points_schema = UsdGeom.Points(prim)
    if points_schema:
        points_attr = points_schema.GetPointsAttr()
        if points_attr:
            points_data = points_attr.Get()
            if points_data:
                points = _vt_vec3_to_numpy(points_data)
                await app.next_update_async()

                colors = None
                color_attr = points_schema.GetDisplayColorAttr()
                if color_attr:
                    colors_data = color_attr.Get()
                    if colors_data and len(colors_data) > 0:
                        colors = _vt_vec3_to_numpy(colors_data)

                actual_prim_path = prim_path if track_actual_prim else None
                return points, colors, actual_prim_path

    # --- Try Mesh prim ---
    mesh_schema = UsdGeom.Mesh(prim)
    if mesh_schema:
        points_attr = mesh_schema.GetPointsAttr()
        if points_attr:
            points_data = points_attr.Get()
            if points_data:
                points = _vt_vec3_to_numpy(points_data)
                await app.next_update_async()

                xformable = UsdGeom.Xformable(prim)
                if xformable:
                    transform = _xform.gf_matrix4d_to_numpy(
                        xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                    )
                    points_h = np.column_stack(
                        [points, np.ones(len(points), dtype=np.float32)]
                    )
                    points = (points_h @ transform.T)[:, :3].astype(np.float32)

                actual_prim_path = prim_path if track_actual_prim else None
                return points, None, actual_prim_path

    # --- Try traversing Xform children ---
    if prim.IsA(UsdGeom.Xform):
        for child in prim.GetChildren():
            result = await extract_point_cloud(
                str(child.GetPath()), track_actual_prim=track_actual_prim
            )
            if result:
                return result

    _log.warning("Could not extract point cloud from prim: %s", prim_path)
    return None
