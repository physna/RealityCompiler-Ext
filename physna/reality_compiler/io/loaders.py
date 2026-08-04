# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Point-cloud file loaders for NPY, NPZ, E57, LAS/LAZ, PLY/PCD, XYZ/PTS.

Used to bring a point cloud into the Omniverse stage as a ``Points`` prim
(for viewing, and to give reloaded runs a scene to place matches against).

Every loader returns ``(points, colors, intensity, metadata)`` where
``points`` is ``Nx3 float32`` and ``colors`` (when present) is a canonical
``Nx3 uint8`` array (see :mod:`._color_norm`). Only ``numpy`` is required
(it also covers ``.npy``/``.npz`` and ASCII ``.xyz``/``.pts``); ``laspy``
(+``lazrs`` for LAZ), ``pye57`` (E57), ``trimesh`` (PLY), and ``pypcd4`` (PCD)
are imported lazily and only when those formats are loaded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

from ..logger import get_logger
from ._color_norm import normalise_colors_to_uint8

_log = get_logger("physna.reality_compiler.io.loaders")

MAX_REASONABLE_COORD = 1e10
MIN_REASONABLE_COORD = -1e10
LARGE_POINT_CLOUD_WARN_THRESHOLD = 10_000_000
# Refuse to load files above this size before reading a byte - a guard against a
# hostile/corrupt "point cloud" OOMing or pinning the app (the warn threshold
# above only fires *after* loading, so it doesn't protect anything).
MAX_LOAD_FILE_BYTES = 2 * 1024**3  # 2 GB
# A plain ``.npy`` scan carries its colour/segmentation in co-located
# ``color.npy``/``segmentation.npy`` sidecars - this is the canonical Physna
# scan layout, so we load them. Both sidecars are read with ``allow_pickle=False``
# (no code-exec risk); a tampered sidecar can at worst mis-colour the display,
# and that already requires write access to the directory being loaded.
LOAD_NPY_SIDECARS = True

LoadResult = Tuple[np.ndarray, Optional[np.ndarray], Optional[np.ndarray], Dict]


def _finite_coord_mask(points: np.ndarray) -> np.ndarray:
    """Boolean mask of finite, in-range Nx3 coordinates (drops NaN/inf/absurd)."""
    return (
        np.isfinite(points).all(axis=1)
        & (points > MIN_REASONABLE_COORD).all(axis=1)
        & (points < MAX_REASONABLE_COORD).all(axis=1)
    )


# ---------------------------------------------------------------------------
# LAS / LAZ
# ---------------------------------------------------------------------------


def _load_las_laz(file_path: Path) -> LoadResult:
    try:
        import laspy
    except ImportError as exc:
        raise ImportError(
            "laspy is required to load LAS/LAZ files (pip install laspy). "
            "Reading .laz also needs a decompression backend (pip install lazrs)."
        ) from exc

    with laspy.open(str(file_path)) as f:
        _log.debug("Loading %s points...", f"{f.header.point_count:,}")
        las = f.read()

    # Assign into a preallocated float32 array rather than column_stack-then-cast,
    # which would transiently hold a full float64 copy of a possibly huge cloud.
    n = len(las.x)
    points = np.empty((n, 3), dtype=np.float32)
    points[:, 0] = las.x
    points[:, 1] = las.y
    points[:, 2] = las.z

    colors = None
    if hasattr(las, "red") and hasattr(las, "green") and hasattr(las, "blue"):
        colors = normalise_colors_to_uint8(
            np.column_stack([las.red, las.green, las.blue])
        )

    intensity = None
    if hasattr(las, "intensity"):
        intensity = np.asarray(las.intensity, dtype=np.float32)

    # Drop non-finite / absurd coordinates (mirrors the E57 path); LAS no-data
    # and garbage points otherwise pass straight through.
    valid = _finite_coord_mask(points)
    if not valid.all():
        _log.warning("Filtering out %s invalid LAS/LAZ points", f"{int((~valid).sum()):,}")
        points = points[valid]
        if colors is not None:
            colors = colors[valid]
        if intensity is not None:
            intensity = intensity[valid]
    if len(points) == 0:
        raise ValueError(f"No valid points in {file_path.name}")
    if len(points) > LARGE_POINT_CLOUD_WARN_THRESHOLD:
        _log.warning("Very large point cloud (%s points).", f"{len(points):,}")

    metadata = {
        "source_format": file_path.suffix.lower(),
        "point_count": len(points),
        "has_colors": colors is not None,
        "has_intensity": intensity is not None,
    }
    return points, colors, intensity, metadata


