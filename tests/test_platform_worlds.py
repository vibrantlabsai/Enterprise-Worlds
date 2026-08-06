"""``gym.materializeState`` and ``session.*`` — worlds built and held over the protocol.

The materialize tests pin the two rules that matter: foreign-key closure (an out-of-scope record
referenced from in-scope data is present; an unreferenced one is absent) and the load → delta →
slice ORDER — pinned by a delta that creates an out-of-scope, unreferenced record, which only the
correct order removes. A delta that merely edits in-scope rows would pass under either order and
prove nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from enterprise_worlds.domains.itsm import data_model as itsm_data
from enterprise_worlds.domains.itsm.environment import ITSM_DB_PATH, get_tasks
from enterprise_worlds.platform import wire
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.sessions import SessionRegistry
from enterprise_worlds.platform.state import materialize_state

from test_platform_server import INITIALIZE, drive, req


def _msp_task():
    """A real cross-org task from the shipped suite — the case FK closure exists for."""
    return next(
        t for t in get_tasks()
        if t.org_ids and len(t.org_ids) >= 2 and (t.seed_db or "msp_db.json") == "msp_db.json"
    )


def _raw_seed() -> dict:
    return json.loads(ITSM_DB_PATH.read_text(encoding="utf-8"))


# ── materializeState ──────────────────────────────────────────────────────────

def test_scoped_world_closes_over_foreign_keys():
    task = _msp_task()
    scope = set(task.org_ids)
    state = materialize_state(task.model_dump())["state"]
    raw = _raw_seed()

    kept_out_of_scope = [
        (coll, rid) for coll, recs in state.items() for rid, rec in recs.items()
        if isinstance(rec, dict) and rec.get("org_id") and rec["org_id"] not in scope
    ]
    dropped_out_of_scope = [
        (coll, rid) for coll, recs in raw.items() for rid, rec in recs.items()
        if isinstance(rec, dict) and rec.get("org_id") and rec["org_id"] not in scope
        and rid not in state.get(coll, {})
    ]
    # Referenced cross-org records are pulled in; unreferenced ones are gone. Both sets must be
    # non-empty or the seed stopped exercising closure and the test is vacuous.
    assert kept_out_of_scope, "closure kept nothing out-of-scope — did the slice degrade to an org filter?"
    assert dropped_out_of_scope, "nothing was sliced away — is the scope the whole world?"


def test_delta_applies_before_the_slice():
    task = _msp_task()
    raw = _raw_seed()
    scope = set(task.org_ids)

    ghost_org = dict(raw["organization"]["ORG_012"], org_id="ORG_998", name="Ghost Org")
    in_scope_uid = next(rid for rid, r in raw["users"].items() if r.get("org_id") in scope)
    ghost_user = dict(raw["users"][in_scope_uid], user_id="USER_998",
                      user_name="ghost.user", email="ghost@example.com")

    payload = task.model_dump()
    payload["initial_state_delta"] = {
        "organization": {"ORG_998": {"create": ghost_org}},
        "users": {"USER_998": {"create": ghost_user}},
    }
    state = materialize_state(payload)["state"]

    # In-scope created record survives (the delta ran at all)…
    assert "USER_998" in state["users"]
    # …and the out-of-scope, unreferenced one was sliced away — which only happens when the delta
    # ran BEFORE the slice. Under slice-then-delta it would still be present.
    assert "ORG_998" not in state["organization"]


def test_missing_fk_spec_on_a_scoped_task_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(itsm_data, "_FK_SPEC_PATH", tmp_path / "fk_spec.json")
    with pytest.raises(FileNotFoundError, match="fk_spec"):
        materialize_state(_msp_task().model_dump())


def test_missing_fk_spec_is_fine_for_an_unscoped_task(monkeypatch, tmp_path):
    monkeypatch.setattr(itsm_data, "_FK_SPEC_PATH", tmp_path / "fk_spec.json")
    payload = _msp_task().model_dump()
    payload["org_ids"] = None
    payload["org_id"] = None
    state = materialize_state(payload)["state"]
    # Unscoped means the whole world: no slice, no closure needed.
    assert set(state["organization"]) == set(_raw_seed()["organization"])


def test_provenance_names_the_seed_scope_and_clock():
    task = _msp_task()
    provenance = materialize_state(task.model_dump())["provenance"]
    assert provenance["seed_db"] == "msp_db.json"
    assert provenance["org_ids"] == sorted(task.org_ids)
    assert provenance["clock"] == (task.current_time or "2024-06-01 00:00:00")


def test_an_invalid_task_is_invalid_params_not_an_internal_error():
    with pytest.raises(WireFailure) as exc:
        materialize_state({"id": "x"})  # no scenario
    assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS


def test_materialize_over_the_wire():
    task = _msp_task()
    replies = drive(INITIALIZE, req(2, "gym.materializeState", {"task": task.model_dump()}))
    snapshot = replies[1]["result"]
    assert "users" in snapshot["state"]
    assert snapshot["provenance"]["seed_db"] == "msp_db.json"


# ── sessions ──────────────────────────────────────────────────────────────────

WORLD_SPEC = {"kind": "world", "gymConfig": {"db": "msp_db.json"}, "clock": "2025-01-02T03:04:05"}
LOCATION_ARGS = {"name": "Clock Test Site", "city": "Nowhere", "country": "Nowhere"}


def test_two_sessions_are_independent_worlds():
    reg = SessionRegistry()
    s1, s2 = reg.create(dict(WORLD_SPEC)), reg.create(dict(WORLD_SPEC))
    assert s1 != s2
    r1 = reg.call_tool(s1, "add_location", dict(LOCATION_ARGS))
    r2 = reg.call_tool(s2, "add_location", dict(LOCATION_ARGS))
    # Same fresh world → the deterministic id generator mints the SAME id in both. A shared world
    # would mint a successor id (or refuse the duplicate name) on the second call.
    assert r1["success"] and r2["success"]
    assert r1["result"]["location_id"] == r2["result"]["location_id"]


def test_a_failing_tool_is_a_result_and_the_session_stays_usable():
    reg = SessionRegistry()
    sid = reg.create(dict(WORLD_SPEC))
    refused = reg.call_tool(sid, "get_user", {"user_id": "USR_NOPE"})
    assert refused["success"] is False and "not found" in refused["error"].lower()
    unknown = reg.call_tool(sid, "no_such_tool", {})
    assert unknown["success"] is False
    ok = reg.call_tool(sid, "add_location", dict(LOCATION_ARGS))
    assert ok["success"] is True


def test_the_session_clock_drives_tool_timestamps():
    reg = SessionRegistry()
    sid = reg.create(dict(WORLD_SPEC))
    result = reg.call_tool(sid, "add_location", dict(LOCATION_ARGS))["result"]
    assert result["created_on"] == "2025-01-02 03:04:05"  # the clock, in seed format


def test_close_releases_and_afterwards_is_session_not_found():
    reg = SessionRegistry()
    sid = reg.create(dict(WORLD_SPEC))
    reg.close(sid)
    for attempt in (lambda: reg.call_tool(sid, "get_user", {"user_id": "USER_001"}),
                    lambda: reg.close(sid)):
        with pytest.raises(WireFailure) as exc:
            attempt()
        assert exc.value.code == wire.WireErrorCode.SESSION_NOT_FOUND


def test_a_busy_session_answers_session_busy_retryable():
    reg = SessionRegistry()
    sid = reg.create(dict(WORLD_SPEC))
    session = reg._sessions[sid]
    assert session.lock.acquire(blocking=False)
    try:
        with pytest.raises(WireFailure) as exc:
            reg.call_tool(sid, "get_user", {"user_id": "USER_001"})
        assert exc.value.code == wire.WireErrorCode.SESSION_BUSY
        assert exc.value.retryable is True
    finally:
        session.lock.release()


def test_a_task_session_authenticates_the_personas_caller():
    task = _msp_task()
    reg = SessionRegistry()
    sid = reg.create({"kind": "task", "task": task.model_dump()})
    me = reg.call_tool(sid, "get_user", {"user_id": task.scenario.persona.identity.user_id})
    assert me["success"] is True
    assert me["result"]["user_id"] == task.scenario.persona.identity.user_id


def test_a_bad_spec_kind_is_invalid_params():
    reg = SessionRegistry()
    with pytest.raises(WireFailure) as exc:
        reg.create({"kind": "banana"})
    assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS


def test_the_session_lifecycle_over_the_wire():
    replies = drive(
        INITIALIZE,
        req(2, "session.create", {"spec": WORLD_SPEC}),
        req(3, "session.callTool", {"sessionId": "s1", "name": "add_location", "args": LOCATION_ARGS}),
        req(4, "session.close", {"sessionId": "s1"}),
        req(5, "session.callTool", {"sessionId": "s1", "name": "get_user", "args": {"user_id": "USER_001"}}),
    )
    assert replies[1]["result"] == {"sessionId": "s1"}
    assert replies[2]["result"]["success"] is True
    assert replies[3]["result"] is None
    assert replies[4]["error"]["code"] == wire.WireErrorCode.SESSION_NOT_FOUND
