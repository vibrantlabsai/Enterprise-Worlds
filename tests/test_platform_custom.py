"""The six ``custom`` operations — the miner's synthesis surface.

The three schema ops describe the gym and need no session; the three world ops read or grade a live
session. ``dbHashEval``'s three load-bearing properties each get their own test: structure-only
hashing (paraphrase survives), the session's acting user bound into the gold replay, and the
session's delta/scope/seed threaded through — a gold world built from a different recipe is not
comparable, and would fail the faithful-replay test here.
"""
from __future__ import annotations

import pytest

from enterprise_worlds.platform import custom, wire
from enterprise_worlds.platform.custom import OPS
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.sessions import SessionRegistry

from test_platform_server import drive, req

MINER_INIT = req(1, "gym.initialize", {"protocolVersion": 1, "role": "miner"})
SCOPE = {"db": "msp_db.json", "acting_user_id": "USER_039", "org_ids": ["ORG_001", "ORG_007"]}


def _world(gym_config: dict | None = None) -> tuple[SessionRegistry, str]:
    reg = SessionRegistry()
    sid = reg.create({"kind": "world", "gymConfig": dict(SCOPE, **(gym_config or {}))})
    return reg, sid


def _call(reg, sid, op, args=None):
    params = {"op": op}
    if sid is not None:
        params["sessionId"] = sid
    if args is not None:
        params["args"] = args
    return custom.dispatch(reg, params)


def _group_args(reg, sid) -> dict:
    """Valid ``add_new_user_group`` arguments, with a manager found in the sliced world."""
    users = _call(reg, sid, "snapshot", {"collections": ["users"]})["users"]
    manager = next(u["user_id"] for u in users.values() if u.get("role") == "manager")
    return {"name": "Network Ops", "type": "IT Support", "manager_id": manager,
            "description": "Handles all networking tickets end to end"}


# ── the gym itself: no session required ───────────────────────────────────────

def test_the_schema_ops_need_no_session():
    reg = SessionRegistry()  # empty on purpose
    schemas = _call(reg, None, "toolSchemas")
    assert len(schemas) == 93
    assert all(s["type"] == "function" and {"name", "description", "parameters"} <= set(s["function"])
               for s in schemas)

    outputs = _call(reg, None, "toolOutputSchemas")
    assert len(outputs) == 93
    assert all(set(o) == {"name", "output_schema"} for o in outputs)
    assert any(o["output_schema"] is None for o in outputs)  # null is meaningful: no declared shape

    fields = _call(reg, None, "dbFieldSchema")
    assert "incident" in fields and "incident_id" in fields["incident"]
    assert "users" in fields and "user_id" in fields["users"]


# ── a session's world ─────────────────────────────────────────────────────────

def test_snapshot_filters_to_the_requested_collections():
    reg, sid = _world()
    full = _call(reg, sid, "snapshot")
    assert "incident" in full and "users" in full
    filtered = _call(reg, sid, "snapshot", {"collections": ["incident", "no_such_collection"]})
    assert set(filtered) == {"incident", "no_such_collection"}
    assert filtered["incident"] == full["incident"]
    assert filtered["no_such_collection"] == {}


def test_db_hash_changes_iff_a_tool_mutated_the_world():
    reg, sid = _world()
    before = _call(reg, sid, "dbHash")
    assert _call(reg, sid, "dbHash") == before  # reading is not mutating
    assert reg.call_tool(sid, "add_new_user_group", _group_args(reg, sid))["success"]
    assert _call(reg, sid, "dbHash") != before


def test_db_hash_eval_is_structure_only_and_binds_the_sessions_recipe():
    # The session carries the miner's exact synthesis combo: a delta, an acting user and an org
    # scope. The gold replay must be built from the SAME recipe or the faithful case fails.
    delta = {"user_group": {"GROUP_900": {"create": {
        "group_id": "GROUP_900", "name": "Seeded Group", "type": "IT Support",
        "manager_id": "USER_031", "active": True, "email": None,
        "description": "Created by the session delta", "org_id": "ORG_001",
        "created_on": "2024-01-01 00:00:00", "updated_on": "2024-01-01 00:00:00",
    }}}}
    reg, sid = _world({"initial_state_delta": delta})
    assert "GROUP_900" in _call(reg, sid, "snapshot", {"collections": ["user_group"]})["user_group"]

    base = _group_args(reg, sid)
    assert reg.call_tool(sid, "add_new_user_group", dict(base))["success"]

    def ev(gold_args):
        return _call(reg, sid, "dbHashEval",
                     {"goldActions": [{"name": "add_new_user_group", "arguments": gold_args}]})

    # Faithful replay matches (recipe threaded), a paraphrased free-text field still matches
    # (structure only), a structural change does not.
    assert ev(dict(base)) == {"db_match": True, "reward": 1.0}
    assert ev(dict(base, description="Networking team")) == {"db_match": True, "reward": 1.0}
    assert ev(dict(base, type="Service Desk")) == {"db_match": False, "reward": 0.0}


def test_db_hash_eval_validates_its_arguments():
    reg, sid = _world()
    bad = [{}, {"goldActions": "x"}, {"goldActions": [1, 2]},
           {"goldActions": [{"name": 7}]}, {"goldActions": [{"name": "t", "arguments": []}]}]
    for args in bad:
        with pytest.raises(WireFailure) as exc:
            _call(reg, sid, "dbHashEval", args)
        assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS, args
        assert "pydantic" not in str(exc.value)  # caller error, refused concisely
    # The snake_case spelling the bridge-era miner used is accepted too, and an explicit null under
    # the camel key must not shadow it.
    assert _call(reg, sid, "dbHashEval", {"gold_actions": []}) == {"db_match": True, "reward": 1.0}
    assert _call(reg, sid, "dbHashEval", {"goldActions": None, "gold_actions": []}) == \
        {"db_match": True, "reward": 1.0}