# ---------------------------------------------------------------------------
# E57
# ---------------------------------------------------------------------------


def _load_e57(file_path: Path, scan_index: int = 0) -> LoadResult:
    # pye57 segfaults inside Kit's process, so the read runs in an isolated
    # subprocess — see e57_isolated.py for the full story.
    from .e57_isolated import read_e57_to_npz

    _log.info("E57: reading %s in an isolated process (pye57)...", file_path.name)
    npz_path = read_e57_to_npz(file_path, scan_index=scan_index)
    try:
        with np.load(npz_path, allow_pickle=False) as z:
            points = np.asarray(z["points"], dtype=np.float32)
            colors_raw = np.asarray(z["colors"])
            intensity_raw = np.asarray(z["intensity"])
            scan_count = (
                int(z["scan_count"][0]) if "scan_count" in z.files else 1
            )
    finally:
        try:
            os.remove(npz_path)
        except Exception:
            pass

    if len(points) == 0:
        raise ValueError("E57 file contains no valid points after filtering")
    _log.info("E57: %s read OK (%s points)", file_path.name, f"{len(points):,}")
    if len(points) > LARGE_POINT_CLOUD_WARN_THRESHOLD:
        _log.warning("Very large point cloud (%s points).", f"{len(points):,}")

    colors: Optional[np.ndarray] = None
    if colors_raw.size:
        try:
            colors = normalise_colors_to_uint8(colors_raw)
        except Exception as e:
            _log.warning("Could not normalize E57 colors: %s", e)

    intensity: Optional[np.ndarray] = None
    if intensity_raw.size:
        intensity = intensity_raw.astype(np.float32)

    metadata = {
        "source_format": ".e57",
        "point_count": len(points),
        "scan_count": scan_count,
        "scan_index": scan_index,
        "has_colors": colors is not None,
        "has_intensity": intensity is not None,
    }
    return points, colors, intensity, metadata


# ---------------------------------------------------------------------------
# PLY (trimesh) / PCD (pypcd4)
# ---------------------------------------------------------------------------


def _load_ply(file_path: Path) -> LoadResult:
    try:
        import trimesh
    except ImportError as exc:
        raise ImportError(
            "trimesh is required to load PLY files (pip install trimesh)."
        ) from exc

    loaded = trimesh.load(str(file_path), process=False)
    verts = getattr(loaded, "vertices", None)
    if verts is None or len(verts) == 0:
        raise ValueError(f"No points found in {file_path.name}")
    points = np.asarray(verts, dtype=np.float32)

    # PointCloud exposes .colors (Nx4 RGBA); a mesh exposes visual.vertex_colors.
    raw = getattr(loaded, "colors", None)
    if raw is None or len(raw) == 0:
        visual = getattr(loaded, "visual", None)
        raw = getattr(visual, "vertex_colors", None) if visual is not None else None
    colors = None
    if raw is not None and len(raw) == len(points):
        colors = normalise_colors_to_uint8(np.asarray(raw)[:, :3])

    metadata = {
        "source_format": ".ply",
        "point_count": len(points),
        "has_colors": colors is not None,
        "has_intensity": False,
    }
    return points, colors, None, metadata


