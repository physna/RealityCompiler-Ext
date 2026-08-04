# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Typed views over the Scan Search API's JSON payloads.

Kept deliberately thin: each model is a ``@dataclass`` with a
``from_json`` classmethod that tolerates the extra fields the API may
add over time.  No ``omni``/``pxr`` imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

# ---------------------------------------------------------------------------
# Asset states (per the platform's asset lifecycle)
# ---------------------------------------------------------------------------

# Non-terminal states — keep polling while an asset reports one of these.
WORKING_STATES: frozenset[str] = frozenset({"indexing", "generating"})

# Representative non-terminal state to seed an asset with when its real state
# isn't known yet (e.g. reconstructing an interrupted run from disk before the
# first resume poll reports the truth). Any WORKING_STATES member works.
DEFAULT_WORKING_STATE = "indexing"

# Terminal states — indexing has stopped, for better or worse.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"finished", "failed", "unsupported", "no-3d-data", "missing-dependencies"}
)

# States in which an asset is actually queryable for scene matches.
QUERYABLE_STATES: frozenset[str] = frozenset({"finished", "missing-dependencies"})

# The only acceptable terminal state for a scene: without it there is
# nothing to match parts against.
SCENE_REQUIRED_STATE = "finished"

# Asset ``type`` values inferred by the API from the file extension.
TYPE_SCAN = "scan"   # point clouds -> the scene
TYPE_MODEL = "model"  # CAD/USD -> the parts


def is_working(state: str | None) -> bool:
    return (state or "") in WORKING_STATES


def is_terminal(state: str | None) -> bool:
    return (state or "") in TERMINAL_STATES


def is_queryable(state: str | None) -> bool:
    return (state or "") in QUERYABLE_STATES


@dataclass
class Asset:
    """An asset as returned by ``POST``/``GET`` on the assets collection."""

    id: str
    state: str
    path: str = ""
    type: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "Asset":
        # ``GET /assets/{id}`` nests the asset under an "asset" key; the
        # upload response returns it flat. Accept either shape.
        body = payload.get("asset", payload) if isinstance(payload, Mapping) else payload
        return cls(
            id=str(body.get("id", "")),
            state=str(body.get("state", "")),
            path=str(body.get("path", "")),
            type=str(body.get("type", "")),
            created_at=str(body.get("createdAt", "")),
            metadata=dict(body.get("metadata") or {}),
            raw=dict(body),
        )

    @property
    def name(self) -> str:
        """Last path segment — the asset's own name within its folder."""
        return self.path.rsplit("/", 1)[-1] if self.path else self.id

    @property
    def is_scene(self) -> bool:
        return self.type == TYPE_SCAN

    @property
    def is_part(self) -> bool:
        return self.type == TYPE_MODEL


@dataclass
class Match:
    """One placement of a part inside a scene."""

    score: float
    transform4x4: np.ndarray  # (4, 4) row-major rigid transform

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "Match":
        flat = payload.get("transform4x4") or []
        arr = np.asarray(flat, dtype=np.float64).reshape(4, 4)
        return cls(score=float(payload.get("score", 0.0)), transform4x4=arr)


@dataclass
class SceneMatches:
    """Result of ``GET /assets/{partId}/scene-matches/{sceneId}``."""

    model_asset_id: str
    scene_asset_id: str
    matches: list[Match] = field(default_factory=list)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SceneMatches":
        raw_matches = payload.get("matches") or []
        matches = [Match.from_json(m) for m in raw_matches]
        # The API sorts by score desc; sort defensively in case that changes.
        matches.sort(key=lambda m: m.score, reverse=True)
        return cls(
            model_asset_id=str(payload.get("modelAssetId", "")),
            scene_asset_id=str(payload.get("sceneAssetId", "")),
            matches=matches,
        )
