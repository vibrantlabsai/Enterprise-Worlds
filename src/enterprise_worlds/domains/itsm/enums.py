"""Canonical ITSM enum value sets — the single source of truth for tool-layer validation.

The reference ServiceNow MCP enforces every enum-typed field at the request boundary and rejects
any value not in the canonical set (case-sensitive). The port keeps model fields as ``str`` (so the
seed loads), and instead validates at the tool layer via ``ItsmToolsBase._check_enum`` using the
sets below.

Values were taken from the reference's own definitions — the live ``tools/list`` input schemas for
body-validated fields, and the decompiled ``schemas/*.pyc`` enum classes for manager-validated ones
(``change``/``problem`` status etc., which carry no input-schema enum). The same field *name* means
different things per entity (``status``, ``type``, ``priority``, ``channel``), so sets are namespaced
by entity. See ``docs/itsm_fidelity_audit.md`` §4.1.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

# -- incident -----------------------------------------------------------------------------------
INCIDENT_STATUS = frozenset({"new", "in_progress", "on_hold", "resolved", "closed", "canceled"})
INCIDENT_PRIORITY = frozenset({"critical", "high", "moderate", "low", "planning"})
INCIDENT_IMPACT = frozenset({"high", "medium", "low"})
INCIDENT_URGENCY = frozenset({"high", "medium", "low"})
INCIDENT_CATEGORY = frozenset(
    {"inquiry-help", "software", "hardware", "database", "password-reset", "network"}
)
INCIDENT_CHANNEL = frozenset(
    {"self-service", "phone", "email", "chat", "walk-in", "virtual-agent", "web"}
)
INCIDENT_ON_HOLD_REASON = frozenset({"Awaiting Change", "Awaiting Caller", "Awaiting Problem"})
INCIDENT_RESOLUTION_CODE = frozenset(
    {
        "Duplicate", "Known Error", "No resolution provided", "Resolved By Caller",
        "Resolved By Problem", "Resolved By Change", "Solution Provided",
        "Workaround Provided", "User error",
    }
)

# -- incident_template (reuses incident enums; channel set excludes 'web') -----------------------
TEMPLATE_CHANNEL = frozenset(
    {"self-service", "phone", "email", "chat", "walk-in", "virtual-agent"}
)

# -- change -------------------------------------------------------------------------------------
CHANGE_STATUS = frozenset(
    {"new", "assess", "authorize", "scheduled", "implement", "review", "closed", "canceled"}
)
CHANGE_PRIORITY = frozenset({"critical", "high", "moderate", "low"})
CHANGE_IMPACT = frozenset({"high", "medium", "low"})
CHANGE_RISK = frozenset({"high", "medium", "low"})
CHANGE_CATEGORY = frozenset(
    {
        "network", "software", "hardware", "documentation", "system_software",
        "application_software", "service", "telecom", "other",
    }
)
CHANGE_CLOSE_CODE = frozenset({"successful", "successful_with_issues", "unsuccessful"})

# -- problem ------------------------------------------------------------------------------------
PROBLEM_STATUS = frozenset({"new", "assess", "root_cause", "fix_in_progress", "resolved", "closed"})
PROBLEM_PRIORITY = frozenset({"critical", "high", "moderate", "low", "planning"})
PROBLEM_IMPACT = frozenset({"high", "medium", "low"})
PROBLEM_URGENCY = frozenset({"high", "medium", "low"})
PROBLEM_CATEGORY = frozenset({"network", "software", "hardware", "database"})

# -- notification -------------------------------------------------------------------------------
NOTIFICATION_TYPE = frozenset(
    {"alert", "update", "reminder", "report", "solution_proposal", "other"}
)
NOTIFICATION_STATUS = frozenset({"queued", "sent", "delivered", "opened", "failed"})

# -- knowledge ----------------------------------------------------------------------------------
KNOWLEDGE_STATE = frozenset({"draft", "review", "published", "retired"})
KNOWLEDGE_VISIBILITY = frozenset({"internal", "external"})

# -- configuration_item -------------------------------------------------------------------------
CI_STATUS = frozenset({"in_use", "in_stock", "maintenance", "retired", "disposed"})

# -- service / service_offering -----------------------------------------------------------------
SERVICE_STATUS = frozenset(
    {"operational", "non-operational", "repair_in_progress", "ready", "retired"}
)
SERVICE_CLASSIFICATION = frozenset({"business", "technology-management", "application"})
SERVICE_BUSINESS_CRITICALITY = frozenset(
    {"critical", "somewhat-critical", "less-critical", "not-critical"}
)
SERVICE_USED_FOR = frozenset({"production", "QA", "test", "development"})

# -- user / group -------------------------------------------------------------------------------
USER_ROLE = frozenset({"admin", "manager", "agent", "reporter"})
GROUP_TYPE = frozenset(
    {"IT Support", "Service Desk", "Field Support Technicians", "Infrastructure Problem Team"}
)

# -- incident_knowledge -------------------------------------------------------------------------
USED_AS = frozenset({"suggested", "applied", "resolution"})

# -- sla_definition -----------------------------------------------------------------------------
SLA_METRIC = frozenset({"response", "resolution"})
SLA_APPLIES_TO_PRIORITY = frozenset({"critical", "high", "moderate", "low"})

# -- incident_sla -------------------------------------------------------------------------------
#: Note: the reference normalizes case for this one field (e.g. 'Breached' -> 'breached'); callers
#: should lower()/strip() before checking. Spelling is 'cancelled' (two l's) here, unlike the
#: 'canceled' used by incident/change status.
INCIDENT_SLA_STAGE = frozenset(
    {"in_progress", "paused", "completed", "cancelled", "breached"}
)

# -- lifecycle state machines -------------------------------------------------------------------
# from-state -> states it may transition to (enforced by ``ItsmToolsBase._check_*_transition``).
# A target absent from the set is illegal; a state mapped to the empty set is terminal.
INCIDENT_TRANSITIONS = {  # §3.3
    "new": frozenset({"in_progress", "on_hold", "resolved", "canceled"}),
    "in_progress": frozenset({"on_hold", "resolved", "canceled"}),
    "on_hold": frozenset({"in_progress"}),
    "resolved": frozenset({"closed", "in_progress"}),
    "closed": frozenset({"in_progress"}),  # manager reopen (§3.3); the role check is verifier-side
    "canceled": frozenset(),
}
CHANGE_TRANSITIONS = {  # §4.2 (incl. standard/emergency shortcuts)
    "new": frozenset({"assess", "authorize", "canceled"}),
    "assess": frozenset({"authorize", "canceled"}),
    "authorize": frozenset({"scheduled", "implement", "canceled"}),
    "scheduled": frozenset({"implement", "canceled"}),
    "implement": frozenset({"review"}),
    "review": frozenset({"closed", "implement"}),
    "closed": frozenset(),
    "canceled": frozenset(),
}
CI_TRANSITIONS = {  # §5.1
    "in_stock": frozenset({"in_use", "maintenance", "retired"}),
    "in_use": frozenset({"maintenance", "in_stock", "retired"}),
    "maintenance": frozenset({"in_use", "in_stock", "retired"}),
    "retired": frozenset({"disposed"}),
    "disposed": frozenset(),
}

# target-state -> fields that must be non-empty when transitioning into it. rollback_plan and the
# sys_approval gate from §4.2 have no backing schema field, so only fields that exist are listed.
INCIDENT_REQUIRED_ON = {"resolved": ("resolution_code", "resolution_notes")}  # §3.3
CHANGE_REQUIRED_ON = {  # §4.2
    "scheduled": ("implementation_plan",),
    "closed": ("close_code", "close_notes"),
}


#: Optional lookup of every set by a stable name (useful for tests / introspection).
ALL_ENUMS: Dict[str, FrozenSet[str]] = {
    name: value
    for name, value in dict(globals()).items()
    if isinstance(value, frozenset)
}
