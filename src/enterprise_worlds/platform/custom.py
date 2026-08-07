"""The gym's ``custom`` operations — what a MINER needs and a platform does not.

Six operations, agreed between this gym and its miner (the synthesis half of mining: introspecting
the world before any task exists, and grading a gold replay against a live session). The platform
never calls ``custom`` — the server enforces that with ``4002`` before this module is reached.

Every implementation here is a PORT of the corresponding ``Bridge`` method in the platform repo's
``task-generation/bridge/bridge.py`` — the miner's behaviour is defined by what that file does
today, so these are ported, not re-derived. The differential test in
``tests/test_custom_bridge_differential.py`` holds the two to the same answers whenever the
platform repo is checked out beside this one.

``dbHashEval`` carries the three load-bearing properties the bridge's docstring explains at length:

1. **Structure only** — free-text fields are stripped from BOTH worlds before hashing, so an agent
   that paraphrases prose still matches; free-text fidelity is the miner's judge's job.
2. **The gold world binds the session's ``acting_user_id``** — the gym's own ``_build_gold_env``
   forwards only the delta, and without the acting user, attribution-writing tools
   (``opened_by``/``requested_by``/notification sender) diverge on a faithful replay and
   ``create_problem`` fails outright.
3. **Same ``org_ids`` and ``seed_db`` as the session** — a gold world built at a different scope is
   not comparable to the one being graded.

It also runs under the session's frozen clock: timestamps are structural, so a gold replay stamped
at a different "now" than the live session's tool calls would never match.
"""
from __future__ import annotations

import typing
from functools import partial
from typing import Any, Callable, Dict, List, Optional

from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.domains.itsm.data_model import ItsmDB
from enterprise_worlds.domains.itsm.environment import get_environment
from enterprise_worlds.evaluator.evaluator_env import _build_gold_env
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.rollout import _strip_free_text
from enterprise_worlds.platform.sessions import SessionRegistry, _Session
from enterprise_worlds.platform.wire import WireErrorCode
from enterprise_worlds.utils.clock import reset_now, set_now
from enterprise_worlds.utils.hash_utils import get_dict_hash

#: The op names served, for 4001's discovery payload. Keys are what the miner sends.
OPS = ("toolSchemas", "toolOutputSchemas", "dbFieldSchema", "snapshot", "dbHash", "dbHashEval")

#: The ops that describe the gym itself rather than a world: served with `sessionId` absent or
#: ignored — opening a session to read a static schema is pure cost.
_SESSIONLESS = frozenset({"toolSchemas", "toolOutputSchemas", "dbFieldSchema"})


def dispatch(sessions: SessionRegistry, params: dict) -> Any:
    """Route one ``custom`` call: ``{sessionId?, op, args?}`` → the op's own result."""
    op = params.get("op")
    handler = _HANDLERS.get(op)
    if handler is None:
        raise WireFailure(WireErrorCode.UNKNOWN_CUSTOM_OP,
                          f"no custom operation {op!r}",
                          kind="unknown_custom_op", retryable=False,
                          details={"ops": list(OPS)})
    args = params.get("args")
    if args is not None and not isinstance(args, dict):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "args must be an object",
                          kind="invalid_params", retryable=False)
    args = args or {}
    if op in _SESSIONLESS:
        return handler(args)
    session = sessions.session(params.get("sessionId"))
    if not session.lock.acquire(blocking=False):
        raise WireFailure(WireErrorCode.SESSION_BUSY,
                          f"session {params.get('sessionId')!r} is already serving another call",
                          kind="session_busy", retryable=True)
    try:
        return handler(session, args)
    finally:
        session.lock.release()


# ── the gym itself (no session) ───────────────────────────────────────────────

def _tool_schemas(args: dict) -> List[dict]:
    """OpenAI-format function schemas for all tools (session-independent)."""
    return get_environment().get_tool_schemas()


def _tool_output_schemas(args: dict) -> List[dict]:
    """Per-tool output JSON schemas: ``[{"name", "output_schema"}]``; null = no declared shape."""
    return get_environment().get_tool_output_schemas()


def _db_field_schema(args: dict) -> Dict[str, List[str]]:
    """Map each ItsmDB collection name to its record's field names."""
    out: Dict[str, List[str]] = {}
    for name, field in ItsmDB.model_fields.items():
        type_args = typing.get_args(field.annotation)  # Dict[str, RecordModel] -> (str, RecordModel)
        rec = type_args[-1] if type_args else None
        out[name] = list(rec.model_fields.keys()) if rec is not None and hasattr(rec, "model_fields") else []
    return out


# ── a session's world ─────────────────────────────────────────────────────────

def _snapshot(session: _Session, args: dict) -> Dict[str, dict]:
    """``{collection: {record_id: record}}`` for the requested collections (all if omitted)."""
    collections = args.get("collections")
    if collections is not None and not isinstance(collections, list):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "collections must be a list of names",
                          kind="invalid_params", retryable=False)
    dump = session.env.tools.db.model_dump()
    if collections is None:
        return dump
    return {c: dump.get(c, {}) for c in collections}


def _db_hash(session: _Session, args: dict) -> str:
    """Hash of the session's live database (changes iff a tool mutated it)."""
    return session.env.get_db_hash()


def _db_hash_eval(session: _Session, args: dict) -> Dict[str, Any]:
    """Structure-only compare of the live session DB against a gold replay (see module docstring)."""
    gold_actions = args.get("goldActions", args.get("gold_actions"))
    if not isinstance(gold_actions, list):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "goldActions must be a list of {name, arguments}",
                          kind="invalid_params", retryable=False)
    env_ctor = partial(
        get_environment, acting_user_id=session.acting_user_id,
        org_id=session.org_id, org_ids=session.org_ids, seed_db=session.seed_db,
    )
    # The session's clock: timestamps are structural, and the live rows were stamped under it.
    set_now(session.clock)
    try:
        gold_env = _build_gold_env(env_ctor, _gold_task(session, gold_actions))
    finally:
        reset_now()
    gold_hash = get_dict_hash(_strip_free_text(gold_env.tools.db.model_dump()))
    pred_hash = get_dict_hash(_strip_free_text(session.env.tools.db.model_dump()))
    match = gold_hash == pred_hash
    return {"db_match": match, "reward": 1.0 if match else 0.0}


def _gold_task(session: _Session, gold_actions: list) -> Task:
    """The Task whose gold-action replay defines the expected DB.

    A placeholder persona — only the structure matters for the replay; the acting user is threaded
    into ``known_info`` (and bound in the env constructor) so attribution-writing tools replay
    faithfully. Ported from ``Bridge._gold_task``.
    """
    known_info = {"user_id": session.acting_user_id} if session.acting_user_id else {}
    return Task.model_validate({
        "id": "custom-db-hash-eval",
        "scenario": {
            "persona": {
                "identity": {
                    "user_id": session.acting_user_id or "bridge",
                    "first_name": "bridge",
                    "last_name": "eval",
                    "email": "bridge@eval",
                    "role": "agent",
                },
                "personality": "bridge",
                "role_description": "",
                "known_info": known_info,
            },
            "task_description": "custom db-hash eval",
        },
        "evaluation_criteria": {"actions": gold_actions, "nl_assertions": []},
        "initial_state_delta": session.delta,
    })


_HANDLERS: Dict[str, Callable] = {
    "toolSchemas": _tool_schemas,
    "toolOutputSchemas": _tool_output_schemas,
    "dbFieldSchema": _db_field_schema,
    "snapshot": _snapshot,
    "dbHash": _db_hash,
    "dbHashEval": _db_hash_eval,
}
