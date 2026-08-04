# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Transform helpers for USD prim operations."""

from __future__ import annotations

import re
import numpy as np
from typing import Optional

import omni.usd
import omni.client
from pxr import UsdGeom, Usd, Tf, Gf

from ..logger import get_logger

_log = get_logger("physna.reality_compiler.scene.transforms")


def get_world_xform(prim_path: str) -> Gf.Matrix4d:
    """Get the world transform for a prim, or identity if missing."""
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return Gf.Matrix4d(1.0)
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return Gf.Matrix4d(1.0)
    xformable = UsdGeom.Xformable(prim)
    if not xformable:
        return Gf.Matrix4d(1.0)
    return xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())


def apply_xform_to_point(mat: Gf.Matrix4d, point: np.ndarray) -> np.ndarray:
    """Transform a 3D point (numpy) by a USD 4x4 matrix."""
    p = np.array([point[0], point[1], point[2], 1.0], dtype=np.float64)
    m = np.array(mat, dtype=np.float64)
    res = m @ p
    return res[:3]


def avg_uniform_scale(mat: Gf.Matrix4d) -> float:
    """Approximate uniform scale from a matrix (average column lengths)."""
    col0 = Gf.Vec3d(mat[0][0], mat[1][0], mat[2][0]).GetLength()
    col1 = Gf.Vec3d(mat[0][1], mat[1][1], mat[2][1]).GetLength()
    col2 = Gf.Vec3d(mat[0][2], mat[1][2], mat[2][2]).GetLength()
    return float((col0 + col1 + col2) / 3.0) if (col0 + col1 + col2) > 0 else 1.0


def gf_matrix4d_to_numpy(mat: Gf.Matrix4d) -> np.ndarray:
    """Convert a ``Gf.Matrix4d`` to a numpy 4x4 float64 array.

    USD's ``Gf.Matrix4d`` uses **row-vector** convention
    (``v_new = v * M`` where ``v`` is a row, translation lives in the
    bottom row at ``mat[3][0..2]``).  The rest of this codebase uses
    numpy **column-vector** convention (``v_new = M @ v_col``,
    translation lives in the right column at ``arr[0..2][3]``).  A
    transpose is required between the two conventions; copying
    element-by-element produces a matrix that puts the translation in
    the wrong slot, which silently maps the asset's geometry origin to
    world origin (the root cause of the long-standing "import lands at
    origin" bug -- highlights happened to work because the codepath
    only exercised pure-rotation matrices, and rotations look identical
    in either convention).
    """
    arr = np.array(
        [
            [mat[0][0], mat[0][1], mat[0][2], mat[0][3]],
            [mat[1][0], mat[1][1], mat[1][2], mat[1][3]],
            [mat[2][0], mat[2][1], mat[2][2], mat[2][3]],
            [mat[3][0], mat[3][1], mat[3][2], mat[3][3]],
        ],
        dtype=np.float64,
    )
    return arr.T


def numpy_to_gf_matrix4d(m: np.ndarray) -> Gf.Matrix4d:
    """Convert a numpy 4x4 array to ``Gf.Matrix4d``.

    Transpose so the column-vector numpy matrix lands in row-vector
    USD form -- translation moves from the right column to the bottom
    row, which is where USD reads it.  See ``gf_matrix4d_to_numpy`` for
    the convention details.
    """
    m = np.asarray(m, dtype=np.float64).T
    return Gf.Matrix4d(
        float(m[0, 0]),
        float(m[0, 1]),
        float(m[0, 2]),
        float(m[0, 3]),
        float(m[1, 0]),
        float(m[1, 1]),
        float(m[1, 2]),
        float(m[1, 3]),
        float(m[2, 0]),
        float(m[2, 1]),
        float(m[2, 2]),
        float(m[2, 3]),
        float(m[3, 0]),
        float(m[3, 1]),
        float(m[3, 2]),
        float(m[3, 3]),
    )


def compute_parent_local_transform(
    pose: np.ndarray,
    actual_points_prim_path: str,
    parent_path: str,
) -> Gf.Matrix4d:
    """Compose *pose* into parent-local space and return a ``Gf.Matrix4d``.

    Args:
        pose: 4x4 match pose in local space.
        actual_points_prim_path: Path to the actual Points prim.
        parent_path: Path to the parent prim where the import will be placed.
    """
    pose_local = pose.astype(np.float64)
    actual_world_np = gf_matrix4d_to_numpy(get_world_xform(actual_points_prim_path))
    parent_inv_np = gf_matrix4d_to_numpy(get_world_xform(parent_path).GetInverse())
    pose_world = actual_world_np @ pose_local
    pose_parent_local = parent_inv_np @ pose_world
    return numpy_to_gf_matrix4d(pose_parent_local)


