# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""USD prim creation and query operations."""

from __future__ import annotations

import numpy as np
from typing import Optional

import omni.usd
from pxr import Usd, UsdGeom, Gf, Sdf, Vt

from ..logger import get_logger
from .point_extraction import _numpy_to_vt_vec3f

_log = get_logger("physna.reality_compiler.scene.prim_ops")


def get_selected_prim_path() -> Optional[str]:
    """Return the first selected prim path, or ``None``."""
    selection = omni.usd.get_context().get_selection()
    selected_paths = selection.get_selected_prim_paths()
    if not selected_paths:
        _log.warning("No prim selected")
        return None
    return selected_paths[0]


def get_selected_prim_paths() -> list[str]:
    """Return every selected prim path (empty list when nothing is selected)."""
    selection = omni.usd.get_context().get_selection()
    return list(selection.get_selected_prim_paths() or [])


def export_prim_to_usd(prim_path: str, out_path: str) -> bool:
    """Export a single prim subtree to a standalone ``.usd`` file.

    Flattens the live stage first so references, payloads, and instancing
    are baked into one layer, then copies just this subtree into a fresh
    layer with the subtree as its default prim.  Returns ``True`` on
    success.  Used to queue an in-stage prim as an uploadable part.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return False
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return False
    try:
        flat = stage.Flatten()
        name = prim_path.rstrip("/").split("/")[-1] or "Part"
        dst = Sdf.Path(f"/{name}")
        export_layer = Sdf.Layer.CreateNew(out_path)
        Sdf.CreatePrimInLayer(export_layer, dst)
        if not Sdf.CopySpec(flat, Sdf.Path(prim_path), export_layer, dst):
            return False
        export_layer.defaultPrim = name
        export_layer.Save()
        return True
    except Exception:
        _log.exception("Exporting prim %s to %s failed", prim_path, out_path)
        return False


def get_prim_point_count(prim_path: str) -> Optional[int]:
    """Return the number of points in a Points prim, or ``None``."""
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
    if not points_attr:
        return None
    points_data = points_attr.Get()
    if not points_data:
        return None
    return len(points_data)


def create_point_cloud_prim(
    points: np.ndarray,
    colors: Optional[np.ndarray],
    name: str,
    parent: str,
    width: Optional[float] = None,
) -> Optional[str]:
    """Create a USD Points prim from numpy arrays.

    Args:
        points: Nx3 float32 point positions.
        colors: Optional Nx3 colours.  Range auto-detected from max
            value: ``[0, 1]`` floats pass through, ``[0, 255]`` uint8/
            float divided by 255, ``[0, 65535]`` uint16 divided by 65535.
        name: Desired prim name (will be made unique).
        parent: Parent prim path.
        width: Optional per-point display width (constant interpolation).
            When the source scene has widths baked into its USD asset, a
            newly-created sibling Points prim without widths renders at
            the renderer's minimum size and is invisible behind the
            source — pass the source's sample width (or a small boost)
            to make the new prim match the existing visual density.

    Returns:
        The final prim path, or ``None`` on failure.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None

    if not stage.GetPrimAtPath(parent).IsValid():
        stage.DefinePrim(parent, "Xform")

    prim_path = f"{parent}/{name}"
    prim_path = omni.usd.get_stage_next_free_path(stage, prim_path, True)

    points_prim = stage.DefinePrim(prim_path, "Points")
    points_schema = UsdGeom.Points(points_prim)

    points_schema.GetPointsAttr().Set(_numpy_to_vt_vec3f(points))

    if colors is not None and len(colors) > 0:
        # USD displayColor wants Color3f in [0, 1].  Source range depends
        # on dtype: uint8 (NPY/E57/LAS sidecars) is [0, 255]; uint16
        # (some PLY exporters) is [0, 65535]; float arrays are normally
        # already in [0, 1].  Detect by max value rather than dtype so a
        # float array that happens to be in [0, 255] still works.
        cmax = float(colors.max())
        if cmax <= 1.0:
            scale = 1.0
        elif cmax <= 255.0:
            scale = 1.0 / 255.0
        else:
            scale = 1.0 / 65535.0
        colors_normalized = colors.astype(np.float32) * scale
        color_primvar = UsdGeom.PrimvarsAPI(points_prim).CreatePrimvar(
            "displayColor",
            Sdf.ValueTypeNames.Color3fArray,
            UsdGeom.Tokens.vertex,
        )
        color_primvar.Set(_numpy_to_vt_vec3f(colors_normalized))

    if width is not None and width > 0.0:
        widths_attr = points_schema.GetWidthsAttr()
        if not widths_attr:
            widths_attr = points_schema.CreateWidthsAttr()
        widths_attr.Set(Vt.FloatArray([float(width)]))
        points_schema.SetWidthsInterpolation(UsdGeom.Tokens.constant)

    min_vals = points.min(axis=0)
    max_vals = points.max(axis=0)
    min_point = Gf.Vec3f(float(min_vals[0]), float(min_vals[1]), float(min_vals[2]))
    max_point = Gf.Vec3f(float(max_vals[0]), float(max_vals[1]), float(max_vals[2]))
    points_schema.GetExtentAttr().Set([min_point, max_point])

    return prim_path


