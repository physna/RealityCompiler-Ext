# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared, cached state for the hosted scan-search workflow.

Holds the user's current selection (scene + parts), the assets created
on the platform, and the placements returned for each part.  No ``omni``
imports beyond ``pxr`` types so it stays a plain data container.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..api import Asset, Match

# Runs live at the tenant root by default, in a folder named after the
# scene (editable by the user). An optional prefix can namespace them.
DEFAULT_FOLDER_ROOT = ""


# eq=False: identity equality. Two queued entries for the same file must stay
# distinct (list lookups, upload folders), and value comparison could reach
# Match.transform4x4 — an ndarray, whose == raises in a dataclass comparison.
@dataclass(eq=False)
class PartEntry:
    """One catalog part queued for (or resolved by) a search."""

    source_path: str            # local file uploaded for this part (USD/CAD)
    display_name: str           # label shown in the UI
    asset: Optional[Asset] = None          # created platform asset (queryable root)
    # Dependency uploads (sublayers, materials, textures) for a USD part.
    # Uploaded and polled so references resolve, but never scene-matched.
    supporting_assets: list[Asset] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)  # placements in scene
    # Set when reading scene-matches failed for this part, so the UI can show
    # the error instead of presenting an empty result as "no matches".
    match_error: Optional[str] = None
    # True while the platform may still hold matches this client hasn't read
    # (relationship computing, read timed out or errored). Drives whether the
    # run record is saved complete — an unread part keeps it resumable.
    matches_pending: bool = False
    placed_prim_paths: list[str] = field(default_factory=list)  # created prims
    # Local USD reference used for placement. For USD parts this is the
    # source file; for CAD parts it's a converted-to-USD copy (resolved
    # lazily at placement time). None until resolved.
    placement_url: Optional[str] = None
    # Max number of top-scoring matches to place (None = all above the
    # global min-score threshold). Set per-part in the Results UI.
    import_limit: Optional[int] = None
    # Scale to real-world metres for placement (Physna's match transform is
    # rigid — no scale). USD parts: authored metersPerUnit; converted CAD:
    # assumed mm (0.001). Resolved with placement_url; 1.0 until then.
    auto_scale: float = 1.0

    @property
    def state(self) -> str:
        return self.asset.state if self.asset else "pending"


@dataclass
class SceneSource:
    """Where the scene point cloud comes from for this run."""

    # A local point-cloud file to upload (.ply/.e57/.pcd/.npy/.npz).
    file_path: Optional[str] = None
    # The stage prim the scene lives under (needed to place matches back).
    prim_path: Optional[str] = None
    # The actual Points prim path (may be a child of ``prim_path``); used
    # to compose placement transforms.  Set when a scene is extracted.
    actual_points_prim_path: Optional[str] = None
    # Assumed scan up-axis ("z" default, "y", or "none"), reconciled to the
    # stage by rotating the scene prim's xform — geometry stays in Physna's
    # frame and placements rotate with it.
    up_axis: str = "z"

    @property
    def is_set(self) -> bool:
        return bool(self.file_path or self.prim_path)


@dataclass
class PipelineState:
    """Everything one search run needs, cached between UI callbacks."""

    # Optional prefix to namespace runs under; empty = tenant root.
    folder_root: str = DEFAULT_FOLDER_ROOT
    # User-facing run name (editable; pre-populated from the scene). The
    # scene + parts are colocated under ``folder_root/run_name``.
    run_name: str = ""
    run_folder: Optional[str] = None  # resolved folder for this run

    scene: SceneSource = field(default_factory=SceneSource)
    scene_asset: Optional[Asset] = None

    parts: list[PartEntry] = field(default_factory=list)

    # Only matches at or above this score (0–100) get placed in the stage.
    min_score: float = 0.0

    # Pristine snapshot of the scene Points prim - ``(local_points, colors)`` -
    # captured lazily the first time the placement hot-swap hides scan points
    # behind placed matches, so the hidden points can always be re-revealed.
    # ``None`` until captured; invalidated whenever the scene changes.
    scene_points_backup: Optional[tuple] = None

    def reset(self) -> None:
        """Clear scene + parts + results back to defaults (keep folder_root)."""
        self.run_name = ""
        self.run_folder = None
        self.scene = SceneSource()
        self.scene_asset = None
        self.parts = []
        self.scene_points_backup = None

    def clear_results(self) -> None:
        """Drop matches + created assets but keep the user's selection."""
        self.run_folder = None
        self.scene_asset = None
        self.scene_points_backup = None
        for part in self.parts:
            part.asset = None
            part.supporting_assets = []
            part.matches = []
            part.match_error = None
            part.matches_pending = False
            part.placed_prim_paths = []
            part.placement_url = None
            part.auto_scale = 1.0
