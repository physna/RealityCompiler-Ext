# Developing and Testing

The extension is a thin client over the hosted Physna Scan Search API.
There are two ways to run code; pick the right one.

## Which Python do I use?

| Task                                                        | Use                                    |
| ----------------------------------------------------------- | -------------------------------------- |
| The `physna.reality_compiler.api` package (pure Python)     | Any Python 3.12 env (`environment.yml`)|
| Anything importing `omni` / `pxr` / `carb` (SceneOps, UI…)  | Run inside Kit                         |
| Running the full extension in a stage                       | Launch the app, or `kit.bat --exec …`  |

`environment.yml` provides a conda spec (`conda env create -f
environment.yml`) with everything the pure-Python side needs; any plain
Python 3.12 with `numpy` + `requests` also works.

## Unit tests (conda env, no Kit)

`physna/reality_compiler/tests/` holds pure-Python `unittest` files covering
the run store, API state model, deps gate, path layout, and last-dir store.
Run them from the extension root:

```bash
python -m unittest discover -s physna/reality_compiler/tests -p "test_*.py"
```

`test_hello.py` (the in-Kit import smoke test) skips itself automatically
outside Kit.

## Testing the API client in the conda env

`physna.reality_compiler.api` has no `omni`/`pxr` dependency, so you can
exercise auth, models, and the HTTP client from a plain interpreter:

```bash
python - <<'PY'
import physna.reality_compiler.api as api
cfg = api.ApiConfig().with_overrides(token_url="https://<stack>.auth.<region>.amazoncognito.com/oauth2/token")
sess = api.ApiSession(config=cfg)
sess.login("<client-id>", "<client-secret>")   # verifies against the token endpoint
client = sess.client
scene = client.upload_asset("scene.npy", "scan-search-demo/warehouse/scene.npy")
print(scene.id, scene.state)
PY
```

Run from the extension root so `import physna.reality_compiler...`
resolves. `keyring` may be absent from the conda env — the credential
store degrades gracefully (nothing is persisted), which is fine for
one-off validation. To validate credentials without touching the vault,
call `login(..., persist=False)`.

You can also point the client at a stack via `PHYSNA_*` environment
variables (`PHYSNA_API_BASE`, `PHYSNA_TENANT_ID`, `PHYSNA_TOKEN_URL`,
`PHYSNA_SCOPE`); `ApiConfig.from_env()` reads them.

## Running the extension in Kit

`SceneOps`, the pipelines, and the UI import `omni`/`pxr`/`carb`, which
only load inside Kit. Enable the extension in the Extension Manager and
drive it from the window, or run a script with `kit.bat --exec
myscript.py` / the in-app Script Editor for stage-touching code. `import
carb` fails in a bare interpreter because `kit.exe` sets up the DLL search
paths.

## Where files and creds live

- All temp scratch (extracted scene points, converted part USDs, downloaded
  scenes/runs, isolated-E57 output) lives under
  `%TEMP%/physna_reality_compiler/<name>/` — see `paths.py`.
- Persistent data (API config, saved run records, remembered picker dirs)
  lives under `~/.physna_reality_compiler/`. Saved runs power the
  resume-interrupted-search feature, so they must survive restarts.
- The service-account secret is stored in the OS credential vault under
  the service name `physna.reality_compiler` (see `api/credentials.py`).
  `Sign out` clears it.
