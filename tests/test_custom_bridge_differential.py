"""Differential: the six custom ops must answer exactly what ``bridge.py`` answers.

The miner's behaviour is DEFINED by what the bridge does today, so the strongest acceptance is to
run both implementations on the same inputs and diff. This needs the platform repo checked out
beside this one; when it is not (CI, another machine), the whole module skips — same convention as
the oracle tests.

The bridge is imported, not subprocessed: its ``Bridge`` class is plain Python over ``eops_gym``,
which the alias shim serves from this very tree — both sides therefore run the same gym code, and
what this test actually pins is the PORT: argument threading, free-text stripping, acting-user
binding, and result shapes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BRIDGE_DIR = Path(__file__).resolve().parents[2] / "synthetic-enterprise-ops" / "task-generation" / "bridge"

pytestmark = pytest.mark.skipif(
    not (_BRIDGE_DIR / "bridge.py").exists(),
    reason="synthetic-enterprise-ops checkout not found beside this repo",
)

if (_BRIDGE_DIR / "bridge.py").exists():
    sys.path.insert(0, str(_BRIDGE_DIR))
    import bridge as bridge_mod  # noqa: E402 - the reference implementation under test

from enterprise_worlds.platform import custom  # noqa: E402
from enterprise_worlds.platform.sessions import SessionRegistry  # noqa: E402

SCOPE = {"acting_user_id": "USER_039", "org_ids": ["ORG_001", "ORG_007"], "seed_db": "msp_db.json"}
GROUP = {"name": "Network Ops", "type": "IT Support", "manager_id": "USER_031",
         "description": "Handles all networking tickets end to end"}
DELTA = {"user_group": {"GROUP_900": {"create": {
    "group_id": "GROUP_900", "name": "Seeded Group", "type": "IT Support",
    "manager_id": "USER_031", "active": True, "email": None,
    "description": "Created by the session delta", "org_id": "ORG_001",
    "created_on": "2024-01-01 00:00:00", "updated_on": "2024-01-01 00:00:00",
}}}}


def _pair():
    """One bridge session and one gym session, opened from the identical recipe.

    Neither names a clock: the bridge runs under the gym's ambient DEFAULT_NOW and the gym session
    defaults to the same, so stamped timestamps agree — which the hashes then prove.
    """
    bridge = bridge_mod.Bridge()
    b_sid = bridge.open_session(delta=DELTA, acting_user_id=SCOPE["acting_user_id"],
                                org_ids=SCOPE["org_ids"], seed_db=SCOPE["seed_db"])
    reg = SessionRegistry()
    g_sid = reg.create({"kind": "world", "gymConfig": {
        "initial_state_delta": DELTA, "acting_user_id": SCOPE["acting_user_id"],
        "org_ids": SCOPE["org_ids"], "db": SCOPE["seed_db"],
    }})
    return bridge, b_sid, reg, g_sid


def _custom(reg, sid, op, args=None):
    params = {"op": op}
    if sid is not None:
        params["sessionId"] = sid
    if args is not None:
        params["args"] = args
    return custom.dispatch(reg, params)


def test_the_three_schema_ops_answer_identically():
    bridge = bridge_mod.Bridge()
    reg = SessionRegistry()
    assert _custom(reg, None, "toolSchemas") == bridge.tool_schemas()
    assert _custom(reg, None, "toolOutputSchemas") == bridge.tool_output_schemas()
    assert _custom(reg, None, "dbFieldSchema") == bridge.db_field_schema()


def test_snapshot_and_hash_agree_on_a_freshly_opened_world():
    bridge, b_sid, reg, g_sid = _pair()
    assert _custom(reg, g_sid, "snapshot") == bridge.snapshot(b_sid)
    assert _custom(reg, g_sid, "snapshot", {"collections": ["incident", "users"]}) == \
        bridge.snapshot(b_sid, collections=["incident", "users"])
    assert _custom(reg, g_sid, "dbHash") == bridge.db_hash(b_sid)


def test_the_same_tool_sequence_yields_the_same_world_and_hash():
    bridge, b_sid, reg, g_sid = _pair()
    b_result = bridge.call_tool(b_sid, "add_new_user_group", dict(GROUP))
    g_result = reg.call_tool(g_sid, "add_new_user_group", dict(GROUP))
    assert b_result == g_result
    assert b_result["success"] is True
    assert _custom(reg, g_sid, "dbHash") == bridge.db_hash(b_sid)
    assert _custom(reg, g_sid, "snapshot") == bridge.snapshot(b_sid)
    # And a REFUSED tool answers identically too — failure is data on both sides.
    assert reg.call_tool(g_sid, "add_new_user_group", dict(GROUP)) == \
        bridge.call_tool(b_sid, "add_new_user_group", dict(GROUP))


def test_db_hash_eval_agrees_on_match_paraphrase_and_divergence():
    bridge, b_sid, reg, g_sid = _pair()
    bridge.call_tool(b_sid, "add_new_user_group", dict(GROUP))
    reg.call_tool(g_sid, "add_new_user_group", dict(GROUP))

    cases = [
        dict(GROUP),                                    # faithful → both match
        dict(GROUP, description="Networking team"),     # paraphrased free text → both still match
        dict(GROUP, type="Service Desk"),               # structural flip → both refuse
    ]
    for gold_args in cases:
        gold = [{"name": "add_new_user_group", "arguments": gold_args}]
        assert _custom(reg, g_sid, "dbHashEval", {"goldActions": gold}) == \
            bridge.db_hash_eval(b_sid, gold), gold_args
    assert _custom(reg, g_sid, "dbHashEval", {"goldActions": [
        {"name": "add_new_user_group", "arguments": dict(GROUP, type="Service Desk")},
    ]})["db_match"] is False  # the divergent case really is the divergent one
