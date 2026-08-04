# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Discover the files a USD asset needs so they upload as a resolvable set.

A USD part often references sublayers, payloads, and material/texture
assets by *relative* path. Uploading only the root ``.usd`` — or uploading
every file flattened into one folder — breaks those relative references on
the platform. :func:`compute_usd_upload_set` walks the composition graph
and returns each dependency paired with a path relative to a common base,
so the caller can re-create the same directory layout under the tenant
folder and let the relative references resolve.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..logger import get_logger

_log = get_logger("physna.reality_compiler.scene.usd_deps")


@dataclass
class UploadItem:
    """One file to upload for a part, with its path relative to the set root."""

    local_path: str   # absolute path on disk
    rel_path: str     # forward-slash path relative to the common base
    is_root: bool     # True for the part's own root USD (the queryable asset)


# USD document extensions — the only dependencies we upload. Textures
# (.png/.jpg), MDL shaders (.mdl), volumes, etc. are NOT geometry the
# scan-search API accepts: it rejects them (400/204), and matching is
# geometry-only, so a part still indexes fine (missing-dependencies is
# tolerated) without them.
_USD_LAYER_EXTS = {".usd", ".usda", ".usdc", ".usdz"}


def _to_posix_rel(path: str, base: str) -> str:
    return os.path.relpath(path, base).replace(os.sep, "/")


def compute_usd_upload_set(usd_path: str) -> list[UploadItem]:
    """Return the ordered upload set for a USD part (root first).

    Only the root USD and its USD-family *layer* dependencies (sublayers /
    references / payloads) are included — never textures, MDL shaders, or
    other binary assets, and never layers outside the part's own directory
    tree (e.g. Kit's built-in material libraries pulled in by reference).

    Falls back to just the root file (flat) if dependency analysis is
    unavailable or the files don't share a sane common base.
    """
    usd_path = os.path.abspath(usd_path)
    root_dir = os.path.dirname(usd_path)
    root_only = [UploadItem(usd_path, os.path.basename(usd_path), True)]

    try:
        from pxr import Sdf, UsdUtils
    except Exception:
        return root_only

    try:
        # Returns (layers, assets, unresolvedPaths). Layers are the USD
        # documents; assets are non-layer deps (textures, MDL) we skip.
        result = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(usd_path))
        layers = result[0]
    except Exception:
        _log.exception("ComputeAllDependencies failed for %s; uploading flat", usd_path)
        return root_only

    def _near_root(real: str) -> bool:
        """True if *real* is close to the part (not a far-off system lib).

        Keeps project-local layers — including parent-relative siblings
        like ``../Materials/Materials.usd`` — while rejecting layers on
        another drive or many levels up the tree (e.g. Kit's built-in
        materials under the install dir).
        """
        try:
            anc = os.path.commonpath([real, root_dir])
        except ValueError:
            return False  # different drive
        rel = os.path.relpath(root_dir, anc)
        depth = 0 if rel == os.curdir else len(rel.split(os.sep))
        return depth <= 3

    paths: set[str] = {usd_path}
    skipped_outside = 0
    for layer in layers or []:
        real = getattr(layer, "realPath", "") or ""
        if not real:
            continue
        real = os.path.abspath(real)
        if real == usd_path or not os.path.isfile(real):
            continue
        if os.path.splitext(real)[1].lower() not in _USD_LAYER_EXTS:
            continue
        if not _near_root(real):
            skipped_outside += 1
            continue
        paths.add(real)

    if skipped_outside:
        _log.info(
            "Skipped %d out-of-tree USD dependency layer(s) for %s",
            skipped_outside, os.path.basename(usd_path),
        )

    existing = sorted(paths)
    if len(existing) <= 1:
        return root_only

    # Prefer the root USD's own directory as the base, so the root sits
    # DIRECTLY under the part folder (parts/<name>/root.usd) and its
    # children go in subfolders (parts/<name>/Materials/…). Only fall
    # back to the deepest shared ancestor when a dependency is a
    # parent-relative sibling (../Materials/…) that would otherwise
    # escape the part folder.
    def _escapes(p: str, base: str) -> bool:
        return os.path.relpath(p, base).split(os.sep, 1)[0] == os.pardir

    if not any(_escapes(p, root_dir) for p in existing):
        base = root_dir
    else:
        try:
            base = os.path.commonpath(existing)
            if os.path.isfile(base):
                base = os.path.dirname(base)
        except ValueError:
            return root_only

    items = [
        UploadItem(p, _to_posix_rel(p, base), p == usd_path) for p in existing
    ]
    # Root first so callers can treat items[0] as the queryable part.
    items.sort(key=lambda it: (not it.is_root, it.rel_path))
    return items
