# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
Logging helpers for the physna.reality_compiler extension.

Provides:
- ``get_logger(name)`` — returns a logger under ``physna.reality_compiler.*``
- ``setup_extension_logging()`` — configures handlers for the
  ``physna.reality_compiler`` logger hierarchy (call once from
  ``on_startup``).
- ``set_log_level(level)`` — change console verbosity at runtime.
- ``set_console(enabled)`` — toggle visible stdout logging on/off.
- ``notify_user(msg, status, duration)`` — fire-and-forget UI toast via
  ``omni.kit.notification_manager``.
"""

import logging
import sys

_LOG_FMT = "[%(name)s] %(levelname)s: %(message)s"
_ROOTS = ("physna.reality_compiler",)

# -----------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------


class _OmniLogHandler(logging.Handler):
    """Route Python log records through ``omni.log`` with an explicit channel."""

    CHANNEL = "physna.reality_compiler"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            import omni.log
        except ImportError:
            return

        msg = self.format(record)
        level = record.levelno

        if level >= logging.ERROR:
            omni.log.error(msg, channel=self.CHANNEL)
        elif level >= logging.WARNING:
            omni.log.warn(msg, channel=self.CHANNEL)
        elif level >= logging.INFO:
            omni.log.info(msg, channel=self.CHANNEL)
        else:
            omni.log.verbose(msg, channel=self.CHANNEL)


class _StdoutHandler(logging.Handler):
    """Write log records to sys.stdout (Kit displays stdout cleanly)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()
        except Exception:
            pass


# -----------------------------------------------------------------------
# Public helpers
# -----------------------------------------------------------------------

_current_level = logging.INFO


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``physna.reality_compiler.*`` hierarchy."""
    return logging.getLogger(name)


def setup_extension_logging(
    level: int = logging.INFO,
    console: bool = True,
) -> None:
    """Attach handlers to both logger hierarchies.

    Args:
        level: Minimum level shown in the console.  ``logging.INFO`` by
            default.  Pass ``logging.DEBUG`` to include verbose pipeline
            details.
        console: If ``True`` (default), attach a stdout handler so that
            messages are always visible in the Kit console.

    Safe to call more than once — stale handlers from previous reloads
    are removed before attaching fresh ones.
    """
    global _current_level
    _current_level = level

    _enable_channels(level)

    omni_handler = _OmniLogHandler()
    omni_handler.setFormatter(logging.Formatter(_LOG_FMT))
    omni_handler.setLevel(level)

    stdout_handler = _StdoutHandler()
    stdout_handler.setFormatter(logging.Formatter(_LOG_FMT))
    stdout_handler.setLevel(level)

    for root_name in _ROOTS:
        root = logging.getLogger(root_name)
        # Remove old handlers from previous hot-reloads to avoid dupes
        root.handlers = [
            h
            for h in root.handlers
            if not isinstance(h, (_OmniLogHandler, _StdoutHandler))
        ]
        root.addHandler(omni_handler)
        if console:
            root.addHandler(stdout_handler)
        # Let the handlers decide what to show; accept everything at root
        root.setLevel(logging.DEBUG)


def set_console(enabled: bool) -> None:
    """Toggle stdout console logging on or off at runtime.

    Examples::

        from physna.reality_compiler.logger import set_console
        set_console(True)   # show logs in Kit console
        set_console(False)  # silence console output
    """
    for root_name in _ROOTS:
        root = logging.getLogger(root_name)
        has_stdout = any(isinstance(h, _StdoutHandler) for h in root.handlers)

        if enabled and not has_stdout:
            handler = _StdoutHandler()
            handler.setFormatter(logging.Formatter(_LOG_FMT))
            handler.setLevel(_current_level)
            root.addHandler(handler)
        elif not enabled and has_stdout:
            root.handlers = [
                h for h in root.handlers if not isinstance(h, _StdoutHandler)
            ]


def teardown_extension_logging() -> None:
    """Remove all our handlers from both logger hierarchies.

    Call from ``on_shutdown`` so that a hot-reload starts with a clean
    slate and no duplicate or stale handlers survive across reloads.
    """
    for root_name in _ROOTS:
        root = logging.getLogger(root_name)
        root.handlers = [
            h
            for h in root.handlers
            if not isinstance(h, (_OmniLogHandler, _StdoutHandler))
        ]


def set_log_level(level: int = logging.DEBUG) -> None:
    """Change the console output level at runtime.

    Examples::

        from physna.reality_compiler.logger import set_log_level
        import logging
        set_log_level(logging.DEBUG)   # verbose
        set_log_level(logging.WARNING) # quiet
    """
    global _current_level
    _current_level = level

    _enable_channels(level)

    for root_name in _ROOTS:
        root = logging.getLogger(root_name)
        for h in root.handlers:
            if isinstance(h, (_OmniLogHandler, _StdoutHandler)):
                h.setLevel(level)


def _enable_channels(level: int) -> None:
    """Enable our omni.log channels at the requested severity."""
    try:
        import omni.log

        omni_level = (
            omni.log.Level.VERBOSE if level <= logging.DEBUG else omni.log.Level.INFO
        )
        log = omni.log.get_log()
        for ch in _ROOTS:
            log.set_channel_enabled(ch, True, omni.log.SettingBehavior.OVERRIDE)
            log.set_channel_level(ch, omni_level, omni.log.SettingBehavior.OVERRIDE)
    except Exception:
        pass


# -----------------------------------------------------------------------
# UI toasts
# -----------------------------------------------------------------------

_NM_STATUS_MAP = None  # lazily resolved


def notify_user(msg: str, status: str = "info", duration: float = 5.0) -> None:
    """Show a toast notification in the Kit viewport.

    Args:
        msg: Text to display.
        status: One of ``"info"``, ``"warning"``, ``"error"``.
        duration: Seconds before the notification auto-hides.
    """
    global _NM_STATUS_MAP
    try:
        import omni.kit.notification_manager as nm

        if _NM_STATUS_MAP is None:
            _NM_STATUS_MAP = {
                "info": nm.NotificationStatus.INFO,
                "warning": nm.NotificationStatus.WARNING,
                "error": nm.NotificationStatus.WARNING,  # Kit has no ERROR enum
            }

        nm.post_notification(
            msg,
            hide_after_timeout=True,
            duration=duration,
            status=_NM_STATUS_MAP.get(status, nm.NotificationStatus.INFO),
        )
    except Exception:
        pass