def _load_pcd(file_path: Path) -> LoadResult:
    try:
        from pypcd4 import PointCloud
    except ImportError as exc:
        raise ImportError(
            "pypcd4 is required to load PCD files (pip install pypcd4)."
        ) from exc

    pc = PointCloud.from_path(str(file_path))
    points = np.asarray(pc.numpy(("x", "y", "z")), dtype=np.float32).reshape(-1, 3)
    if points.size == 0:
        raise ValueError(f"No points found in {file_path.name}")

    colors = None
    if "rgb" in set(pc.fields):
        try:
            # PCL packs RGB into a float32; reinterpret the bits as uint32.
            packed = np.ascontiguousarray(pc.numpy(("rgb",)), dtype=np.float32).ravel()
            rgb = packed.view(np.uint32)
            channels = np.column_stack([
                (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
            ])
            colors = normalise_colors_to_uint8(channels)
        except Exception as exc:  # best-effort; points still load
            _log.warning("Could not decode PCD rgb: %s", exc)

    metadata = {
        "source_format": ".pcd",
        "point_count": len(points),
        "has_colors": colors is not None,
        "has_intensity": False,
    }
    return points, colors, None, metadata


# ---------------------------------------------------------------------------
# NPZ / NPY
# ---------------------------------------------------------------------------


def _segment_labels_to_colors(labels: np.ndarray) -> np.ndarray:
    unique_labels = np.unique(labels)
    n = max(len(unique_labels), 1)
    hsv = np.empty((n, 3), dtype=np.float64)
    hsv[:, 0] = np.arange(n, dtype=np.float64) / n
    hsv[:, 1] = 0.85
    hsv[:, 2] = 0.95
    try:
        from matplotlib.colors import hsv_to_rgb as _mpl_hsv_to_rgb

        palette = _mpl_hsv_to_rgb(hsv)
    except Exception:
        import colorsys

        palette = np.array(
            [colorsys.hsv_to_rgb(h, s, v) for h, s, v in hsv], dtype=np.float64
        )
    indices = np.searchsorted(unique_labels, labels)
    return normalise_colors_to_uint8(palette[indices].astype(np.float32))


def _load_npz(file_path: Path, color_mode: str = "color") -> LoadResult:
    # allow_pickle=False: never unpickle from a user-picked file (pickle =
    # arbitrary code execution). Metadata is carried as a JSON string.
    data = np.load(file_path, allow_pickle=False)
    keys = set(data.files)
    points = np.asarray(data["points"], dtype=np.float32)

    file_colors = data["colors"] if "colors" in keys and data["colors"].size else None
    intensity = data["intensity"] if "intensity" in keys and data["intensity"].size else None

    seg_colors: Optional[np.ndarray] = None
    has_segmentation = False
    if "segmentation" in keys and data["segmentation"].size:
        seg = data["segmentation"].ravel().astype(np.float32)
        if len(seg) == len(points):
            has_segmentation = True
            if intensity is None:
                intensity = seg
            seg_colors = _segment_labels_to_colors(seg)

    if color_mode == "segmentation" and seg_colors is not None:
        colors = seg_colors
    elif file_colors is not None:
        colors = normalise_colors_to_uint8(file_colors)
    elif seg_colors is not None:
        colors = seg_colors
    else:
        colors = None

    metadata: Dict = {}
    if "metadata_json" in keys:
        try:
            metadata = json.loads(str(data["metadata_json"]))
        except Exception:
            metadata = {}
    metadata.setdefault("source_format", file_path.suffix.lower())
    metadata.setdefault("point_count", len(points))
    if has_segmentation:
        metadata["has_segmentation"] = True
    metadata["color_mode"] = color_mode
    return points, colors, intensity, metadata


def _load_npy_color(colors_path: Path, n: int) -> Optional[np.ndarray]:
    raw = np.load(colors_path, allow_pickle=False)
    if raw.ndim == 2 and raw.shape[1] >= 3 and raw.shape[0] == n:
        return normalise_colors_to_uint8(raw[:, :3])
    if raw.ndim == 2 and raw.shape[0] >= 3 and raw.shape[1] == n:
        return normalise_colors_to_uint8(raw.T[:, :3])
    _log.warning("Ignoring %s — unexpected shape %s", colors_path.name, raw.shape)
    return None


def _load_npy(file_path: Path, color_mode: str = "color") -> LoadResult:
    points = np.load(file_path).astype(np.float32)
    if points.ndim == 1 and points.size % 3 == 0:
        points = points.reshape((-1, 3))
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(
            f"Expected Nx3 points array, got shape {points.shape} from {file_path.name}"
        )

    n = len(points)
    parent = file_path.parent
    intensity: Optional[np.ndarray] = None
    seg_colors: Optional[np.ndarray] = None
    color_file_colors: Optional[np.ndarray] = None

    # Co-located sidecars are opt-in (see LOAD_NPY_SIDECARS) - don't silently
    # ingest files just because they share the .npy's directory.
    if LOAD_NPY_SIDECARS:
        seg_path = parent / "segmentation.npy"
        if seg_path.exists():
            raw = np.load(seg_path, allow_pickle=False)
            if raw.ndim == 1 and len(raw) == n:
                intensity = raw.astype(np.float32)
                seg_colors = _segment_labels_to_colors(intensity)
            elif raw.ndim == 2 and raw.shape[0] == n and raw.shape[1] >= 3:
                intensity = raw[:, 0].astype(np.float32)
                seg_colors = normalise_colors_to_uint8(raw[:, :3])

        colors_path = parent / "color.npy"
        if colors_path.exists():
            color_file_colors = _load_npy_color(colors_path, n)

    if color_mode == "segmentation" and seg_colors is not None:
        colors = seg_colors
    elif color_file_colors is not None:
        colors = color_file_colors
    elif seg_colors is not None:
        colors = seg_colors
    else:
        colors = None

    metadata = {
        "source_format": ".npy",
        "point_count": n,
        "has_colors": colors is not None,
        "has_intensity": intensity is not None,
        "color_mode": color_mode,
    }
    return points, colors, intensity, metadata


# ---------------------------------------------------------------------------
# XYZ / PTS (ASCII)
# ---------------------------------------------------------------------------


def _load_xyz_pts(file_path: Path) -> LoadResult:
    """Load a whitespace-delimited ASCII point cloud (.xyz / .pts).

    Each row is ``x y z`` with optional trailing columns; a leading bare
    point-count line (the PTS convention) is skipped. Extra columns map to:
    4 -> +intensity, 6 -> +rgb, >=7 -> +intensity +rgb.
    """
    skip = 0
    with open(file_path, "r") as fh:
        # Cap the peek so a crafted no-newline blob can't pull the whole file
        # into one string just to detect a header.
        first = fh.readline(4096).split()
    # PTS files begin with a bare point count on its own line.
    if len(first) == 1 and first[0].lstrip("-").isdigit():
        skip = 1

    try:
        rows = np.loadtxt(file_path, comments=("#", "//"), skiprows=skip, ndmin=2)
    except Exception as exc:
        raise ValueError(f"Could not parse {file_path.name} as XYZ/PTS: {exc}")
    if rows.size == 0 or rows.shape[1] < 3:
        raise ValueError(f"No X Y Z columns found in {file_path.name}")

    points = np.ascontiguousarray(rows[:, :3], dtype=np.float32)
    ncol = rows.shape[1]
    colors = None
    intensity = None
    if ncol == 4:
        intensity = rows[:, 3].astype(np.float32)
    elif ncol == 6:
        colors = normalise_colors_to_uint8(rows[:, 3:6])
    elif ncol >= 7:
        intensity = rows[:, 3].astype(np.float32)
        colors = normalise_colors_to_uint8(rows[:, 4:7])

    if len(points) > LARGE_POINT_CLOUD_WARN_THRESHOLD:
        _log.warning("Very large point cloud (%s points).", f"{len(points):,}")
    metadata = {
        "source_format": file_path.suffix.lower(),
        "point_count": len(points),
        "has_colors": colors is not None,
        "has_intensity": intensity is not None,
    }
    return points, colors, intensity, metadata


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    ".npy", ".npz", ".e57", ".las", ".laz", ".ply", ".pcd", ".xyz", ".pts",
}


