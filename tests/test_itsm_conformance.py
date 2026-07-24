"""Differential conformance: the ITSM port vs the live reference MCP (original ServiceNow server).

Each scenario runs an identical sequence of tool calls against BOTH our in-memory port and the
live reference, then asserts they agree. Docker-gated: the whole module is skipped when the
reference oracle is not reachable (see ``tests/itsm_oracle.py`` for connection env vars).

Two check modes:
  - ``outcome`` — the final call's success/error outcome must match (validation / edge paths).
  - ``state``   — the resulting DB state must be identical, volatile timestamps ignored
                  (happy paths; collection names already line up 1:1 between port and reference).

Scenarios marked ``xfail`` encode a CONFIRMED fidelity gap (see ``docs/itsm_fidelity_audit.md``)
that the port does not yet honour. They flip to ``xpass`` as the P1 fixes land, at which point the
marker is removed — so this file doubles as the executable spec / progress tracker for the fixes.

Run locally with the reference container up:
    uv run pytest tests/test_itsm_conformance.py -q -rxX
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # for itsm_oracle
from itsm_oracle import MCPOracle, diff_db, oracle_available  # noqa: E402

from enterprise_worlds.domains.itsm.data_model import ItsmDB  # noqa: E402
from enterprise_worlds.domains.itsm.tools import ItsmTools  # noqa: E402

SEED_JSON = Path(__file__).resolve().parents[1] / "data" / "itsm" / "db.json"
ACTING_USER = "USER_039"  # karen.watkins — an active ORG_001 admin in the 20-org seed
# The reference authenticates by the acting user's per-user ``static_token`` (the old shared
# ``admin_token_marcus_2024_secure`` does not exist in the 20-org seed; USER_001 is now inactive).
# This JWT is USER_039's ``static_token`` from the seed, so the port (``acting_user_id``) and the
# reference act as the SAME identity — preserving the original "ORG_001 admin" intent.
ACTING_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJVU0VSXzAzOSIsImlzcyI6Iml0c20ifQ."
    "Ab6_dflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_yz1"
)


@dataclass
class Scenario:
    """One differential check: the same ``calls`` run against port and reference."""

    id: str
    calls: list[tuple[str, dict]]
    mode: str = "outcome"  # "outcome" | "state"
    xfail: str | None = None  # confirmed-gap reference; None for scenarios that pass today
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- runners
def _run_port(calls: list[tuple[str, dict]]) -> tuple[list[bool], ItsmDB]:
    db = ItsmDB.model_validate(json.loads(SEED_JSON.read_text()))
    tools = ItsmTools(db, acting_user_id=ACTING_USER)
    outcomes: list[bool] = []
    for tool, args in calls:
        try:
            tools.use_tool(tool, **args)
            outcomes.append(True)
        except Exception:  # noqa: BLE001 - any raise is an error outcome
            outcomes.append(False)
    return outcomes, db


def _oracle_is_error(result: Any) -> bool:
    """The reference surfaces errors as a FastAPI/pydantic envelope; success is the record/list."""
    if isinstance(result, dict):
        if "detail" in result:  # pydantic 422 list or HTTPException dict
            return True
        if result.get("error") is True or "error_code" in result:
            return True
        if "error" in result and isinstance(result.get("error"), dict):  # JSON-RPC error
            return True
    return False


def _run_oracle(calls: list[tuple[str, dict]]) -> tuple[list[bool], MCPOracle]:
    o = MCPOracle("conformance", token=ACTING_TOKEN)
    outcomes: list[bool] = []
    for tool, args in calls:
        outcomes.append(not _oracle_is_error(o.call(tool, args)))
    return outcomes, o


def _normalize_member_ids(dump: dict) -> dict:
    """Neutralise a pure cross-export PK-label artifact in ``user_group_member``.

    The port seed (``data/itsm/db.json``) labels its membership rows ``MEMBER_xxx`` while the
    reference's canonical SQL seed labels the *identical* rows ``MEM_xxx``; every other column
    matches. That synthetic id is orthogonal to any tool call, so for state comparisons we re-key
    the rows by their natural ``(group_id, user_id)`` identity and drop the synthetic ``member_id``.
    This keeps full membership *content* parity in scope without tripping on the label difference.
    """
    coll = dump.get("user_group_member")
    if not coll:
        return dump
    rekeyed: dict[str, dict] = {}
    for row in coll.values():
        row = {k: v for k, v in row.items() if k != "member_id"}
        rekeyed[f"{row.get('group_id')}:{row.get('user_id')}"] = row
    return {**dump, "user_group_member": rekeyed}


# ---------------------------------------------------------------------------- scenarios
SCENARIOS: list[Scenario] = [
    # -- positive: should agree TODAY (guards the harness + the success path) ------------
    Scenario("pos_find_incident", [("find_incident_by_id", {"incident_id": "INC_001"})]),
    Scenario(
        "pos_update_new_to_resolved",  # the calibration case: there is NO status state machine
        # INC_012 is genuinely status='new' in the 20-org seed (ORG_001) — new->resolved must succeed
        [("update_incident", {"incident_id": "INC_012", "status": "resolved"})],
    ),
    Scenario(
        "pos_create_incident_valid",  # USER_005 is an active ORG_001 caller in the 20-org seed
        [("create_incident", {"caller_id": "USER_005", "short_description": "harness sanity"})],
    ),

    # -- enum_validation (§4.1): FIXED — port now rejects invalid/wrong-case enums (guards regression)
    Scenario("enum_incident_priority",
             [("update_incident", {"incident_id": "INC_001", "priority": "urgent"})], tags=["enum"]),
    Scenario("enum_change_status",
             [("update_change", {"change_id": "CHG_001", "status": "in_progress"})], tags=["enum"]),
    Scenario("enum_notification_type",  # status is system-managed and no longer a tool parameter
             [("update_notification", {"notification_id": "NOTIF_001", "type": "broadcast"})], tags=["enum"]),
    Scenario("enum_problem_status",
             [("update_problem", {"problem_id": "PRB_001", "status": "open"})], tags=["enum"]),
    Scenario("enum_ci_status",
             [("update_configuration_item", {"configuration_item_id": "CI_001", "status": "active"})],
             tags=["enum"]),
    Scenario("enum_user_role",
             [("update_user_details", {"user_id": "USER_003", "role": "superuser"})], tags=["enum"]),
    Scenario("enum_service_classification",
             [("update_service", {"service_id": "SVC_001", "service_classification": "infrastructure"})],
             tags=["enum"]),
    Scenario("enum_knowledge_state",
             [("update_knowledge_article", {"knowledge_id": "KB_001", "state": "active"})], tags=["enum"]),
    Scenario("enum_used_as",
             [("link_knowledge_to_incident",
               {"incident_id": "INC_002", "knowledge_id": "KB_001", "used_as": "primary"})],
             tags=["enum"]),

    # -- destructive-delete guards (§4.3): FIXED — no-filter delete now errors, leaves table intact
    Scenario("delete_slas_no_filter",
             [("delete_incident_slas", {})], tags=["delete"]),
    Scenario("remove_affected_ci_no_filter",
             [("remove_affected_ci_from_incident", {})], tags=["delete"]),

    # -- idempotency (§4.5): FIXED — a no-op update now errors instead of re-stamping updated_on ---
    Scenario("noop_update_notification",  # set a value, then repeat it — the repeat is a true no-op
             [("update_notification", {"notification_id": "NOTIF_001", "type": "reminder"}),
              ("update_notification", {"notification_id": "NOTIF_001", "type": "reminder"})],
             tags=["idempotency"]),
    Scenario("noop_update_incident_sla_only_id",
             [("update_incident_sla_details", {"incident_sla_id": "TSLA_001"})],
             tags=["idempotency"]),

    # -- required-field guards (§4.4): FIXED ------------------------------------------------------
    Scenario("required_empty_start_time",
             [("link_new_incident_sla",
               {"incident_id": "INC_003", "sla_def_id": "SLA_002", "start_time": ""})],
             tags=["required"]),
    Scenario("required_update_kb_only_id",
             [("update_knowledge_article", {"knowledge_id": "KB_001"})], tags=["required"]),

    # -- duplicate detection (§4.2): FIXED -------------------------------------------------------
    Scenario("dup_change_number",
             [("update_change", {"change_id": "CHG_002", "number": "CHG0000001"})], tags=["duplicate"]),

    # -- FK coverage (§4.7): FIXED — update_incident validates all referenced entities ------------
    Scenario("fk_update_incident_service",
             [("update_incident", {"incident_id": "INC_001", "service": "SVC_GHOST"})], tags=["fk"]),

    # -- cross-entity rule (§4.7): FIXED — incident_template offering must belong to service ------
    Scenario("xentity_template_offering_service",
             [("update_incident_template",
               {"incident_template_id": "TMPL_001",
                "change_request_values": {"service": "SVC_001", "service_offering": "SVCOFF_002"}})],
             tags=["fk"]),

    # -- added from the independent (Andrew) audit, live-confirmed -------------------------------
    # idempotency on update_incident / update_problem (a repeated identical update must error)
    Scenario("noop_update_incident",
             [("update_incident", {"incident_id": "INC_002", "worknotes": "conf-probe"}),
              ("update_incident", {"incident_id": "INC_002", "worknotes": "conf-probe"})],
             tags=["idempotency"]),
    Scenario("noop_update_problem",
             [("update_problem", {"problem_id": "PRB_001", "worknotes": "conf-probe"}),
              ("update_problem", {"problem_id": "PRB_001", "worknotes": "conf-probe"})],
             tags=["idempotency"]),
    # duplicate detection: knowledge (title+owner), group (email)
    Scenario("dup_knowledge_title_owner",
             [("create_knowledge_article", {"title": "ConfDup", "owner_id": "USER_002"}),
              ("create_knowledge_article", {"title": "ConfDup", "owner_id": "USER_002"})],
             tags=["duplicate"]),
    Scenario("dup_group_email",
             [("add_new_user_group",
               {"name": "ConfA", "type": "IT Support", "manager_id": "USER_002", "email": "cd@x.com"}),
              ("add_new_user_group",
               {"name": "ConfB", "type": "IT Support", "manager_id": "USER_002", "email": "cd@x.com"})],
             tags=["duplicate"]),
    # get_count_of_incident_priority_wise: optional priority_list (no-arg must succeed, count-all)
    Scenario("count_priority_no_args",
             [("get_count_of_incident_priority_wise", {})], tags=["schema"]),

    # -- contact-format validation (users router pydantic field validators) ----------------------
    Scenario("fmt_user_bad_phone",
             [("add_new_user", {"first_name": "P", "last_name": "R", "email": "okp@x.com",
                                "phone": "1", "role": "agent", "active": True})], tags=["format"]),
    Scenario("fmt_user_bad_email",
             [("add_new_user", {"first_name": "P", "last_name": "R", "email": "notanemail",
                                "phone": "+1-555-100-2000", "role": "agent", "active": True})],
             tags=["format"]),
    Scenario("fmt_update_user_bad_phone",
             [("update_user_details", {"user_id": "USER_039", "phone": "1"})], tags=["format"]),
    Scenario("fmt_group_bad_email",  # USER_004 is an ORG_001 manager in the seed
             [("add_new_user_group", {"name": "FmtGrpX", "type": "IT Support",
                                      "manager_id": "USER_004", "email": "bademail"})],
             tags=["format"]),

    # -- no-op guard: update_incident with only the id is NO_CHANGES_DETECTED ---------------------
    Scenario("noop_update_incident_id_only",
             [("update_incident", {"incident_id": "INC_001"})], tags=["idempotency"]),

    # -- empty-string CLEAR model (incident): '' clears a set nullable field (both accept) --------
    Scenario("empty_incident_clear_service",
             [("update_incident", {"incident_id": "INC_001", "service": ""})], tags=["empty"]),
    # NOT NULL field passed '' is rejected by the column constraint
    Scenario("empty_incident_notnull_reject",
             [("update_incident", {"incident_id": "INC_001", "short_description": ""})],
             tags=["empty"]),

    # -- empty-string DROP model (problem/change): a sole '' field => "No fields provided" --------
    Scenario("empty_problem_drop",
             [("update_problem", {"problem_id": "PRB_001", "worknotes": ""})], tags=["empty"]),
    Scenario("empty_change_drop",
             [("update_change", {"change_id": "CHG_001", "description": ""})], tags=["empty"]),

    # -- min_length=1 reject: '' rejected outright on these constrained fields --------------------
    Scenario("minlen_ci_serial",
             [("update_configuration_item", {"configuration_item_id": "CI_001", "serial_number": ""})],
             tags=["empty"]),
    Scenario("minlen_notification_subject",
             [("update_notification", {"notification_id": "NOTIF_001", "subject": ""})],
             tags=["empty"]),

    # -- create-path empty-string normalization: '' FK normalized to None (both accept) ----------
    Scenario("empty_create_problem_fk",
             [("create_problem", {"problem_statement": "harness", "status": "new", "impact": "medium",
                                  "urgency": "medium", "priority": "moderate", "service": ""})],
             tags=["empty"]),

    # -- state parity (happy path): a real-change update must produce identical DB state ---------
    Scenario("state_update_incident_fields",
             [("update_incident",
               {"incident_id": "INC_001", "category": "hardware", "impact": "high"})],
             mode="state"),
]


def _param(s: Scenario):
    marks = [pytest.mark.xfail(reason=s.xfail, strict=False)] if s.xfail else []
    return pytest.param(s, id=s.id, marks=marks)


@pytest.mark.skipif(not oracle_available(), reason="reference MCP oracle not reachable")
@pytest.mark.parametrize("s", [_param(s) for s in SCENARIOS])
def test_conformance(s: Scenario):
    port_out, port_db = _run_port(s.calls)
    orc_out, orc = _run_oracle(s.calls)

    if s.mode == "outcome":
        p, o = port_out[-1], orc_out[-1]
        assert p == o, (
            f"outcome mismatch: port={'ok' if p else 'error'} reference={'ok' if o else 'error'}"
        )
    else:  # state
        assert all(port_out) and all(orc_out), (
            f"setup calls errored — port={port_out} reference={orc_out}"
        )
        diffs = diff_db(
            _normalize_member_ids(port_db.model_dump()),
            _normalize_member_ids(orc.dump_db()),
        )
        assert not diffs, "DB state diverges:\n" + "\n".join(diffs[:15])
