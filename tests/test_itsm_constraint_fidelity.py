"""Port-side regression guards for the FK / constraint / field-validation fidelity fixes.

These assert the in-memory port's behaviour DIRECTLY (no Docker reference needed), pinning the
divergences that were found and fixed by differential probing against the live ServiceNow ITSM
MCP. Each fix below was confirmed to match the reference at the time it landed; see
``tests/test_itsm_conformance.py`` for the oracle-gated differential version.

Behaviour families guarded:
  * contact-format validation  — phone ``+<cc>-XXX-XXX-XXXX`` and email shape (users) / email (groups)
  * no-op guard                — ``update_incident`` with only the id is NO_CHANGES_DETECTED
  * empty-string CLEAR model   — incident / knowledge: ``''`` clears a nullable column; NOT NULL ⇒ error
  * empty-string DROP model    — problem / change: ``''`` is dropped (cannot clear; id-only ⇒ no fields)
  * min_length=1 reject        — CI serial / user names / group name / notif subject+message / kb title
  * create normalization       — problem / change normalise ``''`` FK & text fields to None on create
"""

from __future__ import annotations

import pytest

from enterprise_worlds.domains.itsm.environment import ITSM_DB_PATH
from enterprise_worlds.domains.itsm.data_model import ItsmDB
from enterprise_worlds.domains.itsm.tools import ItsmTools
from enterprise_worlds.domains.itsm.tools._base import ItsmError

ACTING = "USER_039"  # active ORG_001 admin in the 20-org seed
ORG = "ORG_001"
GOOD_PHONE = "+1-555-100-2000"


@pytest.fixture
def tools() -> ItsmTools:
    """Fresh tools over a freshly loaded seed (reloaded per test so writes don't leak)."""
    return ItsmTools(ItsmDB.load(ITSM_DB_PATH), acting_user_id=ACTING)


def _first(db_coll, pred):
    return next((k for k, v in db_coll.items() if pred(v)), None)


def _inorg(tools, coll, pred=lambda v: True):
    return _first(getattr(tools.db, coll), lambda v: v.org_id == ORG and pred(v))


# --------------------------------------------------------------------- contact formats
def test_add_user_rejects_bad_phone(tools):
    with pytest.raises(ItsmError) as e:
        tools.use_tool("add_new_user", first_name="P", last_name="R", email="ok1@x.com",
                       phone="1", role="agent", active=True)
    assert e.value.code == "INVALID_PHONE_FORMAT"


def test_add_user_rejects_bad_email(tools):
    with pytest.raises(ItsmError) as e:
        tools.use_tool("add_new_user", first_name="P", last_name="R", email="notanemail",
                       phone=GOOD_PHONE, role="agent", active=True)
    assert e.value.code == "INVALID_EMAIL_FORMAT"


def test_add_user_accepts_valid_contact(tools):
    u = tools.use_tool("add_new_user", first_name="P", last_name="R", email="ok2@x.com",
                       phone=GOOD_PHONE, role="agent", active=True)
    assert u.phone == GOOD_PHONE


def test_update_user_rejects_bad_phone_and_email(tools):
    uid = _inorg(tools, "users", lambda v: v.active)
    with pytest.raises(ItsmError) as e1:
        tools.use_tool("update_user_details", user_id=uid, phone="1")
    assert e1.value.code == "INVALID_PHONE_FORMAT"
    with pytest.raises(ItsmError) as e2:
        tools.use_tool("update_user_details", user_id=uid, email="bad")
    assert e2.value.code == "INVALID_EMAIL_FORMAT"


def test_add_group_rejects_bad_email(tools):
    mgr = _inorg(tools, "users", lambda v: v.role == "manager")
    with pytest.raises(ItsmError) as e:
        tools.use_tool("add_new_user_group", name="GZ", type="IT Support",
                       manager_id=mgr, email="bademail")
    assert e.value.field == "email"


# --------------------------------------------------------------------- no-op guard
def test_update_incident_id_only_is_no_changes(tools):
    inc = _inorg(tools, "incident")
    with pytest.raises(ItsmError) as e:
        tools.use_tool("update_incident", incident_id=inc)
    assert e.value.code == "NO_CHANGES_DETECTED"