def get_points_prim_sample_width(path: str) -> Optional[float]:
    """Return a representative scalar width for the Points prim at *path*.

    Returns the median of the prim's ``widths`` attribute — handles both
    constant (single value) and per-point (vertex) interpolations.
    Returns ``None`` when the prim is missing, isn't a Points prim, or
    has no widths authored.  Use this to size preview / highlight prims
    to match the source cloud visually.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        return None
    points_schema = UsdGeom.Points(prim)
    if not points_schema:
        return None
    widths_attr = points_schema.GetWidthsAttr()
    if not widths_attr:
        return None
    widths = widths_attr.Get()
    if widths is None or len(widths) == 0:
        return None
    arr = np.asarray(widths, dtype=np.float64)
    med = float(np.median(arr))
    return med if med > 0.0 else None


def create_highlight_points(
    points: np.ndarray,
    parent_path: str,
    name: str,
) -> Optional[str]:
    """Create a yellow highlight Points prim from transformed points.

    Args:
        points: Nx3 float32 transformed point positions.
        parent_path: Parent prim path.
        name: Desired prim name (will be made unique).

    Returns:
        The final prim path, or ``None`` on failure.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None

    if not stage.GetPrimAtPath(parent_path).IsValid():
        stage.DefinePrim(parent_path, "Xform")

    prim_path = f"{parent_path}/{name}"
    prim_path = omni.usd.get_stage_next_free_path(stage, prim_path, True)

    points_prim = stage.DefinePrim(prim_path, "Points")
    points_schema = UsdGeom.Points(points_prim)

    points_schema.GetPointsAttr().Set(_numpy_to_vt_vec3f(points))

    yellow = np.full((len(points), 3), [1.0, 1.0, 0.0], dtype=np.float32)
    color_attr = points_schema.CreateDisplayColorAttr()
    color_attr.Set(_numpy_to_vt_vec3f(yellow))

    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    min_point = Gf.Vec3f(float(mins[0]), float(mins[1]), float(mins[2]))
    max_point = Gf.Vec3f(float(maxs[0]), float(maxs[1]), float(maxs[2]))
    points_schema.GetExtentAttr().Set([min_point, max_point])

    points_prim.SetInstanceable(False)
    return prim_path


def create_xform_with_reference(
    path: str,
    transform: Gf.Matrix4d,
    url: str,
) -> str:
    """Create an Xform prim with a transform and USD reference.

    Args:
        path: Desired prim path (will be made unique).
        transform: The world-to-local transform matrix.
        url: Asset URL to reference.

    Returns:
        The final prim path.
    """
    stage = omni.usd.get_context().get_stage()
    path = omni.usd.get_stage_next_free_path(stage, path, True)

    import_prim = stage.DefinePrim(path, "Xform")
    xform = UsdGeom.Xformable(import_prim)
    xform.ClearXformOpOrder()

    transform_op = xform.AddTransformOp(UsdGeom.XformOp.PrecisionDouble)
    transform_op.Set(transform)
    xform.SetXformOpOrder([transform_op])

    import_prim.GetReferences().AddReference(url)
    # Scenegraph instancing: every placement of the same part (same reference)
    # shares one GPU prototype, so N copies cost one geometry upload instead of
    # N. The per-instance transform lives on this prim (above the instanced
    # content), so placements still land in the right spots.
    import_prim.SetInstanceable(True)
    return path


