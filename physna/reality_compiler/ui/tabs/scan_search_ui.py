# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Scan Search UI - a thin client over the hosted Physna API.

A single **accordion column** in the shape of Kit's native Property panel.
The user reads it top to bottom:

    Account -> [Search: Scene -> Parts -> Run card] ->
    [Results: Matches -> Scene Editing] -> Previous Searches

A one-time click-through tutorial (replayable from Account) introduces
the flow to first-time users.  Each :class:`omni.ui.CollapsableFrame` is
built **once** (so its collapsed state survives) and only its inner body
:class:`omni.ui.Frame` is rebuilt on refresh - this keeps field focus and
lets the accordion refresh without losing state.

Progress is shown **inline, at the action**: a single shared progress row
(status text + determinate bar) renders inside whichever section owns the
running operation - under the Run button while a search runs, inside
Previous Searches while a run downloads, and so on.  The bar binds to a
persistent fraction model, so the row can be rebuilt with its section body
and still reflect the true fraction.  Only one long operation runs at a
time (serialized by ``_is_running``), so one row of live widget refs is
enough.  All workflow logic lives in :class:`PipelineManager`; this module
is widgets and orchestration only.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, contextmanager
from typing import Callable, Optional

import omni.kit.async_engine
import omni.ui as ui

from ..util import (
    _BG_FIELD,
    _BORDER,
    _COLOR_ACCENT,
    _COLOR_ACCENT_HI,
    _COLOR_ERROR,
    _COLOR_INFO,
    _COLOR_MUTED,
    _COLOR_SUCCESS,
    _COLOR_TEXT,
    _COLOR_WARNING,
    _DIVIDER,
    _FONT_LG,
    _FONT_MD,
    _FONT_SM,
    _PANEL_STYLE,
    _hex,
    chip_tone,
)
from physna.reality_compiler.api import (
    AuthError,
    QUERYABLE_STATES,
    TERMINAL_STATES,
)
from physna.reality_compiler.logger import get_logger, notify_user
from physna.reality_compiler.pipelines import PipelineManager, WorkflowError

_log = get_logger("physna.reality_compiler.ui")

# Terminal states that mean a part is usable vs. a hard failure, for status
# color. Derived from the API's canonical sets so they can't drift: "good" =
# queryable; "bad" = every other terminal state.
_GOOD_STATES = QUERYABLE_STATES
_BAD_STATES = TERMINAL_STATES - QUERYABLE_STATES

# Known Physna stacks: (label, api_base, token_url). Selecting one fills the
# API Base + Token URL fields together so the user doesn't paste two URLs by
# hand; "Custom" (appended in the UI) leaves the fields editable.
_ENVIRONMENTS = [
    ("Production (app)",
     "https://app-api.physna.com/v3",
     "https://physna-app.auth.us-east-2.amazoncognito.com/oauth2/token"),
    ("Dev3",
     "https://dev3-api.physna.com/v3",
     "https://physna-dev3.auth.us-east-2.amazoncognito.com/oauth2/token"),
    ("Dev2",
     "https://dev2-api.physna.com/v3",
     "https://physna-dev2.auth.us-east-2.amazoncognito.com/oauth2/token"),
]

# Persistent flag so the tutorial auto-shows only on the very first launch.
_TUTORIAL_SETTING = "/persistent/exts/physna.reality_compiler/tutorial_seen"

# Wait this long after the last keystroke before re-filtering a list.
_FILTER_DEBOUNCE_S = 0.2

# An armed "Confirm?" delete disarms itself after this long unclicked.
_DELETE_CONFIRM_S = 4.0

# Style for the tooltip WINDOW FRAME itself (the "Tooltip" selector on the
# owning widget). Kit's default is unreadable in this panel (blue text,
# transparent bg), and the frame otherwise shows a transparent fringe around
# any content we draw — so the frame carries the solid card look and the
# content is just an explicitly-colored label.
_TOOLTIP_STYLE = {
    "background_color": _hex("#2A2A28"),
    "border_color": _BORDER,
    "border_width": 1,
    "border_radius": 4,
    "margin_width": 8,
    "margin_height": 6,
    "color": _COLOR_TEXT,
}

# The small muted text style used by hints, row labels, and metadata lines —
# one definition so every quiet label matches.
_STYLE_MUTED_SM = {"color": _COLOR_MUTED, "font_size": _FONT_SM}
_STYLE_MUTED_SM_LEFT = {
    "color": _COLOR_MUTED, "font_size": _FONT_SM,
    "alignment": ui.Alignment.LEFT_CENTER,
}
_STYLE_MUTED_SM_CENTER = {
    "color": _COLOR_MUTED, "font_size": _FONT_SM,
    "alignment": ui.Alignment.CENTER,
}


