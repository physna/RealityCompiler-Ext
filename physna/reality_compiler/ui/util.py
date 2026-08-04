# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared style tokens, colours, and icon resolution for the UI.

Pure helpers - no ``omni.ui`` import here, so this stays cheap to import
and easy to reason about.  The widgets live in ``tabs/scan_search_ui``.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# omni.ui colour helper  (omni.ui expects 0xAABBGGRR, not 0xAARRGGBB)
# ---------------------------------------------------------------------------
def _hex(rgb: str) -> int:
    """Convert '#RRGGBB' hex string to omni.ui's 0xAABBGGRR int (full alpha)."""
    h = rgb.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0xFF000000 | (b << 16) | (g << 8) | r


def _rgba(rgb: str, alpha: float) -> int:
    """Like ``_hex`` but with a fractional ``alpha`` in [0, 1] (for pill fills)."""
    h = rgb.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    a = max(0, min(255, int(round(alpha * 255))))
    return (a << 24) | (b << 16) | (g << 8) | r


# ---------------------------------------------------------------------------
# Colour tokens (standard #RRGGBB, converted automatically)
# ---------------------------------------------------------------------------
_COLOR_MUTED = _hex("#B4B4B2")      # secondary / hint text (neutral, readable)
_COLOR_TEXT = _hex("#E4E6E8")       # primary text
_COLOR_SUCCESS = _hex("#76B900")    # NVIDIA green
_COLOR_ACCENT = _hex("#76B900")     # primary accent (green)
_COLOR_ACCENT_HI = _hex("#8CC63F")  # brighter green (hover)
_COLOR_WARNING = _hex("#FFAA00")    # amber
_COLOR_ERROR = _hex("#FF4444")      # red
_COLOR_INFO = _hex("#4DA6FF")       # light blue

# Surfaces (dark theme, matched to Kit's Property panel).
_BG_SECTION = _hex("#2E2E2C")       # collapsable body
_BG_HEADER = _hex("#383836")        # section header bar
# Hover DARKENS the header (a lighter hover blended into Kit's medium-gray
# default window background).
_BG_HEADER_HOVER = _hex("#2B2E2E")
_BG_FIELD = _hex("#1F2123")
_BORDER = _hex("#454543")
_DIVIDER = _hex("#3A3A38")

# Font sizes - all even (odd sizes render blurry in Kit's font), matched to
# Omniverse defaults: 14 is the standard control/label size, 12 for hints.
_FONT_SM = 12   # hints / secondary
_FONT_MD = 14   # body / controls / section titles (Omniverse default)
_FONT_LG = 16   # emphasis (tutorial + app title)


# ---------------------------------------------------------------------------
# Status-chip tones - (foreground, translucent pill background).  Used by the
# accordion header chips and the banner checklist.
# ---------------------------------------------------------------------------
def chip_tone(tone: str) -> tuple[int, int]:
    """Return ``(fg_color, bg_color)`` for a status-chip ``tone``."""
    if tone == "done":
        return (_COLOR_SUCCESS, _rgba("#76B900", 0.16))
    if tone in ("todo", "warn", "warning"):
        return (_COLOR_WARNING, _rgba("#FFAA00", 0.16))
    if tone == "info":
        return (_COLOR_INFO, _rgba("#4DA6FF", 0.16))
    return (_COLOR_MUTED, _rgba("#9BA0A6", 0.14))

# ---------------------------------------------------------------------------
# One cascading style for the whole panel's native widgets. (Buttons are the
# custom ZStack ``_icon_button``, which styles itself inline, so there are no
# ``Button`` rules here.)
# ---------------------------------------------------------------------------
_PANEL_STYLE = {
    # --- fields ---
    "Field": {
        "background_color": _BG_FIELD,
        "border_radius": 4,
        "border_color": _hex("#33332F"),
        "border_width": 1,
        "color": _COLOR_TEXT,
        "font_size": _FONT_MD,
        "padding": 5,
    },
    "Field:hovered": {"border_color": _hex("#4A4A46")},

    # --- labels ---
    "Label": {"color": _COLOR_TEXT, "font_size": _FONT_MD},

    # --- accordion frame: native header (secondary_color) + body
    #     (background_color), plus a thin border so each section (and nested
    #     sub-section) reads as a distinct panel. ---
    "CollapsableFrame": {
        "background_color": _BG_SECTION,
        "secondary_color": _BG_HEADER,
        "border_radius": 3,
        "border_color": _BORDER,
        "border_width": 1,
        "margin_height": 3,
        "margin_width": 0,
        "padding": 6,
    },
    "CollapsableFrame:hovered": {"secondary_color": _BG_HEADER_HOVER},

    # --- sliders ---
    "Slider": {
        "background_color": _BG_FIELD,
        "secondary_color": _hex("#3A3A38"),
        "draw_mode": 0,
        "color": _COLOR_TEXT,
        "border_radius": 3,
        "font_size": _FONT_SM,
    },
    "Slider.Fill": {"background_color": _COLOR_ACCENT},

    # --- combo box ---
    "ComboBox": {
        "background_color": _BG_FIELD,
        "secondary_color": _hex("#2A2A28"),
        "color": _COLOR_TEXT,
        "border_radius": 4,
        "border_color": _hex("#33332F"),
        "border_width": 1,
        "font_size": _FONT_MD,
        "padding": 5,
    },

    # --- progress bar ---
    "ProgressBar": {
        "color": _COLOR_ACCENT,
        "background_color": _BG_FIELD,
        "border_radius": 3,
        "secondary_color": _BG_FIELD,
    },
}
