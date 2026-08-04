# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Runtime state for the background pip-dependency install.

The heavy point-cloud format libraries (``pye57``/``laspy``/``trimesh``/…)
install on a daemon thread at startup so the UI isn't blocked (see
``extension.py``). A lazy consumer that reaches for one before the install
finishes can ``await`` :func:`ensure_deferred_ready` to block just its own
coroutine until the installs complete, instead of hitting a spurious
``ImportError`` during that startup window.

Kept free of ``omni`` imports so ``io``/``converters`` can use it and so it
imports under a plain interpreter. ``extension.py`` injects the actual
installer (``omni.kit.pipapi.install``) via :func:`start_deferred_install`.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable, Iterable, Optional

from .logger import get_logger

_log = get_logger("physna.reality_compiler.deps")

# Max time a lazy consumer waits on the background install before giving up and
# proceeding anyway (the lazy import then raises a clear ImportError if the dep
# genuinely never installed). Guards against a wedged pip hanging a file load.
_WAIT_TIMEOUT_S = 300.0

_done = threading.Event()
_started = False


def start_deferred_install(
    packages: Iterable[str], install_one: Callable[[str], None]
) -> None:
    """Install ``packages`` on a daemon thread, flagging completion when done.

    ``install_one(pkg)`` performs the actual install; passing it in keeps this
    module free of ``omni``. Idempotent — only the first call starts a thread."""
    global _started
    if _started:
        return
    _started = True

    def _run() -> None:
        try:
            for pkg in packages:
                install_one(pkg)
        finally:
            _done.set()

    threading.Thread(
        target=_run, name="physna-reality-compiler-pipdeps", daemon=True
    ).start()


def deferred_install_pending() -> bool:
    """True while the background install thread is still running."""
    return _started and not _done.is_set()


async def ensure_deferred_ready(
    on_progress: Optional[Callable[[str], None]] = None
) -> None:
    """Await the background dep install if it's still running (else return now).

    Waits off the event loop (on a worker thread) so the UI stays responsive.
    Returns immediately once installs finish, so it's cheap to call on every
    lazy load — the wait only ever happens in the brief startup window."""
    if not deferred_install_pending():
        return
    _log.info("Waiting for background dependency install to finish...")
    if on_progress is not None:
        try:
            on_progress("Finishing dependency install...")
        except Exception:
            pass
    loop = asyncio.get_running_loop()
    ready = await loop.run_in_executor(None, lambda: _done.wait(_WAIT_TIMEOUT_S))
    if not ready:
        _log.warning(
            "Background dependency install still running after %ss; proceeding "
            "(a missing dep will surface as an ImportError).",
            int(_WAIT_TIMEOUT_S),
        )
