# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Async orchestration of the hosted scan-search workflow.

Bridges the synchronous, ``omni``-free :mod:`..api` client and the USD
scene helpers.  All blocking work (file IO, network, USD writes on the
main thread) is dispatched so the Kit event loop keeps pumping and the UI
stays responsive.

Flow (the hosted Scan Search workflow):

1. Prepare an uploadable point-cloud file for the scene (a file the user
   picked, or points extracted from a selected stage prim).
2. Upload the scene and every part into a unique, colocated run folder.
3. Poll all assets on a ~30s cadence until each reaches a terminal state.
4. Require the scene to be ``finished``; read scene-matches for every
   queryable part.
5. Place a referenced copy of each matched part into the stage using the
   returned 4x4 transform.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

import numpy as np

import omni.kit.app

from ..api import (
    ApiError,
    Asset,
    Match,
    PhysnaClient,
    PollState,
    SCENE_REQUIRED_STATE,
    is_asset_not_found,
    is_queryable,
    is_terminal,
    poll_step,
)
from ..api.polling import DEFAULT_POLL_INTERVAL_S
from ..logger import get_logger
from ..paths import TEMP_ROOT
from ..scene import SceneOps
from ..scene.usd_deps import UploadItem as _UploadItem
from .state import PartEntry, PipelineState

_log = get_logger("physna.reality_compiler.pipelines.workflow")

ProgressFn = Callable[[str], None]


def _safe(cb: Optional[Callable], *args) -> None:
    """Invoke an optional UI callback, swallowing errors (never break a run)."""
    if cb is None:
        return
    try:
        cb(*args)
    except Exception:
        _log.exception("workflow callback failed")


def _progress_fns(on_progress: Optional[ProgressFn]) -> tuple:
    """``(progress, tick)`` wrappers over an optional UI progress callback.

    ``progress`` logs and forwards (round summaries, milestones); ``tick``
    only forwards (per-second countdown updates — no log spam)."""
    def progress(msg: str) -> None:
        _log.info(msg)
        _safe(on_progress, msg)

    def tick(msg: str) -> None:
        _safe(on_progress, msg)

    return progress, tick


async def _countdown(seconds: float, on_tick: Optional[ProgressFn], label: str) -> None:
    """Sleep ``seconds`` a second at a time, ticking ``label; checking again in Ns``.

    The per-second updates let the user see the timer moving and let a Cancel
    land within ~1s instead of blocking on one long sleep."""
    remaining = max(1, int(round(seconds)))
    while remaining > 0:
        _safe(on_tick, f"{label}; checking again in {remaining}s")
        await asyncio.sleep(1)
        remaining -= 1

# Temp dir for files we generate to upload (extracted scene points, converted
# part USDs). One place so the layout is defined once.
_UPLOADS_DIR = os.path.join(TEMP_ROOT, "uploads")


def _cleanup_temp_file(path: Optional[str]) -> None:
    """Delete a file we wrote under ``_UPLOADS_DIR`` (never a user's own file)."""
    try:
        if path and os.path.commonpath([_UPLOADS_DIR, os.path.abspath(path)]) == _UPLOADS_DIR:
            os.remove(path)
    except Exception:
        pass


# Point-cloud extensions the API accepts directly as a scene (scan type).
_SCENE_EXTS = {".ply", ".e57", ".pcd", ".npy", ".npz"}

# USD-family extensions that can be referenced into the stage directly.
_USD_EXTS = {".usd", ".usda", ".usdc", ".usdz"}

# Stop polling after this long even if some assets never terminalize, so
# the UI never hangs indefinitely on a stuck asset.
DEFAULT_POLL_TIMEOUT_S = 30 * 60.0  # 30 minutes

# After the scene + a part both reach 'finished', the platform computes their
# scene-match relationship as a separate async step; querying it too early
# returns 404 "Scene match relationship has not been computed". Keep retrying
# on the poll cadence up to this long before giving up on a part.
MATCH_WAIT_TIMEOUT_S = 10 * 60.0  # 10 minutes


def _is_matches_pending(exc: Exception) -> bool:
    """True for the 404 the API returns while a scene-match is still computing.

    That's a transient "not ready yet" (the matcher runs a beat after both
    assets finish), not a real failure — the caller should retry, not surface
    it as an error."""
    return (
        isinstance(exc, ApiError)
        and exc.status_code == 404
        and "not been computed" in (getattr(exc, "body", "") or "").lower()
    )


# Shown per part when its platform asset was deleted. Rendered by the UI as
# "<name>   [couldn't read matches] <this>", so keep it short and lowercase.
_GONE_MSG = "no longer on the platform (deleted)"


