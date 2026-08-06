"""Tool-layer enforcement of the policy lifecycle state machines (§3.3, §4.2, §4.3, §5.1)."""

from __future__ import annotations

import pytest

from enterprise_worlds.domains.itsm.environment import get_environment
from enterprise_worlds.domains.itsm.tools._base import ItsmError


def _env():
    return get_environment()


# -- incident (§3.3) ---------------------------------------------------------------------------

def test_incident_illegal_transition_raises():
    env = _env()
    env.tools.db.incident["INC_001"].status = "closed"
    with pytest.raises(ItsmError) as e:
        env.tools.update_incident(incident_id="INC_001", status="resolved")
    assert e.value.code == "TRANSITION_NOT_ALLOWED"


def test_incident_legal_transition_passes():
    env = _env()
    env.tools.db.incident["INC_001"].status = "new"
    env.tools.update_incident(incident_id="INC_001", status="in_progress")
    assert env.tools.db.incident["INC_001"].status == "in_progress"


def test_incident_resolve_requires_resolution_fields():
    env = _env()
    inc = env.tools.db.incident["INC_001"]
    inc.status, inc.resolution_code, inc.resolution_notes = "in_progress", None, None
    with pytest.raises(ItsmError) as e:
        env.tools.update_incident(incident_id="INC_001", status="resolved")
    assert e.value.code == "VALIDATION_ERROR"
    env.tools.update_incident(
        incident_id="INC_001", status="resolved",
        resolution_code="Solution Provided", resolution_notes="root cause fixed",
    )
    assert env.tools.db.incident["INC_001"].status == "resolved"


def test_incident_manager_reopen_from_closed_allowed():
    env = _env()
    env.tools.db.incident["INC_001"].status = "closed"
    env.tools.update_incident(incident_id="INC_001", status="in_progress")
    assert env.tools.db.incident["INC_001"].status == "in_progress"


# -- change (§4.2) -----------------------------------------------------------------------------

def test_change_illegal_transition_raises():
    env = _env()
    env.tools.db.change["CHG_001"].status = "new"
    with pytest.raises(ItsmError) as e:
        env.tools.update_change(change_id="CHG_001", status="closed")
    assert e.value.code == "TRANSITION_NOT_ALLOWED"


def test_change_schedule_requires_implementation_plan():
    env = _env()
    ch = env.tools.db.change["CHG_001"]
    ch.status, ch.implementation_plan = "authorize", None
    with pytest.raises(ItsmError) as e:
        env.tools.update_change(change_id="CHG_001", status="scheduled")
    assert e.value.code == "VALIDATION_ERROR"
    env.tools.update_change(change_id="CHG_001", status="scheduled", implementation_plan="rollout steps")
    assert env.tools.db.change["CHG_001"].status == "scheduled"


def test_change_close_requires_close_fields():
    env = _env()
    ch = env.tools.db.change["CHG_001"]
    ch.status, ch.close_code, ch.close_notes = "review", None, None
    with pytest.raises(ItsmError) as e:
        env.tools.update_change(change_id="CHG_001", status="closed")
    assert e.value.code == "VALIDATION_ERROR"
    env.tools.update_change(
        change_id="CHG_001", status="closed", close_code="successful", close_notes="deployed clean",
    )
    assert env.tools.db.change["CHG_001"].status == "closed"


# -- configuration item (§5.1) -----------------------------------------------------------------

def test_ci_illegal_and_terminal_transitions_raise():
    env = _env()
    env.tools.db.configuration_item["CI_001"].status = "retired"
    with pytest.raises(ItsmError) as e:
        env.tools.update_configuration_item(configuration_item_id="CI_001", status="in_use")
    assert e.value.code == "TRANSITION_NOT_ALLOWED"
    env.tools.db.configuration_item["CI_001"].status = "disposed"
    with pytest.raises(ItsmError):
        env.tools.update_configuration_item(configuration_item_id="CI_001", status="in_use")


def test_ci_legal_transition_passes():
    env = _env()
    env.tools.db.configuration_item["CI_001"].status = "in_use"
    env.tools.update_configuration_item(configuration_item_id="CI_001", status="maintenance")
    assert env.tools.db.configuration_item["CI_001"].status == "maintenance"


# -- problem terminal (§4.3) -------------------------------------------------------------------

def test_problem_closed_is_terminal():
    env = _env()
    env.tools.db.problem["PRB_001"].status = "closed"
    with pytest.raises(ItsmError) as e:
        env.tools.update_problem(problem_id="PRB_001", status="resolved")
    assert e.value.code == "TRANSITION_NOT_ALLOWED"
