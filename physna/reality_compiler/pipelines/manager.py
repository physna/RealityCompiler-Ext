# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Composition root and UI-facing entry point for the reality compiler.

Owns the :class:`ApiSession` (login state), the cached
:class:`PipelineState`, the :class:`ScanSearchWorkflow`, and the Kit file
pickers.  The UI talks only to this object.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import omni.kit.app

from ..api import (
    ApiConfig,
    ApiSession,
    Asset,
    AuthError,
    DEFAULT_WORKING_STATE,
    DiscoveredRun,
    RunPart,
    RunRecord,
    RunStore,
    SCENE_REQUIRED_STATE,
    TYPE_MODEL,
    TYPE_SCAN,
    discover_runs,
)
from .. import last_dir_store
from ..paths import persistent_dir, temp_dir
from ..converters import MeshConverter
from ..io import SUPPORTED_EXTENSIONS as _POINT_CLOUD_EXTENSIONS
from ..logger import get_logger
from ..scene import SceneOps
from .state import PartEntry, PipelineState
from .workflow import ScanSearchWorkflow, WorkflowError, sanitize_folder_name

_log = get_logger("physna.reality_compiler.pipelines.manager")


def _safe_call(fn: Callable, *args) -> None:
    """Invoke a UI callback, swallowing any error (never break the loop)."""
    try:
        fn(*args)
    except Exception:
        _log.exception("UI progress callback failed")


def _progress_logger(on_progress: Optional[Callable[[str], None]]) -> Callable[[str], None]:
    """A progress fn that logs and safely forwards to the optional UI callback."""
    def progress(msg: str) -> None:
        _log.info(msg)
        if on_progress is not None:
            _safe_call(on_progress, msg)
    return progress


def _fraction_emitter(loop, on_fraction: Optional[Callable[[float], None]]):
    """A thread-safe, throttled emitter of [0, 1] progress fractions.

    Chunk callbacks run on the executor thread — hop back to the loop so UI
    widgets are only touched there — and fire per 64KB chunk, so skip updates
    smaller than ~0.5% (a multi-GB file would otherwise queue tens of
    thousands of loop callbacks). ``emit(v, force=True)`` bypasses the
    throttle for the 0.0 / 1.0 endpoints."""
    last = [-1.0]

    def emit(value: float, force: bool = False) -> None:
        if on_fraction is None:
            return
        v = max(0.0, min(1.0, value))
        if not force and (v - last[0]) < 0.005:
            return
        last[0] = v
        loop.call_soon_threadsafe(lambda: _safe_call(on_fraction, v))

    return emit

# Scene = any point-cloud format we can load into the stage (single source of
# truth in io). API-native formats (workflow._SCENE_EXTS) upload as-is; the
# rest upload as points extracted to a temp .npy. E57 is intentionally NOT
# offered: raw .e57 uploads don't match on the platform — the supported path
# is Kit's File > Import + "Use Selected Prim" (extracts points -> .npy).
_E57_EXTS = {".e57"}
SCENE_EXTENSIONS = sorted(e for e in _POINT_CLOUD_EXTENSIONS if e not in _E57_EXTS)
# Parts = USD family + common CAD formats accepted as models.
PART_EXTENSIONS = [
    ".usd", ".usda", ".usdc", ".usdz",
    ".step", ".stp", ".stl", ".iges", ".igs",
    ".x_t", ".x_b", ".obj", ".glb", ".3ds", ".jt",
    ".sldprt", ".sldasm",
]


