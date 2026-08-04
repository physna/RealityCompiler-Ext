# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
# Guard the Kit-specific extension import so that plain interpreters (e.g.
# credential validation, integration tests) can still import pure-Python
# sub-packages like ``physna.reality_compiler.api`` without Kit present.
__all__ = []
try:
    from .extension import PhysnaRealityCompilerExtension
except ImportError:
    PhysnaRealityCompilerExtension = None
else:
    __all__.append("PhysnaRealityCompilerExtension")
