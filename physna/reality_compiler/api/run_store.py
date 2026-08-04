# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Persist completed runs locally so they can be reloaded later.

The hosted API has no "list my runs" endpoint, and we want a user to be
able to close Omniverse and come back later to reload a past run's scene
and all its matched placements. So each completed run is written as a JSON
record under ``~/.physna_reality_compiler/runs/`` capturing the tenant
folder, the asset ids, the local source files, and every match transform.

Pure Python (no ``omni``/``pxr``) so it's testable standalone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .models import Match

_RUNS_DIR = os.path.join(os.path.expanduser("~"), ".physna_reality_compiler", "runs")


def _match_to_dict(m: Match) -> dict[str, Any]:
    return {
        "score": float(m.score),
        "transform4x4": [float(x) for x in m.transform4x4.reshape(-1).tolist()],
    }


def _match_from_dict(d: dict[str, Any]) -> Match:
    return Match.from_json(d)  # reshapes the flat 16 back to 4x4


@dataclass
class RunPart:
    display_name: str
    source_path: str
    asset_id: str = ""
    matches: list[Match] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "source_path": self.source_path,
            "asset_id": self.asset_id,
            "matches": [_match_to_dict(m) for m in self.matches],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunPart":
        return cls(
            display_name=str(d.get("display_name", "")),
            source_path=str(d.get("source_path", "")),
            asset_id=str(d.get("asset_id", "")),
            matches=[_match_from_dict(m) for m in d.get("matches", [])],
        )


@dataclass
class RunRecord:
    id: str                     # stable id (also the filename stem)
    name: str
    run_folder: str
    created_at: str             # ISO-8601 string
    tenant_id: str = ""
    api_base: str = ""
    scene_asset_id: str = ""
    scene_file_path: str = ""
    scene_prim_path: str = ""
    scene_actual_points_prim_path: str = ""
    parts: list[RunPart] = field(default_factory=list)
    # False while a run is still indexing/matching on the platform (checkpointed
    # right after upload). If Kit closes before the run finishes, the record
    # survives with this False, so the run can be resumed (re-polled) later.
    # Defaults True so pre-existing records (all finished runs) load as complete.
    complete: bool = True

    @property
    def total_matches(self) -> int:
        return sum(len(p.matches) for p in self.parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "run_folder": self.run_folder,
            "created_at": self.created_at,
            "tenant_id": self.tenant_id,
            "api_base": self.api_base,
            "scene_asset_id": self.scene_asset_id,
            "scene_file_path": self.scene_file_path,
            "scene_prim_path": self.scene_prim_path,
            "scene_actual_points_prim_path": self.scene_actual_points_prim_path,
            "parts": [p.to_dict() for p in self.parts],
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunRecord":
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            run_folder=str(d.get("run_folder", "")),
            created_at=str(d.get("created_at", "")),
            tenant_id=str(d.get("tenant_id", "")),
            api_base=str(d.get("api_base", "")),
            scene_asset_id=str(d.get("scene_asset_id", "")),
            scene_file_path=str(d.get("scene_file_path", "")),
            scene_prim_path=str(d.get("scene_prim_path", "")),
            scene_actual_points_prim_path=str(d.get("scene_actual_points_prim_path", "")),
            parts=[RunPart.from_dict(p) for p in d.get("parts", [])],
            complete=bool(d.get("complete", True)),
        )


class RunStore:
    """List/save/load/delete run records under a per-user directory."""

    def __init__(self, directory: str = _RUNS_DIR) -> None:
        self._dir = directory

    def _path(self, run_id: str) -> str:
        return os.path.join(self._dir, f"{run_id}.json")

    def save(self, record: RunRecord) -> None:
        # Write-temp-then-rename so a crash mid-write can never corrupt an
        # existing record (os.replace is atomic on the same volume).
        try:
            os.makedirs(self._dir, exist_ok=True)
            path = self._path(record.id)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record.to_dict(), fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            # Persistence is best-effort; a failed save must not break a run.
            pass

    def get(self, run_id: str) -> RunRecord | None:
        """Load one record by id, or ``None`` if missing/unreadable."""
        try:
            with open(self._path(run_id), "r", encoding="utf-8") as fh:
                return RunRecord.from_dict(json.load(fh))
        except (OSError, ValueError):
            return None

    def list(self) -> list[RunRecord]:
        records: list[RunRecord] = []
        try:
            names = os.listdir(self._dir)
        except OSError:
            return records
        for fname in names:
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self._dir, fname), "r", encoding="utf-8") as fh:
                    records.append(RunRecord.from_dict(json.load(fh)))
            except (OSError, ValueError):
                continue
        # Newest first (created_at is ISO-8601, so string sort is chronological).
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def delete(self, run_id: str) -> None:
        try:
            os.remove(self._path(run_id))
        except OSError:
            pass