def sanitize_folder_name(name: str) -> str:
    """Turn a user-entered run name into a safe single path segment.

    Keeps alphanumerics, dash, underscore, and dot; collapses runs of
    other characters (spaces, slashes) to a single dash; trims leading and
    trailing dashes/dots. Returns "" if nothing usable remains.
    """
    out = []
    prev_dash = False
    for ch in (name or "").strip():
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-.")


class WorkflowError(RuntimeError):
    """A hard stop that should abort the whole run (e.g. scene not finished)."""


class ScanSearchWorkflow:
    """Stateless-ish orchestrator; all run state lives in :class:`PipelineState`."""

    # Physna's scene-match transform is RIGID (no scale) and the scene is
    # uploaded/displayed in real-world metres, so a placed part must also be in
    # metres. The scale that gets there is the part's "metres per unit". CAD/STL
    # carries no unit and the converter's output tag is meaningless, so assume
    # millimetres (the near-universal CAD default): 1 unit = 1 mm = 0.001 m.
    # A cm part -> set Size 10; a metre part -> 1000.
    _CAD_ASSUMED_MPU = 0.001

    def __init__(self, scene_ops: SceneOps, mesh_converter=None) -> None:
        self._scene = scene_ops
        # Optional MeshConverter — used to convert CAD parts (.stl/.step/…)
        # into a USD we can reference into the stage for placement.
        self._mesh_converter = mesh_converter

    # ------------------------------------------------------------------
    # File preparation
    # ------------------------------------------------------------------

    async def prepare_scene_file(self, state: PipelineState) -> Optional[str]:
        """Return a local point-cloud file to upload as the scene.

        An API-native file (``_SCENE_EXTS``) is uploaded as-is. Anything else -
        a selected stage prim, or a non-native file (LAS/LAZ/XYZ/PTS) we loaded
        into a ``Points`` prim - is uploaded as its points extracted to a
        temporary ``.npy`` (the API only ingests the native scan formats).
        """
        src = state.scene
        loop = asyncio.get_running_loop()

        ext = Path(src.file_path).suffix.lower() if src.file_path else None
        if src.file_path and ext in _SCENE_EXTS:
            return src.file_path

        # Non-native file that's in the stage as a prim, or a directly selected
        # prim: extract its points and upload those.
        if src.prim_path:
            extracted = await self._scene.extract_point_cloud(
                src.prim_path, track_actual_prim=True
            )
            if not extracted:
                raise WorkflowError(
                    f"Could not extract points from prim {src.prim_path}."
                )
            points, _colors, actual_prim = extracted
            src.actual_points_prim_path = actual_prim or src.prim_path
            # np.save on a multi-million-point scene is a real chunk of
            # CPU/IO — never run it on the UI loop thread.
            stem = Path(src.prim_path).name or "scene"
            return await loop.run_in_executor(
                None, lambda: self._save_points_npy(points, stem)
            )

        # Non-native file that couldn't be shown in the stage: load it directly
        # (LAS/LAZ/XYZ/PTS etc.) and upload the points.
        if src.file_path:
            from .. import deps
            from ..io import load_point_cloud

            # Format libraries install on a background thread at startup; wait
            # for them rather than surfacing a spurious ImportError.
            await deps.ensure_deferred_ready()

            try:
                points = (
                    await loop.run_in_executor(
                        None, lambda: load_point_cloud(src.file_path)
                    )
                )[0]
            except Exception as exc:
                raise WorkflowError(
                    f"Could not read scene file '{src.file_path}': {exc}"
                )
            stem = Path(src.file_path).stem or "scene"
            return await loop.run_in_executor(
                None, lambda: self._save_points_npy(points, stem)
            )

        return None

    @staticmethod
    def _save_points_npy(points: np.ndarray, stem: str) -> str:
        """Write an Nx3 float array to a temp ``.npy`` for upload."""
        safe_stem = "".join(c if c.isalnum() else "_" for c in stem) or "scene"
        os.makedirs(_UPLOADS_DIR, exist_ok=True)
        path = os.path.join(_UPLOADS_DIR, f"{safe_stem}_{uuid.uuid4().hex[:8]}.npy")
        np.save(path, np.asarray(points, dtype=np.float32))
        return path

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(
        self,
        client: PhysnaClient,
        state: PipelineState,
        *,
        on_progress: Optional[ProgressFn] = None,
        on_fraction: Optional[Callable[[float], None]] = None,
        on_status: Optional[Callable[[], None]] = None,
        on_part_matches: Optional[Callable[[PartEntry], None]] = None,
        on_uploaded: Optional[Callable[[], None]] = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> None:
        """Upload, poll, and read matches. Populates ``state`` in place.

        Callbacks (all fired on the caller's loop thread, safe for UI):
        ``on_progress`` (text), ``on_fraction`` (0..1, -1 = indeterminate),
        ``on_status`` (after each poll round, to refresh per-part state),
        ``on_part_matches`` (after each part's matches are read),
        ``on_uploaded`` (once, after every asset is uploaded and has an id —
        the checkpoint the run can be resumed from if the app dies mid-poll).
        """
        loop = asyncio.get_running_loop()
        progress, tick = _progress_fns(on_progress)

        def frac(value: float) -> None:
            _safe(on_fraction, value)

        if not state.parts:
            raise WorkflowError("No parts selected to search for.")

        frac(-1.0)
        progress("Preparing scene for upload...")
        scene_file = await self.prepare_scene_file(state)
        if not scene_file:
            raise WorkflowError("No scene selected (pick a file or a stage prim).")

        name = sanitize_folder_name(state.run_name) or f"run-{uuid.uuid4().hex[:8]}"
        prefix = state.folder_root.strip("/")
        state.run_folder = f"{prefix}/{name}" if prefix else name

        # --- upload scene ---
        scene_name = Path(scene_file).name
        progress(f"Uploading scene: {scene_name}")
        state.scene_asset = await loop.run_in_executor(
            None,
            lambda: client.upload_asset(
                scene_file, f"{state.run_folder}/{scene_name}"
            ),
        )
        _log.info("Scene asset %s (%s)", state.scene_asset.id, state.scene_asset.state)
        # The extracted scene .npy holds the full scan geometry and is only
        # needed for the upload - don't leave it lying in temp.
        _cleanup_temp_file(scene_file)

        # --- upload parts (+ their USD dependencies) ---
        # Uploads occupy the 0.0–0.2 band of the progress bar.
        for i, part in enumerate(state.parts, start=1):
            progress(f"Uploading part {i}/{len(state.parts)}: {part.display_name}")
            await self._upload_part(client, loop, state, part, i, progress)
            frac(0.2 * i / len(state.parts))

        # Checkpoint: every asset now has a platform id. Persist the run as
        # in-progress so a mid-poll app close leaves something to resume.
        _safe(on_uploaded)

        # --- poll to terminal, then read matches (0.2–1.0 band) ---
        await self._finalize(
            client, state, progress, frac, on_status, on_part_matches,
            poll_interval_s, poll_timeout_s, on_tick=tick,
        )

    async def refresh(
        self,
        client: PhysnaClient,
        state: PipelineState,
        *,
        on_progress: Optional[ProgressFn] = None,
        on_status: Optional[Callable[[], None]] = None,
        on_part_matches: Optional[Callable[[PartEntry], None]] = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> None:
        """Re-poll and re-read matches for an already-uploaded run.

        Used to bring a run reloaded from disk up to date (e.g. one that
        was saved mid-indexing). Uses the asset ids already in ``state``;
        performs no uploads.
        """
        progress, tick = _progress_fns(on_progress)
        if state.scene_asset is None:
            raise WorkflowError("This run has no uploaded scene to refresh.")
        await self._finalize(
            client, state, progress, lambda _v: None, on_status, on_part_matches,
            poll_interval_s, poll_timeout_s, on_tick=tick,
        )

    async def reconcile(self, client: PhysnaClient, state: PipelineState) -> str:
        """One-shot, non-blocking completion check for a run rebuilt from disk.

        Polls each asset exactly ONCE (never waits on the poll cadence, unlike
        ``refresh``). Returns one of:
        - ``"complete"`` — everything is terminal and the scene is queryable;
          each part's matches have been read into ``state``;
        - ``"gone"``     — the run was deleted from the platform (all assets
          404, or the scene is gone, or no part's matches can ever be read),
          so it can never finish or be resumed;
        - ``"pending"``  — anything else (still indexing, scene not queryable,
          a match relationship still computing, or a transient read failure),
          leaving the run resumable.
        """
        loop = asyncio.get_running_loop()
        ids = [a.id for a in self._all_assets(state)]
        if not ids:
            return "pending"
        poll = PollState.for_ids(ids)
        await loop.run_in_executor(None, lambda: poll_step(client, poll))
        if poll.miss_counts:
            # Deletion needs MISSING_CONFIRMATIONS consecutive observations;
            # confirm now rather than leaving the run in limbo until the next
            # Refresh (this is the only single-step poller).
            await asyncio.sleep(1.0)
            await loop.run_in_executor(None, lambda: poll_step(client, poll))
        self._apply_resolved(state, poll)
        if len(poll.missing) == len(ids):
            return "gone"  # nothing left on the platform
        scene = state.scene_asset
        if scene is not None and scene.id in poll.missing:
            return "gone"  # scene deleted -> the run can never be matched
        if not poll.done:
            return "pending"  # something is still indexing
        if scene is None or scene.state != SCENE_REQUIRED_STATE:
            return "pending"  # scene never became queryable
        noop: ProgressFn = lambda _m: None
        gone = 0
        checked = 0
        for part in state.parts:
            asset = part.asset
            if asset is None:
                continue
            checked += 1
            if asset.id in poll.missing:
                gone += 1  # deleted on the platform; can never be read
                continue
            if not is_terminal(asset.state):
                continue
            outcome = await self._read_part_matches(loop, client, part, scene, noop)
            if outcome == "pending" or outcome == "error":
                # Still computing, or a transient read failure — either way the
                # truth is unknown, so the run must stay resumable. Marking it
                # complete here would overwrite checkpointed matches with [].
                return "pending"
            if outcome == "gone":
                gone += 1
        if checked and gone == checked:
            return "gone"  # every part's data is unreadable forever
        return "complete"

    async def _finalize(
        self,
        client: PhysnaClient,
        state: PipelineState,
        progress: ProgressFn,
        frac: Callable[[float], None],
        on_status: Optional[Callable[[], None]],
        on_part_matches: Optional[Callable[[PartEntry], None]],
        poll_interval_s: float,
        poll_timeout_s: float,
        on_tick: Optional[ProgressFn] = None,
    ) -> None:
        """Poll all assets to terminal, reading each part's matches the moment
        it (and the scene) are ready, so an early-finishing part shows its
        results without waiting for the slowest one to finish indexing."""
        loop = asyncio.get_running_loop()

        def status() -> None:
            _safe(on_status)

        def part_done(part: PartEntry) -> None:
            _safe(on_part_matches, part)

        resolved_ids: set[str] = set()  # part-asset ids whose matches are settled

        async def read_ready() -> None:
            """Read matches for any newly-terminal parts. No-op until the scene
            is queryable (scene-matches need both the scene and the part). A part
            whose match is still computing (``pending``) is left unresolved so a
            later round retries it; anything settled is read at most once."""
            scene = state.scene_asset
            if scene is None or scene.state != SCENE_REQUIRED_STATE:
                return
            for part in state.parts:
                asset = part.asset
                if (asset is None or asset.id in resolved_ids
                        or not is_terminal(asset.state)):
                    continue
                outcome = await self._read_part_matches(loop, client, part, scene, progress)
                if outcome == "pending":
                    continue  # relationship still computing — retry next round
                resolved_ids.add(asset.id)
                part_done(part)  # surface this part's results as soon as ready

        # --- poll until every asset is terminal, reading matches as parts land ---
        poll = await self._poll_all(
            client, state, progress, frac, status,
            poll_interval_s, poll_timeout_s, on_round=read_ready, on_tick=on_tick,
        )

        # --- react to assets deleted on the platform ---
        if poll.missing:
            self._flag_gone_parts(state, poll.missing)
            status()
            scene = state.scene_asset
            if len(poll.missing) == len(self._all_assets(state)):
                raise WorkflowError(
                    "This search was deleted from the platform - its assets no "
                    "longer exist there. Any locally saved matches still load; "
                    "use Delete to remove the search from the list."
                )
            if scene is not None and scene.id in poll.missing:
                raise WorkflowError(
                    "This search's scene was deleted from the platform, so "
                    "matches can no longer be read. Any locally saved matches "
                    "still load; use Delete to remove the search from the list."
                )
            # Only some parts are gone: keep going and read the survivors.

        # --- enforce scene policy ---
        scene = state.scene_asset
        if scene is None or scene.state != SCENE_REQUIRED_STATE:
            raise WorkflowError(
                f"Scene did not reach '{SCENE_REQUIRED_STATE}' "
                f"(state={scene.state if scene else 'unknown'}); nothing to match."
            )

        # --- final sweep + wait for lazily-computed matches: the assets are all
        #     finished, but a part's scene-match relationship can lag a little
        #     behind (transient 404). Keep retrying the stragglers on the poll
        #     cadence until they compute or we hit the wait timeout. ---
        await read_ready()
        await self._await_pending_matches(
            loop, client, state, list(state.parts), resolved_ids, progress,
            on_tick, part_done, poll_interval_s,
        )

        total = sum(len(part.matches or []) for part in state.parts)
        frac(1.0)
        progress(
            f"Done - {total} placement(s) across {len(state.parts)} part(s)."
        )

    async def _await_pending_matches(
        self,
        loop: asyncio.AbstractEventLoop,
        client: PhysnaClient,
        state: PipelineState,
        parts: list[PartEntry],
        resolved_ids: set[str],
        progress: ProgressFn,
        on_tick: Optional[ProgressFn],
        part_done: Callable[[PartEntry], None],
        interval_s: float,
    ) -> None:
        """Retry the given parts whose scene-match relationship is still
        computing.

        Waits on the poll cadence (with a live countdown) up to
        ``MATCH_WAIT_TIMEOUT_S``; on timeout, the stragglers get an informative
        ``match_error`` rather than hanging the run forever."""
        scene = state.scene_asset
        if scene is None:
            return

        def pending() -> list[PartEntry]:
            return [
                p for p in parts
                if p.asset and is_queryable(p.asset.state)
                and p.asset.id not in resolved_ids
            ]

        started = time.monotonic()
        while True:
            waiting = pending()
            if not waiting:
                return
            if time.monotonic() - started > MATCH_WAIT_TIMEOUT_S:
                for p in waiting:
                    if not p.match_error:
                        p.match_error = (
                            "scene match not computed yet (timed out waiting; "
                            "reopen the run later to read it)"
                        )
                    resolved_ids.add(p.asset.id)
                    part_done(p)
                return
            base = f"Waiting for scene matches ({len(waiting)} computing)"
            _log.info("%s; retry in %ds", base, int(interval_s))
            await _countdown(interval_s, on_tick, base)
            for p in waiting:
                outcome = await self._read_part_matches(loop, client, p, scene, progress)
                if outcome != "pending":
                    resolved_ids.add(p.asset.id)
                    part_done(p)

    async def _read_part_matches(
        self,
        loop: asyncio.AbstractEventLoop,
        client: PhysnaClient,
        part: PartEntry,
        scene: Asset,
        progress: ProgressFn,
    ) -> str:
        """Read one part's scene-matches against ``scene``. Sets ``part.matches``
        and ``part.match_error`` in place.

        Returns one of:
        - ``"ready"``   — resolved (matches read, or the part isn't queryable so
          there's simply nothing to match);
        - ``"pending"`` — the scene-match relationship is still being computed
          (transient 404); the caller should retry on the next poll round;
        - ``"gone"``    — the part or scene asset was deleted on the platform
          (permanent 404); any already-loaded matches are kept;
        - ``"error"``   — a real failure, recorded in ``part.match_error``.
        """
        part.match_error = None
        asset = part.asset
        if asset is None or not is_queryable(asset.state):
            _log.info(
                "Skipping matches for %s (state=%s)",
                part.display_name,
                asset.state if asset else "none",
            )
            part.matches = []
            part.matches_pending = False  # nothing to read for this part
            return "ready"
        progress(f"Reading matches: {part.display_name}")
        try:
            result = await loop.run_in_executor(
                None,
                lambda a=asset: client.get_scene_matches(a.id, scene.id),
            )
            part.matches = result.matches
            part.matches_pending = False
            return "ready"
        except Exception as exc:
            if _is_matches_pending(exc):
                _log.info(
                    "Scene match for %s not computed yet; will retry",
                    part.display_name,
                )
                part.matches_pending = True
                return "pending"
            if is_asset_not_found(exc):
                # Deleted on the platform — permanent, so don't surface the
                # raw response; keep any locally saved matches usable.
                _log.info(
                    "Asset for %s was deleted on the platform", part.display_name
                )
                part.match_error = _GONE_MSG
                part.matches_pending = False  # nothing will ever be readable
                return "gone"
            _log.exception("scene-matches failed for %s", part.display_name)
            progress(f"Matches failed for {part.display_name}: {exc}")
            part.matches = []
            part.match_error = str(exc)
            part.matches_pending = True  # unread — keep the run resumable
            return "error"

    async def add_part(
        self,
        client: PhysnaClient,
        state: PipelineState,
        part: PartEntry,
        *,
        on_progress: Optional[ProgressFn] = None,
        on_status: Optional[Callable[[], None]] = None,
        on_part_matches: Optional[Callable[[PartEntry], None]] = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> None:
        """Add one part to the active run and read its matches (no re-upload).

        Colocation means uploading a new part into the existing run folder
        matches it against the already-finished scene automatically. Polls
        only the new part (+ its deps); reads only its matches.
        """
        if not state.run_folder or state.scene_asset is None:
            raise WorkflowError("No active run; run a search first, then add parts.")
        scene = state.scene_asset
        if scene.state != SCENE_REQUIRED_STATE:
            raise WorkflowError(
                f"Scene is not finished (state={scene.state}); cannot add parts yet."
            )

        loop = asyncio.get_running_loop()
        progress, _tick = _progress_fns(on_progress)

        def status() -> None:
            _safe(on_status)

        # Position by identity (PartEntry is eq=False): a retried duplicate of
        # the same file must get its OWN index, not the first equal entry's —
        # the index feeds the tenant-wide-unique upload folder.
        index = next(
            (i + 1 for i, p in enumerate(state.parts) if p is part),
            len(state.parts),
        )
        progress(f"Uploading part: {part.display_name}")
        await self._upload_part(client, loop, state, part, index, progress)
        if part.asset is None:
            raise WorkflowError(f"Upload failed for {part.display_name}.")

        # Poll only this part + its supporting assets (same loop as a full run;
        # _apply_resolved matches by id, so the subset applies cleanly).
        poll = await self._poll_all(
            client, state, progress, lambda _v: None, status,
            poll_interval_s, poll_timeout_s, on_tick=_tick,
            ids=[part.asset.id] + [a.id for a in part.supporting_assets],
            subject=part.display_name,
        )
        if part.asset.id in poll.missing:
            raise WorkflowError(
                f"'{part.display_name}' was deleted from the platform while it "
                "was indexing; add it again to retry."
            )

        # Read matches, retrying while the scene-match relationship is still
        # being computed (a lag after both assets finish -> transient 404).
        outcome = await self._read_part_matches(loop, client, part, scene, progress)
        if outcome == "pending":
            await self._await_pending_matches(
                loop, client, state, [part], set(), progress, on_progress,
                lambda _p: None, poll_interval_s,
            )
        _safe(on_part_matches, part)
        progress(
            f"Done: {len(part.matches or [])} placement(s) for {part.display_name}."
        )

    async def _upload_part(
        self,
        client: PhysnaClient,
        loop: asyncio.AbstractEventLoop,
        state: PipelineState,
        part: PartEntry,
        index: int,
        progress: ProgressFn,
    ) -> None:
        """Upload a part and its USD dependencies, preserving relative layout.

        Each part gets its own subfolder so parts that share dependency
        filenames don't collide on the tenant-wide-unique path. The root
        USD becomes the queryable ``part.asset``; the rest are supporting
        assets (polled so references resolve, never scene-matched).
        """
        part.asset = None
        part.supporting_assets = []

        ext = Path(part.source_path).suffix.lower()
        if ext in _USD_EXTS:
            items = self._scene.compute_usd_upload_set(part.source_path)
        else:
            items = [_UploadItem(part.source_path, Path(part.source_path).name, True)]

        part_folder = f"{self._scene.safe_identifier(part.display_name)}_{index}"
        base = f"{state.run_folder}/parts/{part_folder}"

        for item in items:
            dest = f"{base}/{item.rel_path}"
            try:
                asset = await loop.run_in_executor(
                    None,
                    lambda p=item.local_path, d=dest: client.upload_asset(p, d),
                )
            except Exception as exc:
                _log.exception("Upload failed for %s", item.local_path)
                if item.is_root:
                    # No root asset -> the part is unusable; stop here.
                    progress(f"Upload failed for {part.display_name}: {exc}")
                    part.asset = None
                    return
                # A missing dependency is tolerated (missing-dependencies).
                progress(f"Dependency upload failed ({item.rel_path}): {exc}")
                continue
            if item.is_root:
                part.asset = asset
            else:
                part.supporting_assets.append(asset)

    async def _poll_all(
        self,
        client: PhysnaClient,
        state: PipelineState,
        progress: ProgressFn,
        frac: Callable[[float], None],
        status: Callable[[], None],
        interval_s: float,
        timeout_s: float,
        on_round: Optional[Callable[[], Awaitable[None]]] = None,
        on_tick: Optional[ProgressFn] = None,
        ids: Optional[list[str]] = None,
        subject: str = "",
    ) -> PollState:
        """Poll assets to terminal, applying resolutions into ``state`` each
        round. Polls every asset in the run by default; pass ``ids`` to poll a
        subset (add-part polls just that part + its dependencies). ``subject``
        names the work in the timeout error (defaults to a count summary).
        Returns the final ``PollState`` so callers can react to assets that
        turned out to be deleted (``poll.missing``)."""
        loop = asyncio.get_running_loop()
        if ids is None:
            ids = [a.id for a in self._all_assets(state)]
        poll = PollState.for_ids(ids)
        total = max(1, len(ids))
        started = time.monotonic()
        round_no = 0
        while not poll.done:
            round_no += 1
            await loop.run_in_executor(None, lambda: poll_step(client, poll))
            self._apply_resolved(state, poll)
            frac(0.2 + 0.7 * len(poll.resolved) / total)
            status()  # refresh per-part status labels each round (main thread)
            if poll.done:
                # Callers do a final read after the loop, and they may need to
                # bail out first (e.g. every asset turned out deleted) — don't
                # start reading matches here on the last round.
                break
            if on_round is not None:
                await on_round()  # read matches for parts that just finished
            if time.monotonic() - started > timeout_s:
                self._apply_resolved(state, poll)
                raise WorkflowError(
                    f"Timed out after {int(timeout_s)}s"
                    + (f" indexing {subject}" if subject else "")
                    + f" with {len(poll.pending)}/{len(ids)} asset(s) still indexing."
                )
            base = (
                f"Indexing{' ' + subject if subject else '...'} "
                f"{len(poll.resolved)}/{len(ids)} ready (round {round_no})"
            )
            _log.info("%s; checking again in %ds", base, int(interval_s))
            await _countdown(interval_s, on_tick, base)
        self._apply_resolved(state, poll)
        return poll

    @staticmethod
    def _all_assets(state: PipelineState) -> list[Asset]:
        assets = [state.scene_asset] if state.scene_asset else []
        for part in state.parts:
            if part.asset:
                assets.append(part.asset)
            assets.extend(part.supporting_assets)
        return assets

    @staticmethod
    def _flag_gone_parts(state: PipelineState, missing: set[str]) -> None:
        """Give parts whose platform asset was deleted a human-readable error.

        Keeps any locally saved matches — they're still valid placements, the
        platform just can't recompute them anymore."""
        for part in state.parts:
            if part.asset and part.asset.id in missing and not part.match_error:
                part.match_error = _GONE_MSG

    @staticmethod
    def _apply_resolved(state: PipelineState, poll: PollState) -> None:
        """Copy terminal states back into the cached assets."""
        if state.scene_asset and state.scene_asset.id in poll.resolved:
            state.scene_asset = poll.resolved[state.scene_asset.id]
        for part in state.parts:
            if part.asset and part.asset.id in poll.resolved:
                part.asset = poll.resolved[part.asset.id]
            part.supporting_assets = [
                poll.resolved.get(a.id, a) for a in part.supporting_assets
            ]

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    async def _resolve_placement_url(self, part: PartEntry) -> Optional[str]:
        """Return a stage-referenceable USD URL for *part*, converting if needed.

        USD parts are referenced directly. A CAD part (.stl/.step/…) can't
        be referenced into USD as-is, so it's converted to a temporary USD
        via ``MeshConverter`` (result cached on the part). Returns ``None``
        if the part can't be made placeable.
        """
        if part.placement_url:
            return part.placement_url

        ext = Path(part.source_path).suffix.lower()
        if ext in _USD_EXTS:
            part.placement_url = self._scene.to_asset_url(part.source_path)
            # USD references don't apply metersPerUnit across layers, so read
            # the authored value and scale to metres at placement.
            part.auto_scale = self._scene.get_usd_meters_per_unit(part.placement_url)
            return part.placement_url

        if self._mesh_converter is None:
            _log.warning(
                "Cannot place %s: %s is not USD and no converter is available",
                part.display_name, ext,
            )
            return None

        os.makedirs(_UPLOADS_DIR, exist_ok=True)
        out = os.path.join(_UPLOADS_DIR, f"{self._scene.safe_identifier(part.display_name)}_{uuid.uuid4().hex[:8]}.usd")
        try:
            ok = await self._mesh_converter.mesh_to_usd(part.source_path, out)
        except Exception:
            _log.exception("mesh_to_usd failed for %s", part.source_path)
            return None
        if not ok or not os.path.exists(out):
            _log.warning("Could not convert %s to USD for placement", part.source_path)
            return None
        part.placement_url = self._scene.to_asset_url(out)
        # The converter's metersPerUnit is meaningless (forced 1.0), so assume
        # mm — see _CAD_ASSUMED_MPU for the full rationale.
        part.auto_scale = self._CAD_ASSUMED_MPU
        return part.placement_url

    @staticmethod
    def _scale_pose(pose: np.ndarray, scale: float) -> np.ndarray:
        """Pre-scale a match pose so the referenced geometry is scaled *before*
        the match transform is applied (geometry -> scale -> pose). Leaves the
        pose's translation untouched (scale only affects the linear block)."""
        if scale == 1.0:
            return pose
        return pose @ np.diag([scale, scale, scale, 1.0])

    def _place_many(
        self, state: PipelineState, part: PartEntry, matches: list, url: str
    ) -> list:
        """Place `matches` as referenced Xforms in one batch.

        Transforms are computed up front, then every prim is authored in a
        single pass with no awaits in between, so Hydra syncs once for the whole
        set on the next tick instead of once per prim - the dominant cost when
        placing many at a time.
        """
        if not matches:
            return []
        name = self._scene.safe_identifier(f"{part.display_name}_match")
        # Scale the referenced geometry to metres before the rigid match
        # transform places it (see PartEntry.auto_scale).
        scale = part.auto_scale
        _log.info("Placing %s at size x%.4g", part.display_name, scale)
        scene_prim = state.scene.prim_path
        if scene_prim:
            actual = state.scene.actual_points_prim_path or scene_prim
            parent = self._scene.resolve_import_parent_path(actual, scene_prim)
            transforms = [
                self._scene.compute_parent_local_transform(
                    self._scale_pose(m.transform4x4, scale), actual, parent
                )
                for m in matches
            ]
        else:
            # No scene prim in the stage: place in world space under /World.
            parent = "/World"
            self._scene.ensure_prim_exists(parent, "Xform")
            transforms = [
                self._scene.numpy_to_gf_matrix4d(
                    self._scale_pose(m.transform4x4, scale)
                )
                for m in matches
            ]

        base = f"{parent}/{name}"
        paths = self._scene.create_xforms_with_references(
            [(base, t, url) for t in transforms]
        )
        part.placed_prim_paths.extend(paths)
        return paths

    def selected_matches(self, state: PipelineState, part: PartEntry) -> list[Match]:
        """Matches that would be placed: at/above min_score, capped at import_limit.

        ``part.matches`` is already sorted by score descending, so the cap
        keeps the top-scoring ones.
        """
        matches = [m for m in part.matches if m.score >= state.min_score]
        if part.import_limit is not None:
            matches = matches[: max(0, part.import_limit)]
        return matches

    @staticmethod
    def qualifying_count(state: PipelineState, part: PartEntry) -> int:
        """How many of a part's matches meet the min-score threshold.

        Matches are sorted by score descending, so this is a top prefix - the
        cap on how many can be placed. Placements are always the top-scoring
        matches, so any count up to this is guaranteed to be at/above min_score.
        """
        return sum(1 for m in part.matches if m.score >= state.min_score)

    async def place_all(self, state: PipelineState) -> int:
        """Place every part's matches that meet min_score; returns total placed.

        Reconciles through :meth:`set_placed_count` so it shares the same
        placed-prim list as the per-part slider (no double-placing).
        """
        for part in state.parts:
            await self.set_placed_count(
                state, part, self.qualifying_count(state, part)
            )
        return sum(len(p.placed_prim_paths) for p in state.parts)

    async def set_placed_count(
        self, state: PipelineState, part: PartEntry, target: int
    ) -> None:
        """Ensure exactly ``target`` of a part's top matches are in the stage.

        ``part.placed_prim_paths`` stays in score order (matches are sorted
        descending), so index i corresponds to the i-th best match. Places
        the missing top matches or removes the surplus, so a slider can
        drag placements in and out live.

        The target is capped at the min-score qualifying count, so raising
        min_score can only ever place matches at/above the threshold.
        """
        target = max(0, min(int(target), self.qualifying_count(state, part)))
        current = len(part.placed_prim_paths)
        if target == current:
            return
        app = omni.kit.app.get_app()
        if target > current:
            url = await self._resolve_placement_url(part)
            if not url:
                return
            self._place_many(state, part, part.matches[current:target], url)
            await app.next_update_async()
        else:
            surplus = part.placed_prim_paths[target:]
            part.placed_prim_paths = part.placed_prim_paths[:target]
            self._scene.delete_prims(surplus)
            await app.next_update_async()

    def placed_count(self, part: PartEntry) -> int:
        return len(part.placed_prim_paths)

    async def reapply_placements(
        self, state: PipelineState, part: PartEntry
    ) -> None:
        """Re-author a part's current placements at the current effective scale.

        Called after the placement scale changes (metersPerUnit or the user
        override). Removes the placed prims and re-creates the same top-N
        matches so the new scale takes effect. No-op if nothing is placed."""
        n = len(part.placed_prim_paths)
        if n == 0:
            return
        app = omni.kit.app.get_app()
        surplus = part.placed_prim_paths[:]
        part.placed_prim_paths = []
        self._scene.delete_prims(surplus)
        await app.next_update_async()
        url = await self._resolve_placement_url(part)
        if not url:
            return
        self._place_many(state, part, part.matches[:n], url)
        await app.next_update_async()
