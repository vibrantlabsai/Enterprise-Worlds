"""The env clock governs a new incident SLA's start_time.

A newly linked incident SLA starts "now", so ``link_new_incident_sla`` stamps ``start_time`` from
the env clock (like created_on/updated_on) when the caller omits it — making it reproducible across
the gold replay and the agent run instead of an agent-supplied, unguessable value.
"""

from enterprise_worlds.domains.itsm import environment as itsm_env
from enterprise_worlds.utils.clock import reset_now, set_now


def _tools(now: str):
    set_now(now)
    return itsm_env.get_environment(acting_user_id="USER_005").tools


def test_link_new_incident_sla_autostamps_start_time_from_clock():
    try:
        tools = _tools("2025-03-04T09:00:00")
        rec = tools.link_new_incident_sla(incident_id="INC_004", sla_def_id="SLA_001")
        # Auto-stamped (clock-derived) times are canonicalised to the seed format ("YYYY-MM-DD HH:MM:SS").
        assert rec.start_time == "2025-03-04 09:00:00"
        assert rec.created_on == "2025-03-04 09:00:00"
        assert rec.updated_on == "2025-03-04 09:00:00"
    finally:
        reset_now()


def test_link_new_incident_sla_honours_explicit_start_time():
    """Back-compat: an explicit start_time is still stored verbatim (e.g. a backdated SLA); only
    clock-derived stamps (created_on) are canonicalised to the seed format."""
    try:
        tools = _tools("2025-03-04T09:00:00")
        rec = tools.link_new_incident_sla(
            incident_id="INC_004", sla_def_id="SLA_001", start_time="2025-01-01T00:00:00"
        )
        assert rec.start_time == "2025-01-01T00:00:00"
        assert rec.created_on == "2025-03-04 09:00:00"
    finally:
        reset_now()