def test_the_bridge_dialect_passes_through_verbatim():
    # world.ts speaks snake_case today; a pass-through adapter must work COMPLETELY, not half-work.
    reg, sid = _world()
    base = _group_args(reg, sid)
    assert reg.call_tool(sid, "add_new_user_group", dict(base))["success"]
    assert custom.dispatch(reg, {"op": "db_field_schema"}) == custom.dispatch(reg, {"op": "dbFieldSchema"})
    assert custom.dispatch(reg, {"op": "tool_schemas"}) == custom.dispatch(reg, {"op": "toolSchemas"})
    snake = custom.dispatch(reg, {"session_id": sid, "op": "db_hash_eval", "args": {
        "gold_actions": [{"name": "add_new_user_group", "arguments": dict(base)}]}})
    assert snake == {"db_match": True, "reward": 1.0}
    assert custom.dispatch(reg, {"session_id": sid, "op": "db_hash"}) == \
        custom.dispatch(reg, {"sessionId": sid, "op": "dbHash"})


def test_malformed_op_and_session_ids_are_caller_errors_not_gym_faults():
    reg, sid = _world()
    # op missing / non-string: required and nameless — INVALID_PARAMS, with discovery still riding.
    for params in ({}, {"op": None}, {"op": {}}, {"op": ["x"]}, {"op": True}):
        with pytest.raises(WireFailure) as exc:
            custom.dispatch(reg, params)
        assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS, params
        assert exc.value.details == {"ops": list(OPS)}
    # Unhashable/malformed session ids must not surface as INTERNAL_ERROR out of a dict probe.
    for bad_sid in ({}, [], 7, None):
        with pytest.raises(WireFailure) as exc:
            custom.dispatch(reg, {"sessionId": bad_sid, "op": "dbHash"})
        assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS, bad_sid
    with pytest.raises(WireFailure) as exc:
        _call(reg, sid, "snapshot", {"collections": [{}]})
    assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS
    # The same boundary holds for the protocol session methods.
    with pytest.raises(WireFailure) as exc:
        reg.call_tool({}, "get_user", {})
    assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS


def test_schema_ops_ignore_a_bogus_session_id():
    # Handoff §1: "absent or ignored" — the ignored half.
    reg = SessionRegistry()
    for op in ("toolSchemas", "toolOutputSchemas", "dbFieldSchema"):
        assert custom.dispatch(reg, {"sessionId": "TOTALLY_BOGUS", "op": op}) == \
            custom.dispatch(reg, {"op": op}), op


def test_db_hash_eval_is_clock_coherent_on_a_clocked_session():
    # Deliberate divergence from the bridge (whose gold replay stamps under the ambient clock at
    # eval time): the gym's verdict must be self-consistent whatever the session's clock is.
    reg = SessionRegistry()
    sid = reg.create({"kind": "world", "gymConfig": dict(SCOPE), "clock": "2025-06-01T00:00:00"})
    base = _group_args(reg, sid)
    assert reg.call_tool(sid, "add_new_user_group", dict(base))["success"]
    assert _call(reg, sid, "dbHashEval",
                 {"goldActions": [{"name": "add_new_user_group", "arguments": dict(base)}]}) == \
        {"db_match": True, "reward": 1.0}


def test_world_ops_on_an_unknown_session_are_2001():
    reg = SessionRegistry()
    for op in ("snapshot", "dbHash", "dbHashEval"):
        with pytest.raises(WireFailure) as exc:
            _call(reg, "s404", op, {"goldActions": []})
        assert exc.value.code == wire.WireErrorCode.SESSION_NOT_FOUND, op


def test_an_unknown_op_is_4001_with_the_known_names_attached():
    reg = SessionRegistry()
    with pytest.raises(WireFailure) as exc:
        custom.dispatch(reg, {"op": "frobnicate"})
    assert exc.value.code == wire.WireErrorCode.UNKNOWN_CUSTOM_OP
    assert exc.value.details == {"ops": list(OPS)}


# ── over the wire ─────────────────────────────────────────────────────────────

def test_the_miner_surface_over_the_wire():
    replies = drive(
        MINER_INIT,
        req(2, "custom", {"op": "dbFieldSchema"}),
        req(3, "session.create", {"spec": {"kind": "world", "gymConfig": dict(SCOPE)}}),
        req(4, "custom", {"sessionId": "s1", "op": "dbHash"}),
        req(5, "custom", {"sessionId": "s1", "op": "dbHashEval", "args": {"goldActions": []}}),
        req(6, "custom", {"op": "frobnicate"}),
    )
    assert "incident" in replies[1]["result"]
    assert isinstance(replies[3]["result"], str) and len(replies[3]["result"]) >= 32
    assert replies[4]["result"] == {"db_match": True, "reward": 1.0}
    assert replies[5]["error"]["code"] == wire.WireErrorCode.UNKNOWN_CUSTOM_OP
    assert replies[5]["error"]["data"]["details"]["ops"] == list(OPS)


def test_a_platform_connection_gets_4002_even_for_a_real_op():
    platform_init = req(1, "gym.initialize", {"protocolVersion": 1, "role": "platform"})
    replies = drive(platform_init, req(2, "custom", {"op": "toolSchemas"}))
    assert replies[1]["error"]["code"] == wire.WireErrorCode.CUSTOM_FORBIDDEN
