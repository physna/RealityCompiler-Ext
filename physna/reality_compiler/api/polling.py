# SPDX-FileCopyrightText: Copyright (c) 2026 Physna, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Poll a set of assets until every one reaches a terminal state.

The transport-agnostic core is :func:`poll_step`, which advances a
:class:`PollState` by one round of ``GET /assets/{id}`` calls.  Callers
own the waiting: the Kit pipeline sleeps between rounds with
``asyncio.sleep`` so the UI stays responsive, while a plain script can
loop with ``time.sleep``.  This keeps :mod:`.polling` free of any event
loop or ``omni`` dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .client import ApiError, PhysnaClient
from .models import Asset, is_working

# Guidance from the API guide: poll on a ~30-second interval.
DEFAULT_POLL_INTERVAL_S = 30.0

# Consecutive "asset not found" 404s required before an id counts as deleted.
# Callers act destructively on ``missing`` (fail a run, drop a saved record),
# so a lone transient 404 must never be enough.
MISSING_CONFIRMATIONS = 2


def is_asset_not_found(exc: Exception) -> bool:
    """True for the 404 the API returns when an asset itself no longer exists
    ("Asset not found" / "Model asset not found" / "Scene asset not found").

    Distinct from other 404s (a proxy error page, the transient scene-match
    "not been computed") — this one means the asset was deleted."""
    return (
        isinstance(exc, ApiError)
        and exc.status_code == 404
        and "asset not found" in (getattr(exc, "body", "") or "").lower()
    )


@dataclass
class PollState:
    """Mutable progress record for a batch poll."""

    pending: set[str]  # asset ids still in a working state
    resolved: dict[str, Asset] = field(default_factory=dict)  # id -> terminal asset
    missing: set[str] = field(default_factory=set)  # ids confirmed deleted
    # Consecutive not-found observations per id; cleared when the asset is
    # seen again, promoted to ``missing`` at MISSING_CONFIRMATIONS.
    miss_counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def for_ids(cls, asset_ids: Iterable[str]) -> "PollState":
        return cls(pending=set(asset_ids))

    @property
    def done(self) -> bool:
        return not self.pending


def poll_step(
    client: PhysnaClient,
    state: PollState,
    *,
    on_update: Callable[[Asset], None] | None = None,
) -> PollState:
    """Fetch each pending asset once; move terminal ones to ``resolved``.

    A transient error on one asset (network blip, momentary 5xx, a 404 that
    isn't the API's "asset not found") leaves it in ``pending`` to be retried
    next round; it does not abort the batch. An "asset not found" 404 seen on
    MISSING_CONFIRMATIONS consecutive steps moves the id to ``missing`` —
    deletion is permanent, so retrying it forever would hang the poll.
    """
    for asset_id in list(state.pending):
        try:
            asset = client.get_asset(asset_id)
        except ApiError as exc:
            if is_asset_not_found(exc):
                seen = state.miss_counts.get(asset_id, 0) + 1
                state.miss_counts[asset_id] = seen
                if seen >= MISSING_CONFIRMATIONS:
                    state.pending.discard(asset_id)
                    state.missing.add(asset_id)
            # Anything else: keep it pending and try again on the next round.
            continue
        state.miss_counts.pop(asset_id, None)  # it exists; not-founds weren't real
        if on_update is not None:
            on_update(asset)
        if not is_working(asset.state):
            state.pending.discard(asset_id)
            state.resolved[asset_id] = asset
    return state
