# Physna Reality Compiler

Physna Reality Compiler is a **lightweight Omniverse Kit extension** that
drives the **hosted Physna Scan Search API**. You sign in with a service
account, pick a scanned scene and a catalog of parts, and the extension
uploads them, waits for the platform to index and match them, and places
each matched part back into the stage at the transform the engine found.

Matching runs entirely on the Physna platform, so the extension has no
GPU or Open3D dependency and installs anywhere Kit runs.

## License, Support & Disclaimer

This is an **open-source project** under the
[Apache License 2.0](LICENSE), provided **"as is", without warranty of any
kind**. It is maintained on a best-effort basis: **large issues and
failures** (crashes, data loss, security problems, a broken scan-search
workflow) will be prioritized, but minor issues and feature requests may
not receive a timely response. Pull requests are welcome.

The hosted Physna platform this extension talks to is a **separate
commercial service** governed by your agreement with Physna — platform
problems go through your Physna account contact, not this repo. See
[`docs/DISCLAIMER.md`](docs/DISCLAIMER.md) for the full policy.

## What It Does

- Sign in with a Physna **service account** (client credentials); the
  secret is stored in the OS credential vault, not on disk.
- Choose a **scene**: a point-cloud file (`.npy`/`.npz`/`.e57`/`.las`/`.laz`/
  `.xyz`/`.pts`/`.ply`/`.pcd`) or the currently selected stage prim (its
  points are extracted for you).
- Queue **parts**: USD/CAD files individually or a whole folder.
- **Run** the search — the extension uploads everything, polls until the
  scene and parts finish indexing, and reads the placements.
- **Place** matched parts into the stage at their detected transforms.

See [`docs/Overview.md`](docs/Overview.md) for the architecture and the
underlying API workflow.

## Repo Layout

```text
physna.reality_compiler/
|- config/                   Extension metadata (extension.toml)
|- data/                     Extension icon and preview assets
|- docs/                     Overview, changelog, and developer docs
|- physna/reality_compiler/  Extension code
|  |- api/                   Pure-Python client for the hosted API
|  |- converters/            Mesh and point-cloud conversion helpers
|  |- io/                    Point-cloud file loaders (E57 via subprocess)
|  |- pipelines/             Manager, workflow, and cached run state
|  |- scene/                 USD and stage operations (SceneOps)
|  |- tests/                 Pure-Python unit tests + in-Kit smoke test
|  |- ui/                    Omniverse UI panel
|  \- deps.py / paths.py     Background pip-install gate; temp-dir layout
|- scripts/publish/          Release packaging
\- README.md
```

## Prerequisites

- An Omniverse Kit workspace with this repo at `Kit/shared/exts/physna.reality_compiler`
- Python 3.12
- A Physna **service account** (Client ID + Client Secret) and your
  tenant's **token endpoint** URL — created in the Physna app under
  Settings → Users → Service Accounts

## Install In Kit

1. Place or clone this repo at `Kit/shared/exts/physna.reality_compiler`.
2. Launch your Kit app.
3. Open the Extension Manager and enable `Physna Reality Compiler`.

On startup the extension installs its pip dependencies — `requests`,
`keyring`, `laspy`, `lazrs`, `pye57`, `trimesh`, and `pypcd4` — via
`omni.kit.pipapi` (declared in `extension.toml` and installed by
`extension.py`), so no separate setup step is required. `numpy` is not
installed: it ships with Kit. Only `requests`/`keyring` install before the
window opens; the per-format loaders (`pye57` for E57, `trimesh` for PLY,
`pypcd4` for PCD, `laspy`+`lazrs` for LAS/LAZ) install on a background
thread and are imported lazily — a file load that arrives before its
dependency finishes installing simply waits for it (with a status message).

## Using The Extension

The `Physna Reality Compiler` window is a single **accordion property
panel** — expand a section to work in it. A progress bar and **Cancel**
button track any in-flight work, and each action reports progress inline on
its own row.

### Account

