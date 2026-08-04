# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Read E57 files in an isolated subprocess.

``pye57`` hard-crashes (segfaults) when opened inside Kit's process — a native
library conflict, confirmed by the same file opening cleanly in a plain
interpreter. This module runs :mod:`._e57_worker` under a separate Python so a
crash becomes a catchable non-zero exit rather than taking Kit down.

The worker needs ``numpy`` + ``pye57`` importable, so we pass the running
process's ``sys.path`` through ``PYTHONPATH`` (that's where ``omni.kit.pipapi``
put ``pye57``) and reuse the same interpreter binary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

from ..logger import get_logger
from ..paths import temp_dir

_log = get_logger("physna.reality_compiler.io.e57_isolated")

_WORKER = os.path.join(os.path.dirname(__file__), "_e57_worker.py")

# Reap temp .npz files stranded by an abnormal exit (Kit crash / cancelled load
# in the window between the subprocess writing one and the caller deleting it).
# Only files older than this, so a concurrent in-flight read is never touched.
_STALE_NPZ_AGE_S = 3600.0


def _sweep_stale_npz(out_dir: str) -> None:
    """Best-effort delete of leftover ``e57_*.npz`` files older than an hour."""
    now = time.time()
    try:
        entries = os.listdir(out_dir)
    except Exception:
        return
    for name in entries:
        if not (name.startswith("e57_") and name.endswith(".npz")):
            continue
        path = os.path.join(out_dir, name)
        try:
            if now - os.path.getmtime(path) > _STALE_NPZ_AGE_S:
                os.remove(path)
        except Exception:
            pass


def _candidate_pythons() -> list[str]:
    """Plausible Python executables to run the worker (existing files only).

    Inside Kit ``sys.executable`` may be the app binary, not a python, so also
    probe the base executable and ``python[3]`` next to / under the exe and the
    interpreter prefixes."""
    names = ["python.exe", "python3.exe"] if os.name == "nt" else ["python3", "python"]
    exe = sys.executable or ""
    exe_dir = os.path.dirname(exe)
    raw: list[str] = []
    # The real interpreter behind a launcher/venv — most reliable when present.
    raw.append(getattr(sys, "_base_executable", "") or "")
    raw.append(exe)
    for nm in names:
        raw.append(os.path.join(exe_dir, nm))
        raw.append(os.path.join(exe_dir, "python", nm))
        raw.append(os.path.join(sys.base_prefix, nm))
        raw.append(os.path.join(sys.prefix, nm))
    out: list[str] = []
    seen: set[str] = set()
    for c in raw:
        if not c:
            continue
        c = os.path.normpath(c)
        # Only real python interpreters. Inside Kit, sys.executable /
        # _base_executable are kit.exe, which "runs" a .py through the
        # omni.kit.app serializer (fails noisily) rather than as Python — skip it.
        if not os.path.basename(c).lower().startswith("python"):
            continue
        if c not in seen and os.path.isfile(c):
            seen.add(c)
            out.append(c)
    return out


def read_e57_to_npz(file_path, scan_index: int = 0, timeout: float = 1200.0) -> str:
    """Read an E57 in a subprocess; return the path to a temp ``.npz``.

    The ``.npz`` holds ``points``/``colors``/``intensity``/``scan_count`` (see
    :mod:`._e57_worker`); the caller loads it and deletes it. Raises
    :class:`RuntimeError` if no interpreter can be found or every attempt fails
    (including a native crash, which surfaces as a non-zero exit code)."""
    if not os.path.isfile(_WORKER):
        raise RuntimeError("E57 worker script is missing from the extension.")

    out_dir = temp_dir("e57")
    _sweep_stale_npz(out_dir)  # clear orphans left by any prior crash/cancel
    out_npz = os.path.join(out_dir, f"e57_{uuid.uuid4().hex[:8]}.npz")

    env = dict(os.environ)
    # Hand the child the same import paths this process has, so it finds the
    # pip-installed pye57 + numpy that omni.kit.pipapi added at runtime.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)

    pythons = _candidate_pythons()
    if not pythons:
        raise RuntimeError(
            "Could not find a Python interpreter to read the E57 in an isolated "
            "process. Convert the scan to .ply/.npy, or report this."
        )

    def _cleanup() -> None:
        try:
            if os.path.exists(out_npz):
                os.remove(out_npz)
        except Exception:
            pass

    last_err = "no interpreter attempts"
    for py in pythons:
        _log.info("E57 isolated read: %s", py)
        try:
            proc = subprocess.run(
                [py, _WORKER, str(file_path), out_npz, str(scan_index)],
                env=env, capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last_err = f"timed out after {int(timeout)}s"
            _log.warning("Isolated E57 read timed out (%s)", py)
            continue
        except Exception as exc:
            last_err = f"could not launch {py}: {exc}"
            continue

        if proc.returncode == 0 and os.path.exists(out_npz):
            return out_npz

        stderr = proc.stderr.decode("utf-8", "replace").strip()
        # A negative / huge return code is a native crash (segfault) in the
        # child — exactly the case this isolation exists to contain.
        last_err = f"exit code {proc.returncode}"
        if stderr:
            last_err += f": {stderr[-800:]}"
        _log.warning("Isolated E57 read failed via %s (%s)", py, last_err)

    # Every attempt failed — never leave a partial .npz behind (a crash-exit,
    # timeout, or no-interpreter run may have written one). The success path
    # already returned above, so this only runs on failure.
    _cleanup()

    # Missing pye57 in the child is a dependency problem, not a mystery crash —
    # surface the actionable install hint the old in-process reader gave.
    low = last_err.lower()
    if "pye57" in low and ("no module named" in low or "modulenotfound" in low):
        raise ImportError(
            "pye57 is required to load E57 files (pip install pye57)."
        )
    raise RuntimeError(f"Isolated E57 read failed: {last_err}")
