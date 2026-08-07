"""``session.*`` — one world instance, held open, used by one caller at a time.

The invariant most likely to be broken by a future refactor: **a tool that fails is a successful
RPC call carrying ``{"success": false, "error": ...}``, never an RPC error.** A tool refusing — bad
arguments, a policy violation, a missing record — is the environment behaving correctly, and the
agent must see it as a tool response so it can react. RPC errors are for transport and protocol
faults only. ``bridge.py``'s ``call_tool`` on the miner side keeps the same rule.

The gym's clock is thread-local and module-global, not per-environment, so each call re-freezes it
to the session's clock and restores it after — two sessions with different clocks interleave
correctly on the single dispatch thread.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from enterprise_worlds.environment.environment import Environment
from enterprise_worlds.platform import state as _state
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.serialize import jsonable
from enterprise_worlds.platform.wire import WireErrorCode
from enterprise_worlds.utils.clock import DEFAULT_NOW, reset_now, set_now


class _Session:
    def __init__(self, env: Environment, clock: str, *, delta: Any = None,
                 acting_user_id: Optional[str] = None, org_id: Optional[str] = None,
                 org_ids: Optional[list] = None, seed_db: Optional[str] = None) -> None:
        self.env = env
        self.clock = clock
        # How this world was built. The miner's `dbHashEval` rebuilds a GOLD world that must be
        # comparable to this one — same delta, same acting user (attribution-writing tools), same
        # scope and seed — so the session keeps its own recipe.
        self.delta = delta
        self.acting_user_id = acting_user_id
        self.org_id = org_id
        self.org_ids = org_ids
        self.seed_db = seed_db
        # A session belongs to one caller at a time; a second call while one is being served is
        # SESSION_BUSY, which is the retryable kind of failure.
        self.lock = threading.Lock()


class SessionRegistry:
    """Sessions by id. Ids are opaque to the platform — any stable string."""

    def __init__(self) -> None:
        self._sessions: Dict[str, _Session] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def create(self, spec: Any) -> str:
        if not isinstance(spec, dict):
            raise WireFailure(WireErrorCode.INVALID_PARAMS, "spec must be an object",
                              kind="invalid_params", retryable=False)
        kind = spec.get("kind")
        if kind == "task":
            task = _state.decode_task(spec.get("task"))
            clock = spec.get("clock") or task.current_time or DEFAULT_NOW
            recipe: Dict[str, Any] = {
                "delta": task.initial_state_delta, "acting_user_id": task.acting_user_id,
                "org_id": task.org_id, "org_ids": task.org_ids, "seed_db": task.seed_db,
            }
            env = _state.build_environment(
                db_delta=task.initial_state_delta, acting_user_id=task.acting_user_id,
                org_id=task.org_id, org_ids=task.org_ids, seed_db=task.seed_db, clock=clock,
            )
        elif kind == "world":
            cfg = spec.get("gymConfig") or {}
            if not isinstance(cfg, dict):
                raise WireFailure(WireErrorCode.INVALID_PARAMS, "gymConfig must be an object",
                                  kind="invalid_params", retryable=False)
            # The gym has two gymConfig dialects: the ROLLOUT one (userModel/judgeModel/maxSteps…)
            # and this world one (seed_db/org_ids/acting_user_id…). Passing the former here would
            # silently build an unscoped default world; refuse loudly instead.
            rollout_keys = {"userModel", "judgeModel", "maxSteps", "freeTextMatch", "targetModel", "kRuns"}
            if rollout_keys & set(cfg):
                raise WireFailure(
                    WireErrorCode.INVALID_PARAMS,
                    "gymConfig looks like a rollout config (%s); a world spec takes "
                    "seed_db/db, org_ids, acting_user_id, initial_state_delta, current_time"
                    % ", ".join(sorted(rollout_keys & set(cfg))),
                    kind="invalid_params", retryable=False)
            clock = spec.get("clock") or cfg.get("current_time") or DEFAULT_NOW
            recipe = {
                "delta": cfg.get("initial_state_delta") or cfg.get("delta"),
                "acting_user_id": cfg.get("acting_user_id"),
                "org_id": cfg.get("org_id"), "org_ids": cfg.get("org_ids"),
                "seed_db": cfg.get("seed_db") or cfg.get("db"),
            }
            env = _state.build_environment(db_delta=recipe["delta"], acting_user_id=recipe["acting_user_id"],
                                           org_id=recipe["org_id"], org_ids=recipe["org_ids"],
                                           seed_db=recipe["seed_db"], clock=clock)
        else:
            raise WireFailure(WireErrorCode.INVALID_PARAMS, 'spec.kind must be "task" or "world"',
                              kind="invalid_params", retryable=False)

        with self._lock:
            self._counter += 1
            sid = f"s{self._counter}"
            self._sessions[sid] = _Session(env, clock, **recipe)
        return sid

    def session(self, session_id: Any) -> _Session:
        """The live session, for the custom operations that read or grade its world."""
        _require_id(session_id)
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise _not_found(session_id)
        return session

    def close(self, session_id: Any) -> None:
        _require_id(session_id)
        with self._lock:
            if self._sessions.pop(session_id, None) is None:
                raise _not_found(session_id)

    def call_tool(self, session_id: Any, name: Any, args: Any) -> Dict[str, Any]:
        _require_id(session_id)
        if not isinstance(name, str) or not name:
            raise WireFailure(WireErrorCode.INVALID_PARAMS, "name must be a non-empty string",
                              kind="invalid_params", retryable=False)
        if args is not None and not isinstance(args, dict):
            raise WireFailure(WireErrorCode.INVALID_PARAMS, "args must be an object",
                              kind="invalid_params", retryable=False)
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise _not_found(session_id)
        if not session.lock.acquire(blocking=False):
            raise WireFailure(WireErrorCode.SESSION_BUSY,
                              f"session {session_id!r} is already serving another call",
                              kind="session_busy", retryable=True)
        try:
            set_now(session.clock)
            try:
                result = session.env.make_tool_call(name, **(args or {}))
                return {"success": True, "result": jsonable(result)}
            except Exception as e:  # noqa: BLE001 - a tool refusing is data, not a fault
                return {"success": False, "error": f"{type(e).__name__}: {e}"}
            finally:
                reset_now()
        finally:
            session.lock.release()


def _require_id(session_id: Any) -> None:
    """A malformed id is the caller's error, not a lookup miss — and an unhashable one must not
    surface as an INTERNAL_ERROR out of a dict probe."""
    if not isinstance(session_id, str):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "sessionId must be a string",
                          kind="invalid_params", retryable=False)


def _not_found(session_id: Any) -> WireFailure:
    return WireFailure(WireErrorCode.SESSION_NOT_FOUND,
                       f"no session {session_id!r}, or it has been closed",
                       kind="session_not_found", retryable=False)
