# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
# Star-import each test module so Kit's test runner (omni.kit.test) discovers
# the cases. All modules except test_hello are pure Python and also run in a
# plain interpreter: python -m unittest discover -s physna/reality_compiler/tests
from .test_api_models import *
from .test_hello import *
from .test_last_dir_store import *
from .test_paths_and_deps import *
from .test_polling import *
from .test_run_store import *

__all__ = []
