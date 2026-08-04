# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Standalone subprocess worker that reads an E57 into a temp ``.npz``.

``pye57`` (libE57Format/xerces) can **hard-crash** (segfault) when run inside
Kit's process — a native DLL conflict, not a file problem (the same file reads
fine in a plain interpreter). Running the read here, in a separate process,
turns that crash into a clean non-zero exit the parent can report instead of
Kit going down with it.

Deliberately dependency-light: imports only ``numpy`` and ``pye57`` (never
``omni``/``carb``/the extension package), so it runs under any interpreter that
has those two on its path. Invoked as::

    python _e57_worker.py <in.e57> <out.npz> <scan_index>

Writes ``points`` (Nx3 float32), ``colors`` (raw Nx3, or empty), ``intensity``
(raw N, or empty), and ``scan_count`` to ``out.npz``. Colours are left raw for
the parent to normalize (keeps this worker free of extension helpers).
"""

import sys


def _read(in_path, scan_index):
    import numpy as np
    import pye57

    e57 = pye57.E57(in_path)
    scan_count = int(e57.scan_count)
    if scan_count == 0:
        raise ValueError("E57 file has no scans")
    if scan_index >= scan_count:
        raise ValueError(
            f"E57 has {scan_count} scan(s), requested index {scan_index}"
        )

    # Same fallback ladder the in-process reader used, minus the field-filtered
    # variant (which needs the header's field set; the ignore variants cover it).
    strategies = [
        lambda: e57.read_scan_raw(scan_index, ignore_unsupported_fields=True),
        lambda: e57.read_scan(scan_index, ignore_missing_fields=True),
    ]
    data = None
    for fn in strategies:
        try:
            data = fn()
            break
        except Exception:
            data = None
    if data is None:
        raise ValueError(f"Could not read E57 scan {scan_index}")

    required = ("cartesianX", "cartesianY", "cartesianZ")
    if not all(k in data for k in required):
        raise ValueError("E57 data missing required cartesian coordinate fields")

    cx = np.asarray(data["cartesianX"], dtype=np.float64)
    cy = np.asarray(data["cartesianY"], dtype=np.float64)
    cz = np.asarray(data["cartesianZ"], dtype=np.float64)
    if not (len(cx) == len(cy) == len(cz)):
        raise ValueError("E57 coordinate arrays have different lengths")

    pts = np.column_stack([cx, cy, cz])
    mask = (
        np.isfinite(pts).all(axis=1)
        & (pts > -1e10).all(axis=1)
        & (pts < 1e10).all(axis=1)
    )
    points = pts[mask].astype(np.float32)

    # Colours/intensity are optional: a malformed one must not fail an
    # otherwise-valid geometry read, so drop it (empty array) on any error.
    colors = np.empty((0,), dtype=np.float32)
    if all(k in data for k in ("colorRed", "colorGreen", "colorBlue")):
        try:
            cr = np.asarray(data["colorRed"])[mask]
            cg = np.asarray(data["colorGreen"])[mask]
            cb = np.asarray(data["colorBlue"])[mask]
            colors = np.column_stack([cr, cg, cb])
        except Exception:
            colors = np.empty((0,), dtype=np.float32)

    intensity = np.empty((0,), dtype=np.float32)
    if "intensity" in data:
        try:
            inten = np.asarray(data["intensity"], dtype=np.float64)[mask]
            intensity = np.where(np.isfinite(inten), inten, 0.0).astype(np.float32)
        except Exception:
            intensity = np.empty((0,), dtype=np.float32)

    return points, colors, intensity, scan_count


def main():
    if len(sys.argv) < 4:
        sys.stderr.write("usage: _e57_worker.py <in.e57> <out.npz> <scan_index>\n")
        return 2
    in_path, out_path, scan_index = sys.argv[1], sys.argv[2], int(sys.argv[3])
    import numpy as np

    points, colors, intensity, scan_count = _read(in_path, scan_index)
    np.savez(
        out_path,
        points=points,
        colors=colors,
        intensity=intensity,
        scan_count=np.array([scan_count], dtype=np.int64),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
