"""Deterministic clock for tool timestamps.

Gold-action DB-hash matching requires created/updated timestamps to be reproducible: the gold
replay and the agent run must stamp identical times. Tools therefore read the current time via
``get_now()`` (never ``datetime.now()`` directly). The environment sets a fixed per-task time;
by default it is a stable constant so behaviour is deterministic even outside a task run.

The clock canonicalises to the **seed timestamp format** ``"YYYY-MM-DD HH:MM:SS"`` (space-separated,
matching ``data/itsm/db.json``). A task's ``current_time`` arrives ISO-8601 with a ``T`` separator
(e.g. ``"2025-12-03T13:00:00"``); ``set_now`` reformats it to the seed format so a freshly stamped
``created_at``/``updated_at`` is byte-identical to seed/unmodified rows. Without this, the DB compare
(a raw string equality) flags a spurious mismatch whenever one side re-stamps a timestamp the other
left at its seed value (``"2025-12-03 11:45:00"`` vs ``"2025-12-03T13:00:00"``).
"""

from __future__ import annotations

import threading
from datetime import datetime

#: Default frozen time when no task time is set, in the seed format ("YYYY-MM-DD HH:MM:SS").
DEFAULT_NOW = "2024-06-01 00:00:00"

#: The canonical stored timestamp format — matches the seed database (space-separated, seconds).
_SEED_FORMAT = "%Y-%m-%d %H:%M:%S"

_state = threading.local()


def _to_seed_format(timestamp: str) -> str:
    """Reformat an ISO-8601-ish timestamp to the seed format. ``T``-separated, space-separated,
    fractional-second and offset inputs all canonicalise to ``"YYYY-MM-DD HH:MM:SS"``. An
    unparseable value is returned verbatim (defensive — callers validate upstream)."""
    try:
        return datetime.fromisoformat(timestamp).strftime(_SEED_FORMAT)
    except (ValueError, TypeError):
        return timestamp


def set_now(iso_timestamp: str) -> None:
    """Freeze ``get_now()`` for the current thread, canonicalising to the seed format."""
    _state.now = _to_seed_format(iso_timestamp)


def reset_now() -> None:
    """Restore the default frozen time."""
    _state.now = DEFAULT_NOW


def get_now() -> str:
    """Return the current (frozen) timestamp in the seed format ("YYYY-MM-DD HH:MM:SS")."""
    return getattr(_state, "now", DEFAULT_NOW)
