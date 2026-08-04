# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Point removal operations on USD Point prims."""

from __future__ import annotations

import asyncio

import numpy as np

import omni.usd
import omni.kit.app
from pxr import Gf, UsdGeom, Vt

from ..logger import get_logger
from .point_extraction import _vt_vec3_to_numpy, _numpy_to_vt_vec3f

_log = get_logger("physna.reality_compiler.scene.point_removal")


def _compute_keep_mask(
    points: np.ndarray,
    centers: np.ndarray,
    radius: float,
) -> np.ndarray:
    """Return a mask keeping points outside *radius* of all *centers*."""
    centers = np.asarray(centers, dtype=np.float32)
    if centers.ndim == 1:
        centers = centers.reshape(1, 3)

    if centers.size == 0:
        return np.ones(len(points), dtype=bool)

    try:
        from scipy.spatial import cKDTree

        tree = cKDTree(centers)
        distances, _ = tree.query(points, distance_upper_bound=radius)
        return distances > radius
    except Exception:
        radius_sq = float(radius * radius)
        keep_mask = np.ones(len(points), dtype=bool)
        chunk_size = 2048
        for start in range(0, len(centers), chunk_size):
            chunk = centers[start : start + chunk_size]
            diffs = points[:, None, :] - chunk[None, :, :]
            min_dist_sq = np.min(np.sum(diffs * diffs, axis=2), axis=1)
            keep_mask &= min_dist_sq > radius_sq
        return keep_mask


def remove_points_near(prim_path: str, center: np.ndarray, radius: float) -> int:
    """Remove points within *radius* of one or more centers from a Points prim.

    Args:
        prim_path: Path to the Points prim.
        center: 3D centre point or Nx3 array of centres.
        radius: Removal radius.

    Returns:
        Number of points removed.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return 0
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return 0
    points_schema = UsdGeom.Points(prim)
    if not points_schema:
        return 0
    points_attr = points_schema.GetPointsAttr()
    if not points_attr:
        return 0
    points_data = points_attr.Get()
    if not points_data:
        return 0

    points = _vt_vec3_to_numpy(points_data)
    keep_mask = _compute_keep_mask(points, center, radius)
    removed = int(np.sum(~keep_mask))

    if removed > 0:
        points_attr.Set(_numpy_to_vt_vec3f(points[keep_mask]))
        color_attr = points_schema.GetDisplayColorAttr()
        if color_attr:
            colors_data = color_attr.Get()
            if colors_data and len(colors_data) == len(points):
                colors = _vt_vec3_to_numpy(colors_data)
                color_attr.Set(_numpy_to_vt_vec3f(colors[keep_mask]))
        center_arr = np.asarray(center)
        target_desc = (
            f"{len(center_arr):,} centers"
            if center_arr.ndim == 2
            else np.array2string(center_arr, precision=3)
        )
        _log.info(
            "Removed %d points near %s (radius=%.3f)",
            removed,
            target_desc,
            radius,
        )

    return removed


async def remove_points_near_async(
    prim_path: str, center: np.ndarray, radius: float
) -> int:
    """Async version of :func:`remove_points_near` (yields to UI between steps)."""
    app = omni.kit.app.get_app()
    await app.next_update_async()
    removed = remove_points_near(prim_path, center, radius)
    await app.next_update_async()
    return removed


async def remove_points_in_world_boxes_async(
    prim_path: str, boxes: list, keep_inside: bool = False
) -> int:
    """Remove (or keep-only) points by world-box membership, off-threading the
    heavy numpy so a large cloud doesn't hitch the UI.

    Reads points and authors the result on the main thread (USD requires it);
    the world projection + box test run on a worker thread.
    """
    app = omni.kit.app.get_app()
    loop = asyncio.get_running_loop()
    await app.next_update_async()
    if not boxes:
        return 0
    read = read_points(prim_path)
    if read is None:
        return 0
    local, colors = read
    world_mat = points_world_matrix(prim_path)
    keep_mask = await loop.run_in_executor(
        None, _box_keep_mask, local, world_mat, boxes, keep_inside
    )
    removed = int(np.count_nonzero(~keep_mask))
    if removed <= 0:
        return 0
    write_points_mask(prim_path, local, colors, keep_mask)
    await app.next_update_async()
    _log.info(
        "Removed %d points (%s %d box(es))",
        removed,
        "keeping only inside" if keep_inside else "carving out",
        len(boxes),
    )
    return removed


def _box_keep_mask(local_points, world_mat, boxes: list, keep_inside: bool):
    """Keep-mask (by point index) for world-space AABB membership.

    Pure numpy - safe to run on a worker thread. Projects the local points to
    world once, then OR-s membership across the boxes.
    """
    world = apply_world_matrix(local_points, world_mat)
    inside = np.zeros(len(world), dtype=bool)
    for mn, mx in boxes:
        mn = np.asarray(mn, dtype=np.float64)
        mx = np.asarray(mx, dtype=np.float64)
        inside |= np.all((world >= mn) & (world <= mx), axis=1)
    return inside if keep_inside else ~inside


# ---------------------------------------------------------------------------
# Reversible occlusion (for the placement "hot-swap": hide scan points behind
# placed matches without losing them). Unlike the removals above, this never
# mutates in place - it always rewrites the prim from a caller-held pristine
# snapshot, so sliding placements out re-reveals the points exactly.
# ---------------------------------------------------------------------------

def is_points_prim(prim_path: str) -> bool:
    """True if ``prim_path`` is a valid ``UsdGeom.Points`` prim."""
    stage = omni.usd.get_context().get_stage()
    if not stage or not prim_path:
        return False
    prim = stage.GetPrimAtPath(prim_path)
    return bool(prim and prim.IsValid() and UsdGeom.Points(prim))


def read_points(prim_path: str):
    """Return ``(local_points Nx3 float32, colors Nx3 float32 | None)``.

    ``colors`` is returned only when a per-point ``displayColor`` matches the
    point count (a single constant colour is left untouched by occlusion, so
    it needs no snapshot). Returns ``None`` if the prim has no points.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None
    points_schema = UsdGeom.Points(prim)
    if not points_schema:
        return None
    points_attr = points_schema.GetPointsAttr()
    points_data = points_attr.Get() if points_attr else None
    if not points_data:
        return None
    pts = _vt_vec3_to_numpy(points_data).astype(np.float32)
    colors = None
    color_attr = points_schema.GetDisplayColorAttr()
    colors_data = color_attr.Get() if color_attr else None
    if colors_data and len(colors_data) == len(points_data):
        colors = _vt_vec3_to_numpy(colors_data).astype(np.float32)
    return pts, colors


