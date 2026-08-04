# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Scene operations package — stateless USD prim helpers.

Replaces ``managers/stage_manager.py``.  The :class:`SceneOps` facade
delegates to focused submodules while keeping the same call-site style
(``scene_ops.create_point_cloud_prim(...)``).
"""

from __future__ import annotations

import numpy as np
from typing import Optional, Tuple

from pxr import Gf

from . import transforms as _xform
from . import prim_ops as _ops
from . import point_extraction as _extract
from . import point_removal as _removal
from . import usd_deps as _deps

__all__ = ["SceneOps"]


class SceneOps:
    """Stateless facade for USD stage operations. Delegates to submodules."""

    # ------------------------------------------------------------------
    # Transforms
    # ------------------------------------------------------------------

    @staticmethod
    def get_world_xform(prim_path: str) -> Gf.Matrix4d:
        return _xform.get_world_xform(prim_path)

    @staticmethod
    def apply_xform_to_point(mat: Gf.Matrix4d, point: np.ndarray) -> np.ndarray:
        return _xform.apply_xform_to_point(mat, point)

    @staticmethod
    def avg_uniform_scale(mat: Gf.Matrix4d) -> float:
        return _xform.avg_uniform_scale(mat)

    @staticmethod
    def gf_matrix4d_to_numpy(mat: Gf.Matrix4d) -> np.ndarray:
        return _xform.gf_matrix4d_to_numpy(mat)

    @staticmethod
    def numpy_to_gf_matrix4d(m: np.ndarray) -> Gf.Matrix4d:
        return _xform.numpy_to_gf_matrix4d(m)

    @staticmethod
    def safe_identifier(name: str) -> str:
        return _xform.safe_identifier(name)

    @staticmethod
    def to_asset_url(path_or_url: str) -> str:
        return _xform.to_asset_url(path_or_url)

    @staticmethod
    def compute_parent_local_transform(
        pose: np.ndarray,
        actual_points_prim_path: str,
        parent_path: str,
    ) -> Gf.Matrix4d:
        return _xform.compute_parent_local_transform(
            pose,
            actual_points_prim_path,
            parent_path,
        )

    @staticmethod
    def transform_points_to_parent_local(
        instance_points: np.ndarray,
        pose: np.ndarray,
        actual_points_prim_path: str,
        parent_path: str,
    ) -> np.ndarray:
        return _xform.transform_points_to_parent_local(
            instance_points,
            pose,
            actual_points_prim_path,
            parent_path,
        )

    @staticmethod
    def resolve_import_parent_path(
        actual_points_prim_path: Optional[str],
        scene_prim_path: str,
    ) -> str:
        return _xform.resolve_import_parent_path(
            actual_points_prim_path,
            scene_prim_path,
        )

    @staticmethod
    def resolve_sibling_parent_path(
        actual_points_prim_path: Optional[str],
        scene_prim_path: str,
    ) -> str:
        return _xform.resolve_sibling_parent_path(
            actual_points_prim_path,
            scene_prim_path,
        )

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    @staticmethod
    def compute_usd_upload_set(usd_path: str):
        """Return the ordered (root-first) upload set for a USD part.

        See :func:`.usd_deps.compute_usd_upload_set`.
        """
        return _deps.compute_usd_upload_set(usd_path)

    @staticmethod
    def get_selected_prim_path() -> Optional[str]:
        return _ops.get_selected_prim_path()

    @staticmethod
    def get_selected_prim_paths() -> list:
        return _ops.get_selected_prim_paths()

    @staticmethod
    def export_prim_to_usd(prim_path: str, out_path: str) -> bool:
        return _ops.export_prim_to_usd(prim_path, out_path)

    @staticmethod
    def get_prim_point_count(prim_path: str) -> Optional[int]:
        return _ops.get_prim_point_count(prim_path)

    # ------------------------------------------------------------------
    # Point extraction
    # ------------------------------------------------------------------

    @staticmethod
    async def extract_point_cloud(
        prim_path: str,
        track_actual_prim: bool = False,
    ) -> Optional[Tuple[np.ndarray, Optional[np.ndarray], Optional[str]]]:
        return await _extract.extract_point_cloud(prim_path, track_actual_prim)

    # ------------------------------------------------------------------
    # Prim creation
    # ------------------------------------------------------------------

    @staticmethod
    def create_point_cloud_prim(
        points: np.ndarray,
        colors: Optional[np.ndarray],
        name: str,
        parent: str,
        width: Optional[float] = None,
    ) -> Optional[str]:
        return _ops.create_point_cloud_prim(points, colors, name, parent, width)

    @staticmethod
    def get_points_prim_sample_width(path: str) -> Optional[float]:
        return _ops.get_points_prim_sample_width(path)

    @staticmethod
    def create_highlight_points(
        points: np.ndarray,
        parent_path: str,
        name: str,
    ) -> Optional[str]:
        return _ops.create_highlight_points(points, parent_path, name)

    @staticmethod
    def create_xform_with_reference(
        path: str,
        transform: Gf.Matrix4d,
        url: str,
    ) -> str:
        return _ops.create_xform_with_reference(path, transform, url)

    @staticmethod
    def create_xform_with_points(path: str, points: np.ndarray) -> str:
        return _ops.create_xform_with_points(path, points)

    @staticmethod
    def create_match_visualization(
        path: str,
        transform: Gf.Matrix4d,
        radius: float,
    ) -> str:
        return _ops.create_match_visualization(path, transform, radius)

    @staticmethod
    def ensure_prim_exists(path: str, prim_type: str = "Xform") -> None:
        _ops.ensure_prim_exists(path, prim_type)

    @staticmethod
    def create_unique_xform(parent: str, name: str) -> Optional[str]:
        return _ops.create_unique_xform(parent, name)

    @staticmethod
    def get_usd_meters_per_unit(usd_url: str) -> float:
        """The ``metersPerUnit`` authored in a USD file/URL (1.0 if unknown)."""
        return _ops.get_usd_meters_per_unit(usd_url)

    @staticmethod
    def get_stage_up_axis() -> str:
        """The stage's up-axis token, ``"Y"`` or ``"Z"``."""
        return _ops.get_stage_up_axis()

    @staticmethod
    def set_prim_x_rotation(prim_path: str, degrees: float) -> None:
        """Set a single rotate-about-X xform op on a prim (up-axis correction)."""
        _ops.set_prim_x_rotation(prim_path, degrees)

    # ------------------------------------------------------------------
    # Point removal
    # ------------------------------------------------------------------

    @staticmethod
    def remove_points_near(
        prim_path: str,
        center: np.ndarray,
        radius: float,
    ) -> int:
        return _removal.remove_points_near(prim_path, center, radius)

    @staticmethod
    async def remove_points_near_async(
        prim_path: str,
        center: np.ndarray,
        radius: float,
    ) -> int:
        return await _removal.remove_points_near_async(prim_path, center, radius)

    @staticmethod
    def compute_world_bbox(prim_path: str):
        """World-space ``(min, max)`` bounds for a prim, or ``None``."""
        return _ops.compute_world_bbox(prim_path)

    @staticmethod
    def delete_prim(prim_path: str) -> bool:
        return _ops.delete_prim(prim_path)

    @staticmethod
    def create_xforms_with_references(items: list) -> list:
        """Author many (base_path, transform, url) Xforms in one batch pass."""
        return _ops.create_xforms_with_references(items)

    @staticmethod
    def delete_prims(prim_paths: list) -> None:
        """Remove many prims in one batch pass (caller yields once after)."""
        return _ops.delete_prims(prim_paths)

    @staticmethod
    async def remove_points_in_world_boxes_async(
        prim_path: str,
        boxes: list,
        keep_inside: bool = False,
    ) -> int:
        return await _removal.remove_points_in_world_boxes_async(
            prim_path, boxes, keep_inside
        )

    # ------------------------------------------------------------------
    # Reversible point occlusion (placement hot-swap)
    # ------------------------------------------------------------------

    @staticmethod
    def is_points_prim(prim_path: str) -> bool:
        """True if ``prim_path`` is a valid ``UsdGeom.Points`` prim."""
        return _removal.is_points_prim(prim_path)

    @staticmethod
    def read_points(prim_path: str):
        """Snapshot ``(local_points, colors|None)`` for later occlusion."""
        return _removal.read_points(prim_path)

    @staticmethod
    def points_world_matrix(prim_path: str) -> np.ndarray:
        """The Points prim's local-to-world 4x4 (read USD; main thread)."""
        return _removal.points_world_matrix(prim_path)

    @staticmethod
    def apply_world_matrix(local_points, world_mat) -> np.ndarray:
        """Project local points to world (pure numpy; thread-safe)."""
        return _removal.apply_world_matrix(local_points, world_mat)

    @staticmethod
    def cover_delta(world, cover, add_boxes: list, remove_boxes: list):
        """Update per-point box-cover counts (pure numpy; thread-safe)."""
        return _removal.cover_delta(world, cover, add_boxes, remove_boxes)

    @staticmethod
    def compute_world_bboxes(prim_paths: list) -> dict:
        """World AABBs for many prims via one shared BBoxCache."""
        return _ops.compute_world_bboxes(prim_paths)

    @staticmethod
    async def write_points_mask_async(
        prim_path: str, snapshot_points, snapshot_colors, keep_mask
    ) -> int:
        """Author the snapshot filtered by ``keep_mask`` (None = keep all)."""
        return await _removal.write_points_mask_async(
            prim_path, snapshot_points, snapshot_colors, keep_mask
        )

    @staticmethod
    def read_point_widths(prim_path: str):
        """Snapshot ``(widths, interpolation)`` to restore after width-hiding."""
        return _removal.read_point_widths(prim_path)

    @staticmethod
    async def set_point_visibility_async(
        prim_path: str, visible_mask, visible_width: Optional[float]
    ) -> int:
        """Hide/show points via per-point widths (0 = hidden; count constant)."""
        return await _removal.set_point_visibility_async(
            prim_path, visible_mask, visible_width
        )

    @staticmethod
    async def clear_point_visibility_async(
        prim_path: str, orig_widths=None, orig_interp=None
    ) -> None:
        """Restore the prim's original widths verbatim (undo width-hiding)."""
        return await _removal.clear_point_visibility_async(
            prim_path, orig_widths, orig_interp
        )