def transform_points_to_parent_local(
    instance_points: np.ndarray,
    pose: np.ndarray,
    actual_points_prim_path: str,
    parent_path: str,
) -> np.ndarray:
    """Transform instance points through: pose -> world -> parent local.

    Args:
        instance_points: Nx3 point cloud.
        pose: 4x4 match pose in local space.
        actual_points_prim_path: Path to the actual Points prim.
        parent_path: Path to the parent prim where the import will be placed.
    """
    pose_local = pose.astype(np.float64)

    # match pose
    points_h = np.column_stack(
        [instance_points, np.ones(len(instance_points), dtype=np.float64)]
    )
    transformed_local = (pose_local @ points_h.T).T[:, :3]

    # local -> world
    world_xform_np = gf_matrix4d_to_numpy(get_world_xform(actual_points_prim_path))
    local_h = np.column_stack(
        [transformed_local, np.ones(len(transformed_local), dtype=np.float64)]
    )
    transformed_world = (world_xform_np @ local_h.T).T[:, :3]

    # world -> parent local
    parent_inv_np = gf_matrix4d_to_numpy(get_world_xform(parent_path).GetInverse())
    world_h = np.column_stack(
        [transformed_world, np.ones(len(transformed_world), dtype=np.float64)]
    )
    return (parent_inv_np @ world_h.T).T[:, :3].astype(np.float32)


def safe_identifier(name: str) -> str:
    """Return a valid USD identifier from *name*.

    Uses ``Tf.MakeValidIdentifier`` and ensures the result does not
    start with a digit.
    """
    name = name.strip() or "PhysnaAsset"
    name = re.sub(r"\s+", "_", name)
    safe = Tf.MakeValidIdentifier(name)
    if safe[0].isdigit():
        safe = f"Asset_{safe}"
    return safe


def to_asset_url(path_or_url: str) -> str:
    """Normalize a filesystem path into an Omniverse-friendly asset URL."""
    return omni.client.normalize_url(path_or_url)


def resolve_import_parent_path(
    actual_points_prim_path: Optional[str],
    scene_prim_path: str,
) -> str:
    """Determine where to place an imported prim.

    Imports must land as a *sibling* of the scene's Points prim, never
    as a child of it.  USD Points are leaf geometry — descendants under
    them don't compose ancestor xforms correctly and the imported asset
    renders at origin instead of at the match transform.  This mirrors
    :func:`resolve_sibling_parent_path` so highlight + import end up at
    the same world location.

    Args:
        actual_points_prim_path: Path to the actual Points prim (may differ
            from *scene_prim_path* if a parent Xform was selected).
        scene_prim_path: The user-selected scene prim path.
    """
    stage = omni.usd.get_context().get_stage()
    candidate = actual_points_prim_path or scene_prim_path
    if not candidate:
        return scene_prim_path

    prim = stage.GetPrimAtPath(candidate) if stage else None
    if not prim or not prim.IsValid():
        return scene_prim_path

    # If the candidate is a Points prim (typical for an NPY/E57/LAS
    # scene imported as Points geometry), walk up one level.  If it's
    # already an Xform / Scope, use it directly.
    if UsdGeom.Points(prim):
        parent = Usd.Prim.GetParent(prim)
        if parent and parent.IsValid():
            parent_path = str(parent.GetPath())
            _log.debug(
                "Points prim %s selected; importing as sibling under %s",
                candidate, parent_path,
            )
            return parent_path

    _log.debug(
        "Non-Points selection %s; importing as child", candidate,
    )
    return candidate


def resolve_sibling_parent_path(
    actual_points_prim_path: Optional[str],
    scene_prim_path: str,
) -> str:
    """Return the parent path where a new prim should live to end up as a
    *sibling* of the scene's Points prim, regardless of whether the user
    originally selected the Points prim or its parent Xform.

    This differs from :func:`resolve_import_parent_path` in the
    Points-prim-selected case: that function returns the selected Points
    prim itself (making the new prim a child), whereas a preview /
    highlight overlay must be a sibling — USD Points prims are leaf
    geometry, and renderers can silently ignore descendants under them
    or mis-compute extents for the overlay.
    """
    stage = omni.usd.get_context().get_stage()
    candidate = actual_points_prim_path or scene_prim_path
    if not candidate:
        return scene_prim_path

    prim = stage.GetPrimAtPath(candidate) if stage else None
    if not prim or not prim.IsValid():
        return scene_prim_path

    # If the candidate is a Points prim, walk up one level.  If it's
    # already an Xform / Scope, use it directly.
    if UsdGeom.Points(prim):
        parent = Usd.Prim.GetParent(prim)
        if parent and parent.IsValid():
            parent_path = str(parent.GetPath())
            _log.debug(
                "Points prim at %s selected; placing sibling under %s",
                candidate,
                parent_path,
            )
            return parent_path

    _log.debug("Non-Points selection %s; using it as parent", candidate)
    return candidate