def points_world_matrix(prim_path: str) -> np.ndarray:
    """The prim's local-to-world transform as a 4x4 numpy matrix.

    Cheap (a single ``ComputeLocalToWorldTransform``); reads USD, so call on
    the main thread. Pair with :func:`apply_world_matrix` (pure numpy) to
    project the snapshot's local points into world space once.
    """
    from . import transforms as _xform

    return _xform.gf_matrix4d_to_numpy(_xform.get_world_xform(prim_path))


def apply_world_matrix(local_points: np.ndarray, world_mat: np.ndarray) -> np.ndarray:
    """Project Nx3 local points into world space. Pure numpy - thread-safe."""
    local = np.asarray(local_points, dtype=np.float64)
    if len(local) == 0:
        return local.reshape(-1, 3)
    homog = np.column_stack([local, np.ones(len(local), dtype=np.float64)])
    return (np.asarray(world_mat, dtype=np.float64) @ homog.T).T[:, :3]


def cover_delta(world, cover, add_boxes: list, remove_boxes: list):
    """Update per-point box-cover counts for added/removed world AABBs.

    ``cover[i]`` counts how many placed boxes contain world point ``i``;
    visible points are those with ``cover == 0``. Only the changed boxes are
    tested (one O(N) pass each), so a single placement costs one box test, not
    a full re-mask against every placement. Pure numpy - safe off-thread.
    Mutates and returns ``cover``.
    """
    for mn, mx in add_boxes:
        inside = np.all(
            (world >= np.asarray(mn)) & (world <= np.asarray(mx)), axis=1
        )
        cover[inside] += 1
    for mn, mx in remove_boxes:
        inside = np.all(
            (world >= np.asarray(mn)) & (world <= np.asarray(mx)), axis=1
        )
        cover[inside] -= 1
    return cover