def load_point_cloud(
    file_path: "str | Path",
    e57_scan_index: int = 0,
    color_mode: str = "color",
) -> LoadResult:
    """Load a point-cloud file into ``(points, colors, intensity, metadata)``.

    Supports ``.npy``/``.npz`` and ASCII ``.xyz``/``.pts`` (numpy), ``.e57``
    (pye57), ``.las``/``.laz`` (laspy), ``.ply`` (trimesh), and ``.pcd``
    (pypcd4). Raises ``ValueError`` for an unsupported extension or an
    oversized file, ``ImportError`` if the format's dependency isn't installed.
    """
    file_path = Path(file_path)
    try:
        size = os.path.getsize(file_path)
    except OSError:
        size = 0
    if size > MAX_LOAD_FILE_BYTES:
        raise ValueError(
            f"{file_path.name} is {size / 1024**3:.1f} GB, over the "
            f"{MAX_LOAD_FILE_BYTES / 1024**3:.0f} GB point-cloud load limit."
        )
    suffix = file_path.suffix.lower()
    if suffix in {".las", ".laz"}:
        return _load_las_laz(file_path)
    if suffix == ".e57":
        return _load_e57(file_path, scan_index=e57_scan_index)
    if suffix == ".ply":
        return _load_ply(file_path)
    if suffix == ".pcd":
        return _load_pcd(file_path)
    if suffix in {".xyz", ".pts"}:
        return _load_xyz_pts(file_path)
    if suffix == ".npz":
        return _load_npz(file_path, color_mode=color_mode)
    if suffix == ".npy":
        return _load_npy(file_path, color_mode=color_mode)
    raise ValueError(f"Unsupported point-cloud format: {suffix}")