class _PagedList:
    """Filter + debounce + pager mechanics shared by the paginated list
    sections (Matches' part rows, Previous Searches).

    Owns the filter model, current page, debounce task, and the list's own
    frame — so filtering/paging rebuilds only the list, never the filter field
    (which would steal focus while the user types). The section supplies the
    frame's build fn via :meth:`attach` and calls :meth:`paginate` /
    :meth:`build_pager` inside it."""

    def __init__(self, owner: "ScanSearchUI", per_page: int) -> None:
        self._owner = owner
        self.per_page = per_page
        self.page = 0
        self.frame: Optional[ui.Frame] = None
        self._debounce_task = None
        self.filter_model = ui.SimpleStringModel("")
        self.filter_model.add_value_changed_fn(self._on_filter_changed)
        self.filter_model.add_end_edit_fn(lambda m: self.rebuild())

    @property
    def query(self) -> str:
        """The current filter text, normalized for matching."""
        return self.filter_model.get_value_as_string().strip().lower()

    def attach(self, build_fn: Callable[[], None]) -> None:
        """Create the list frame at the current UI cursor (called per body build)."""
        self.frame = ui.Frame(height=0)
        self.frame.set_build_fn(build_fn)

    def rebuild(self) -> None:
        self._owner._safe_rebuild(self.frame)

    def paginate(self, items: list) -> tuple:
        """Clamp the page to the (filtered) size and slice out the visible page.

        Returns ``(page_items, start, total)``; clamping matters when a filter
        or deletion shrinks the list below the current page."""
        total = len(items)
        last = max(0, (total - 1) // self.per_page)
        self.page = max(0, min(self.page, last))
        start = self.page * self.per_page
        return items[start:start + self.per_page], start, total

    def build_pager(self, start: int, total: int) -> None:
        """Prev / 'a-b of N' / Next below the list when it exceeds one page."""
        if total <= self.per_page:
            return
        last = (total - 1) // self.per_page
        hi = min(start + self.per_page, total)
        ui.Line(height=4, style={"color": _DIVIDER})
        with ui.HStack(height=24, spacing=8):
            self._owner._icon_button(
                "Prev", None, lambda: self._page(-1),
                name="ghost", width=64, height=24, enabled=self.page > 0,
            )
            ui.Spacer()
            ui.Label(
                f"{start + 1}-{hi} of {total}",
                style=_STYLE_MUTED_SM_CENTER,
            )
            ui.Spacer()
            self._owner._icon_button(
                "Next", None, lambda: self._page(1),
                name="ghost", width=64, height=24, enabled=self.page < last,
            )

    def _page(self, delta: int) -> None:
        self.page = max(0, self.page + delta)
        self.rebuild()

    def _on_filter_changed(self, _model) -> None:
        # Debounce: re-filter a beat after typing stops; a new filter starts
        # back at the first page.
        self.page = 0
        task = self._debounce_task
        if task is not None and not task.done():
            task.cancel()
        self._debounce_task = self._owner._run(self._debounced())

    async def _debounced(self) -> None:
        try:
            await asyncio.sleep(_FILTER_DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        self.rebuild()

    def cancel(self) -> None:
        """Cancel any pending debounce task (extension teardown)."""
        task, self._debounce_task = self._debounce_task, None
        if task is not None and not task.done():
            task.cancel()

_TUTORIAL_STEPS = [
    ("Welcome",
     "Physna Reality Compiler finds real-world parts inside a 3D scan. "
     "Matching runs on Physna's servers - this panel is your control surface. "
     "Here's the flow, start to finish."),
    ("Step 1 - Sign in",
     "Open the Account section and sign in with your tenant service-account "
     "credentials. Everything else stays locked until you're connected."),
    ("Step 2 - Choose a scene",
     "In Scene, use a Points prim you've selected in the stage, or load a "
     "point-cloud file. This is the scan your parts get searched against."),
    ("Step 3 - Add parts",
     "In Parts, add the reference shapes to locate: individual files, a whole "
     "folder, or prims you've selected in the stage."),
    ("Step 4 - Search & place",
     "Click Run Search. When matches come back, open Matches and set a Min "
     "score - only matches at or above it can be placed. Use Place All, or "
     "each part's slider, to place the found parts into the stage."),
    ("Step 5 - Refine & revisit",
     "Keep 'Hide scan points behind placements' on to swap covered scan "
     "points out as you place them (slide back to restore them). Scene Editing "
     "can then keep or remove the matched region. Every search is saved under "
     "Previous Searches to reopen any time."),
]


class ScanSearchUI:
    """Single-column accordion panel for the hosted scan-search workflow."""

    def __init__(self, manager: PipelineManager):
        self.manager = manager

        # Input models.
        self._client_id_model = ui.SimpleStringModel("")
        self._client_secret_model = ui.SimpleStringModel("")
        self._token_url_model = ui.SimpleStringModel(manager.config.token_url)
        self._api_base_model = ui.SimpleStringModel(manager.config.api_base)
        self._tenant_model = ui.SimpleStringModel(manager.config.tenant_id)
        self._run_name_model = ui.SimpleStringModel("")
        self._run_name_model.add_value_changed_fn(
            lambda m: self.manager.set_run_name(m.get_value_as_string())
        )
        self._min_score_model = ui.SimpleFloatModel(manager.state.min_score)
        self._min_score_model.add_value_changed_fn(self._on_min_score_changed)
        # On release, reconcile placements to the new threshold (remove any that
        # no longer qualify) and rebuild slider maxes.
        self._min_score_model.add_end_edit_fn(self._on_min_score_settled)
        # Determinate progress fraction (0..1). The inline ProgressBar binds to
        # this persistent model, so the progress row can be rebuilt with its
        # owning section body and still reflect the true fraction.
        self._frac_model = ui.SimpleFloatModel(0.0)
        # Per-part "placed N" slider models, keyed by id(part).
        self._import_models: dict = {}
        # In-flight per-part "Placed" reconcile tasks, keyed by id(part);
        # slider-settle awaits these (CAD placement is async) or it reads a
        # stale count.
        self._place_tasks: dict = {}
        # Tracks any-placed so only its 0<->nonzero flips rebuild the
        # placement-dependent frames — never the part rows/sliders mid-drag.
        self._last_any_placed = False
        # Own frame for Place All / Clear All (Clear All enables off placement).
        self._matches_actions_frame: Optional[ui.Frame] = None
        # Collapse state of the logged-out "Advanced" fold (persisted across
        # account-body rebuilds, which would otherwise reset it).
        self._advanced_collapsed = True

        # Accordion plumbing - built once, only bodies rebuilt.
        self._sections: dict[str, ui.CollapsableFrame] = {}
        self._bodies: dict[str, ui.Frame] = {}
        self._run_button_frame: Optional[ui.Frame] = None

        # Inline progress. `_progress_owner` names the section hosting the
        # active row - "run", "searches", "scene", "scene_editing", or
        # "account". For "searches", `_progress_item_id` further pins the row to
        # a single run entry so its bar renders on that entry's own row. The
        # widget refs below are re-captured each time that body (or the run
        # frame) renders the row via `_progress_row`.
        self._progress_active = False
        self._progress_owner: Optional[str] = None
        self._progress_item_id: Optional[str] = None
        self._progress_msg = ""
        self._progress_cancelable = False
        # Determinate ops (search, download) drive `_frac_model` and show the
        # bar; text-only ops (upload/index add-part, refresh, load, scene edit,
        # sign-in) show just the status line so a stuck-at-0% bar never lies.
        self._progress_determinate = True
        self._progress_label: Optional[ui.Label] = None
        self._run_progress_frame: Optional[ui.Frame] = None
        self._is_running = False
        self._run_task = None

        # Live per-part result label refs (for smooth placed-count updates).
        self._part_count_labels: dict = {}
        # Platform-discovered runs (None until the first check completes) and a
        # flag while a platform check is in flight (drives the Refresh button).
        self._discovered = None
        self._discovering = False
        self._logging_in = False  # double-click guard for Sign in
        # Two-click delete: id of the run whose Delete is armed ("Confirm?"),
        # and the timer that disarms it if the confirm never comes.
        self._delete_armed_id: Optional[str] = None
        self._delete_disarm_task = None
        # True once Cancel has been clicked for the current run (one cancel
        # only — see _on_cancel).
        self._cancel_requested = False

        # Search-bar placeholder labels + one-shot value hooks, keyed by bar.
        self._ph_labels: dict[str, ui.Label] = {}
        self._ph_hooked: set = set()

        # Tutorial overlay state.
        self._tutorial_window: Optional[ui.Window] = None
        self._tutorial_step = 0

        # "Scene was a stage prim" chooser (shown on load when the prim isn't
        # in the current stage): pick a local file or download from the platform.
        self._prim_scene_window: Optional[ui.Window] = None
        self._prim_scene_record = None

        # The two filterable, paginated lists (shared mechanics in _PagedList):
        # Previous Searches and the Matches per-part rows. Each list lives in
        # its own frame so filtering/paging rebuilds only the list and never
        # steals focus from its filter field.
        self._searches_paged = _PagedList(self, per_page=5)
        self._matches_paged = _PagedList(self, per_page=5)

    # ==================================================================
    # Top-level build
    # ==================================================================

    def build_ui(self):
        # Content-sized; the extension window wraps this in a ScrollingFrame.
        with ui.VStack(spacing=4, height=0, style=_PANEL_STYLE):
            # 1 - Account: open when signed out (you need to act), closed once
            # signed in (it's out of the way).
            self._add_section(
                "account", "Account", collapsed=self.manager.is_logged_in,
                body=self._build_account_body,
                chip_fn=lambda: ("Signed in", "done") if self.manager.is_logged_in
                else ("Sign in", "todo"),
            )

            # 2 - Search - set up the scene + parts, then run (Run card pinned).
            with self._group("search", "Search", collapsed=False):
                self._add_section(
                    "scene", "Scene", collapsed=False,
                    body=self._build_scene_body,
                    chip_fn=lambda: ("Ready", "done")
                    if self.manager.state.scene.is_set else ("Needed", "todo"),
                )
                self._add_section(
                    "parts", "Parts", collapsed=False,
                    body=self._build_parts_body,
                    chip_fn=self._parts_chip,
                )
                self._build_run_card()

            # 3 - Results - placement + matches + scene editing + add-a-part.
            with self._group("results", "Results", collapsed=True):
                self._add_section(
                    "matches", "Matches", collapsed=True,
                    body=self._build_matches_body,
                    chip_fn=self._matches_chip,
                )
                self._add_section(
                    "scene_editing", "Scene Editing", collapsed=True,
                    body=self._build_scene_editing_body,
                    chip_fn=lambda: ("Ready", "done") if self._any_placed()
                    else ("Optional", "muted"),
                )

            # 4 - Previous Searches - unified history: local + platform.
            self._add_section(
                "searches", "Previous Searches", collapsed=True,
                body=self._build_searches_body,
                # Count everything the list shows: local + discovered platform runs.
                chip_fn=lambda: (
                    str(len(self.manager.list_runs()) + len(self._discovered or [])),
                    "info",
                ),
            )

        if self.manager.is_logged_in:
            self._run(self._validate_session_on_start())
        if not self._tutorial_seen():
            self._show_tutorial()

    @contextmanager
    def _group(self, key, title, *, collapsed):
        """A large top-level section that wraps nested sub-sections. Built once
        (its body is static structure - only the leaf sub-section bodies
        rebuild), so nested collapse state survives refreshes."""
        frame = ui.CollapsableFrame(title, collapsed=collapsed)
        self._sections[key] = frame
        with frame:
            with ui.VStack(height=0, spacing=3):
                yield

    def _add_section(self, key, title, *, collapsed, body, chip_fn=None):
        """One accordion section - a native ui.CollapsableFrame (built once,
        so its collapse state survives) with a rebuildable inner body Frame.
        Any status pill renders at the top-right of the body; the header stays
        the authentic Kit Property-panel header."""
        frame = ui.CollapsableFrame(title, collapsed=collapsed)
        self._sections[key] = frame
        with frame:
            body_frame = ui.Frame(height=0)
            body_frame.set_build_fn(
                lambda b=body, c=chip_fn: self._render_body(b, c)
            )
            self._bodies[key] = body_frame

    def _render_body(self, body_fn, chip_fn):
        if chip_fn is not None:
            try:
                res = chip_fn()
            except Exception:
                _log.exception("Section chip failed")
                res = None
            if res:
                with ui.HStack(height=15):
                    ui.Spacer()
                    self._chip(res[0], res[1])
        body_fn()

    # ==================================================================
    # Small shared primitives
    # ==================================================================

    def _chip(self, text, tone):
        """A rounded status pill, vertically centered within its row.

        The outer VStack + flexible Spacers do the vertical centering (a bare
        fixed-height child otherwise top-justifies inside a taller HStack)."""
        fg, bg = chip_tone(tone)
        with ui.VStack(width=0):
            ui.Spacer()
            with ui.ZStack(width=0, height=14):
                ui.Rectangle(style={"background_color": bg, "border_radius": 6})
                with ui.HStack():
                    ui.Spacer(width=6)
                    ui.Label(
                        text,
                        style={"color": fg, "font_size": _FONT_SM,
                               "alignment": ui.Alignment.CENTER},
                    )
                    ui.Spacer(width=6)
            ui.Spacer()

    @staticmethod
    def _tooltip_fn(text: str):
        """A build fn for a readable hover tooltip (see ``_TOOLTIP_STYLE``:
        the frame carries the solid card, the label carries explicit colors —
        never use plain ``set_tooltip`` in this panel)."""
        def build():
            ui.Label(
                text, word_wrap=True, width=0,
                style={"color": _COLOR_TEXT, "font_size": _FONT_SM},
            )
        return build

    def _icon_button(self, text, glyph, clicked_fn, *, enabled=True, name=None,
                     width=0, height=0, tooltip: str = ""):
        """A custom, reliably-centered text button.

        omni.ui's native Button mis-centers text (and bare Spacers don't
        expand), so we stack three things in a ZStack: a background Rectangle,
        a Label that fills the button and centers its text, and a transparent
        hit Rectangle on top that owns the click + hover.  Icons are omitted
        for now (``glyph`` is ignored). ``tooltip`` renders via
        :meth:`_tooltip_fn` (explicit colors — the default tooltip style is
        unreadable in this panel)."""
        variant = name or "default"
        # height=None -> fill the parent's cross-axis (match a taller sibling
        # like a search field); otherwise a fixed height (default 30).
        fill_height = height is None
        if variant == "primary":
            bg, bg_hover, fg, border = (
                _COLOR_ACCENT, _COLOR_ACCENT_HI, _hex("#0E1206"), _COLOR_ACCENT_HI
            )
        elif variant == "ghost":
            bg, bg_hover, fg, border = (
                0x00000000, _hex("#3C3C3A"), _COLOR_MUTED, _BORDER
            )
        else:
            bg, bg_hover, fg, border = (
                _hex("#3C3C3A"), _hex("#4A4A48"), _COLOR_TEXT, _BORDER
            )
        if not enabled:
            bg = bg_hover = _hex("#2E2E2C")
            fg, border = _hex("#6B6E72"), _hex("#3A3A38")

        def _bg_style(hovered):
            return {"background_color": bg_hover if hovered else bg,
                    "border_radius": 4, "border_color": border, "border_width": 1}

        stack_kwargs = {}
        if not fill_height:
            stack_kwargs["height"] = height or 30
        if width:
            stack_kwargs["width"] = width
        stack = ui.ZStack(**stack_kwargs)
        with stack:
            bg_rect = ui.Rectangle(style=_bg_style(False))
            if text:
                ui.Label(
                    text,
                    style={"color": fg, "font_size": _FONT_MD,
                           "alignment": ui.Alignment.CENTER},
                )
            # Transparent overlay owns the interaction (a plain background
            # Rectangle wouldn't get hover once the Label sits on top).
            # Selector-keyed style so the "Tooltip" frame gets the card look.
            hit = ui.Rectangle(style={
                "Rectangle": {"background_color": 0x00000000},
                "Tooltip": _TOOLTIP_STYLE,
            })
            if tooltip:
                try:
                    hit.set_tooltip_fn(self._tooltip_fn(tooltip))
                except Exception:
                    pass  # never let tooltip plumbing break a button
        if enabled and clicked_fn:
            # Fire on release-while-still-hovered (native click semantics), so a
            # press-then-drag-off cancels instead of triggering the action.
            st = {"pressed": False, "hovered": False}

            def _on_hover(hovered, r=bg_rect):
                st["hovered"] = hovered
                r.set_style(_bg_style(hovered))

            def _on_press(x, y, b, m):
                if b == 0:
                    st["pressed"] = True

            def _on_release(x, y, b, m, cb=clicked_fn):
                fire = b == 0 and st["pressed"] and st["hovered"]
                st["pressed"] = False
                if fire:
                    cb()

            hit.set_mouse_hovered_fn(_on_hover)
            hit.set_mouse_pressed_fn(_on_press)
            hit.set_mouse_released_fn(_on_release)
        return stack

    def _body(self, spacing=4):
        """A section body VStack (the CollapsableFrame supplies the inset)."""
        return ui.VStack(spacing=spacing, height=0)

    def _property_row(self, label, model, *, password=False):
        with ui.HStack(height=26, spacing=8):
            ui.Label(
                label, width=100,
                style=_STYLE_MUTED_SM_LEFT,
            )
            field = ui.StringField(model=model, height=24)
            if password:
                field.password_mode = True

    def _slider_row(self, label, build_control):
        with ui.HStack(height=24, spacing=8):
            ui.Label(
                label, width=100,
                style=_STYLE_MUTED_SM_LEFT,
            )
            build_control()

    def _hint(self, text):
        ui.Label(
            text, word_wrap=True,
            style=_STYLE_MUTED_SM,
        )

    def _search_bar(self, key, model, prompt):
        """A search field with a leading magnifier and a muted placeholder.

        The StringField sits on top with a transparent background (so it takes
        clicks), a Label placeholder shows through when empty, and a one-shot
        value hook toggles the placeholder's visibility as the user types."""
        with ui.ZStack(height=24):
            ui.Rectangle(style={
                "background_color": _BG_FIELD, "border_radius": 4,
                "border_color": _hex("#33332F"), "border_width": 1,
            })
            with ui.HStack(spacing=6):
                ui.Spacer(width=8)
                with ui.ZStack():
                    lbl = ui.Label(
                        prompt,
                        style=_STYLE_MUTED_SM_LEFT,
                    )
                    ui.StringField(
                        model=model, height=22,
                        # padding 0 so the caret/text starts flush with the
                        # placeholder (the Field style's default padding pushed
                        # typed text ~5px right, over the prompt).
                        style={"background_color": 0x00000000, "border_width": 0,
                               "padding": 0},
                    )
                ui.Spacer(width=8)
        lbl.visible = not model.get_value_as_string().strip()
        self._ph_labels[key] = lbl
        if key not in self._ph_hooked:
            self._ph_hooked.add(key)
            model.add_value_changed_fn(
                lambda m, k=key: self._update_placeholder(k, m)
            )
            # Hide the prompt the moment the field is focused (so the caret is
            # never over it), and restore it on blur if the field is empty.
            model.add_begin_edit_fn(
                lambda m, k=key: self._set_placeholder_visible(k, False)
            )
            model.add_end_edit_fn(
                lambda m, k=key: self._update_placeholder(k, m)
            )

    def _update_placeholder(self, key, model):
        lbl = self._ph_labels.get(key)
        if lbl:
            try:
                lbl.visible = not model.get_value_as_string().strip()
            except Exception:
                pass

    def _set_placeholder_visible(self, key, visible):
        lbl = self._ph_labels.get(key)
        if lbl:
            try:
                lbl.visible = visible
            except Exception:
                pass

    # ==================================================================
    # Account
    # ==================================================================

    def _build_account_body(self):
        m = self.manager
        with self._body():
            self._progress_row("account")
            if m.is_logged_in:
                ui.Label(
                    f"Signed in as {m.session.client_id or 'service account'}",
                    style={"color": _COLOR_SUCCESS, "font_size": _FONT_SM},
                )
                ui.Label(
                    f"Tenant: {m.config.tenant_id or '(not set)'}",
                    style=_STYLE_MUTED_SM,
                )
                # Sign out lives in the shared bottom row (with Replay Tutorial).
            else:
                self._hint(
                    "Connect to Physna to begin. These are your tenant "
                    "service-account credentials."
                )
                self._build_env_row()
                self._property_row(
                    "Tenant ID", self._tenant_model,
                )
                self._property_row(
                    "Client ID", self._client_id_model,
                )
                self._property_row(
                    "Client Secret", self._client_secret_model, password=True,
                )
                with ui.CollapsableFrame(
                    "Advanced", collapsed=self._advanced_collapsed,
                    collapsed_changed_fn=self._on_advanced_collapsed,
                ):
                    with self._body():
                        # Environment sets these two for a known stack; expose
                        # them here for a Custom stack or manual overrides.
                        self._property_row(
                            "Token URL", self._token_url_model,
                        )
                        self._property_row(
                            "API Base", self._api_base_model,
                        )
                        with ui.HStack(spacing=8):
                            ui.Spacer()
                            self._icon_button(
                                "Apply", None, self._on_apply_clicked,
                                name="ghost", width=88,
                            )
                ui.Spacer(height=2)
                self._icon_button(
                    "Sign in", "play", self._on_login, name="primary", height=30,
                )
                if not m.session.credential_store.available:
                    ui.Label(
                        "No secure credential store found - the secret will not "
                        "persist across restarts.",
                        style={"color": _COLOR_WARNING, "font_size": _FONT_SM},
                        word_wrap=True,
                    )

            ui.Line(height=6, style={"color": _DIVIDER})
            with ui.HStack(height=30, spacing=8):
                self._icon_button(
                    "Replay Tutorial", None, self._show_tutorial, name="ghost",
                    width=150,
                )
                ui.Spacer()
                if m.is_logged_in:
                    self._icon_button(
                        "Sign out", None, self._on_logout, name="ghost", width=88,
                        enabled=not self._is_running,
                    )

    # ==================================================================
    # Scene
    # ==================================================================

    def _build_scene_body(self):
        scene = self.manager.state.scene
        locked = self._is_running
        with self._body():
            self._hint(
                "The scan to search inside: a stage prim or a point-cloud file. "
                "E57: import via File > Import, then Use Selected Prim."
            )
            self._progress_row("scene")
            with ui.HStack(spacing=6):
                self._icon_button(
                    "Use Selected Prim", "prim", self._on_use_selected_scene,
                    enabled=not locked,
                )
                self._icon_button(
                    "Pick File...", "file",
                    lambda: self._run(self._pick_scene_file()), enabled=not locked,
                )
            if scene.prim_path:
                ui.Label(
                    f"Prim: {scene.prim_path}", word_wrap=True,
                    style={"color": _COLOR_SUCCESS, "font_size": _FONT_SM},
                )
            elif scene.file_path:
                ui.Label(
                    f"File: {os.path.basename(scene.file_path)}", word_wrap=True,
                    style={"color": _COLOR_SUCCESS, "font_size": _FONT_SM},
                )
            else:
                self._hint(
                    "No scene chosen yet - pick a stage prim (Use Selected Prim) "
                    "or a point-cloud file. This is what your parts get searched "
                    "against."
                )
            # Up-axis correction is for point clouds we load from a file; a
            # stage prim (incl. a Kit-imported E57) is already oriented for the
            # stage, so don't offer to re-rotate it.
            if scene.file_path:
                self._build_up_axis_row(locked)
            ui.Line(height=4, style={"color": _DIVIDER})
            self._property_row(
                "Search name", self._run_name_model,
            )

    # Up-axis choices: label -> stored axis. "z" (auto) suits most scans; flip
    # to "y" if a scan still comes in on its side, or "none" to leave it raw.
    _UP_AXIS_OPTIONS = [("Auto (Z-up)", "z"), ("Y-up", "y"), ("As-is", "none")]

    def _combo_row(self, label: str, labels: list, index: int,
                   on_selected, *, enabled: bool = True):
        """A labelled dropdown row (shared by Environment and Up axis)."""
        with ui.HStack(height=26, spacing=8):
            ui.Label(label, width=100, style=_STYLE_MUTED_SM_LEFT)
            combo = ui.ComboBox(index, *labels, enabled=enabled)
            combo.model.get_item_value_model().add_value_changed_fn(
                lambda m: on_selected(m.get_value_as_int())
            )

    def _build_up_axis_row(self, locked):
        labels = [lbl for lbl, _ in self._UP_AXIS_OPTIONS]
        axes = [ax for _, ax in self._UP_AXIS_OPTIONS]
        cur = self.manager.scene_up_axis
        idx = axes.index(cur) if cur in axes else 0
        # Disabled mid-run: re-orienting re-authors placements, which would
        # race the workflow mutating the same state.
        self._combo_row(
            "Up axis", labels, idx, self._on_up_axis_selected,
            enabled=not locked,
        )
        self._hint(
            "Rotates the scan to the stage's up-axis so it isn't on its side. "
            "Flip this if it still looks wrong (placements rotate with it)."
        )

    def _on_up_axis_selected(self, index):
        if not (0 <= index < len(self._UP_AXIS_OPTIONS)):
            return
        axis = self._UP_AXIS_OPTIONS[index][1]
        if axis == self.manager.scene_up_axis:
            return
        self._run(self._apply_up_axis(axis))

    async def _apply_up_axis(self, axis):
        try:
            await self.manager.set_scene_up_axis(axis)
        except Exception as exc:
            _log.exception("Up-axis change failed")
            notify_user(f"Couldn't re-orient the scan: {exc}", "error")
        self._safe_rebuild(self._bodies.get("matches"))

    # ==================================================================
    # Parts
    # ==================================================================

    def _parts_chip(self):
        n = len(self.manager.state.parts)
        return (f"{n} queued", "done") if n else ("Needed", "todo")

    def _state_tone(self, state):
        if state in _GOOD_STATES:
            return "done"
        if state in _BAD_STATES:
            return "warn"
        return "info"

    def _build_parts_body(self):
        parts = self.manager.state.parts
        locked = self._is_running
        with self._body():
            self._hint(
                "The reference parts to locate in the scene. Add from files "
                "(Ctrl/Shift-click to pick several), a whole folder, or prims "
                "selected in the stage."
            )
            with ui.HStack(spacing=6):
                self._icon_button(
                    "Use Selected Prim(s)", "prim", self._on_use_selected_parts,
                    enabled=not locked,
                )
                self._icon_button(
                    "Add File(s)...", "file",
                    lambda: self._run(self._add_part_file()), enabled=not locked,
                )
            with ui.HStack(spacing=6):
                self._icon_button(
                    "Add Folder...", "folder",
                    lambda: self._run(self._add_parts_folder()), enabled=not locked,
                )
                self._icon_button(
                    "Clear", "trash", self._on_clear_parts, name="ghost", width=88,
                    enabled=bool(parts) and not locked,
                    tooltip="Remove all queued parts",
                )
            if not parts:
                self._hint(
                    "No parts added yet - pick a file, a folder, or your current "
                    "stage selection. Each part is searched against your scene."
                )
            else:
                with ui.VStack(spacing=0, height=0):
                    for part in parts:
                        ui.Line(height=4, style={"color": _DIVIDER})
                        with ui.HStack(height=24, spacing=8):
                            ui.Label(
                                part.display_name,
                                style={"font_size": _FONT_MD,
                                       "alignment": ui.Alignment.LEFT_CENTER},
                            )
                            ui.Spacer()
                            self._chip(part.state, self._state_tone(part.state))

    # ==================================================================
    # Run button (pinned inside Search, built once)
    # ==================================================================

    def _build_run_card(self):
        # The Run button plus an inline progress row that appears right below it
        # while a search (or add-part) runs. The Search wrapper is static
        # structure, so these frames survive body rebuilds; the progress row
        # itself is rebuilt only when the run starts/ends.
        ui.Line(height=6, style={"color": _DIVIDER})
        self._run_button_frame = ui.Frame(height=0)
        self._run_button_frame.set_build_fn(self._build_run_button)
        self._run_progress_frame = ui.Frame(height=0)
        self._run_progress_frame.set_build_fn(lambda: self._progress_row("run"))

    def _build_run_button(self):
        # Always enabled (except while searching); if prerequisites are missing
        # the click reports exactly what's needed via a notification.
        text = "Searching..." if self._is_running else "Run Search"
        self._icon_button(
            text, None, self._start_run, name="primary", height=34,
            enabled=not self._is_running,
        )

    def _missing_requirements(self) -> list[str]:
        m = self.manager
        missing = []
        if not m.is_logged_in:
            missing.append("sign in (Account)")
        if not m.state.scene.is_set:
            missing.append("choose a Scene")
        if not m.state.parts:
            missing.append("add at least one Part")
        return missing

    # ==================================================================
    # Matches (matches + placement combined)
    # ==================================================================

    def _matches_chip(self):
        placed = sum(self.manager.placed_count(p) for p in self.manager.state.parts)
        return (f"{placed} placed", "info") if placed else ("0 placed", "muted")

    def _build_matches_body(self):
        parts = self.manager.state.parts
        with self._body(spacing=8):
            if not parts:
                self._hint(
                    "Matches appear here after a search. Run a search above (or "
                    "load one from Previous Searches), then set a min score and "
                    "place matches into the stage."
                )
                return

            # --- placement controls (apply to every part) ---
            self._slider_row(
                "Min score",
                lambda: ui.FloatSlider(
                    model=self._min_score_model, min=0.0, max=100.0, step=1.0,
                    enabled=not self._is_running,
                ),
            )
            self._hint(
                "Min score gates how many matches can be placed - only matches at "
                "or above it qualify. Place All drops every qualifying match; each "
                "part's slider fine-tunes it, best score first."
            )
            self._matches_actions_frame = ui.Frame(height=0)
            self._matches_actions_frame.set_build_fn(self._build_matches_actions)
            self._last_any_placed = self._any_placed()  # sync tracker to display
            self._progress_row("matches")  # Place All / Clear All progress
            self._hotswap_row()
            ui.Line(height=6, style={"color": _DIVIDER})

            # --- filter + paginated per-part rows (own frame so filtering
            #     rebuilds only the list and keeps focus in the field) ---
            self._search_bar(
                "matches_filter", self._matches_paged.filter_model, "Filter parts..."
            )
            self._matches_paged.attach(self._build_matches_list)

    def _build_matches_actions(self):
        """Place All / Clear All (own frame; Clear All enables off placement)."""
        with ui.HStack(spacing=8):
            self._icon_button(
                "Place All", "plus", self._on_place_all,
                name="primary", height=30, enabled=not self._is_running,
            )
            self._icon_button(
                "Clear All", "trash", self._on_clear_all,
                name="ghost", width=100, height=30,
                enabled=not self._is_running and self._any_placed(),
            )

    def _sync_placement_enabled(self):
        """Rebuild the placement-dependent frames (Place/Clear row, Scene
        Editing) — but only when any-placed flips 0<->nonzero, so slider drags
        never recreate the part rows."""
        now = self._any_placed()
        if now == self._last_any_placed:
            return
        self._last_any_placed = now
        self._safe_rebuild(self._matches_actions_frame)
        self._safe_rebuild(self._bodies.get("scene_editing"))

    def _build_matches_list(self):
        """The filtered, paginated per-part rows (rebuilt on filter/page)."""
        self._part_count_labels = {}
        parts = self.manager.state.parts
        q = self._matches_paged.query
        filtered = [p for p in parts if not q or q in p.display_name.lower()]
        with ui.VStack(height=0, spacing=0):
            if not filtered:
                self._hint("No parts match the filter.")
                return

            page_parts, start, total = self._matches_paged.paginate(filtered)
            with ui.VStack(height=0, spacing=0):
                for i, part in enumerate(page_parts):
                    if i > 0:
                        ui.Line(height=4, style={"color": _DIVIDER})
                    self._build_part_row(part)

            self._matches_paged.build_pager(start, total)
            if not any(p.matches for p in filtered) and not q:
                self._hint(
                    "No matches yet - parts may still be indexing, or none matched."
                )

    def _rebuild_matches_list(self):
        self._matches_paged.rebuild()

    def _hotswap_row(self):
        """Checkbox: hide scan points behind placed matches (on by default)."""
        has_pts = self.manager.has_scene_points()
        with ui.HStack(height=22, spacing=8):
            cb = ui.CheckBox(width=18, height=18, enabled=has_pts)
            # Set the value before hooking the change fn so this doesn't fire.
            cb.model.set_value(self.manager.hide_points and has_pts)
            cb.model.add_value_changed_fn(
                lambda m: self._on_hotswap_toggled(m.get_value_as_bool())
            )
            note = "" if has_pts else "  (no scene points in the stage)"
            ui.Label(
                f"Hide scan points behind placements{note}",
                style=_STYLE_MUTED_SM_LEFT,
            )

    def _build_part_row(self, part):
        state_str = part.state
        if part.match_error:
            # Reading matches failed - show it, don't pass empty off as success.
            ui.Label(
                f"{part.display_name}   [couldn't read matches] {part.match_error}",
                style={"color": _COLOR_ERROR, "font_size": _FONT_SM},
                word_wrap=True,
            )
            return
        if not part.matches:
            # Still indexing, or terminal with no matches - status only.
            if state_str in _GOOD_STATES:
                color, note = _COLOR_MUTED, "[finished] no matches"
            else:
                color, note = _COLOR_WARNING, f"[{state_str}]"
            ui.Label(
                f"{part.display_name}   {note}",
                style={"color": color, "font_size": _FONT_SM},
            )
            return

        qual = self.manager.qualifying_count(part)
        placed = self.manager.placed_count(part)
        with ui.VStack(spacing=2):
            ui.Label(part.display_name, style={"font_size": _FONT_MD})
            lbl = ui.Label(
                self._match_readout(part),
                style=_STYLE_MUTED_SM,
            )
            self._part_count_labels[id(part)] = (part, lbl)
            with ui.HStack(height=22, spacing=8):
                ui.Label("Placed", width=100, style={"font_size": _FONT_SM,
                         "alignment": ui.Alignment.LEFT_CENTER})
                model = self._slider_model_for(part)
                # Sync the slider to the actual placed count (guarded no-op in
                # the handler when equal, so this doesn't reconcile).
                if model.get_value_as_int() != placed:
                    model.set_value(placed)
                # Slider max is the min-score qualifying count, so it can't
                # place a sub-threshold match. Disabled during a run: matches
                # are still streaming in and the models were just reset.
                ui.IntSlider(
                    model=model, min=0, max=max(0, qual),
                    enabled=not self._is_running,
                )
            if qual == 0:
                self._hint("No matches meet the min score - lower it to place some.")

    def _reset_part_models(self):
        """Drop per-part UI state (slider models, in-flight place tasks, count
        labels) — used whenever the part list is replaced, so closures can't
        pin dead PartEntry objects alive."""
        # Cancel, don't just drop: an orphaned placement task would finish
        # after the reset and leave prims in the stage nothing tracks.
        for task in self._place_tasks.values():
            if task is not None and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        self._import_models = {}
        self._place_tasks = {}
        self._part_count_labels = {}

    def _slider_model_for(self, part):
        model = self._import_models.get(id(part))
        if model is None:
            model = ui.SimpleIntModel(self.manager.placed_count(part))
            model.add_value_changed_fn(
                lambda m, p=part: self._on_slider_changed(p, m)
            )
            # On release, apply the point hot-swap once and refresh the sections
            # whose enabled state depends on placement (deferred off the per-tick
            # path so dragging the slider stays smooth).
            model.add_end_edit_fn(lambda m, p=part: self._on_slider_settled(p))
            self._import_models[id(part)] = model
        return model

    def _on_slider_changed(self, part, model):
        n = model.get_value_as_int()
        if n == self.manager.placed_count(part):
            return  # already there (also guards the programmatic sync above)
        # Place/remove live; the expensive point hot-swap waits for slider
        # release. Tracked so settle can await async (CAD) placements.
        self._place_tasks[id(part)] = self._run(
            self._reconcile_placed(part, n, occlude=False)
        )

    def _on_slider_settled(self, part):
        self._run(self._settle_placement())

    async def _settle_placement(self):
        # Await in-flight placements first, else a slow CAD conversion leaves
        # the slider snapped back to a stale count.
        for task in list(self._place_tasks.values()):
            if task is not None and not task.done():
                try:
                    await task
                except Exception:
                    pass
        try:
            await self.manager.refresh_point_occlusion()
        except Exception:
            _log.exception("Point hot-swap refresh failed")
        # Refresh the placement-dependent buttons (Clear All, Scene Editing)
        # without recreating the part rows/sliders.
        self._sync_placement_enabled()

    async def _reconcile_placed(self, part, n, occlude: bool = True):
        try:
            await self.manager.set_part_placed_count(part, n, occlude=occlude)
        except Exception as exc:
            _log.exception("Slider placement failed")
            notify_user(f"Placement error: {exc}", "error")
        entry = self._part_count_labels.get(id(part))
        if entry:
            _p, lbl = entry
            try:
                lbl.text = self._match_readout(part)
            except Exception:
                pass
        self._sync_placement_enabled()

    def _match_readout(self, part) -> str:
        """One-line summary of a part's matches, threshold, and placements."""
        matches = part.matches
        total = len(matches)
        best = matches[0].score if matches else 0.0
        thr = self.manager.state.min_score
        qual = self.manager.qualifying_count(part)
        placed = self.manager.placed_count(part)
        if qual == 0:
            return (f"0 of {total} match(es) >= min {thr:.0f}  "
                    f"(best {best:.1f})")
        cutoff = matches[qual - 1].score
        return (f"{placed} of {qual} placed   ({qual} of {total} match(es) "
                f">= min {thr:.0f}; best {best:.1f}, cutoff {cutoff:.1f})")

    def _on_min_score_changed(self, model):
        # Live: update the model + per-part readouts as the slider drags (cheap,
        # text only). Slider maxes and surplus removal happen on release, in
        # _on_min_score_settled, to avoid churny rebuilds mid-drag.
        self.manager.set_min_score(model.get_value_as_float())
        for _id, (part, lbl) in list(self._part_count_labels.items()):
            try:
                lbl.text = self._match_readout(part)
            except Exception:
                pass

    def _on_min_score_settled(self, model):
        self.manager.set_min_score(model.get_value_as_float())
        self._run(self._do_min_score_reconcile())

    async def _do_min_score_reconcile(self):
        try:
            await self.manager.reconcile_min_score()
        except Exception as exc:
            _log.exception("Min-score reconcile failed")
            notify_user(f"Placement update failed: {exc}", "error")
        # Rebuild just the results bodies (updates slider maxes + placed counts).
        for key in ("matches", "scene_editing"):
            self._safe_rebuild(self._bodies.get(key))

    # ==================================================================
    # Scene Editing
    # ==================================================================

    def _any_placed(self) -> bool:
        return any(
            self.manager.placed_count(p) for p in self.manager.state.parts
        )

    def _build_scene_editing_body(self):
        active = self._any_placed() and not self._is_running
        with self._body():
            self._hint(
                "After placing matches, carve the scan by what was found (uses "
                "the placed parts' bounding boxes). This deletes scene points, "
                "so save first."
            )
            with ui.HStack(spacing=8):
                self._icon_button(
                    "Remove Matched", "trash",
                    lambda: self._run(self._remove_points(keep_only=False)),
                    enabled=active,
                    tooltip="Delete scan points inside placed parts",
                )
                self._icon_button(
                    "Keep Only Matched", "prim",
                    lambda: self._run(self._remove_points(keep_only=True)),
                    enabled=active,
                    tooltip="Keep only scan points inside placed parts",
                )
            if not active:
                self._hint("Place at least one match first.")
            self._progress_row("scene_editing")

    # ==================================================================
    # Searches - unified local + platform history
    # ==================================================================

    def _build_searches_body(self):
        """One unified list: local runs (Load / Update / Delete) and
        platform-only runs (Download & Load). The buttons tell them apart, so
        there are no sub-headers. The filter + Refresh stay put; the list lives
        in its own frame so live-filtering (debounced as you type) rebuilds only
        the list and keeps focus in the field. The list reloads on load/sign-in
        and via Refresh; each entry hosts its own download progress row."""
        with self._body(spacing=6):
            # Filter + a Refresh that reloads local and re-checks the platform.
            # The button fills the row height so it matches the field exactly
            # (the field renders taller than a fixed 30px button).
            with ui.HStack(spacing=6):
                self._search_bar(
                    "searches", self._searches_paged.filter_model,
                    "Filter searches...",
                )
                self._icon_button(
                    "Refreshing..." if self._discovering else "Refresh", None,
                    lambda: self._run(self._do_discover()), name="ghost",
                    width=110, height=None,  # fits both labels without resizing
                    enabled=self.manager.is_logged_in and not self._discovering
                    and not self._is_running,
                    tooltip="Re-check local and platform searches",
                )
            self._searches_paged.attach(self._build_searches_list)

    def _build_searches_list(self):
        q = self._searches_paged.query

        def matches(name, folder):
            return not q or q in name.lower() or q in (folder or "").lower()

        local = [r for r in self.manager.list_runs() if matches(r.name, r.run_folder)]
        remote = [r for r in (self._discovered or []) if matches(r.name, r.folder)]
        # One ordered list; the buttons on each row tell local from remote.
        entries = [("local", r) for r in local] + [("remote", r) for r in remote]
        with ui.VStack(height=0, spacing=6):
            if not entries:
                self._hint(
                    "No searches match the filter." if q else
                    "No searches yet. Run a search above, or sign in and hit "
                    "Refresh to find searches on the platform."
                )
                return

            page_entries, start, total = self._searches_paged.paginate(entries)
            with ui.VStack(spacing=2, height=0):
                for kind, item in page_entries:
                    if kind == "local":
                        self._build_local_run_row(item)
                    else:
                        self._build_remote_run_row(item)

            self._searches_paged.build_pager(start, total)

    def _rebuild_searches_list(self):
        self._searches_paged.rebuild()

    def _build_local_run_row(self, record):
        ui.Line(height=6, style={"color": _DIVIDER})
        ui.Label(record.name, style={"font_size": _FONT_MD}, word_wrap=True)
        ui.Label(
            f"{len(record.parts)} part(s), {record.total_matches} match(es)   "
            f"{record.created_at}",
            style=_STYLE_MUTED_SM, word_wrap=True,
        )
        # A run that's incomplete only counts as "interrupted" if it isn't the
        # one running right now — the active in-flight run is still going, not
        # stranded (its live status shows in the progress row below).
        is_active = self._is_running and record.id == self.manager.active_run_id
        interrupted = not record.complete and not is_active
        if is_active:
            ui.Label(
                "Running now - indexing on the platform...",
                style=_STYLE_MUTED_SM,
                word_wrap=True,
            )
        elif interrupted:
            ui.Label(
                "Interrupted while indexing - Resume to finish reading matches.",
                style={"color": _COLOR_WARNING, "font_size": _FONT_SM},
                word_wrap=True,
            )
        with ui.HStack(spacing=8):
            self._icon_button(
                "Load", None, lambda r=record: self._on_load_run(r),
                name="primary" if not interrupted else None,
                enabled=not self._is_running,
                tooltip="Restore this search's scene and matches",
            )
            # An interrupted run resumes (re-poll to terminal); a finished one
            # updates (re-read matches). Same handler, different framing.
            self._icon_button(
                "Resume" if interrupted else "Update", None,
                lambda r=record: self._on_refresh_run(r, resume=interrupted),
                name="primary" if interrupted else None,
                enabled=not self._is_running,
                tooltip=("Finish this search on the platform" if interrupted
                         else "Re-read matches from the platform"),
            )
            # Two-click delete: first click arms ("Confirm?"), second deletes.
            armed = self._delete_armed_id == record.id
            self._icon_button(
                "Confirm?" if armed else "Delete", None,
                lambda r=record: self._on_delete_run(r),
                name="primary" if armed else "ghost", width=82,
                enabled=not self._is_running,
                tooltip="Delete the local copy (platform assets are kept)",
            )
        self._progress_row("searches", record.id)

    def _build_remote_run_row(self, run):
        ui.Line(height=6, style={"color": _DIVIDER})
        ui.Label(run.name, style={"font_size": _FONT_MD}, word_wrap=True)
        # Two single-line labels - a word-wrap label with an embedded newline
        # mis-measures its height and under-sizes the frame.
        ui.Label(
            f"folder: {run.folder}", word_wrap=True,
            style=_STYLE_MUTED_SM,
        )
        ui.Label(
            f"{len(run.parts)} part(s)   {run.created_at}",
            style=_STYLE_MUTED_SM,
        )
        self._icon_button(
            "Download & Load", None,
            lambda r=run: self._run(self._on_load_discovered(r)),
            name="primary", enabled=not self._is_running,
            tooltip="Download from the platform and save locally",
        )
        self._progress_row("searches", run.folder)

    # ==================================================================
    # Tutorial overlay (one-time, replayable)
    # ==================================================================

    def _tutorial_seen(self) -> bool:
        try:
            import carb.settings

            return bool(carb.settings.get_settings().get(_TUTORIAL_SETTING))
        except Exception:
            # Fail safe: if settings aren't available, don't nag on every launch.
            return True

    def _mark_tutorial_seen(self):
        try:
            import carb.settings

            carb.settings.get_settings().set(_TUTORIAL_SETTING, True)
        except Exception:
            pass

    def _show_tutorial(self):
        # Deferred onto the loop so the window isn't created inside the panel's
        # synchronous build context.
        self._run(self._present_tutorial())

    async def _present_tutorial(self):
        self._tutorial_step = 0
        if self._tutorial_window is None:
            self._tutorial_window = ui.Window(
                "Getting Started - Physna", width=400, height=250,
            )
            self._tutorial_window.frame.set_build_fn(self._build_tutorial)
        self._tutorial_window.visible = True
        self._tutorial_window.frame.rebuild()

    def _build_tutorial(self):
        title, body = _TUTORIAL_STEPS[self._tutorial_step]
        n = len(_TUTORIAL_STEPS)
        with ui.VStack(spacing=8, height=0, style=_PANEL_STYLE):
            with ui.HStack(height=20, spacing=8):
                ui.Label(title, style={"font_size": _FONT_LG, "color": _COLOR_TEXT})
                ui.Spacer()
                self._icon_button(
                    "Skip", None, self._close_tutorial, name="ghost", width=56,
                    height=24,
                )
            ui.Label(
                body, word_wrap=True,
                style={"color": _COLOR_TEXT, "font_size": _FONT_MD},
            )
            ui.Spacer()
            with ui.HStack(height=8, spacing=6):
                ui.Spacer()
                for i in range(n):
                    on = i == self._tutorial_step
                    with ui.ZStack(width=8, height=8):
                        ui.Rectangle(style={
                            "background_color": _COLOR_INFO if on else _hex("#4A4A48"),
                            "border_radius": 4,
                        })
                ui.Spacer()
            with ui.HStack(height=30, spacing=8):
                if self._tutorial_step > 0:
                    self._icon_button(
                        "Back", None, self._tutorial_back, width=80,
                    )
                ui.Spacer()
                if self._tutorial_step < n - 1:
                    self._icon_button(
                        "Next", None, self._tutorial_next, name="primary",
                        width=92,
                    )
                else:
                    self._icon_button(
                        "Get Started", None, self._close_tutorial, name="primary",
                        width=112,
                    )

    def _tutorial_next(self):
        if self._tutorial_step < len(_TUTORIAL_STEPS) - 1:
            self._tutorial_step += 1
            if self._tutorial_window:
                self._tutorial_window.frame.rebuild()

    def _tutorial_back(self):
        if self._tutorial_step > 0:
            self._tutorial_step -= 1
            if self._tutorial_window:
                self._tutorial_window.frame.rebuild()

    def _close_tutorial(self):
        self._mark_tutorial_seen()
        if self._tutorial_window:
            self._tutorial_window.visible = False

    # ==================================================================
    # "Scene was a stage prim" chooser
    # ==================================================================

    def _maybe_prompt_prim_scene(self, record):
        """Offer a scene source when a loaded run's prim scene isn't in the stage.

        No-op when the run has a local scene file, or when the scene prim is
        still present in the current stage (nothing to supply)."""
        if record is None or record.scene_file_path:
            return
        if self.manager.has_scene_points():
            return
        self._prim_scene_record = record
        # Deferred onto the loop so the window isn't created inside a build.
        self._run(self._present_prim_scene_prompt())

    async def _present_prim_scene_prompt(self):
        if self._prim_scene_window is None:
            self._prim_scene_window = ui.Window(
                "Scene not in stage - Physna", width=460, height=250,
            )
            self._prim_scene_window.frame.set_build_fn(self._build_prim_scene_prompt)
        self._prim_scene_window.visible = True
        self._prim_scene_window.frame.rebuild()

    def _build_prim_scene_prompt(self):
        record = self._prim_scene_record
        has_remote = bool(record and record.scene_asset_id)
        can_download = has_remote and self.manager.is_logged_in
        with ui.VStack(spacing=10, height=0, style=_PANEL_STYLE):
            ui.Label(
                "This search's scene was a stage prim",
                style={"font_size": _FONT_LG, "color": _COLOR_TEXT},
            )
            ui.Label(
                "It wasn't saved as a local file, and that prim isn't in the "
                "current stage - so there's nothing to display or place against. "
                "Choose where to get the scene from:",
                word_wrap=True,
                style={"color": _COLOR_TEXT, "font_size": _FONT_MD},
            )
            ui.Spacer(height=2)
            self._icon_button(
                "Select Local File...", None, self._on_prim_scene_local,
                name="primary", height=30,
            )
            self._icon_button(
                "Download from Platform", None, self._on_prim_scene_download,
                height=30, enabled=can_download,
            )
            if has_remote and not self.manager.is_logged_in:
                ui.Label(
                    "Sign in to download the uploaded scene from the platform.",
                    style={"color": _COLOR_WARNING, "font_size": _FONT_SM},
                    word_wrap=True,
                )
            elif not has_remote:
                ui.Label(
                    "This run has no uploaded scene on the platform; pick a local "
                    "file instead.",
                    style=_STYLE_MUTED_SM,
                    word_wrap=True,
                )
            ui.Spacer(height=2)
            with ui.HStack(height=28, spacing=8):
                ui.Spacer()
                self._icon_button(
                    "Skip", None, self._close_prim_scene_prompt, name="ghost",
                    width=88, height=26,
                )

    def _close_prim_scene_prompt(self):
        if self._prim_scene_window:
            self._prim_scene_window.visible = False
        # Drop the record ref so the prompt can't pin a stale run alive.
        self._prim_scene_record = None

    def _on_prim_scene_local(self):
        self._close_prim_scene_prompt()
        self._run(self._supply_prim_scene_local())

    def _on_prim_scene_download(self):
        record = self._prim_scene_record
        self._close_prim_scene_prompt()
        self._run(self._supply_prim_scene_download(record))

    async def _supply_prim_scene_local(self):
        if self._is_running:
            return
        path = await self.manager.pick_scene_file()
        # Re-check: the prompt (and the picker) stay open indefinitely, so a
        # run may have started in the meantime — don't stack a second long op.
        if not path or self._is_running:
            return
        self._collapse("scene", False)
        async with self._long_op("scene", f"Loading {os.path.basename(path)}..."):
            try:
                await self.manager.import_scene_file(path, on_progress=self._set_progress)
                notify_user("Scene loaded; matches now place against it", "info")
            except Exception as exc:
                _log.exception("Prim-scene local import failed")
                notify_user(f"Couldn't load that scene file: {exc}", "error")

    async def _supply_prim_scene_download(self, record):
        # The prompt stays open indefinitely; a run may have started since.
        if record is None or self._is_running:
            return
        self._collapse("scene", False)
        async with self._long_op(
            "scene", f"Downloading scene for '{record.name}'...", determinate=True,
        ):
            try:
                await self.manager.download_scene_for_record(
                    record, on_progress=self._set_progress,
                    on_fraction=self._set_fraction,
                )
                notify_user("Scene downloaded; matches now place against it", "info")
            except (AuthError, WorkflowError) as exc:
                notify_user(str(exc), "warning")
            except Exception as exc:
                _log.exception("Prim-scene download failed")
                notify_user(f"Scene download failed: {exc}", "error")

    def destroy(self):
        # Cancel in-flight tasks first so nothing keeps polling the platform or
        # poking torn-down widgets after the extension unloads.
        self._searches_paged.cancel()
        self._matches_paged.cancel()
        tasks = [
            self._run_task, self._delete_disarm_task,
            *self._place_tasks.values(),
        ]
        for task in tasks:
            if task is not None and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        self._run_task = None
        self._place_tasks.clear()

        for attr in ("_tutorial_window", "_prim_scene_window"):
            win = getattr(self, attr, None)
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
                setattr(self, attr, None)

    # ==================================================================
    # Shared helpers
    # ==================================================================

    def _run(self, coro):
        return omni.kit.async_engine.run_coroutine(coro)

    def _collapse(self, key: str, collapsed: bool):
        frame = self._sections.get(key)
        if frame is not None:
            try:
                frame.collapsed = collapsed
            except Exception:
                pass

    def _refresh_content(self):
        for frame in self._bodies.values():
            self._safe_rebuild(frame)

    # Bodies safe to rebuild mid-search: everything except the ones holding an
    # editable field (scene's search-name, saved/discover search bars) whose
    # focus a rebuild would steal while the user types during a long search.
    _RUN_REFRESH_BODIES = (
        "parts", "matches", "scene_editing",
    )

    @staticmethod
    def _safe_rebuild(frame):
        """Rebuild a Frame, tolerating a None ref or a torn-down widget."""
        if frame is not None:
            try:
                frame.rebuild()
            except Exception:
                pass

    def _refresh_during_run(self):
        """A lighter refresh for search/add-part progress callbacks that leaves
        focusable fields (search name, search bars, the matches filter)
        untouched. For Matches, only the per-part list rebuilds (its filter
        field lives outside the list), so streaming results never steal focus."""
        for key in self._RUN_REFRESH_BODIES:
            if key == "matches":
                self._rebuild_matches_list()
            else:
                self._safe_rebuild(self._bodies.get(key))

    def _rebuild_all(self):
        self._refresh_content()
        self._update_run_button()

    def _update_run_button(self):
        self._safe_rebuild(self._run_button_frame)

    def _set_running(self, running: bool):
        self._is_running = running
        self._refresh_content()
        self._update_run_button()

    @asynccontextmanager
    async def _long_op(self, owner: str, msg: str = "", *,
                       determinate: bool = False, cancelable: bool = False,
                       item_id: Optional[str] = None):
        """Serialize a long-running operation and bracket its progress row.

        Locks the UI (``_is_running``), shows ``owner``'s progress row, and —
        no matter how the body exits — tears both down again. Every long
        operation must run inside one of these so two can't interleave and a
        forgotten ``finally`` can't leave the UI stuck locked."""
        self._set_running(True)
        self._begin_progress(
            owner, msg, determinate=determinate, cancelable=cancelable,
            item_id=item_id,
        )
        try:
            yield
        finally:
            self._end_progress()
            self._set_running(False)

    # ------------------------------------------------------------------
    # Inline progress row (shared; owned by the running section)
    # ------------------------------------------------------------------

    def _progress_row(self, owner: str, item_id: Optional[str] = None):
        """Render the shared progress row when ``owner`` owns the active op.

        Called from each section body (and the run frame) at the point where
        progress should appear. Renders nothing unless that section currently
        owns a running operation. ``item_id`` pins the row to one list entry
        (used by Previous Searches so a download's bar renders on its own row);
        it must match the id passed to ``_begin_progress``. The bar binds to the
        persistent frac model, so a rebuild of the owning body re-creates the
        row at the right value; text updates land on ``_progress_label`` in
        place between rebuilds."""
        if not (self._progress_active and self._progress_owner == owner
                and self._progress_item_id == item_id):
            return
        with ui.VStack(height=0, spacing=4):
            ui.Spacer(height=2)
            with ui.HStack(spacing=8):
                self._progress_label = ui.Label(
                    self._progress_msg or "Working...", word_wrap=True,
                    style={"color": _COLOR_INFO, "font_size": _FONT_SM},
                )
                if self._progress_cancelable:
                    ui.Spacer()
                    self._icon_button(
                        "Cancel", None, self._on_cancel, name="ghost",
                        width=88, height=24,
                    )
            if self._progress_determinate:
                ui.ProgressBar(model=self._frac_model, height=8)

    def _render_owner(self, owner: Optional[str]):
        """Rebuild the frame that hosts ``owner``'s progress row."""
        if owner is None:
            return
        self._safe_rebuild(
            self._run_progress_frame if owner == "run"
            else self._bodies.get(owner)
        )

    def _begin_progress(self, owner: str, msg: str = "", *,
                        determinate: bool = False, cancelable: bool = False,
                        item_id: Optional[str] = None):
        """Show the inline progress row inside ``owner`` and seed its state.

        ``determinate`` renders the bar (bound to ``_frac_model``, reset to 0);
        otherwise only the status line shows. ``cancelable`` adds a Cancel.
        ``item_id`` pins the row to a single list entry within ``owner``."""
        self._progress_active = True
        self._progress_owner = owner
        self._progress_item_id = item_id
        self._progress_msg = msg
        self._progress_determinate = determinate
        self._progress_cancelable = cancelable
        if determinate:
            self._frac_model.set_value(0.0)
        self._render_owner(owner)

    def _end_progress(self):
        """Hide the inline progress row and drop its widget refs."""
        owner = self._progress_owner
        self._progress_active = False
        self._progress_owner = None
        self._progress_item_id = None
        self._progress_cancelable = False
        self._progress_determinate = True
        self._progress_msg = ""
        self._progress_label = None
        self._render_owner(owner)

    def _set_progress(self, msg: str):
        """Update the progress text in place (no rebuild)."""
        self._progress_msg = msg
        if self._progress_label:
            try:
                self._progress_label.text = msg
                self._progress_label.visible = bool(msg)
            except Exception:
                self._progress_label = None

    def _set_fraction(self, value: float):
        """Update the progress fraction; negative means indeterminate (hold)."""
        if value >= 0.0:
            self._frac_model.set_value(max(0.0, min(1.0, value)))

    async def _validate_session_on_start(self):
        ok = await self.manager.validate_saved_session()
        if not ok:
            notify_user("Saved sign-in is no longer valid: please sign in again", "warning")
            self._rebuild_all()
            return
        # Signed in and valid - populate Previous Searches from the platform.
        await self._do_discover()

    # ==================================================================
    # Auth handlers
    # ==================================================================

    def _build_env_row(self):
        """A dropdown that fills API Base + Token URL for a known stack.

        Rebuilt with the account body; the selected index is re-derived from
        the current config each build, so the choice survives refreshes."""
        labels = [name for name, _a, _t in _ENVIRONMENTS] + ["Custom"]
        self._combo_row(
            "Environment", labels, self._current_env_index(),
            self._on_env_selected,
        )

    def _current_env_index(self) -> int:
        """Index of the environment matching the current config, else Custom."""
        api = self._api_base_model.get_value_as_string().strip().rstrip("/")
        tok = self._token_url_model.get_value_as_string().strip()
        for i, (_name, env_api, env_tok) in enumerate(_ENVIRONMENTS):
            if env_api.rstrip("/") == api and env_tok == tok:
                return i
        return len(_ENVIRONMENTS)  # Custom

    def _on_env_selected(self, index: int):
        if not (0 <= index < len(_ENVIRONMENTS)):
            # "Custom": the URLs live under Advanced, so pop the fold open and
            # leave the fields as typed — otherwise picking Custom looks like a
            # no-op (and the combo snaps back on the next rebuild).
            self._advanced_collapsed = False
            self._safe_rebuild(self._bodies.get("account"))
            return
        _name, api, tok = _ENVIRONMENTS[index]
        self._api_base_model.set_value(api)
        self._token_url_model.set_value(tok)
        self._on_apply_config()

    def _on_advanced_collapsed(self, collapsed):
        self._advanced_collapsed = collapsed

    def _on_login(self):
        self._run(self._do_login())

    async def _do_login(self):
        if self._logging_in:
            return  # double-click guard: one login at a time
        self._logging_in = True
        try:
            await self._do_login_inner()
        finally:
            self._logging_in = False

    async def _do_login_inner(self):
        self._on_apply_config()
        client_id = self._client_id_model.get_value_as_string().strip()
        secret = self._client_secret_model.get_value_as_string().strip()
        token_url = self._token_url_model.get_value_as_string().strip()
        tenant = self._tenant_model.get_value_as_string().strip()
        if not tenant:
            notify_user("Enter your Tenant ID", "warning"); return
        if not (client_id and secret):
            notify_user("Enter a Client ID and Client Secret", "warning"); return
        if not token_url:
            notify_user(
                "Pick an Environment, or set a Token URL under Advanced",
                "warning",
            ); return
        self._begin_progress("account", "Signing in...")
        try:
            await self.manager.login(client_id, secret)
        except AuthError as exc:
            notify_user(f"Sign-in failed: {exc}", "error")
            return
        except Exception as exc:
            _log.exception("Unexpected sign-in error")
            notify_user(f"Sign-in error: {exc}", "error")
            return
        finally:
            self._end_progress()
        self._client_secret_model.set_value("")
        notify_user("Signed in to Physna", "info")
        # Collapse the finished step, open the next one (guide the user down).
        self._collapse("account", True)
        self._collapse("search", False)
        self._collapse("scene", False)
        self._rebuild_all()
        # Now that we're signed in, populate Previous Searches from the platform.
        self._run(self._do_discover())

    def _on_logout(self):
        self.manager.logout()
        notify_user("Signed out", "info")
        # Platform list belongs to the signed-in session - clear it on sign-out.
        self._discovered = None
        self._collapse("account", False)
        self._rebuild_all()

    def _on_apply_config(self):
        self.manager.set_config(
            api_base=self._api_base_model.get_value_as_string().strip(),
            tenant_id=self._tenant_model.get_value_as_string().strip(),
            token_url=self._token_url_model.get_value_as_string().strip(),
        )

    def _on_apply_clicked(self):
        self._on_apply_config()
        cfg = self.manager.config
        notify_user(f"Connection settings applied (API base: {cfg.api_base})", "info")

    # ==================================================================
    # Scene handlers
    # ==================================================================

    def _on_use_selected_scene(self):
        prim = self.manager.set_scene_from_selection()
        if not prim:
            notify_user("No prim selected in the stage", "warning")
            return
        self._prefill_run_name()
        self._rebuild_all()

    async def _pick_scene_file(self):
        path = await self.manager.pick_scene_file()
        if not path:
            return
        # Derive from the picked path directly — the scene's file_path isn't set
        # until import_scene_file runs below, so suggest_run_name() would be empty
        # if we relied on state here.
        self._prefill_run_name(path)
        self._begin_progress("scene", f"Loading {os.path.basename(path)}...")
        try:
            prim = await self.manager.import_scene_file(path, on_progress=self._set_progress)
            if prim:
                notify_user("Scene loaded into the stage", "info")
        except Exception as exc:
            _log.exception("Scene display failed")
            self.manager.set_scene_from_file(path)
            notify_user(f"Scene set for upload, but couldn't display it: {exc}", "warning")
        finally:
            self._end_progress()
        self._rebuild_all()

    def _prefill_run_name(self, path: Optional[str] = None):
        if self._run_name_model.get_value_as_string().strip():
            return
        name = self.manager.run_name_for_file(path) if path else self.manager.suggest_run_name()
        if name:
            self._run_name_model.set_value(name)

    # ==================================================================
    # Parts handlers
    # ==================================================================

    def _on_use_selected_parts(self):
        added, selected = self.manager.add_parts_from_selection()
        if selected == 0:
            notify_user("Select prim(s) in the stage first", "warning")
        elif added == 0:
            notify_user("Selected prim(s) had no exportable geometry", "warning")
        elif added < selected:
            notify_user(
                f"Queued {added} of {selected} part(s) "
                "(the rest had no exportable geometry)", "info"
            )
        else:
            notify_user(f"Queued {added} part(s) from selection", "info")
        self._rebuild_all()

    async def _add_part_file(self):
        paths = await self.manager.pick_part_files()
        if not paths:
            return
        n = self.manager.add_part_files(paths)
        # Always confirm — silence after picking a file reads as "it didn't work"
        # (and n == 0 means every pick was already queued or unusable).
        if n:
            notify_user(f"Queued {n} part(s)", "info")
        else:
            notify_user("No new parts queued (already added?)", "warning")
        self._rebuild_all()

    async def _add_parts_folder(self):
        paths = await self.manager.pick_parts_from_folder()
        if not paths:
            notify_user("No supported part files found in that folder", "warning")
            return
        n = self.manager.add_part_files(paths)
        notify_user(f"Queued {n} part(s)", "info")
        self._rebuild_all()

    def _on_clear_parts(self):
        self.manager.clear_parts()
        # Drop the per-part slider models (their value_changed closures pin the
        # now-dead PartEntry objects alive otherwise).
        self._reset_part_models()
        self._rebuild_all()

    # ==================================================================
    # Run + results handlers
    # ==================================================================

    def _start_run(self):
        self._cancel_requested = False
        self._run_task = self._run(self._on_run())

    def _teardown_run_progress(self):
        """Force the run's progress row + UI lock down, idempotently."""
        self._run_task = None
        self._cancel_requested = False
        if self._progress_active and self._progress_owner == "run":
            self._end_progress()
        if self._is_running:
            self._set_running(False)

    def _on_cancel(self):
        # One cancel only. A second task.cancel() would set asyncio's
        # must-cancel flag, re-injecting CancelledError at the coroutine's
        # next await — the context-manager exit — which SKIPS the teardown
        # finally and freezes "Cancelling..." on screen.
        if self._cancel_requested:
            return
        task = self._run_task
        if task is None or task.done():
            return
        self._cancel_requested = True
        task.cancel()
        # Drop the Cancel button immediately (nothing left to cancel) and
        # leave just the status text while the run unwinds.
        self._progress_cancelable = False
        self._set_progress("Cancelling...")
        self._render_owner("run")
        # Watchdog: await the task itself, then force teardown — independent
        # of the coroutine's own cleanup, so the row can never get stuck.
        self._run(self._await_cancelled_run(task))

    async def _await_cancelled_run(self, task):
        try:
            await task
        except BaseException:
            pass  # cancelled (expected) or failed — either way it's finished
        self._teardown_run_progress()

    async def _on_run(self):
        if self._is_running:
            return
        missing = self._missing_requirements()
        if missing:
            notify_user("Can't search yet - " + ", ".join(missing) + ".", "warning")
            return
        # Fresh search - drop any stale per-part slider/size models from a prior one.
        self._reset_part_models()
        # Open the results sections so per-part progress is visible as it arrives.
        self._collapse("results", False)
        self._collapse("matches", False)
        async with self._long_op(
            "run", "Starting search...", determinate=True, cancelable=True
        ):
            try:
                await self.manager.run_search(
                    on_progress=self._set_progress, on_fraction=self._set_fraction,
                    on_status=self._refresh_during_run,
                    on_part_matches=lambda _p: self._refresh_during_run(),
                )
                notify_user("Search complete", "info")
            except asyncio.CancelledError:
                notify_user("Search cancelled", "warning")
                raise
            except (AuthError, WorkflowError) as exc:
                notify_user(str(exc), "error")
            except Exception as exc:
                _log.exception("Scan search failed")
                notify_user(f"Search error: {exc}", "error")
            finally:
                self._run_task = None

    def _on_place_all(self):
        self._run(self._do_place_all())

    async def _do_place_all(self):
        if self._is_running:
            return
        # Serialized like every other long op: placing can await CAD->USD
        # conversion, and a search starting mid-placement would race it.
        async with self._long_op("matches", "Placing all matches..."):
            try:
                placed = await self.manager.place_all_matches()
                if placed:
                    notify_user(f"Placed {placed} match(es) in the stage", "info")
                else:
                    notify_user("Nothing placed; lower the min score", "warning")
            except Exception as exc:
                _log.exception("Place all failed")
                notify_user(f"Placement error: {exc}", "error")
        # (_long_op's exit already refreshed every body, syncing the sliders.)

    def _on_clear_all(self):
        self._run(self._do_clear_all())

    async def _do_clear_all(self):
        if self._is_running:
            return
        async with self._long_op("matches", "Clearing placements..."):
            try:
                removed = await self.manager.clear_all_placements()
                if removed:
                    notify_user(f"Cleared {removed} placement(s)", "info")
            except Exception as exc:
                _log.exception("Clear all failed")
                notify_user(f"Clear error: {exc}", "error")

    def _on_hotswap_toggled(self, enabled: bool):
        self._run(self._do_set_hide_points(enabled))

    async def _do_set_hide_points(self, enabled: bool):
        try:
            await self.manager.set_hide_points(enabled)
        except Exception as exc:
            _log.exception("Hot-swap toggle failed")
            notify_user(f"Couldn't update scan points: {exc}", "error")

    async def _remove_points(self, keep_only: bool):
        self._begin_progress("scene_editing", "Editing scene points...")
        try:
            removed = await self.manager.remove_matched_points(keep_only=keep_only)
            notify_user(f"Removed {removed:,} scene point(s)", "info")
        except (WorkflowError,) as exc:
            notify_user(str(exc), "warning")
        except Exception as exc:
            _log.exception("Point removal failed")
            notify_user(f"Point removal error: {exc}", "error")
        finally:
            self._end_progress()

    # ==================================================================
    # Saved-search handlers
    # ==================================================================

    def _on_load_run(self, record):
        self._run(self._do_load_run(record))

    async def _do_load_run(self, record):
        if self._is_running:
            return
        async with self._long_op(
            "searches", f"Loading '{record.name}'...", item_id=record.id
        ):
            try:
                self.manager.load_run(record)
                note = await self._finish_run_loaded(record)
                notify_user(
                    f"Loaded search '{record.name}': "
                    f"{record.total_matches} match(es).{note}",
                    "info",
                )
            except Exception as exc:
                _log.exception("Load search failed")
                notify_user(f"Load error: {exc}", "error")
        # After the load settles, if the scene was a stage prim that isn't in
        # this stage, offer to supply it (local file or platform download).
        self._maybe_prompt_prim_scene(record)

    async def _finish_run_loaded(self, record) -> str:
        """Import the scene (if available) and refresh the UI. Returns a note."""
        self._run_name_model.set_value(record.name)
        self._reset_part_models()
        scene_note = ""
        if record.scene_file_path and os.path.exists(record.scene_file_path):
            try:
                await self.manager.import_scene_file(
                    record.scene_file_path, on_progress=self._set_progress
                )
                self._set_progress("")
            except Exception:
                _log.exception("Scene import on run load failed")
                self._set_progress("")
                scene_note = " (couldn't display the scene; place uses world coords)"
        elif record.scene_file_path:
            scene_note = " (scene file not found; place uses world coords)"
        elif self.manager.has_scene_points():
            scene_note = " (using the scene prim already in the stage)"
        else:
            # Prim scene, and the prim isn't in this stage — the chooser (popped
            # by _maybe_prompt_prim_scene after the load) offers a source.
            scene_note = " (scene was a stage prim - choose a source)"
        # Surface the results the user just loaded.
        self._collapse("results", False)
        self._collapse("matches", False)
        self._rebuild_all()
        return scene_note

    async def _do_discover(self):
        """Reload the platform list. Reflected on the Refresh button (not the
        inline row, which is reserved for a specific run's download)."""
        if not self.manager.is_logged_in:
            notify_user("Sign in to check the platform", "warning")
            return
        if self._discovering:
            return  # a sweep is already running; don't double the requests
        self._discovering = True
        self._render_owner("searches")
        try:
            # First reconcile: an interrupted run may have finished on the
            # platform since it was saved — flip those to complete (drops their
            # Resume button) — or been deleted from it — drop those records —
            # before re-listing.
            try:
                done, removed = await self.manager.reconcile_incomplete_runs()
                if done:
                    notify_user(
                        f"{done} interrupted run(s) finished on the platform", "info"
                    )
                if removed:
                    notify_user(
                        f"{removed} interrupted run(s) no longer exist on the "
                        "platform - removed from the list",
                        "info",
                    )
            except Exception:
                _log.exception("Reconcile of incomplete runs failed")
            self._discovered = await self.manager.discover_platform_runs()
        except AuthError as exc:
            notify_user(str(exc), "warning")
        except Exception as exc:
            _log.exception("Platform discovery failed")
            notify_user(f"Discovery error: {exc}", "error")
        finally:
            self._discovering = False
            self._render_owner("searches")

    async def _on_load_discovered(self, run):
        if self._is_running:
            return
        async with self._long_op(
            "searches", f"Downloading '{run.name}' ({len(run.parts)} part(s))...",
            determinate=True, item_id=run.folder,
        ):
            try:
                record = await self.manager.load_discovered_run(
                    run, on_progress=self._set_progress,
                    on_fraction=self._set_fraction,
                )
                # It's local now - drop it from the platform list so it isn't
                # listed twice (once downloaded, once still "on the platform").
                if self._discovered:
                    self._discovered = [
                        r for r in self._discovered if r.folder != run.folder
                    ]
                note = await self._finish_run_loaded(record)
                notify_user(
                    f"Loaded '{record.name}' from platform: "
                    f"{record.total_matches} match(es).{note}",
                    "info",
                )
            except (AuthError, WorkflowError) as exc:
                notify_user(str(exc), "error")
            except Exception as exc:
                _log.exception("Load discovered search failed")
                notify_user(f"Load error: {exc}", "error")

    def _on_refresh_run(self, record, resume: bool = False):
        self._run(self._do_refresh_run(record, resume=resume))

    async def _do_refresh_run(self, record, resume: bool = False):
        # "Resume" (an interrupted run) and "Update" (re-poll a finished run)
        # are the same operation — re-poll the run's assets to terminal and
        # re-read matches — with different messaging.
        verb = "Resuming" if resume else "Updating"
        if not self.manager.is_logged_in:
            notify_user(
                f"Sign in to {'resume' if resume else 'update'} a search", "warning"
            )
            return
        if self._is_running:
            return
        async with self._long_op(
            "searches", f"{verb} '{record.name}'...", item_id=record.id
        ):
            try:
                # refresh_run saves and returns a fresh record; the one passed
                # in still holds the pre-refresh match counts.
                updated = await self.manager.refresh_run(
                    record, on_progress=self._set_progress,
                    on_status=self._refresh_during_run,
                    on_part_matches=lambda _p: self._refresh_during_run(),
                ) or record
                self._run_name_model.set_value(updated.name)
                self._reset_part_models()
                done = "Resumed" if resume else "Updated"
                notify_user(
                    f"{done} '{updated.name}': {updated.total_matches} match(es)",
                    "info",
                )
            except (AuthError, WorkflowError) as exc:
                notify_user(str(exc), "error")
            except Exception as exc:
                _log.exception("Refresh search failed")
                notify_user(f"Refresh error: {exc}", "error")

    def _on_delete_run(self, record):
        # First click arms this row's button ("Confirm?"); the second click —
        # on the same row, within _DELETE_CONFIRM_S — actually deletes.
        disarm = self._delete_disarm_task
        if disarm is not None and not disarm.done():
            disarm.cancel()
        if self._delete_armed_id != record.id:
            self._delete_armed_id = record.id
            self._delete_disarm_task = self._run(self._auto_disarm_delete(record.id))
            self._rebuild_searches_list()
            return
        self._delete_armed_id = None
        self.manager.delete_run(record.id)
        notify_user(f"Deleted saved search '{record.name}'", "info")
        self._refresh_content()
        # Deleting only removes the local copy - the search usually still lives
        # on the platform. Re-check so it reappears as a remote (downloadable)
        # entry instead of disappearing from the list entirely.
        if self.manager.is_logged_in and not self._is_running:
            self._run(self._do_discover())

    async def _auto_disarm_delete(self, run_id: str):
        """Snap an unconfirmed "Confirm?" back to "Delete" after a beat."""
        try:
            await asyncio.sleep(_DELETE_CONFIRM_S)
        except asyncio.CancelledError:
            return
        if self._delete_armed_id == run_id:
            self._delete_armed_id = None
            self._rebuild_searches_list()
