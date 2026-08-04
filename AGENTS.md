# Physna Reality Compiler — Development Guide

This document maps the public code surfaces of the repo. The extension is
a **thin client over the hosted Physna Scan Search API** — matching runs
on the platform, not in Kit. Use this as a guide when working on the
extension, the `api` client, or the release tooling.

## Running / testing code

Two environments matter:

- **Inside Kit** — anything that imports `omni`/`pxr`/`carb`: `SceneOps`,
  the pipelines, the UI, `extension.py`. Run the app, or `kit.bat --exec
  myscript.py`, or the in-app Script Editor. `import carb` / `omni.usd`
  will not load in a bare interpreter (DLL paths need `kit.exe`).

- **Plain Python 3.12 env** (`environment.yml` provides a conda spec) —
  the `physna.reality_compiler.api` package is pure Python (`requests` +
  `keyring` + `numpy`, no `omni`/`pxr`), so it runs here for credential
  validation and integration tests. The `CredentialStore` degrades
  gracefully when `keyring` is missing (inside Kit the
  `[[python.pipapi.install]]` block installs it). `import
  physna.reality_compiler` also works here — the extension import is
  guarded, so it returns `None` for the extension class without Kit.

## `physna.reality_compiler.api`

Pure-Python client for the hosted Physna Scan Search API.
Public surface (`api/__init__.py`):

- `ApiSession` — login/logout/restore facade (login state + a ready client)
- `PhysnaClient` — `upload_asset`, `get_asset`, `get_scene_matches`,
  `delete_asset`
- `ApiConfig` — non-secret routing (API base, tenant, token URL, scope)
- `CredentialStore` / `ServiceAccount` — secure secret storage via the OS
  vault (`keyring`)
- `TokenProvider` — OAuth2 client-credentials token cache
- `Asset` / `Match` / `SceneMatches` + state predicates (`is_working`,
  `is_terminal`, `is_queryable`) and constants
- `PollState` / `poll_step` — transport-agnostic batch polling
- `AuthError`, `ApiError`

## `physna.reality_compiler.pipelines`

- `PipelineManager` — composition root and UI facade: owns the
  `ApiSession` (login state), scene/part selection, `run_search()`
  orchestration, `place_*_matches()`, and the Kit file pickers.
- `ScanSearchWorkflow` (`workflow.py`) — prepare an uploadable scene
  (picked file or points extracted from a prim), upload scene + parts into
  a unique colocated run folder, poll to a terminal state, read
  scene-matches, and place matched part USDs via `transform4x4`.
- `PipelineState` / `SceneSource` / `PartEntry` — cached run state.

## `physna.reality_compiler.scene`

`SceneOps`, a stateless facade over stage/USD operations: transforms
(incl. USD↔NumPy matrix conversion), selection, point extraction, prim
creation (incl. `create_xform_with_reference` used for placement),
parent-path resolution for imports, and point removal.

## `physna.reality_compiler.converters`

Conditionally exports `MeshConverter` (mesh→USD via the Omniverse asset
converter), used to convert CAD parts to USD ahead of placement.

## `physna.reality_compiler.io`

Point-cloud file loaders: `load_point_cloud` (NPY/NPZ + ASCII XYZ/PTS via
numpy, E57 via pye57, LAS/LAZ via laspy+lazrs, PLY via trimesh, PCD via
pypcd4) and the executor-backed `load_point_cloud_async`. Used to bring a
scan into the stage as a `Points` prim (viewing + a placement frame for
reloaded runs). Only numpy is required; other deps are imported lazily per
format. E57 reads run in an isolated subprocess (`e57_isolated.py` +
`_e57_worker.py`) because pye57 can crash inside Kit's process.

## UI Contract

The window is driven by `physna/reality_compiler/ui/tabs/scan_search_ui.py`
(Account / Scene / Parts / Run / Results). UI code is orchestration-only:
sections gather input, `PipelineManager` performs workflow logic,
`SceneOps` touches USD, and `api` talks to the platform.

## Release Contract

Release behavior is split across three artifacts:

- `scripts/publish/make_release.py` — builds `physna.reality_compiler-vX.Y.Z.zip`
- `.github/workflows/release.yml` — publishes a GitHub Release on a `v*` tag
- `config/extension.toml` — source of truth for the version

The release workflow expects the git tag to match `config/extension.toml`,
the changelog entry for that version to already exist in
`docs/CHANGELOG.md`, and the zip to be rebuildable from source (not committed).
