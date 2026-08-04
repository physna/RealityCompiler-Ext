# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

> **Rearchitected at 0.4.0.** Through `v0.3.0` the extension ran an
> in-Omniverse matching engine. Starting with `0.4.0` it is a lightweight
> client for the hosted Physna Scan Search API — the two designs share
> almost no surface. The pre-0.4.0 local-matching history is preserved in
> git (tags `v0.1.0`–`v0.3.0`) but is not carried forward here.

## [0.6.0] - 2026-08-04

Open-source release. This version prepares the repository for its public
home: proper licensing, a support disclaimer, cleaned-up docs, and the
removal of the last dead code left over from the pre-0.4.0 in-process
matching engine.

### Added
- **Apache License 2.0.** A root `LICENSE` file, Physna SPDX headers on
  every source file, and the license shipped inside the release zip.
- **Support disclaimer** (`docs/DISCLAIMER.md`, also shown on the
  extension's documentation pages and summarized in the README): the
  project is open source and maintained best-effort — large issues and
  failures are prioritized; minor issues and feature requests may not
  receive a timely response. The hosted Physna platform remains a separate
  commercial service.

### Changed
- **Default environment is now Production** (`app-api.physna.com`) instead
  of the internal dev stack; a fresh install's Environment dropdown starts
  on "Production (app)" with the matching token endpoint pre-filled.
- Developer docs (`docs/DEVELOPING.md`, `AGENTS.md`) now use generic
  Python-environment instructions instead of machine-specific paths.

### Removed
- **`PointCloudConverter`** — dead code from the in-process matching era
  (USD/STL→NPZ export, Open3D mesh reconstruction); nothing called it. CAD
  parts still convert to USD for placement via `MeshConverter`.
- The NVIDIA-proprietary Kit-template license headers (replaced by the
  Apache-2.0 headers above).
- The internal API guide (`docs/API-Guide-ScanSearch.md`) — it carried
  internal stack URLs and a tenant ID and had drifted out of date; the
  workflow summary lives in `docs/Overview.md`.
- Stale editor/tooling config: tracked `.vscode/` files (already
  gitignored) and an unused `renovate.json`.

## [0.5.0] - 2026-07-16

Reliability + workflow release: interrupted searches are now resumable,
placed parts land at real-world size, E57 reads can no longer crash Kit, and
the panel got a broad UX/performance pass.

### Added
- **Resume interrupted searches.** Runs are checkpointed to disk right after
  upload (marked incomplete) and finalized on completion — if Kit closes
  mid-search, the run reappears in Previous Searches with a **Resume** button
  that re-polls to terminal and reads matches. Refresh (and startup discovery)
  also reconciles: an interrupted run that finished on the platform while Kit
  was closed flips to complete automatically.
- **Placement size normalization.** Physna's match transform is rigid (no
  scale), so parts are now auto-scaled to real-world metres: USD parts by
  their authored `metersPerUnit`, converted CAD parts assuming millimetres.
- **Scene up-axis control.** File-loaded scans are rotated to the stage's
  up-axis (Z-up assumed, flippable to Y-up / as-is); placements rotate with
  the scene prim.
- **Environment dropdown** (Production / Dev3 / Dev2 / Custom) fills API Base
  + Token URL together; the URLs moved under Advanced.
- **Multi-select part picking** (Ctrl/Shift-click in Add File(s)) and
  remembered last-used directories per picker.
- **Matches list pagination + filter**, same pattern as Previous Searches.
- **"Scene was a stage prim" chooser**: when a loaded run's scene prim isn't
  in the stage, pick a local file or download the uploaded scan back from the
  platform (cached on the record for later loads).
- Button tooltips (custom-drawn with explicit colors — Kit's default tooltip
  style is unreadable in this panel), two-click Delete ("Confirm?"),
  per-second poll countdowns, and add-part feedback.
- Pure-Python unit tests for the run store, API state model, deps gate, and
  path helpers (`physna/reality_compiler/tests/`).

