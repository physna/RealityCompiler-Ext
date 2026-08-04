# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct runs from the tenant's asset list.

A "run" on the platform is a folder that directly contains a ``scan``
asset (the scene); every ``model`` asset anywhere under that folder is a
part (the subfolder layout — ``USD/``, ``parts/<name>/``, ``objects/`` or
flat — doesn't matter). This lets the extension surface runs created
anywhere (another machine, a teammate, the reference script), not just
ones recorded locally.

Pure Python — no ``omni``/``pxr`` — so it's testable standalone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import TYPE_MODEL, TYPE_SCAN, Asset, is_queryable


def _folder_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


@dataclass
class DiscoveredRun:
    """A run reconstructed from platform assets (not yet downloaded)."""

    folder: str
    scene: Asset
    parts: list[Asset] = field(default_factory=list)        # queryable models
    supporting: list[Asset] = field(default_factory=list)   # deps (materials, …)

    @property
    def name(self) -> str:
        return self.folder.rsplit("/", 1)[-1] or self.folder

    @property
    def created_at(self) -> str:
        return self.scene.created_at

    @property
    def all_assets(self) -> list[Asset]:
        return [self.scene] + self.parts + self.supporting


def discover_runs(assets: list[Asset]) -> list[DiscoveredRun]:
    """Group a flat asset list into runs (newest first).

    Each ``scan`` in a folder starts a run; ``model`` assets are assigned
    to the deepest run folder that is a path prefix, so sibling runs like
    ``demo/warehouse`` and ``demo/warehouse-2`` don't bleed into each
    other. Queryable models become parts; the rest are supporting files
    (still downloaded so references resolve, but never scene-matched).
    """
    scene_by_folder: dict[str, Asset] = {}
    for a in assets:
        if a.type == TYPE_SCAN and a.path:
            folder = _folder_of(a.path)
            if folder:
                scene_by_folder[folder] = a

    runs = {f: DiscoveredRun(folder=f, scene=s) for f, s in scene_by_folder.items()}
    # Longest folder first so a nested run claims its assets before an
    # ancestor run would.
    folders_by_depth = sorted(runs.keys(), key=len, reverse=True)

    for a in assets:
        if a.type != TYPE_MODEL or not a.path:
            continue
        for folder in folders_by_depth:
            if a.path.startswith(folder + "/"):
                run = runs[folder]
                (run.parts if is_queryable(a.state) else run.supporting).append(a)
                break

    result = [r for r in runs.values() if r.parts]
    result.sort(key=lambda r: r.created_at, reverse=True)
    return result
