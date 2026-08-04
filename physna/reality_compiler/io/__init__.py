# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Point-cloud file loading for the extension.

Loads NPY/NPZ/E57/LAS/LAZ/PLY/PCD point clouds into numpy arrays so the
extension can materialise them as ``Points`` prims in the stage.
"""

from .async_loader import load_point_cloud_async
from .loaders import SUPPORTED_EXTENSIONS, load_point_cloud

__all__ = ["load_point_cloud", "load_point_cloud_async", "SUPPORTED_EXTENSIONS"]
