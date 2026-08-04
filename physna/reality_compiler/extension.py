# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
import carb
import omni.ext
import omni.ui as ui
import omni.kit.app
import omni.kit.pipapi

from . import deps as _deps
from . import paths as _paths


# Thin-client pip dependencies. extension.toml also declares these via
# [[python.pipapi.install]]; this is the runtime backstop (it logs per-package
# failures instead of failing enable). numpy is intentionally omitted: it ships
# with Kit, and pip-installing it can shadow Kit's build and break the omni/pxr
# ABI.
#
# ``requests`` is imported at on_startup() time (the api package needs it), so
# it must be installed before the UI builds. The point-cloud format libraries
# are imported lazily on first file load — a user action long after startup —
# so they install on a background thread (see deps.py) and never block the
# window opening. A load that races the install waits via deps.ensure_deferred_ready.
_STARTUP_PACKAGES = [
    "requests",   # API HTTP client (needed by the api package at import time)
    "keyring",    # OS credential vault for the service-account secret
]
_DEFERRED_PACKAGES = [
    "laspy",      # LAS/LAZ point clouds
    "lazrs",      # LAZ decompression backend for laspy (laspy can't read .laz alone)
    "pye57",      # E57 point clouds
    "trimesh",    # PLY point clouds (lighter than open3d)
    "pypcd4",     # PCD point clouds
]


def _install_one(pkg: str) -> None:
    try:
        omni.kit.pipapi.install(pkg)
    except Exception as exc:  # keep loading; a missing dep surfaces at first use
        carb.log_error(f"physna.reality_compiler: failed to install '{pkg}': {exc}")


for _pkg in _STARTUP_PACKAGES:
    _install_one(_pkg)
_deps.start_deferred_install(_DEFERRED_PACKAGES, _install_one)


class PhysnaRealityCompilerExtension(omni.ext.IExt):
    """Main Extension Class for Physna Reality Compiler."""

    def on_startup(self, ext_id):
        from .logger import get_logger, setup_extension_logging

        setup_extension_logging()
        self._log = get_logger("physna.reality_compiler.extension")
        self._log.info("Starting Physna Reality Compiler...")

        from .scene import SceneOps
        from .converters import MeshConverter
        from .pipelines import PipelineManager
        from .ui.tabs import scan_search_ui

        temp_root = _paths.temp_dir("converters")

        self._stage_mgr = SceneOps()
        self._mesh_converter = MeshConverter(temp_root)
        self._manager = PipelineManager(self._stage_mgr, self._mesh_converter)
        self._scan_search_ui = scan_search_ui.ScanSearchUI(self._manager)

        self._window = ui.Window("Physna Reality Compiler", width=500, height=800)

        with self._window.frame:
            with ui.ScrollingFrame(
                horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED,
            ):
                with ui.VStack(style={"margin_width": 3, "margin_height": 2}):
                    self._scan_search_ui.build_ui()

    def on_shutdown(self):
        from .logger import get_logger, teardown_extension_logging

        _log = get_logger("physna.reality_compiler.extension")
        _log.info("Shutting down Physna Reality Compiler.")

        # getattr defaults: if on_startup failed partway, Kit still calls
        # on_shutdown and the later attributes were never set.
        ui_obj = getattr(self, "_scan_search_ui", None)
        if ui_obj:
            ui_obj.destroy()

        manager = getattr(self, "_manager", None)
        if manager:
            manager.shutdown()

        window = getattr(self, "_window", None)
        if window:
            window.destroy()

        self._window = None
        self._stage_mgr = None
        self._mesh_converter = None
        self._manager = None
        self._scan_search_ui = None

        teardown_extension_logging()
