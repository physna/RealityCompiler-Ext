# Overview

## Introduction

Physna Reality Compiler is a **Omniverse Kit extension** that
drives the **hosted Physna Scan Search API**. All the heavy lifting —
point-cloud indexing, part matching, registration, and scoring — happens
on the Physna platform. The extension is a thin client: it authenticates,
lets the user select a scene and a set of parts, uploads them, waits for
indexing, reads the placements the engine found, and drops matched parts
back into the stage at the returned transforms.

Because matching is server-side, the extension has no GPU or Open3D
dependency and can be installed anywhere Kit runs.

## The Hosted Workflow

The API workflow is a variation on the standard asset lifecycle:

1. **Upload a scene** — a point-cloud capture (`.npy`/`.npz`/`.las`/`.laz`/
   `.xyz`/`.pts`/`.ply`/`.pcd`) into a tenant folder. It becomes a `scan`
   asset. Non-native scans are uploaded as extracted `.npy`. (E57 comes in
   through Kit's File > Import + "Use Selected Prim" — raw `.e57` uploads
   don't match on the platform.)
2. **Upload parts** — USD/CAD files into the **same parent folder**. Each
   becomes a `model` asset. Colocation is what triggers matching.
3. **Poll** each asset until it reaches a terminal state; the scene must
   reach `finished`.
4. **Read scene-matches** for each part: a list of placements, each with a
   `score` and a row-major `transform4x4` mapping the part's local
   geometry into scene coordinates.
5. **Place** a referenced copy of each matched part into the stage.

## High-Level Architecture

- `physna/reality_compiler/extension.py` — startup: install the thin-client
  packages, build the window, wire the manager and UI.
- `physna/reality_compiler/api/` — pure-Python client for the hosted API
  (no `omni`/`pxr` imports). Config, credentials, auth, HTTP client,
  models, polling, and the `ApiSession` facade.
- `physna/reality_compiler/pipelines/` — the workflow layer:
  - `PipelineManager` — composition root and UI-facing facade (login
    state, selection, run orchestration, Kit file pickers).
  - `ScanSearchWorkflow` — prepare → upload → poll → read matches → place.
  - `PipelineState` / `SceneSource` / `PartEntry` — cached run state.
- `physna/reality_compiler/scene/` — USD/stage helpers (`SceneOps`):
  selection, point extraction, prim creation, placement transforms.
- `physna/reality_compiler/converters/` — `MeshConverter` for converting
  CAD parts to USD (via the Omniverse asset converter) ahead of placement.
- `physna/reality_compiler/io/` — point-cloud file loaders. E57 reads run in
  an **isolated subprocess** (`e57_isolated.py` + `_e57_worker.py`): pye57
  segfaults inside Kit's process, so a crash becomes a reported error.
- `physna/reality_compiler/deps.py` / `paths.py` — background pip-install
  gate (`ensure_deferred_ready`) and the single temp-dir layout
  (`<os-temp>/physna_reality_compiler/`).
- `physna/reality_compiler/ui/` — the accordion property panel (Account /
  Scene / Parts / Run / Matches / Previous Searches) with a persistent
  progress bar and inline per-action progress.

## Runtime Flow

### Startup

`PhysnaRealityCompilerExtension` installs `requests` + `keyring`
synchronously and kicks the point-cloud format libraries (`laspy`, `lazrs`,
`pye57`, `trimesh`, `pypcd4`) off to a background install thread (see
`deps.py`; also declared in `extension.toml`), then constructs `SceneOps`,
the converters, the `PipelineManager` (which restores any stored login), and
builds the `Physna Reality Compiler` window. A file load that races the
background install awaits `deps.ensure_deferred_ready()` first.

### Authentication

The user signs in with a **service account** (client id + secret) created
in the Physna app. The token endpoint exchanges those for a one-hour
bearer token, cached in memory. The client **secret** is stored in the OS
credential vault via `keyring` — it never touches disk in plaintext.
`ApiSession.restore()` rebuilds the session on the next launch without
re-prompting.

### Selection & Upload

- **Scene**: pick a point-cloud file, or use the selected stage prim
  (its points are extracted to a temporary `.npy`).
- **Parts**: queue USD/CAD files individually or a whole folder.

`ScanSearchWorkflow.run()` uploads everything into a unique run folder
under `folder_root` (default `scan-search-demo/warehouse`), keeping the
scene and parts colocated so the platform matches them automatically.

For USD parts, the workflow discovers the part's dependencies
(`UsdUtils.ComputeAllDependencies`) and uploads the root plus its
sublayers/materials/textures into a per-part subfolder, **preserving the
relative layout** so those references resolve server-side. The root USD
is the queryable part; its dependencies upload as supporting assets
(polled so they index, never scene-matched). CAD parts (`.stl`/`.step`/…)
upload as a single file. A failed part upload is skipped rather than
aborting the run.

### Polling

All uploaded assets are polled on a ~30-second cadence until each reaches
a terminal state. Blocking calls run on a thread-pool executor so the Kit
UI stays responsive, a progress bar reflects the run's completion, and a
Cancel button interrupts between poll rounds. A transient error on one
asset is retried the next round rather than aborting the batch, and a
30-minute timeout prevents a stuck asset from hanging the run forever.

Runs are **checkpointed to disk right after upload** (marked incomplete in
the run store) and finalized on completion — if Kit closes mid-poll, the run
reappears in Previous Searches with a Resume button, and Refresh reconciles
any interrupted run that has since finished on the platform — or drops the
saved record if the run's assets were deleted from the platform.

### Results & Placement

Results populate incrementally — per-part status refreshes each poll round,
and a part's matches are read (and shown) the moment it and the scene reach
a terminal state, without waiting for the slowest part. A **Min score**
slider **gates** how many of each part's matches are eligible to place.
`SceneOps.compute_parent_local_transform` composes each `transform4x4` into
the scene prim's coordinate frame, and `create_xform_with_reference`
references the part USD into the stage at that transform (as a sibling of
the scene's Points prim), instanceable so repeated placements share one GPU
prototype. CAD parts are converted to USD via `MeshConverter` before
referencing. Physna's transform is **rigid** (no scale), so parts are
pre-scaled to real-world metres — USD parts by their authored
`metersPerUnit`, converted CAD parts assuming millimetres — and file-loaded
scans are rotated to the stage's up-axis via an xform on the scene prim, so
placements rotate with it.

A per-part slider places/removes matches live (`set_placed_count`
reconciles the stage), authored as one batch so a multi-part placement costs
a single Hydra sync. Placed matches can **hide the scan points they cover**
(reversibly, via per-point widths) so parts aren't buried. After placing,
the scene can also be carved by what matched — **Remove Matched Points**
deletes scene points inside the placed matches' bounding boxes (off-thread
so a large scan doesn't hitch), **Keep Only Matched** keeps just those.

### Platform Discovery

Runs aren't local-only. The API exposes a paginated asset list and a file
download (`GET /assets/{id}/file`), so `api/discovery.discover_runs`
groups the tenant's assets into runs (a `scan` at a folder root is the
scene; `model` assets under it are the parts), and
`manager.load_discovered_run` downloads a run's files — preserving the
folder layout so USD references resolve — reads its matches, and saves it
as a local run record. This surfaces runs created on any machine.

## Configuration And Dependencies

### Extension Metadata

Packaging metadata lives in [`config/extension.toml`](../config/extension.toml).

### Routing Configuration

`ApiConfig` carries the non-secret routing info — API base URL, tenant ID,
token endpoint, and scope — defaulting to the production Physna stack and
overridable from `PHYSNA_*` environment variables or the UI's Advanced
panel.

### Python Packages

At startup the extension installs, via `omni.kit.pipapi` (declared in
`extension.toml` and installed by `extension.py`):

- `requests` — HTTP client for the API
- `keyring` — secure OS credential store for the service-account secret
- `laspy` + `lazrs` — LAS/LAZ point clouds (`lazrs` decompresses `.laz`)
- `pye57` — E57 point clouds
- `trimesh` — PLY point clouds
- `pypcd4` — PCD point clouds

`numpy` is not installed — it ships with Kit, and pip-installing it can
shadow Kit's build. The per-format loaders are imported lazily (only when a
file of that type is opened) and live in `physna/reality_compiler/io/`.

## Release Notes

Release packaging is handled by
[`scripts/publish/make_release.py`](../scripts/publish/make_release.py) and
published by the tag-triggered workflow in
[`.github/workflows/release.yml`](../.github/workflows/release.yml). The
tag must match `config/extension.toml` and have a matching
[`CHANGELOG.md`](./CHANGELOG.md) entry.