Pick an **Environment** (Production / Dev3 / Dev2 — or Custom), enter your
**Tenant ID**, **Client ID**, and **Client Secret**, then **Sign in**. The
environment fills the API base and token endpoint together; both live under
**Advanced** for custom stacks. The secret is verified immediately and
stored in the OS credential vault; you stay signed in across restarts (a
background check on launch signs you out if it's been revoked).

### Scene

**Use Selected Prim**, or **Pick File...** to load a point cloud (`.npy`/
`.npz`/`.las`/`.laz`/`.xyz`/`.pts`/`.ply`/`.pcd`) into the stage and use it
as the scene. An **Up axis** control reconciles the scan with the stage's
up-axis (placements rotate with it). **Run name** is the tenant folder for
this run, pre-filled from the scene and editable.

> **E57:** import the file via Kit's **File > Import** (omni.kit.pointclouds)
> and then **Use Selected Prim** — raw `.e57` uploads don't match on the
> platform, so the picker doesn't offer them.

### Parts

**Add File(s)...** (Ctrl/Shift-click to pick several), **Add Folder...**, or
**Clear**. USD parts upload with their dependencies so references resolve
server-side. Parts stay selected after a run, so you can re-run or tweak min
score without re-adding them.

### Run

**Run Scan Search** uploads scene + parts into a unique colocated run
folder, polls to terminal (~30s cadence), and reads scene-matches. Results
populate **as each part finishes** — per-part status updates every poll
round, and a part's matches appear the moment it (and the scene) finish
indexing, without waiting for the slowest part.

### Matches

- Each part has a **slider**: drag it up to place its best matches into the
  stage, drag down to remove them, with a live "N of M placed (best / lowest
  score)" readout. The **Min score** slider **gates** how many of a part's
  matches are eligible to place. **Place All (min score)** drops every
  eligible match in one batch. CAD parts are converted to USD before
  placement.
- **Hide scan behind placed parts** (on by default) reversibly hides the
  scan points a placed match covers, so parts aren't buried in the scan;
  removing the placement re-reveals them.
- **Scene Editing** — **Remove Matched Points** permanently carves the scan
  points inside the placed matches, or **Keep Only Matched** isolates them.

### Previous Searches

Runs are recorded locally **as soon as their upload finishes**, so a search
survives Kit closing mid-poll: it reappears marked *interrupted* with a
**Resume** button that re-polls the platform and reads its matches. Refresh
(and startup discovery) also reconciles — an interrupted run that finished
while Kit was closed flips to complete on its own, and one whose assets were
deleted on the platform is removed from the list.

Local and platform runs share **one list** with a debounced **filter** and a
**Refresh** button; long lists paginate. Per run, **Load** restores its
scene, parts, and matches (re-importing the scene file if present);
**Update** re-reads a finished run's matches from the platform; **Delete**
(two-click confirm) removes the local record and re-checks the platform,
since the run usually still exists remotely. If a run's scene was a stage
prim that isn't in the current stage, a chooser offers to pick a local file
or download the uploaded scan back from the platform.

Runs discovered on the platform — created *anywhere* in your tenant, on
another machine, by a teammate, or by the reference script — appear in the
same list with a **Download & Load** button that pulls the run's files down
(preserving folder layout so USD references resolve), reads its matches, and
loads it like any local run, with download progress shown inline on its row.

## Local Development

`environment.yml` provides a conda env (`physna-reality-compiler`) for repo
tooling and for exercising the pure-Python `physna.reality_compiler.api`
package outside
Kit (it needs only `requests` + `numpy`; `keyring` degrades gracefully
when absent). Anything that imports `omni`/`pxr` must run inside Kit — see
[`docs/DEVELOPING.md`](docs/DEVELOPING.md).

## Release Process

Releases are built from `config/extension.toml`, packaged by
`scripts/publish/make_release.py`, and published by the tag-triggered
workflow in [`.github/workflows/release.yml`](.github/workflows/release.yml).

### Manual Release

1. Pick the new semver version.
2. Update `version` in [`config/extension.toml`](config/extension.toml).
3. Add the new topmost entry in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).
4. Build the package: `python scripts/publish/make_release.py`
5. Verify `physna.reality_compiler-vX.Y.Z.zip` was created in the repo root.
6. Commit the version and changelog updates.
7. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z - Release Title"`
8. Push: `git push origin <branch> && git push origin vX.Y.Z`

Pushing the tag triggers the release workflow, which rebuilds the zip and
creates the GitHub Release. Do not commit the generated zip.

## Additional Docs

- [`docs/Overview.md`](docs/Overview.md)
- [`docs/DEVELOPING.md`](docs/DEVELOPING.md)
- [`docs/DISCLAIMER.md`](docs/DISCLAIMER.md)
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md)