def create_xforms_with_references(items: list) -> list:
    """Author many ``(base_path, transform, url)`` referenced Xforms in one pass.

    Uses the same proven per-prim authoring as :func:`create_xform_with_reference`
    (``DefinePrim`` needs live composition, so an ``Sdf.ChangeBlock`` can't be
    used here). The batching win comes from the caller authoring the whole set
    with no ``await`` in between, so Hydra syncs once for the batch on the next
    update tick instead of once per prim. Returns the created prim paths; a
    single failure is logged and skipped rather than aborting (and losing track
    of) the rest of the batch.
    """
    out = []
    for base, transform, url in items:
        try:
            out.append(create_xform_with_reference(base, transform, url))
        except Exception:
            _log.exception("Failed to place a reference to %s; skipping", url)
    return out


def delete_prims(prim_paths: list) -> None:
    """Remove many prims in one pass (caller yields once afterward, so Hydra
    re-syncs once for the batch rather than per prim)."""
    for prim_path in prim_paths:
        delete_prim(prim_path)


def create_xform_with_points(
    path: str,
    points: np.ndarray,
) -> str:
    """Create an Xform prim containing a child Points prim.

    Args:
        path: Desired prim path (will be made unique).
        points: Nx3 float32 transformed point positions.

    Returns:
        The final prim path.
    """
    stage = omni.usd.get_context().get_stage()
    path = omni.usd.get_stage_next_free_path(stage, path, True)

    stage.DefinePrim(path, "Xform")

    points_prim_path = f"{path}/points"
    points_prim = stage.DefinePrim(points_prim_path, "Points")
    points_schema = UsdGeom.Points(points_prim)

    points_schema.GetPointsAttr().Set(_numpy_to_vt_vec3f(points))

    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    min_point = Gf.Vec3f(float(mins[0]), float(mins[1]), float(mins[2]))
    max_point = Gf.Vec3f(float(maxs[0]), float(maxs[1]), float(maxs[2]))
    points_schema.GetExtentAttr().Set([min_point, max_point])

    return path


def create_match_visualization(
    path: str,
    transform: Gf.Matrix4d,
    radius: float,
) -> str:
    """Create an Xform prim with a child Sphere for match visualisation.

    Args:
        path: Desired prim path (will be made unique).
        transform: The full transform matrix.
        radius: Sphere radius.

    Returns:
        The final prim path.
    """
    stage = omni.usd.get_context().get_stage()
    path = omni.usd.get_stage_next_free_path(stage, path, True)

    xform_prim = stage.DefinePrim(path, "Xform")
    xform = UsdGeom.Xformable(xform_prim)
    xform.ClearXformOpOrder()

    transform_op = xform.AddTransformOp(UsdGeom.XformOp.PrecisionDouble)
    transform_op.Set(transform)
    xform.SetXformOpOrder([transform_op])

    sphere_child_path = f"{path}/visualization"
    sphere_prim = stage.DefinePrim(sphere_child_path, "Sphere")
    sphere_schema = UsdGeom.Sphere(sphere_prim)
    sphere_schema.GetRadiusAttr().Set(radius)

    color_attr = sphere_schema.CreateDisplayColorAttr()
    color_attr.Set([Gf.Vec3f(0.0, 1.0, 1.0)])

    xform_prim.SetInstanceable(False)
    sphere_prim.SetInstanceable(False)

    return path


def ensure_prim_exists(path: str, prim_type: str = "Xform") -> None:
    """Create a prim at *path* if it does not already exist."""
    stage = omni.usd.get_context().get_stage()
    if stage and not stage.GetPrimAtPath(path).IsValid():
        stage.DefinePrim(path, prim_type)