class PipelineManager:
    """Public facade the UI drives."""

    def __init__(
        self,
        stage: SceneOps,
        mesh_converter: MeshConverter,
    ) -> None:
        self._stage = stage
        self._mesh_converter = mesh_converter

        self._state = PipelineState()
        self._session = ApiSession()
        self._workflow = ScanSearchWorkflow(stage, mesh_converter=mesh_converter)
        self._run_store = RunStore()
        # Id of the run currently being executed, so its checkpoint save and its
        # final save target one record instead of minting a new id each time.
        self._active_run_id: Optional[str] = None
        self._picker = None
        # Per-part locks serialize slider-driven place/remove so fast drags
        # don't race on the placed-prim list.
        self._place_locks: dict = {}
        # Placement hot-swap: while on, scan points behind placed matches are
        # hidden (and re-revealed when the placement is removed). A single lock
        # serializes occlusion writes so overlapping placement changes don't
        # race on the Points prim.
        self._hide_points = True
        self._occlusion_lock = asyncio.Lock()
        # Incremental occlusion cache (all reset when the scene/snapshot
        # changes): world-space snapshot positions, a per-point count of how
        # many placed boxes cover each point (visible = count 0), and the
        # world AABB currently applied for each placed prim path.
        self._occ_world = None          # np.ndarray (M, 3) | None
        self._occ_world_mat = None      # world matrix _occ_world was projected with
        self._occ_cover = None          # np.ndarray (M,) int32 | None
        self._occ_boxes: dict = {}      # prim_path -> (min, max)
        self._occ_orig_widths = None    # the prim's original widths, verbatim
        self._occ_orig_widths_interp = None  # ...and their interpolation token
        self._occ_vis_width = None      # width used for visible points while hiding

        # Restore a prior login from the OS credential vault if present.
        try:
            self._session.restore()
        except Exception:
            _log.exception("Failed to restore stored credentials")

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def session(self) -> ApiSession:
        return self._session

    @property
    def state(self) -> PipelineState:
        return self._state

    @property
    def scene_ops(self) -> SceneOps:
        return self._stage

    @property
    def is_logged_in(self) -> bool:
        return self._session.is_logged_in

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def login(self, client_id: str, client_secret: str) -> None:
        """Authenticate and persist the service account. Raises AuthError.

        The token exchange is a blocking network call, so it runs on the
        executor to keep the UI loop responsive.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: self._session.login(client_id, client_secret)
        )

    def logout(self) -> None:
        self._session.logout(forget=True)

    def set_config(
        self,
        *,
        api_base: str = "",
        tenant_id: str = "",
        token_url: str = "",
    ) -> None:
        """Override routing config (base URL / tenant / token endpoint)."""
        self._session.set_config(
            self._session.config.with_overrides(
                api_base=api_base or None,
                tenant_id=tenant_id or None,
                token_url=token_url or None,
            )
        )

    @property
    def config(self) -> ApiConfig:
        return self._session.config

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def set_scene_from_selection(self) -> Optional[str]:
        """Use the currently-selected stage prim as the scene source."""
        prim_path = self._stage.get_selected_prim_path()
        if not prim_path:
            return None
        self._state.scene.file_path = None
        self._state.scene.prim_path = prim_path
        self._state.scene.actual_points_prim_path = None
        return prim_path

    def set_scene_from_file(self, file_path: str) -> None:
        self._state.scene.prim_path = None
        self._state.scene.actual_points_prim_path = None
        self._state.scene.file_path = file_path

    async def import_scene_file(
        self, file_path: str, on_progress: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """Load a point-cloud file into the stage as a Points prim.

        Sets it as the scene: the original file is uploaded as-is, and the
        created prim is the placement reference frame. The file is still
        the upload source even if the prim can't be created, so uploads
        keep working when a format can't be displayed.
        """
        from ..io import load_point_cloud_async

        scene = self._state.scene
        scene.file_path = file_path
        scene.prim_path = None
        scene.actual_points_prim_path = None
        # New scene points - any prior hot-swap snapshot/cache no longer applies.
        self.invalidate_scene_points_backup()

        if on_progress:
            on_progress(f"Loading {Path(file_path).name} into the stage...")
        points, colors, _intensity, _meta = await load_point_cloud_async(
            file_path, on_progress=on_progress
        )

        name = self._stage.safe_identifier(Path(file_path).stem or "Scene")
        if on_progress:
            on_progress(f"Displaying {len(points):,} points ({Path(file_path).name})...")
        # Author a point width up front so the scan renders at a consistent size
        # from load. The hot-swap hides by setting widths to 0, so without a
        # width already present the whole cloud would visibly resize the first
        # time a match is placed.
        prim = self._stage.create_point_cloud_prim(
            points, colors, name, "/World/PhysnaScenes",
            width=self._derive_point_width(points),
        )
        if prim:
            scene.prim_path = prim
            scene.actual_points_prim_path = prim
            # Reconcile the scan's up-axis with the stage so it doesn't come in
            # on its side (rotation lives on the prim's xform, so placements
            # rotate with it and stay in Physna's frame).
            self._apply_scene_orientation()
        return prim

    # ------------------------------------------------------------------
    # Scene orientation (up-axis reconciliation)
    # ------------------------------------------------------------------

    @property
    def scene_up_axis(self) -> str:
        """The scan's assumed up-axis: ``"z"`` (auto), ``"y"``, or ``"none"``."""
        return self._state.scene.up_axis

    def _orientation_degrees(self) -> float:
        """Rotation about X to map the scan's up-axis onto the stage's."""
        up = self._state.scene.up_axis
        if up == "none":
            return 0.0
        stage_up = self._stage.get_stage_up_axis()
        if up == "z":  # scan is Z-up: rotate only if the stage is Y-up
            return -90.0 if stage_up == "Y" else 0.0
        if up == "y":  # scan is Y-up: rotate only if the stage is Z-up
            return 90.0 if stage_up == "Z" else 0.0
        return 0.0

    def _apply_scene_orientation(self) -> None:
        prim = self._state.scene.actual_points_prim_path or self._state.scene.prim_path
        if prim:
            self._stage.set_prim_x_rotation(prim, self._orientation_degrees())

    async def set_scene_up_axis(self, axis: str) -> None:
        """Set the assumed scan up-axis and re-orient the scene + placements.

        Re-authors any existing placements so they follow the new orientation
        (their transforms compose the scene prim's world xform), and refreshes
        the point hot-swap since the scan's world matrix changed."""
        axis = axis if axis in ("z", "y", "none") else "z"
        self._state.scene.up_axis = axis
        self._apply_scene_orientation()
        await omni.kit.app.get_app().next_update_async()
        for part in self._state.parts:
            # Hold the same per-part lock the Placed slider uses, so a
            # mid-drag reconcile can't interleave with the delete/re-create.
            lock = self._place_locks.setdefault(id(part), asyncio.Lock())
            async with lock:
                await self._workflow.reapply_placements(self._state, part)
        await self.refresh_point_occlusion()

    def add_part_files(self, file_paths: list[str]) -> int:
        """Queue one or more local part files. Returns count added."""
        added = 0
        existing = {p.source_path for p in self._state.parts}
        for path in file_paths:
            if path in existing:
                continue
            self._state.parts.append(
                PartEntry(source_path=path, display_name=Path(path).stem)
            )
            existing.add(path)
            added += 1
        return added

    def add_parts_from_selection(self) -> tuple[int, int]:
        """Export the selected stage prim(s) to USD and queue them as parts.

        Each selected prim's subtree is flattened to a standalone ``.usd``
        in a temp dir and queued like any picked file.  Returns
        ``(added, selected)`` so the UI can flag prims that failed to
        export (e.g. an empty group with no geometry).
        """
        prim_paths = self._stage.get_selected_prim_paths()
        if not prim_paths:
            return (0, 0)
        out_dir = temp_dir("prims")
        existing = {p.source_path for p in self._state.parts}
        added = 0
        for prim_path in prim_paths:
            leaf = prim_path.rstrip("/").split("/")[-1] or "Part"
            safe = self._stage.safe_identifier(leaf)
            out = os.path.join(out_dir, f"{safe}_{uuid.uuid4().hex[:6]}.usd")
            if not self._stage.export_prim_to_usd(prim_path, out):
                continue
            if out in existing:
                continue
            self._state.parts.append(
                PartEntry(source_path=out, display_name=leaf)
            )
            existing.add(out)
            added += 1
        return (added, len(prim_paths))

    def clear_parts(self) -> None:
        self._state.parts = []
        # Dead parts' locks would otherwise accumulate (and a recycled id()
        # could alias a new part onto an old lock).
        self._place_locks.clear()

    @staticmethod
    def run_name_for_file(path: str) -> str:
        """Default run name for a scene file: its parent-folder name, else stem."""
        p = Path(path)
        return p.parent.name or p.stem

    def suggest_run_name(self) -> str:
        """A default run-folder name derived from the current scene.

        The scene file's parent-folder name (e.g. ``WarehouseDemo``) or,
        for a stage prim, the prim's own name.
        """
        scene = self._state.scene
        if scene.file_path:
            return self.run_name_for_file(scene.file_path)
        if scene.prim_path:
            return scene.prim_path.rstrip("/").split("/")[-1]
        return ""

    def set_run_name(self, name: str) -> None:
        self._state.run_name = name

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run_search(
        self,
        on_progress: Optional[Callable[[str], None]] = None,
        on_fraction: Optional[Callable[[float], None]] = None,
        on_status: Optional[Callable[[], None]] = None,
        on_part_matches: Optional[Callable[[PartEntry], None]] = None,
    ) -> None:
        """Execute the full upload → poll → matches workflow.

        Raises :class:`AuthError` if not logged in, :class:`WorkflowError`
        for hard stops (no scene, scene failed to index, etc.).
        """
        client = self._session.client
        if client is None:
            raise AuthError("Not logged in: sign in with a service account first.")
        if not self._session.config.tenant_id:
            raise WorkflowError("No Tenant ID configured: set it in Account.")
        # Fresh run: delete prior placements FIRST (while placed_prim_paths
        # still points at them), then drop stale matches/assets — else the new
        # run's matches get attributed to the old run's prims.
        await self.clear_all_placements()
        self._state.clear_results()
        # Restore any hidden scan points to pristine and drop the cache so the
        # next snapshot isn't taken from an already-occluded cloud.
        await self._restore_and_reset_occlusion()
        # Mint a new record id on the first save (the upload checkpoint), and
        # reuse it for the final save.
        self._active_run_id = None
        completed = False
        try:
            await self._workflow.run(
                client, self._state,
                on_progress=on_progress, on_fraction=on_fraction,
                on_status=on_status, on_part_matches=on_part_matches,
                on_uploaded=lambda: self._save_run_record(complete=False),
            )
            completed = True
        finally:
            # Persist whatever the run produced. A run that finished with all
            # matches read is marked complete; one that failed, timed out, was
            # cancelled after uploading, or still has unread matches stays
            # incomplete, so it shows a Resume affordance and can be re-polled
            # later. Nothing is saved if the run never got as far as uploading
            # a scene (scene_asset is None).
            self._save_run_record(
                complete=completed and self._all_matches_read()
            )

    # ------------------------------------------------------------------
    # Run history
    # ------------------------------------------------------------------

    async def validate_saved_session(self) -> bool:
        """Confirm a restored login still works; sign out if it doesn't.

        Returns True if there's nothing to validate or the token check
        passes; False if the stored credentials are no longer valid (the
        session is dropped to logged-out, but the vault entry is kept).
        """
        if not self._session.is_logged_in:
            return True
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._session.verify)
            return True
        except Exception:
            _log.info("Saved session failed validation; signing out")
            self._session.invalidate_session()
            return False

    def _record_matches_session(self, record: RunRecord) -> bool:
        """True when *record* was created in the tenant/environment the session
        is signed into (or predates tenant tracking).

        Polling another tenant's assets just 404s, which would misread a
        healthy run as deleted — refuse up front instead."""
        cfg = self._session.config
        if record.tenant_id and record.tenant_id != cfg.tenant_id:
            return False
        if record.api_base and record.api_base != cfg.api_base:
            return False
        return True

    async def refresh_run(
        self,
        record: RunRecord,
        on_progress: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[], None]] = None,
        on_part_matches: Optional[Callable[[PartEntry], None]] = None,
    ) -> Optional[RunRecord]:
        """Re-poll a saved run's assets and re-read matches from the platform.

        ``on_status``/``on_part_matches`` let the UI update per-part state live
        as assets resolve — the same live feedback a fresh run gives, which
        matters most when resuming a run whose parts are still indexing.
        Returns the freshly saved record (None only if nothing was saved)."""
        client = self._session.client
        if client is None:
            raise AuthError("Sign in to refresh a run.")
        if not self._record_matches_session(record):
            raise WorkflowError(
                f"'{record.name}' was created in a different tenant or "
                "environment; sign in to that account to update it."
            )
        self.load_run(record)
        await self._workflow.refresh(
            client, self._state, on_progress=on_progress,
            on_status=on_status, on_part_matches=on_part_matches,
        )
        # A part whose matches are still unread (relationship computing timed
        # out, or the read failed) keeps the record incomplete so Resume and
        # the background reconcile keep retrying it.
        return self._save_run_record(
            run_id=record.id, complete=self._all_matches_read()
        )

    def _all_matches_read(self) -> bool:
        """True when no part's platform matches are still unread."""
        return not any(p.matches_pending for p in self._state.parts)

    def _save_run_record(
        self, run_id: str | None = None, complete: bool = True
    ) -> Optional[RunRecord]:
        s = self._state
        if s.scene_asset is None:
            return None
        rid = run_id or self._active_run_id or (
            f"{sanitize_folder_name(s.run_name) or 'run'}-{uuid.uuid4().hex[:6]}"
        )
        self._active_run_id = rid
        # Keep the original creation time on re-saves (checkpoint -> final,
        # update, resume, add-part); the list sorts by created_at, so
        # re-stamping would rewrite history and reorder it on every save.
        existing = self._run_store.get(rid)
        created_at = (existing.created_at if existing else None) or (
            datetime.datetime.now().isoformat(timespec="seconds")
        )
        record = RunRecord(
            id=rid,
            name=s.run_name or s.run_folder or rid,
            run_folder=s.run_folder or "",
            created_at=created_at,
            tenant_id=self._session.config.tenant_id,
            api_base=self._session.config.api_base,
            scene_asset_id=s.scene_asset.id if s.scene_asset else "",
            scene_file_path=s.scene.file_path or "",
            scene_prim_path=s.scene.prim_path or "",
            scene_actual_points_prim_path=s.scene.actual_points_prim_path or "",
            parts=[
                RunPart(
                    display_name=p.display_name,
                    source_path=p.source_path,
                    asset_id=p.asset.id if p.asset else "",
                    matches=list(p.matches),
                )
                for p in s.parts
            ],
            complete=complete,
        )
        self._run_store.save(record)
        _log.info(
            "Saved run record %s (%d matches, complete=%s)",
            rid, record.total_matches, complete,
        )
        return record

    @property
    def active_run_id(self) -> Optional[str]:
        """Id of the run currently in state (executing, resuming, or loaded).

        Used by the UI to tell an in-flight run apart from a truly interrupted
        one: an incomplete record that IS the active run is still going, not
        stranded."""
        return self._active_run_id

    def list_runs(self) -> list[RunRecord]:
        return self._run_store.list()

    def delete_run(self, run_id: str) -> None:
        self._run_store.delete(run_id)

    # ------------------------------------------------------------------
    # Platform discovery (runs created anywhere in the tenant)
    # ------------------------------------------------------------------

    async def discover_platform_runs(self) -> list[DiscoveredRun]:
        """List the tenant's assets and group them into runs.

        Runs already saved locally (matched by scene asset id or folder)
        are excluded — they're in the Saved Runs list already.
        """
        client = self._session.client
        if client is None:
            raise AuthError("Sign in to browse platform runs.")
        loop = asyncio.get_running_loop()
        assets = await loop.run_in_executor(None, client.list_all_assets)
        runs = discover_runs(assets)

        saved = self._run_store.list()
        saved_scene_ids = {r.scene_asset_id for r in saved if r.scene_asset_id}
        saved_folders = {r.run_folder for r in saved if r.run_folder}
        return [
            r for r in runs
            if r.scene.id not in saved_scene_ids and r.folder not in saved_folders
        ]

    async def load_discovered_run(
        self,
        run: DiscoveredRun,
        on_progress: Optional[Callable[[str], None]] = None,
        on_fraction: Optional[Callable[[float], None]] = None,
    ) -> RunRecord:
        """Download a platform run's files, read its matches, and load it.

        Downloads every asset preserving the run's folder layout (so USD
        references resolve), reads scene-matches per part, saves it as a
        local run record, and restores it into state.

        ``on_fraction`` receives overall download progress in ``[0, 1]``,
        combining per-file and per-chunk streaming so a big scene on a slow
        link shows continuous movement instead of appearing frozen.  It is
        driven from a worker thread, so it's marshalled back onto this
        event loop before firing.
        """
        client = self._session.client
        if client is None:
            raise AuthError("Sign in first.")
        loop = asyncio.get_running_loop()
        progress = _progress_logger(on_progress)
        emit_fraction = _fraction_emitter(loop, on_fraction)

        safe = sanitize_folder_name(run.folder.replace("/", "_")) or "run"
        base_dir = os.path.join(
            temp_dir("discovered"), f"{safe}_{uuid.uuid4().hex[:6]}"
        )

        id_to_local: dict[str, str] = {}
        total = len(run.all_assets)
        emit_fraction(0.0, force=True)
        for i, asset in enumerate(run.all_assets, start=1):
            rel = (
                asset.path[len(run.folder) + 1:]
                if asset.path.startswith(run.folder + "/")
                else os.path.basename(asset.path)
            )
            local = os.path.join(base_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            progress(f"Downloading {i}/{total}: {rel}")
            # This file's chunk progress maps into its slice of the whole.
            await self._download_asset_to(
                client, loop, asset.id, local, emit_fraction,
                base=(i - 1) / total if total else 0.0,
                span=1.0 / total if total else 1.0,
            )
            id_to_local[asset.id] = local
            emit_fraction(i / total if total else 1.0, force=True)

        scene = run.scene
        parts: list[RunPart] = []
        # Keyed by asset id — display names are file stems and two parts (each
        # in its own subfolder) can share one, which would cross their errors.
        failed: dict[str, str] = {}
        for part in run.parts:
            progress(f"Reading matches: {os.path.basename(part.path)}")
            display_name = Path(part.path).stem
            try:
                result = await loop.run_in_executor(
                    None, lambda p=part: client.get_scene_matches(p.id, scene.id)
                )
                matches = result.matches
            except Exception as exc:
                _log.exception("scene-matches failed for %s", part.path)
                matches = []
                failed[part.id] = str(exc)
            parts.append(
                RunPart(
                    display_name=display_name,
                    source_path=id_to_local.get(part.id, ""),
                    asset_id=part.id,
                    matches=matches,
                )
            )

        record = RunRecord(
            id=f"discovered-{safe}-{uuid.uuid4().hex[:6]}",
            name=run.name,
            run_folder=run.folder,
            created_at=run.created_at
            or datetime.datetime.now().isoformat(timespec="seconds"),
            tenant_id=self._session.config.tenant_id,
            api_base=self._session.config.api_base,
            scene_asset_id=scene.id,
            scene_file_path=id_to_local.get(scene.id, ""),
            parts=parts,
        )
        self._run_store.save(record)
        progress("Downloaded; loading into the stage...")
        self.load_run(record)
        # Surface per-part read failures (else an errored read looks like
        # "no matches", same as _finalize/add_part). Matched by asset id.
        if failed:
            for entry in self._state.parts:
                if entry.asset and entry.asset.id in failed:
                    entry.match_error = failed[entry.asset.id]
        return record

    def load_run(self, record: RunRecord) -> None:
        """Restore a saved run into the live state so its matches can be placed.

        Rebuilds the parts + matches (and scene reference) from the record
        using the locally-stored source files and transforms — no network
        call, so it works offline / in a later session.
        """
        s = self._state
        s.reset()
        self._reset_occlusion_cache()
        self._place_locks.clear()  # parts are being replaced wholesale
        # Subsequent saves (add-part, resume) target this same record, not a
        # stale id from an earlier run or a fresh duplicate.
        self._active_run_id = record.id
        self._populate_state_from_record(s, record)

    @staticmethod
    def _populate_state_from_record(s: PipelineState, record: RunRecord) -> None:
        """Fill a (freshly reset) ``PipelineState`` from a saved record.

        Seeds asset states honestly: a finished run's assets really are done; an
        interrupted run's aren't necessarily, so seed those still-working until a
        poll reports the truth. Stored matches prove an asset finished + was
        queried, so those stay finished (and a matched part implies the scene was
        queryable too)."""
        s.run_name = record.name
        s.run_folder = record.run_folder
        s.scene.file_path = record.scene_file_path or None
        s.scene.prim_path = record.scene_prim_path or None
        s.scene.actual_points_prim_path = record.scene_actual_points_prim_path or None
        scene_done = record.complete or any(rp.matches for rp in record.parts)
        if record.scene_asset_id:
            s.scene_asset = Asset(
                id=record.scene_asset_id,
                state=SCENE_REQUIRED_STATE if scene_done else DEFAULT_WORKING_STATE,
                type=TYPE_SCAN,
            )
        for rp in record.parts:
            entry = PartEntry(source_path=rp.source_path, display_name=rp.display_name)
            if rp.asset_id:
                part_done = record.complete or bool(rp.matches)
                entry.asset = Asset(
                    id=rp.asset_id,
                    state=SCENE_REQUIRED_STATE if part_done else DEFAULT_WORKING_STATE,
                    type=TYPE_MODEL,
                )
            entry.matches = list(rp.matches)
            s.parts.append(entry)

    async def reconcile_incomplete_runs(
        self, on_progress: Optional[Callable[[str], None]] = None
    ) -> tuple[int, int]:
        """Detect interrupted runs that finished on the platform — or left it.

        For each saved run marked incomplete (except the one running now), do a
        single non-blocking poll:
        - finished (assets terminal + matches readable): write the fresh
          matches back and mark the record complete;
        - gone (every asset confirmed deleted): the run was cancelled/deleted
          on the platform, so drop the stale local record.
        Records from another tenant/environment are skipped entirely — their
        assets 404 from this session, which would misread them as deleted.
        Returns ``(finished, removed)`` counts. Never touches the live
        pipeline state, so it's safe to call from a background Refresh."""
        client = self._session.client
        if client is None:
            return 0, 0
        updated = 0
        removed = 0
        for record in self._run_store.list():
            if record.complete or record.id == self._active_run_id:
                continue
            if not self._record_matches_session(record):
                continue
            temp = PipelineState()
            self._populate_state_from_record(temp, record)
            try:
                outcome = await self._workflow.reconcile(client, temp)
            except Exception:
                _log.exception("Reconcile check failed for run %s", record.id)
                continue
            if outcome == "gone":
                self._run_store.delete(record.id)
                removed += 1
                _log.info("Removed run %s: deleted on the platform", record.id)
                if on_progress is not None:
                    _safe_call(
                        on_progress,
                        f"'{record.name}' was removed from the platform",
                    )
                continue
            if outcome != "complete":
                continue
            # Same order as record.parts (temp was built from it), so zip aligns.
            for rp, tp in zip(record.parts, temp.parts):
                rp.matches = list(tp.matches)
                if tp.asset:
                    rp.asset_id = tp.asset.id
            record.complete = True
            self._run_store.save(record)
            updated += 1
            _log.info("Reconciled interrupted run %s -> complete", record.id)
            if on_progress is not None:
                _safe_call(on_progress, f"'{record.name}' finished on the platform")
        return updated, removed

    @staticmethod
    async def _download_asset_to(
        client, loop, asset_id: str, dest: str, emit,
        base: float = 0.0, span: float = 1.0,
    ) -> None:
        """Stream one asset to ``dest`` on the executor (never buffered in RAM),
        mapping its per-chunk progress into ``[base, base + span]`` of the
        overall fraction via ``emit``."""
        def chunk_cb(done: int, tot: int) -> None:
            if tot > 0:
                emit(base + span * (done / tot))

        await loop.run_in_executor(
            None,
            lambda: client.download_asset(
                asset_id, on_progress=chunk_cb, dest_path=dest
            ),
        )

    async def download_scene_for_record(
        self,
        record: RunRecord,
        on_progress: Optional[Callable[[str], None]] = None,
        on_fraction: Optional[Callable[[float], None]] = None,
    ) -> Optional[str]:
        """Download a saved run's uploaded scene and import it into the stage.

        Used when a run's scene was a stage prim (no local file) and that prim
        isn't in the current stage: the scan still lives on the platform under
        ``scene_asset_id``, so fetch it back and set it as the placement frame.
        Returns the local file path, or raises on failure."""
        client = self._session.client
        if client is None:
            raise AuthError("Sign in to download the scene.")
        if not record.scene_asset_id:
            raise WorkflowError("This run has no uploaded scene to download.")
        loop = asyncio.get_running_loop()
        progress = _progress_logger(on_progress)
        emit_fraction = _fraction_emitter(loop, on_fraction)

        progress("Locating scene on the platform...")
        asset = await loop.run_in_executor(
            None, lambda: client.get_asset(record.scene_asset_id)
        )
        remote_name = asset.name or "scene.npy"
        # The scan must land with its real extension so import dispatches right.
        if not Path(remote_name).suffix:
            remote_name += ".npy"
        # Persistent, not temp: the record keeps pointing at this file across
        # sessions, so it must survive OS temp sweeps. Keyed by record id so a
        # re-download replaces the old copy instead of accumulating.
        base_dir = persistent_dir(os.path.join("scenes", record.id))
        local = os.path.join(base_dir, remote_name)

        progress(f"Downloading scene: {remote_name}")
        emit_fraction(0.0, force=True)
        await self._download_asset_to(
            client, loop, record.scene_asset_id, local, emit_fraction,
        )
        emit_fraction(1.0, force=True)
        await self.import_scene_file(local, on_progress=on_progress)
        # Cache the download on the run record so a later Load reuses this file
        # instead of re-downloading (the run's scene was a prim, so it had no
        # local file until now). _finish_run_loaded re-checks the path exists.
        record.scene_file_path = local
        try:
            self._run_store.save(record)
            # Keep the live state in sync so re-saving this run keeps the path.
            self._state.scene.file_path = local
        except Exception:
            _log.exception("Could not persist downloaded scene path on record")
        return local

    def set_min_score(self, value: float) -> None:
        """Set the minimum match score (0–100) for placement."""
        self._state.min_score = max(0.0, min(100.0, float(value)))

    def qualifying_count(self, part: PartEntry) -> int:
        """How many of a part's matches meet the current min-score threshold.

        This is the cap on how many can be placed - the per-part slider max.
        """
        return self._workflow.qualifying_count(self._state, part)

    def selected_match_count(self, part: PartEntry) -> int:
        """How many of a part's matches would be placed under current filters."""
        return len(self._workflow.selected_matches(self._state, part))

    async def place_all_matches(self) -> int:
        placed = await self._workflow.place_all(self._state)
        await self.refresh_point_occlusion()
        return placed

    async def clear_all_placements(self) -> int:
        """Remove every placed match from the stage; returns how many removed."""
        removed = 0
        for part in self._state.parts:
            n = self._workflow.placed_count(part)
            if n:
                await self.set_part_placed_count(part, 0, occlude=False)
                removed += n
        await self.refresh_point_occlusion()
        return removed

    async def reconcile_min_score(self) -> None:
        """After a min-score change, drop placements that no longer qualify.

        Only ever removes (lowers a part's placed count to its new qualifying
        cap); lowering min_score just re-opens headroom without auto-placing.
        """
        for part in self._state.parts:
            cap = self.qualifying_count(part)
            if self._workflow.placed_count(part) > cap:
                await self.set_part_placed_count(part, cap, occlude=False)
        await self.refresh_point_occlusion()

    def placed_count(self, part: PartEntry) -> int:
        """How many of a part's matches are currently placed in the stage."""
        return self._workflow.placed_count(part)

    async def set_part_placed_count(
        self, part: PartEntry, target: int, *, occlude: bool = True
    ) -> None:
        """Place/remove so exactly ``target`` of a part's top matches show.

        Serialized per part so a fast slider drag reconciles cleanly to the
        final value instead of racing. Re-applies the point hot-swap afterward
        unless ``occlude=False`` (batch callers occlude once at the end).
        """
        lock = self._place_locks.setdefault(id(part), asyncio.Lock())
        async with lock:
            await self._workflow.set_placed_count(self._state, part, target)
        if occlude:
            await self.refresh_point_occlusion()

    # ------------------------------------------------------------------
    # Placement hot-swap: hide scan points behind placed matches
    # ------------------------------------------------------------------

    @property
    def hide_points(self) -> bool:
        return self._hide_points

    def _scene_points_prim(self) -> Optional[str]:
        """The Points prim to occlude, or None if there isn't a usable one."""
        prim = (
            self._state.scene.actual_points_prim_path
            or self._state.scene.prim_path
        )
        if prim and self._stage.is_points_prim(prim):
            return prim
        return None

    def has_scene_points(self) -> bool:
        """True when there's a scene Points prim in the stage to hot-swap."""
        return self._scene_points_prim() is not None

    def _reset_occlusion_cache(self) -> None:
        self._occ_world = None
        self._occ_world_mat = None
        self._occ_cover = None
        self._occ_boxes = {}
        self._occ_orig_widths = None
        self._occ_orig_widths_interp = None
        self._occ_vis_width = None

    # Visible point size when the scene has no authored width: a multiple of the
    # estimated point spacing (~ bbox diagonal / sqrt(N)). Bump this if scans
    # render too small, drop it if points look too chunky.
    _POINT_WIDTH_SCALE = 2.0

    @classmethod
    def _derive_point_width(cls, positions) -> float:
        """A reasonable visible point size when the prim has no width authored.

        Sizes points to a multiple of their spacing so the cloud reads as a
        filled surface rather than pinpricks.
        """
        if positions is None or len(positions) == 0:
            return 0.001
        span = float(np.linalg.norm(positions.max(axis=0) - positions.min(axis=0)))
        w = cls._POINT_WIDTH_SCALE * span / max(len(positions) ** 0.5, 1.0)
        return w if w > 0 else 0.001

    async def _restore_and_reset_occlusion(self) -> None:
        """Reveal any hidden scan points, then drop the snapshot + cache."""
        prim = self._scene_points_prim()
        if prim is not None and self._state.scene_points_backup is not None:
            async with self._occlusion_lock:
                await self._stage.clear_point_visibility_async(
                    prim, self._occ_orig_widths, self._occ_orig_widths_interp
                )
                self._state.scene_points_backup = None
                self._reset_occlusion_cache()
        else:
            self._state.scene_points_backup = None
            self._reset_occlusion_cache()

    def invalidate_scene_points_backup(self) -> None:
        """Forget the pristine snapshot (call when the scene prim changes)."""
        self._state.scene_points_backup = None
        self._reset_occlusion_cache()

    async def set_hide_points(self, enabled: bool) -> None:
        """Toggle the hot-swap and immediately apply/restore the scan points."""
        self._hide_points = bool(enabled)
        await self.refresh_point_occlusion()

    async def refresh_point_occlusion(self) -> None:
        """Re-derive which scan points are visible behind placed matches.

        Fast + incremental: the scan's world-space positions are captured once;
        a per-point cover count tracks how many placed boxes contain each point
        (visible = count 0). Each call only tests the boxes *added or removed*
        since last time (usually one), and the numpy runs on a worker thread.
        Hiding is done with a per-point ``displayOpacity`` primvar, so the point
        count (and the big position buffer) never changes - only a 1-float-per-
        point buffer is rewritten. No-op without a scene Points prim.
        """
        prim = self._scene_points_prim()
        if prim is None:
            return
        loop = asyncio.get_running_loop()
        async with self._occlusion_lock:
            backup = self._state.scene_points_backup

            # Hot-swap off: reveal everything once, then forget the cache.
            if not self._hide_points:
                if backup is not None:
                    await self._stage.clear_point_visibility_async(
                        prim, self._occ_orig_widths, self._occ_orig_widths_interp
                    )
                    self._state.scene_points_backup = None
                self._reset_occlusion_cache()
                return

            # First hide: capture the points + their original widths (verbatim,
            # so per-point widths survive) and pick the visible width (real
            # width, or a spacing-derived size when none is authored).
            if backup is None:
                backup = self._stage.read_points(prim)
                if backup is None or len(backup[0]) == 0:
                    return
                self._state.scene_points_backup = backup
                self._occ_orig_widths, self._occ_orig_widths_interp = (
                    self._stage.read_point_widths(prim)
                )
                sample_w = self._stage.get_points_prim_sample_width(prim)
                self._occ_vis_width = (
                    sample_w if sample_w and sample_w > 0
                    else self._derive_point_width(backup[0])
                )
                self._occ_world = None
                self._occ_world_mat = None

            # Re-project to world when the scan's transform changed (or first
            # time): a moved/rotated scan would otherwise be tested against stale
            # world positions. On a change, re-mask from scratch.
            mat = self._stage.points_world_matrix(prim)
            if self._occ_world is None or not np.array_equal(mat, self._occ_world_mat):
                self._occ_world = await loop.run_in_executor(
                    None, self._stage.apply_world_matrix, backup[0], mat
                )
                self._occ_world_mat = mat
                self._occ_cover = np.zeros(len(backup[0]), dtype=np.int32)
                self._occ_boxes = {}

            # Current placed-prim boxes (reuse cached ones; batch-compute new).
            placed = [
                pp for part in self._state.parts for pp in part.placed_prim_paths
            ]
            need = [pp for pp in placed if pp not in self._occ_boxes]
            fresh = self._stage.compute_world_bboxes(need) if need else {}
            # Instanced/referenced geometry can take a tick or two to compose;
            # retry any new placement that came back without a world bound so it
            # isn't silently left un-occluded until the next placement change.
            missing = [pp for pp in need if pp not in fresh]
            app = omni.kit.app.get_app()
            for _ in range(3):
                if not missing:
                    break
                await app.next_update_async()
                fresh.update(self._stage.compute_world_bboxes(missing))
                missing = [pp for pp in missing if pp not in fresh]
            current = {}
            for pp in placed:
                box = self._occ_boxes.get(pp) or fresh.get(pp)
                if box is not None:
                    current[pp] = box

            add_boxes = [current[pp] for pp in current if pp not in self._occ_boxes]
            remove_boxes = [
                self._occ_boxes[pp] for pp in self._occ_boxes if pp not in current
            ]
            if add_boxes or remove_boxes:
                self._occ_cover = await loop.run_in_executor(
                    None, self._stage.cover_delta,
                    self._occ_world, self._occ_cover, add_boxes, remove_boxes,
                )
            self._occ_boxes = current

            await self._stage.set_point_visibility_async(
                prim, self._occ_cover == 0, self._occ_vis_width
            )

    # ------------------------------------------------------------------
    # Incremental search + scene editing
    # ------------------------------------------------------------------

    @property
    def has_active_run(self) -> bool:
        """True when a scene is uploaded + finished, so parts can be added."""
        scene = self._state.scene_asset
        return bool(
            self._state.run_folder
            and scene
            and scene.state == SCENE_REQUIRED_STATE
        )

    async def add_and_search_part(
        self,
        file_path: str,
        on_progress: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[], None]] = None,
        on_part_matches: Optional[Callable[[PartEntry], None]] = None,
    ) -> PartEntry:
        """Upload one more part into the active run and read its matches."""
        client = self._session.client
        if client is None:
            raise AuthError("Sign in first.")
        entry = PartEntry(
            source_path=file_path,
            display_name=Path(file_path).stem,
            # Unread until add_part's match read settles it — if the add fails
            # mid-way the record saves incomplete instead of masquerading as
            # a finished run.
            matches_pending=True,
        )
        self._state.parts.append(entry)
        try:
            await self._workflow.add_part(
                client, self._state, entry,
                on_progress=on_progress, on_status=on_status,
                on_part_matches=on_part_matches,
            )
        finally:
            self._save_run_record(complete=self._all_matches_read())
        return entry

    async def remove_matched_points(self, keep_only: bool = False) -> int:
        """Remove scene points inside placed matches, or keep only those.

        Operates on the bounding boxes of the match prims already placed in
        the stage, so Place must have run first. ``keep_only=True`` isolates
        the matched regions; ``False`` carves them out.
        """
        scene_prim = self._scene_points_prim()
        if not scene_prim:
            raise WorkflowError(
                "No scene points prim in the stage to edit "
                "(load or select the scene first)."
            )
        placed = [
            prim_path
            for part in self._state.parts
            for prim_path in part.placed_prim_paths
        ]
        # Batch bbox computation (one BBoxCache pass), same as the hot-swap.
        boxes = [
            box for box in self._stage.compute_world_bboxes(placed).values()
            if box is not None
        ]
        if not boxes:
            raise WorkflowError("Nothing placed yet — Place matches first.")
        # Hold the occlusion lock so this can't race an in-flight hot-swap
        # refresh authoring the same prim (last-writer-wins would corrupt it).
        # Restore original widths first (the hot-swap may have authored per-point
        # ones); this permanent edit subsets the point + colour arrays, which
        # would otherwise leave a mismatched-length widths buffer behind. Then
        # drop the cache so the removal sticks.
        async with self._occlusion_lock:
            if self._state.scene_points_backup is not None:
                await self._stage.clear_point_visibility_async(
                    scene_prim, self._occ_orig_widths, self._occ_orig_widths_interp
                )
            self.invalidate_scene_points_backup()
            return await self._stage.remove_points_in_world_boxes_async(
                scene_prim, boxes, keep_inside=keep_only
            )

    def shutdown(self) -> None:
        # Nothing long-lived to tear down in the hosted model; kept for
        # symmetry with extension.on_shutdown().
        self._state.reset()

    # ------------------------------------------------------------------
    # File pickers (Kit dialogs)
    # ------------------------------------------------------------------

    async def pick_scene_file(self) -> Optional[str]:
        path = await self._pick_file(
            SCENE_EXTENSIONS,
            initial_dir=last_dir_store.get_last_dir(last_dir_store.SCENE),
        )
        if path:
            last_dir_store.set_last_dir(last_dir_store.SCENE, path)
        return path

    async def pick_part_files(self) -> list[str]:
        """Pick one or more part files (Ctrl/Shift-click to multi-select)."""
        paths = await self._pick_files(
            PART_EXTENSIONS,
            initial_dir=last_dir_store.get_last_dir(last_dir_store.INSTANCE),
        )
        if paths:
            last_dir_store.set_last_dir(last_dir_store.INSTANCE, paths[0])
        return paths

    async def pick_parts_from_folder(self) -> list[str]:
        """Pick a folder and queue every supported part file inside it."""
        folder = await self._pick_folder(
            initial_dir=last_dir_store.get_last_dir(last_dir_store.BATCH)
        )
        if not folder:
            return []
        last_dir_store.set_last_dir(last_dir_store.BATCH, folder)
        exts = {e.lower() for e in PART_EXTENSIONS}
        found = [
            str(p)
            for p in sorted(Path(folder).iterdir())
            if p.is_file() and p.suffix.lower() in exts
        ]
        return found

    _PICKER_TIMEOUT_S = 300.0

    async def _run_picker(self, *, title, apply_label, item_filter_fn, resolve,
                          filter_options=None, error_label="File picker",
                          initial_dir=None):
        """Shared FilePickerDialog shell for the file and folder pickers.

        ``resolve(filename, dirname)`` returns the picked result (path or dir)
        or ``None``; ``item_filter_fn`` filters the browser; ``filter_options``
        is the extension dropdown (files only). Returns the picked value, or
        ``None`` on cancel/timeout/error.
        """
        dialog = None
        try:
            from omni.kit.window.filepicker import FilePickerDialog

            result_future: asyncio.Future = asyncio.Future()

            def _hide(d) -> None:
                try:
                    if hasattr(d, "hide"):
                        d.hide()
                except Exception:
                    pass

            # Handlers capture THEIR dialog (a local), not self._picker — with
            # two pickers alive (double-click, scene+part), the shared attr
            # would make one dialog's handler hide the other.
            def on_accept(filename: str, dirname: str) -> None:
                try:
                    if not result_future.done():
                        result_future.set_result(resolve(filename, dirname))
                finally:
                    _hide(dialog)

            def on_cancel(file_name: str, dir_name: str) -> None:
                if not result_future.done():
                    result_future.set_result(None)
                _hide(dialog)

            kwargs = dict(
                apply_button_label=apply_label,
                click_apply_handler=on_accept,
                click_cancel_handler=on_cancel,
                item_filter_fn=item_filter_fn,
            )
            if filter_options is not None:
                kwargs["item_filter_options"] = filter_options
            dialog = FilePickerDialog(title, **kwargs)
            self._picker = dialog  # item_filter closures read the active picker
            dialog.add_connections({"Local": "C:" if os.name == "nt" else "/"})
            dialog.show()
            await self._navigate_picker(initial_dir)
            try:
                return await asyncio.wait_for(
                    result_future, timeout=self._PICKER_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                _hide(dialog)
                return None
        except Exception:
            _log.exception("%s error", error_label)
            return None
        finally:
            # Destroy the window (hide alone leaks one per pick).
            if dialog is not None:
                try:
                    if hasattr(dialog, "destroy"):
                        dialog.destroy()
                except Exception:
                    pass
                if self._picker is dialog:
                    self._picker = None

    async def _navigate_picker(self, initial_dir: Optional[str]) -> None:
        """Point the just-shown picker at a remembered directory, defensively:
        only existing dirs, one frame after show (navigation races the async
        tree build), and never let a hiccup break the picker."""
        if not initial_dir:
            return
        try:
            if not os.path.isdir(initial_dir):
                return
            picker = self._picker
            if picker is None or not hasattr(picker, "navigate_to"):
                return
            await omni.kit.app.get_app().next_update_async()
            if self._picker is picker:  # not torn down while we waited
                picker.navigate_to(initial_dir.replace(os.sep, "/"))
        except Exception:
            _log.exception("File picker navigate_to failed (non-fatal)")

    async def _pick_folder(self, initial_dir: Optional[str] = None) -> Optional[str]:
        from omni.kit.widget.filebrowser import FileBrowserItem

        def item_filter(item: FileBrowserItem) -> bool:
            return bool(item) and item.is_folder

        def resolve(filename, dirname):
            dirname = (dirname or "").strip()
            return os.path.normpath(dirname) if dirname else None

        return await self._run_picker(
            title="Select Parts Folder", apply_label="Select Folder",
            item_filter_fn=item_filter, resolve=resolve,
            error_label="Folder picker", initial_dir=initial_dir,
        )

    def _file_filter(self, extensions: list[str]) -> tuple:
        """``(extensions_lower, filter_options, item_filter)`` for a file pick.

        The item filter hides non-matching files unless the user flips the
        dropdown to "All Files"."""
        extensions_lower = [ext.lower() for ext in extensions]
        ext_text = ", ".join(f"*{ext}" for ext in extensions)
        filter_options = [f"Supported Files ({ext_text})", "All Files (*)"]

        def item_filter(item) -> bool:
            if not item or item.is_folder:
                return True
            if hasattr(item, "path"):
                _, ext = os.path.splitext(item.path)
                if self._picker is not None and self._picker.current_filter_option != 0:
                    return True
                return ext.lower() in extensions_lower
            return False

        return extensions_lower, filter_options, item_filter

    async def _pick_file(
        self, extensions: list[str], initial_dir: Optional[str] = None
    ) -> Optional[str]:
        extensions_lower, filter_options, item_filter = self._file_filter(extensions)

        def resolve(filename, dirname):
            dirname = (dirname or "").strip()
            if dirname and filename:
                fullpath = os.path.normpath(os.path.join(dirname, filename))
                _, ext = os.path.splitext(fullpath)
                if ext.lower() in extensions_lower and os.path.exists(fullpath):
                    return fullpath
                _log.warning("FilePicker: invalid selection %s", fullpath)
            return None

        return await self._run_picker(
            title="Select a File", apply_label="Select",
            item_filter_fn=item_filter, resolve=resolve,
            filter_options=filter_options, error_label="File picker",
            initial_dir=initial_dir,
        )

    async def _pick_files(
        self, extensions: list[str], initial_dir: Optional[str] = None
    ) -> list[str]:
        """Like :meth:`_pick_file` but returns every selected file.

        The file browser supports Ctrl/Shift-click multi-selection; on apply we
        read the browser's full selection (falling back to the focused item)."""
        extensions_lower, filter_options, item_filter = self._file_filter(extensions)

        def resolve(filename, dirname):
            return self._collect_selected_files(filename, dirname, extensions_lower)

        result = await self._run_picker(
            title="Select File(s)", apply_label="Select",
            item_filter_fn=item_filter, resolve=resolve,
            filter_options=filter_options, error_label="File picker",
            initial_dir=initial_dir,
        )
        return result or []

    def _collect_selected_files(
        self, filename: str, dirname: str, extensions_lower: list[str]
    ) -> list[str]:
        """Gather the browser's multi-selection plus the apply-line file, then
        keep only existing files with an accepted extension (dedup, ordered)."""
        candidates: list[str] = []
        picker = self._picker
        # The file browser tracks every Ctrl/Shift-clicked item; read the whole
        # selection. Signature varies across Kit versions, so try the common
        # forms and fall back to the apply-line file on any mismatch.
        if picker is not None and hasattr(picker, "get_current_selections"):
            for call in (
                lambda: picker.get_current_selections(dirs=False),
                lambda: picker.get_current_selections(),
            ):
                try:
                    candidates.extend(call() or [])
                    break
                except Exception:
                    continue
        dirname = (dirname or "").strip()
        if filename and dirname:
            candidates.append(os.path.join(dirname, filename))

        out: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            path = self._to_local_path(raw)
            if not path:
                continue
            _, ext = os.path.splitext(path)
            if ext.lower() not in extensions_lower:
                continue
            if not os.path.exists(path) or path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    @staticmethod
    def _to_local_path(raw: str) -> Optional[str]:
        """Normalize a browser selection (which may be a ``file:`` URL) to an OS
        path. Returns ``None`` for empty input."""
        if not raw:
            return None
        path = str(raw).strip()
        for scheme in ("file://", "file:"):
            if path.startswith(scheme):
                path = path[len(scheme):]
                break
        # A leading slash before a Windows drive letter (``/C:/...``) trips up
        # normpath on Windows — strip it.
        if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return os.path.normpath(path)