### Changed
- **E57 files are read in an isolated subprocess** — pye57 segfaults inside
  Kit's process (native DLL conflict), so a crash is now a reported error
  instead of taking Kit down. E57 is no longer offered in the scene file
  picker (raw .e57 uploads don't match on the platform): import via
  File > Import and use "Use Selected Prim".
- **Pip installs no longer block the UI**: only `requests`/`keyring` install
  synchronously; the point-cloud format libraries install on a background
  thread, and a file load that races the install waits for it (with status).
- Dragging a part's **Placed slider no longer rebuilds the Matches section**
  (or steals scroll); only the placement-dependent buttons refresh, and only
  when the any-placed state flips.
- Long operations (search, load, resume, place/clear all, downloads) are
  serialized — one at a time, each with an inline progress row.
- Asset downloads stream to disk (no full-file RAM buffering) with throttled
  progress; scan-match reads retry through the platform's transient
  "not been computed" 404 instead of surfacing it as an error.
- All temp scratch consolidated under `<os-temp>/physna_reality_compiler/`;
  stale isolated-E57 output is swept on the next read.

### Fixed
- Re-running a search no longer leaves the previous run's placements in the
  stage attributed to the new results.
- Saved-search records keep their original creation time on update/resume
  (the list no longer reorders on every save) and are written atomically.
- A queued duplicate of the same part file can't collide on the upload
  folder; per-part scene-match read failures are keyed by asset id.
- An interrupted run whose assets were deleted on the platform no longer
  lingers in Previous Searches as resumable forever: Refresh detects the
  deletion (asset 404s are no longer retried indefinitely) and removes the
  stale local record. Update/Resume on a deleted run reports one clear
  "deleted from the platform" error instead of polling to timeout or
  spraying raw per-part 404 bodies, and a part deleted individually gets a
  short human-readable note while its locally saved matches stay loadable.
- File-picker dialogs are destroyed after use (one window leaked per pick).
- Sign in / Refresh double-clicks, mid-run slider drags, and mid-run up-axis
  changes are guarded; extension unload cancels in-flight tasks.

## [0.4.0] - 2026-07-14

Complete rearchitecture: the extension is now a thin client over the hosted
Physna Scan Search API. All point-cloud matching, registration, and scoring
runs on the platform; the extension handles selection, upload, polling,
placement, and visualization inside Omniverse.

### Added
- `physna.reality_compiler.api` — pure-Python client for the hosted API (no
  `omni`/`pxr` imports, so it is testable standalone):
  - OAuth2 client-credentials auth with an in-memory token cache
  - secure service-account storage via the OS credential vault (`keyring`)
  - `upload_asset` / `get_asset` / `get_scene_matches` / `delete_asset`
  - asset-state model + terminal/queryable predicates and batch polling
  - `ApiSession` login/logout/restore facade
- `pipelines/workflow.py` — `ScanSearchWorkflow`: prepare an uploadable scene
  (picked file or points extracted from a selected prim), upload scene + parts
  into a unique colocated run folder, poll to a terminal state, read
  scene-matches, and place matched part USDs at the returned 4×4 transform.
  Per-part matches stream in the moment each part (and the scene) finish
  indexing, instead of after the whole batch.
- Native accordion property-panel UI (Account / Scene / Parts / Run / Results)
  styled to match Kit's Property panel.
- Results: a per-part **match slider** (drag to place/remove matches live),
  combined **Matches + Placement** with a **Min score** slider that gates how
  many matches place, and **Place All (min score)**.
- Placement **hot-swap**: scan points behind placed matches are hidden
  (reversibly, via per-point widths) so parts aren't occluded — on by default.
- **Previous Searches**: local and platform runs in one list with a debounced
  live filter, pagination, and inline per-row download progress; discover and
  download runs created anywhere in the tenant (folder layout preserved so USD
  references resolve).
- Point-cloud loaders: `NPY`/`NPZ`, ASCII `XYZ`/`PTS`, `E57` (pye57), `LAS`/
  `LAZ` (laspy + lazrs), `PLY` (trimesh), and `PCD` (pypcd4); non-native scans
  are uploaded as extracted `.npy`.

### Changed
- `extension.toml`: dependencies trimmed to the Kit surface the client needs;
  pip deps (`requests`, `keyring`, `laspy`, `lazrs`, `pye57`, `trimesh`,
  `pypcd4`) are declared via `[[python.pipapi.install]]` and also installed by
  `extension.py` at import time as a runtime backstop.
- `PipelineManager` is now an API-session + orchestration facade.
- Placement is batched (one Hydra sync per set) and instanceable (placements of
  the same part share one GPU prototype); point removal runs off-thread — so
  importing many matches and carving the scan no longer hitches the UI.

### Removed
- The in-process matching engine (`scansearch/`: GPU kernels, registration,
  scoring, matching, nearest-neighbour, parallel batch).
- `docker_bridge/` (bridge to the old self-hosted matcher container).
- Local search-tuning UI, plane-removal previews, and batch/staged multi-class
  orchestration (all handled server-side now).
- Heavy runtime dependencies: `open3d` (PLY/PCD now load via trimesh/pypcd4),
  `scipy`, `networkx`, `scikit-learn`, and `omni.warp.core`.

### Security
- NPZ metadata loads with `allow_pickle=False` (closes a pickle
  code-execution path through a crafted `.npz`); converters write JSON sidecar
  metadata instead of a pickled object array.
- The service-account secret is never sent over a non-HTTPS connection
  (loopback `http` is allowed for local dev), is redacted from `repr`, and is
  scrubbed from API error bodies.
- A load-size cap (2 GB) guards against a hostile or corrupt "point cloud"
  exhausting memory before a byte is read.

### Fixed
- `color.npy` / `segmentation.npy` sidecars load again — a scan's per-point
  color was being dropped — now read with `allow_pickle=False`.
- A part that finishes indexing early shows its matches immediately rather than
  reporting "0 results" until the slowest part completes.
