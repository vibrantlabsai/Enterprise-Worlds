"""Tests for ItsmDB referential-integrity validators (ID format + foreign keys).

The validators run as a ``model_validator(mode="after")`` on ``ItsmDB``, so they fire at every
``model_validate`` — i.e. seed load and post-delta. These tests exercise both the standalone
``validate_integrity()`` (raising a plain ``ValueError``) and the load/delta gates (raising a
``pydantic.ValidationError`` that wraps it).

Granular cases mutate a freshly-loaded (already-valid) DB *in place* — setattr / dict ops bypass
pydantic validation since ``validate_assignment`` is off — then assert ``validate_integrity()``
catches the corruption. The seed itself is verified clean (the ultimate safety net: if any
enforced FK/format rule were wrong, loading the real seed would fail here).
"""

import pytest
from pydantic import ValidationError

from enterprise_worlds.domains.itsm.data_model import ItsmDB
from enterprise_worlds.domains.itsm.environment import ITSM_DB_PATH
from enterprise_worlds.environment.delta import apply_delta


@pytest.fixture
def db() -> ItsmDB:
    """A freshly loaded, valid ITSM DB (reloaded per test so mutations don't leak)."""
    return ItsmDB.load(ITSM_DB_PATH)


# --- the seed is clean (regression guard for the whole spec) -----------------
def test_seed_loads_clean():
    """The shipped seed satisfies every ID-format / FK rule."""
    loaded = ItsmDB.load(ITSM_DB_PATH)  # would raise if any enforced rule were wrong
    loaded.validate_integrity()  # also callable standalone, no raise


# --- foreign keys ------------------------------------------------------------
def test_dangling_assigned_to(db):
    db.incident["INC_001"].assigned_to = "USER_999"
    with pytest.raises(ValueError, match="assigned_to='USER_999'.*not found in users"):
        db.validate_integrity()


def test_dangling_assignment_group_points_at_user_group(db):
    # assignment_group resolves against user_group, NOT users.
    db.incident["INC_001"].assignment_group = "GROUP_404"
    with pytest.raises(ValueError, match="not found in user_group"):
        db.validate_integrity()


def test_dangling_business_service_no_id_suffix(db):
    # business_service is a FK to service despite not ending in _id.
    so_id = next(iter(db.service_offering))
    db.service_offering[so_id].business_service = "SVC_999"
    with pytest.raises(ValueError, match="business_service='SVC_999'.*not found in service"):
        db.validate_integrity()


def test_dangling_original_task_points_at_incident(db):
    prb_id = next(iter(db.problem))
    db.problem[prb_id].original_task = "INC_999"
    with pytest.raises(ValueError, match="original_task='INC_999'.*not found in incident"):
        db.validate_integrity()


def test_dangling_self_reference_parent_incident(db):
    db.incident["INC_001"].parent_incident = "INC_999"
    with pytest.raises(ValueError, match="parent_incident='INC_999'.*not found in incident"):
        db.validate_integrity()


def test_bad_user_role_resolves_against_role_name(db):
    # users.role is the special FK to role.NAME, not role_id.
    db.users["USER_001"].role = "wizard"
    with pytest.raises(ValueError, match="role='wizard'.*matches no role.name"):
        db.validate_integrity()


def test_valid_role_name_still_passes(db):
    db.users["USER_001"].role = "manager"  # a real role.name
    db.validate_integrity()  # no raise


# --- composite keys ----------------------------------------------------------
def test_user_role_body_field_mismatch(db):
    # Pick a user_role whose key segment role_id is NOT the value we're about to set, so the
    # composite-key-vs-body mismatch genuinely fires. ROLE_002 is a real role, so this is a pure
    # key/body disagreement (not a dangling-FK error). Key shape: "USER_xxx:ROLE_xxx:ORG_xxx".
    key = next(k for k in db.user_role if k.split(":")[1] != "ROLE_002")
    db.user_role[key].role_id = "ROLE_002"  # body now disagrees with the key segment
    with pytest.raises(ValueError, match=r"role_id='ROLE_002'"):
        db.validate_integrity()


def test_role_permission_dangling_segment(db):
    sample = next(iter(db.role_permission.values()))
    bad = sample.model_copy(update={"perm_id": "PERM_999"})
    db.role_permission["ROLE_001:PERM_999"] = bad  # segment PERM_999 does not exist
    with pytest.raises(ValueError, match="not found in permission"):
        db.validate_integrity()


def test_composite_wrong_segment_count(db):
    rec = next(iter(db.user_role.values()))
    db.user_role["USER_001:ROLE_001"] = rec  # 2 segments, user_role expects 3
    with pytest.raises(ValueError, match="expected 3 segments"):
        db.validate_integrity()


def test_composite_bad_segment_format(db):
    rec = next(iter(db.role_permission.values()))
    db.role_permission["ROLE_001:perm2"] = rec  # 'perm2' is not PREFIX_<digits>
    with pytest.raises(ValueError, match="bad format"):
        db.validate_integrity()


# --- ID format ---------------------------------------------------------------
def test_malformed_id_key(db):
    bad = db.users["USER_001"].model_copy(update={"user_id": "usr-7"})
    db.users["usr-7"] = bad
    with pytest.raises(ValueError, match="does not match USER_"):
        db.validate_integrity()


def test_too_few_digits_rejected(db):
    bad = db.incident["INC_001"].model_copy(update={"incident_id": "INC_1"})
    db.incident["INC_1"] = bad
    with pytest.raises(ValueError, match="does not match INC_"):
        db.validate_integrity()


def test_four_digit_id_accepted(db):
    # The id generator pads to >=3 digits, so 4-digit ids must be allowed (no false positive).
    bad = db.incident["INC_001"].model_copy(update={"incident_id": "INC_0001"})
    db.incident["INC_0001"] = bad
    db.validate_integrity()  # no raise


def test_key_id_field_mismatch(db):
    rec = db.incident["INC_001"]  # incident_id stays INC_001
    db.incident["INC_777"] = rec  # placed under a different (valid-format) key
    with pytest.raises(ValueError, match="!= dict key 'INC_777'"):
        db.validate_integrity()


# --- no false positives on non-FK / denormalized fields ----------------------
def test_display_and_email_fields_accept_free_text(db):
    db.incident["INC_001"].assigned_to_display = "Someone Arbitrary"
    db.incident["INC_001"].service_display = "Whatever Service"
    db.incident["INC_001"].resolution_code = "Solution Provided"  # free-form, not a FK
    notif_id = next(iter(db.notification))
    db.notification[notif_id].email = "not-a-user@example.com"
    db.validate_integrity()  # no raise — these are not foreign keys


# --- apply_delta integration (wraps ValueError in ValidationError) -----------
def test_apply_delta_rejects_dangling_fk(db):
    with pytest.raises(ValidationError, match="not found in users"):
        apply_delta(db, {"incident": {"INC_001": {"set": {"caller_id": "USER_999"}}}})


def test_apply_delta_accepts_valid_edit(db):
    new = apply_delta(
        db, {"incident": {"INC_003": {"set": {"assigned_to": "USER_002", "priority": "critical"}}}}
    )
    assert new.incident["INC_003"].assigned_to == "USER_002"


def test_error_aggregates_multiple_violations(db):
    db.incident["INC_001"].assigned_to = "USER_999"
    db.incident["INC_001"].caller_id = "USER_888"
    with pytest.raises(ValueError) as exc:
        db.validate_integrity()
    msg = str(exc.value)
    assert "assigned_to='USER_999'" in msg and "caller_id='USER_888'" in msg