def delete_prim(prim_path: str) -> bool:
    """Remove a prim from the stage. Returns True if it existed and was removed."""
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return False
    if not stage.GetPrimAtPath(prim_path).IsValid():
        return False
    return bool(stage.RemovePrim(prim_path))


def compute_world_bbox(prim_path: str):
    """Return the prim's world-space axis-aligned bounds as ``(min, max)``.

    Both are length-3 float64 numpy arrays. Returns ``None`` if the prim is
    missing or has no computable extent (e.g. an empty Xform).
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return None
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )
    rng = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    if rng.IsEmpty():
        return None
    mn, mx = rng.GetMin(), rng.GetMax()
    return (
        np.array([mn[0], mn[1], mn[2]], dtype=np.float64),
        np.array([mx[0], mx[1], mx[2]], dtype=np.float64),
    )


def compute_world_bboxes(prim_paths: list) -> dict:
    """World AABBs for many prims via a single shared ``BBoxCache``.

    Returns ``{prim_path: (min, max)}`` for every prim with a computable
    extent. Much cheaper than calling :func:`compute_world_bbox` per prim
    (which builds a fresh cache each time), which matters when re-deriving
    the placement hot-swap over many placed matches.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage or not prim_paths:
        return {}
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )
    out: dict = {}
    for prim_path in prim_paths:
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            continue
        rng = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if rng.IsEmpty():
            continue
        mn, mx = rng.GetMin(), rng.GetMax()
        out[prim_path] = (
            np.array([mn[0], mn[1], mn[2]], dtype=np.float64),
            np.array([mx[0], mx[1], mx[2]], dtype=np.float64),
        )
    return out


def get_stage_up_axis() -> str:
    """Return the stage's up-axis token, ``"Y"`` or ``"Z"`` (default ``"Y"``)."""
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return "Y"
    try:
        axis = UsdGeom.GetStageUpAxis(stage)
        return "Z" if axis == UsdGeom.Tokens.z else "Y"
    except Exception:
        return "Y"


def set_prim_x_rotation(prim_path: str, degrees: float) -> None:
    """Set a single rotate-about-X xform op on a prim (replacing its op order).

    Used to reconcile a scan's up-axis with the stage's without touching the
    point geometry: the points stay in the source (Physna) frame, and placements
    — composed through this prim's world transform — rotate with it."""
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return
    xform = UsdGeom.Xformable(prim)
    if not xform:
        return
    xform.ClearXformOpOrder()
    if abs(degrees) > 1e-6:
        op = xform.AddRotateXOp(UsdGeom.XformOp.PrecisionDouble)
        op.Set(float(degrees))


def get_usd_meters_per_unit(usd_url: str) -> float:
    """Return the ``metersPerUnit`` authored in a USD file/URL (1.0 if unknown).

    Opens the layer read-only (no stage mutation) so we can normalize a
    referenced part to the stage's units — USD references do NOT auto-scale
    across differing ``metersPerUnit``, so an mm-authored part (0.001) dropped
    into a meter-scale stage renders 1000x oversized without this.
    """
    try:
        src = Usd.Stage.Open(usd_url)
    except Exception:
        _log.warning("Could not open %s to read metersPerUnit", usd_url)
        return 1.0
    if src is None:
        return 1.0
    try:
        mpu = float(UsdGeom.GetStageMetersPerUnit(src))
        return mpu if mpu > 0.0 else 1.0
    except Exception:
        return 1.0


def create_unique_xform(parent: str, name: str) -> Optional[str]:
    """Create an empty Xform prim under *parent*, uniquified against the stage.

    Returns the final prim path, or ``None`` if the stage is unavailable.
    Use this when you want a group-container that will have children
    attached separately — unlike ``create_xform_with_points``, this
    doesn't seed a ``/points`` child.
    """
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return None
    ensure_prim_exists(parent, "Xform")
    path = omni.usd.get_stage_next_free_path(stage, f"{parent}/{name}", True)
    stage.DefinePrim(path, "Xform")
    return path