# --------------------------------------------------------------------- empty-string CLEAR (incident)
def test_incident_empty_fk_clears(tools):
    inc = _inorg(tools, "incident", lambda v: v.service)
    rec = tools.use_tool("update_incident", incident_id=inc, service="")
    assert rec.service is None


def test_incident_empty_worknotes_clears(tools):
    inc = _inorg(tools, "incident", lambda v: v.worknotes)
    rec = tools.use_tool("update_incident", incident_id=inc, worknotes="")
    assert rec.worknotes is None


def test_incident_empty_notnull_field_rejected(tools):
    inc = _inorg(tools, "incident")
    with pytest.raises(ItsmError) as e:
        tools.use_tool("update_incident", incident_id=inc, short_description="")
    assert e.value.code == "VALIDATION_ERROR"


# --------------------------------------------------------------------- empty-string DROP (problem/change)
def test_problem_empty_only_is_no_fields(tools):
    prb = _inorg(tools, "problem", lambda v: v.worknotes)
    with pytest.raises(ItsmError):
        # worknotes='' is dropped -> nothing left to update
        tools.use_tool("update_problem", problem_id=prb, worknotes="")


def test_change_empty_only_is_no_fields(tools):
    chg = _inorg(tools, "change", lambda v: v.description)
    with pytest.raises(ItsmError):
        tools.use_tool("update_change", change_id=chg, description="")


def test_problem_empty_fk_dropped_not_bad_ref(tools):
    # service='' must drop (no FK error); the real field still applies.
    prb = _inorg(tools, "problem")
    rec = tools.use_tool("update_problem", problem_id=prb, service="", worknotes="dropguard-xyz")
    assert rec.worknotes == "dropguard-xyz"


# --------------------------------------------------------------------- min_length=1 rejects
@pytest.mark.parametrize("tool,args_key,extra", [
    ("update_configuration_item", "serial_number", {}),
    ("update_user_details", "first_name", {}),
    ("update_user_details", "last_name", {}),
    ("update_user_group", "name", {}),
    ("update_notification", "subject", {}),
    ("update_notification", "message", {}),
])
def test_min_length_fields_reject_empty(tools, tool, args_key, extra):
    id_map = {
        "update_configuration_item": ("configuration_item_id", "configuration_item"),
        "update_user_details": ("user_id", "users"),
        "update_user_group": ("group_id", "user_group"),
        "update_notification": ("notification_id", "notification"),
    }
    idf, coll = id_map[tool]
    rid = _inorg(tools, coll)
    with pytest.raises(ItsmError) as e:
        tools.use_tool(tool, **{idf: rid, args_key: ""}, **extra)
    assert e.value.field == args_key


def test_knowledge_title_empty_rejected(tools):
    kb = _inorg(tools, "knowledge")
    with pytest.raises(ItsmError) as e:
        tools.use_tool("update_knowledge_article", knowledge_id=kb, title="")
    assert e.value.field == "title"


def test_knowledge_owner_empty_clears(tools):
    kb = _inorg(tools, "knowledge", lambda v: v.owner_id)
    rec = tools.use_tool("update_knowledge_article", knowledge_id=kb, owner_id="")
    assert rec.owner_id is None


def test_create_knowledge_empty_body_rejected(tools):
    owner = _inorg(tools, "users", lambda v: v.active)
    with pytest.raises(ItsmError):
        tools.use_tool("create_knowledge_article", title="kx", owner_id=owner, body="")


# --------------------------------------------------------------------- create normalization
def test_create_problem_empty_fk_normalized(tools):
    rec = tools.use_tool("create_problem", problem_statement="x", status="new",
                         impact="medium", urgency="medium", priority="moderate", service="")
    assert rec.service is None


def test_create_change_empty_fk_normalized(tools):
    rec = tools.use_tool("create_change", short_description="x", status="new",
                         impact="medium", risk="medium", priority="moderate", service="")
    assert rec.service is None