def write_points_mask(prim_path, snapshot_points, snapshot_colors, keep_mask) -> int:
    """Author the snapshot filtered by ``keep_mask`` (``None`` = keep all).

    Rewrites the Points prim (and matching per-point colours) from the pristine
    snapshot, so occlusion is fully reversible. Returns how many points are
    hidden. Authors USD - call on the main thread.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return 0
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return 0
    points_schema = UsdGeom.Points(prim)
    if not points_schema:
        return 0
    points_attr = points_schema.GetPointsAttr()
    if not points_attr:
        return 0

    snap = np.asarray(snapshot_points, dtype=np.float32)
    if keep_mask is None:
        kept_points = snap
        hidden = 0
    else:
        keep = np.asarray(keep_mask, dtype=bool)
        kept_points = snap[keep]
        hidden = int(keep.size - int(np.count_nonzero(keep)))
    points_attr.Set(_numpy_to_vt_vec3f(kept_points))
    color_attr = points_schema.GetDisplayColorAttr()
    if color_attr and snapshot_colors is not None:
        cols = np.asarray(snapshot_colors, dtype=np.float32)
        if len(cols) == len(snap):
            kept_cols = cols if keep_mask is None else cols[keep]
            color_attr.Set(_numpy_to_vt_vec3f(kept_cols))
    # Keep the authored extent in sync - BBoxCache uses it as a fast path, so a
    # stale (pre-carve) extent would report oversized bounds afterward.
    if len(kept_points) > 0:
        mn = kept_points.min(axis=0)
        mx = kept_points.max(axis=0)
        points_schema.GetExtentAttr().Set(
            [Gf.Vec3f(float(mn[0]), float(mn[1]), float(mn[2])),
             Gf.Vec3f(float(mx[0]), float(mx[1]), float(mx[2]))]
        )
    return hidden


async def write_points_mask_async(
    prim_path, snapshot_points, snapshot_colors, keep_mask
) -> int:
    """Async wrapper that yields to the UI around the point-attr write."""
    app = omni.kit.app.get_app()
    await app.next_update_async()
    hidden = write_points_mask(prim_path, snapshot_points, snapshot_colors, keep_mask)
    await app.next_update_async()
    return hidden


# ---------------------------------------------------------------------------
# Per-point visibility via ``widths`` (occlusion without touching the position
# buffer). Zero-radius points aren't drawn by any renderer, so hidden points
# vanish, while the point count - and the big positions/colours buffers - stay
# constant: only a 1-float-per-point widths buffer is rewritten, never a GPU
# realloc. Visible points get ``visible_width`` (their real width if the prim
# has one, else a size derived from point spacing).
# ---------------------------------------------------------------------------

def _numpy_to_vt_float(arr: np.ndarray) -> Vt.FloatArray:
    a = np.ascontiguousarray(arr, dtype=np.float32)
    if hasattr(Vt.FloatArray, "FromNumpy"):
        return Vt.FloatArray.FromNumpy(a)
    return Vt.FloatArray(a.tolist())


def read_point_widths(prim_path: str):
    """Snapshot ``(widths ndarray | None, interpolation token | None)``.

    Captures the prim's original ``widths`` verbatim (including per-point
    variation) so :func:`clear_point_visibility` can restore it exactly rather
    than collapsing it to a scalar.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None, None
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None, None
    points_schema = UsdGeom.Points(prim)
    if not points_schema:
        return None, None
    widths_attr = points_schema.GetWidthsAttr()
    widths_data = widths_attr.Get() if widths_attr else None
    if not widths_data:
        return None, None
    return np.asarray(widths_data, dtype=np.float32), points_schema.GetWidthsInterpolation()


def set_point_visibility(prim_path: str, visible_mask, visible_width: float) -> int:
    """Hide/show points via per-point ``widths`` (``visible_width`` vs ``0``).

    ``visible_mask`` is a bool array the length of the prim's points. Returns
    the number hidden. Authors USD - call on the main thread.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return 0
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return 0
    points_schema = UsdGeom.Points(prim)
    if not points_schema:
        return 0
    points_data = points_schema.GetPointsAttr().Get()
    n = len(points_data) if points_data else 0
    visible = np.asarray(visible_mask, dtype=bool)
    if n == 0:
        return 0
    if len(visible) != n:
        # The mask is stale (point count changed under us). Don't author a
        # wrong-length per-vertex widths array - drop widths so nothing renders
        # garbage; the caller should re-snapshot.
        _log.warning(
            "Point-visibility mask (%d) != point count (%d); clearing widths",
            len(visible), n,
        )
        if points_schema.GetWidthsAttr():
            prim.RemoveProperty("widths")
        return 0

    w = float(visible_width) if visible_width and visible_width > 0 else 0.001
    widths = np.where(visible, w, 0.0)
    widths_attr = points_schema.GetWidthsAttr()
    if not widths_attr:
        widths_attr = points_schema.CreateWidthsAttr()
    points_schema.SetWidthsInterpolation(UsdGeom.Tokens.vertex)
    widths_attr.Set(_numpy_to_vt_float(widths))
    return int(np.count_nonzero(~visible))


def clear_point_visibility(prim_path: str, orig_widths=None, orig_interp=None) -> None:
    """Undo width-based hiding: restore the prim's original ``widths`` verbatim.

    Re-authors ``orig_widths`` with ``orig_interp`` if the prim had widths,
    otherwise removes the ``widths`` attribute (back to the default point size).
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return
    points_schema = UsdGeom.Points(prim)
    if not points_schema:
        return
    widths_attr = points_schema.GetWidthsAttr()
    if orig_widths is not None and len(orig_widths) > 0:
        if not widths_attr:
            widths_attr = points_schema.CreateWidthsAttr()
        if orig_interp:
            points_schema.SetWidthsInterpolation(orig_interp)
        widths_attr.Set(_numpy_to_vt_float(np.asarray(orig_widths)))
    elif widths_attr:
        prim.RemoveProperty("widths")


async def set_point_visibility_async(
    prim_path: str, visible_mask, visible_width: float
) -> int:
    app = omni.kit.app.get_app()
    await app.next_update_async()
    hidden = set_point_visibility(prim_path, visible_mask, visible_width)
    await app.next_update_async()
    return hidden


async def clear_point_visibility_async(
    prim_path: str, orig_widths=None, orig_interp=None
) -> None:
    app = omni.kit.app.get_app()
    await app.next_update_async()
    clear_point_visibility(prim_path, orig_widths, orig_interp)
    await app.next_update_async()
