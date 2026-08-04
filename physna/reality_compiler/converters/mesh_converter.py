# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Mesh-to-USD conversion via the Omniverse asset converter."""

from __future__ import annotations

import omni.kit.asset_converter as asset_converter

from ..logger import get_logger, notify_user

_log = get_logger("physna.reality_compiler.converters.mesh")


class MeshConverter:
    """Convert mesh files (STL, OBJ, FBX, glTF) to USD format."""

    def __init__(self, temp_root: str) -> None:
        self.temp_root = temp_root

    async def mesh_to_usd(self, in_file: str, out_file: str) -> bool:
        """Convert a mesh file (e.g. STL) to USD using the asset converter.

        Args:
            in_file: Source mesh file path.
            out_file: Destination USD file path.

        Returns:
            ``True`` if the conversion succeeded.
        """
        ctx = asset_converter.AssetConverterContext()
        ctx.ignore_materials = False
        ctx.ignore_animations = True
        ctx.ignore_camera = True
        ctx.ignore_light = True
        ctx.merge_all_meshes = True
        ctx.use_meter_as_world_unit = True
        ctx.baking_scales = True
        ctx.use_double_precision_to_usd_transform_op = True

        try:
            inst = asset_converter.get_instance()
            task = inst.create_converter_task(in_file, out_file, None, ctx)
            ok = await task.wait_until_finished()
            if not ok:
                err = getattr(task, "get_error_message", lambda: "unknown error")()
                _log.error("Conversion failed: %s", err)
                notify_user(f"Mesh conversion failed: {err}", "error")
            else:
                _log.info("Conversion OK: %s", out_file)
            return bool(ok)
        except Exception as e:
            _log.exception("Conversion exception")
            notify_user(f"Mesh conversion failed: {e}", "error")
            return False
