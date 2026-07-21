"""ITSM domain data model — faithful port of the ServiceNow ITSM MCP schema.

Every model mirrors a table in the authoritative DDL (``docs/itsm_schema.sql``, dumped from the
live MCP). Field names/order match the DDL exactly; ``extra="forbid"`` makes loading a ported
``db.json`` an exact-schema gate. Enum-like columns are typed ``str`` (not ``Literal``) so the
seed db loads; allowed values are documented in ``docs/itsm_build_spec.md``.

Per-table timestamp column names intentionally differ (``incident`` uses created_at/updated_at;
most others created_on/updated_on) — preserved verbatim for DB-hash fidelity.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise_worlds.environment.db import DB

#: Shared FK spec, the single source of truth for foreign-key relationships used by both the
#: integrity check / org slice here AND the TypeScript review tool (task-review reads the same
#: ``fk_spec.json`` from the gym data dir). Keep it as data — never hand-duplicate the spec.
_FK_SPEC_PATH = Path(__file__).resolve().parents[3].parent / "data" / "itsm" / "fk_spec.json"


class ItsmRecord(BaseModel):
    """Base for all ITSM records: forbid unknown fields to catch schema drift."""

    model_config = ConfigDict(extra="forbid")


class Organization(ItsmRecord):
    org_id: str
    name: str
    active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class Role(ItsmRecord):
    role_id: str
    name: str


class Permission(ItsmRecord):
    perm_id: str
    resource: str
    action: str


class RolePermission(ItsmRecord):
    role_id: str
    perm_id: str


class Location(ItsmRecord):
    location_id: str
    name: str
    org_id: str
    plot_no: Optional[str] = None
    street: Optional[str] = None
    city: str
    state: Optional[str] = None
    country: str
    active: bool
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class User(ItsmRecord):
    user_id: str
    user_name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    role: str
    active: bool
    static_token: str
    org_id: str
    location_id: Optional[str] = None
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class UserRole(ItsmRecord):
    user_id: str
    role_id: str
    org_id: str


class UserGroup(ItsmRecord):
    group_id: str
    name: str
    type: str
    active: bool
    email: Optional[str] = None
    description: Optional[str] = None
    manager_id: str
    org_id: str
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class UserGroupMember(ItsmRecord):
    member_id: str
    group_id: str
    user_id: str
    org_id: str
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class Service(ItsmRecord):
    service_id: str
    name: str
    owned_by: str
    org_id: str
    used_for: str
    status: str
    service_classification: str
    business_criticality: str
    description: Optional[str] = None
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class ServiceOffering(ItsmRecord):
    service_offering_id: str
    name: str
    short_description: str
    owned_by: str
    business_service: str
    org_id: str
    used_for: str
    status: str
    service_classification: str
    business_criticality: str
    description: Optional[str] = None
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class ConfigurationItem(ItsmRecord):
    configuration_item_id: str
    name: str
    serial_number: str
    owner_id: str
    location_id: Optional[str] = None
    org_id: str
    status: str
    cost: Optional[float] = None
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class IncidentTemplate(ItsmRecord):
    incident_template_id: str
    name: str
    active: Optional[bool] = None
    caller_id: str
    channel: Optional[str] = None
    short_description: str
    category: Optional[str] = None
    impact: str
    urgency: str
    priority: str
    configuration_item: Optional[str] = None
    service: Optional[str] = None
    service_offering: Optional[str] = None
    org_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class Incident(ItsmRecord):
    incident_id: str
    number: str
    short_description: str
    caller_id: str
    service: Optional[str] = None
    service_offering: Optional[str] = None
    configuration_item: Optional[str] = None
    assigned_to: Optional[str] = None
    assignment_group: Optional[str] = None
    resolved_by: Optional[str] = None
    problem: Optional[str] = None
    change_request: Optional[str] = None
    caused_by_change: Optional[str] = None
    incident_template: Optional[str] = None
    parent_incident: Optional[str] = None
    org_id: str
    channel: Optional[str] = None
    contact_type: Optional[str] = None
    status: str
    category: str
    description: Optional[str] = None
    worknotes: Optional[str] = None
    resolution_notes: Optional[str] = None
    close_notes: Optional[str] = None
    impact: str
    urgency: str
    priority: str
    resolution_code: Optional[str] = None
    on_hold_reason: Optional[str] = None
    resolved: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    service_display: Optional[str] = None
    service_offering_display: Optional[str] = None
    configuration_item_display: Optional[str] = None
    assigned_to_display: Optional[str] = None
    assignment_group_display: Optional[str] = None
    parent_incident_display: Optional[str] = None
    problem_display: Optional[str] = None
    change_request_display: Optional[str] = None
    incident_template_display: Optional[str] = None


class ChildIncident(ItsmRecord):
    child_incident_mapping_id: str
    parent_incident: str
    child_incident: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class Problem(ItsmRecord):
    problem_id: str
    number: str
    problem_statement: str
    short_description: Optional[str] = None
    opened_by: str
    service: Optional[str] = None
    service_offering: Optional[str] = None
    configuration_item: Optional[str] = None
    assigned_to: Optional[str] = None
    assignment_group: Optional[str] = None
    original_task: Optional[str] = None
    org_id: str
    status: str
    category: Optional[str] = None
    worknotes: Optional[str] = None
    workaround: Optional[str] = None
    fix_notes: Optional[str] = None
    impact: str
    urgency: str
    priority: str
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class Change(ItsmRecord):
    change_id: str
    number: str
    short_description: str
    requested_by: str
    service: Optional[str] = None
    service_offering: Optional[str] = None
    configuration_item: Optional[str] = None
    assigned_to: Optional[str] = None
    assignment_group: Optional[str] = None
    org_id: str
    status: str
    category: str
    description: Optional[str] = None
    implementation_plan: Optional[str] = None
    testing_plan: Optional[str] = None
    close_notes: Optional[str] = None
    cab_required: Optional[bool] = None
    impact: str
    priority: str
    risk: str
    close_code: Optional[str] = None
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class ChangeRequestMapping(ItsmRecord):
    change_request_mapping_id: str
    change_id: str
    incident_id: Optional[str] = None
    problem_id: Optional[str] = None
    org_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class Knowledge(ItsmRecord):
    knowledge_id: str
    kb_number: str
    title: str
    short_description: Optional[str] = None
    body: Optional[str] = None
    state: str
    visibility: str
    owner_id: Optional[str] = None
    org_id: str
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class IncidentKnowledge(ItsmRecord):
    incident_kb_id: str
    incident_id: str
    knowledge_id: str
    org_id: str
    used_as: str
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class IncidentAffectedCI(ItsmRecord):
    incident_affected_cis_id: str
    configuration_item: str
    incident_id: str
    org_id: str
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class SLADefinition(ItsmRecord):
    sla_def_id: str
    name: str
    metric: str
    target_mins: int
    pause_on_pending: bool
    applies_to_priority: Optional[str] = None
    active: bool
    schedule: Optional[str] = None
    org_id: str
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class IncidentSLA(ItsmRecord):
    incident_sla_id: str
    incident_id: str
    sla_def_id: str
    org_id: str
    stage: str
    has_breached: bool
    start_time: str
    breach_time: Optional[str] = None
    completed_time: Optional[str] = None
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


class Notification(ItsmRecord):
    notification_id: str
    incident_id: str
    org_id: str
    email: str
    subject: Optional[str] = None
    message: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    created_on: Optional[str] = None
    updated_on: Optional[str] = None


# =============================================================================
# Integrity spec — drives ``ItsmDB.validate_integrity`` (ID format + foreign keys).
#
# This is the single, declarative source of truth for cross-record invariants. It is
# enforced at every ``ItsmDB.model_validate`` (seed load + post-delta) via the
# ``model_validator`` below, NOT per tool-write (that would cost O(|DB|) per write in the
# gold-replay hot loop for zero reward benefit). All IDs use a ``<PREFIX>_<digits>`` scheme;
# the generator pads to >=3 digits, so the regex allows 3-or-more.
# =============================================================================

#: Non-composite collections -> (id prefix, id field). The dict key must match
#: ``^<PREFIX>_\d{3,}$`` and equal the record's id field.
_ID_SPEC: Dict[str, Tuple[str, str]] = {
    "organization": ("ORG", "org_id"),
    "role": ("ROLE", "role_id"),
    "permission": ("PERM", "perm_id"),
    "location": ("LOC", "location_id"),
    "users": ("USER", "user_id"),
    "user_group": ("GROUP", "group_id"),
    "user_group_member": ("MEM", "member_id"),
    "service": ("SVC", "service_id"),
    "service_offering": ("SVCOFF", "service_offering_id"),
    "configuration_item": ("CI", "configuration_item_id"),
    "incident_template": ("TMPL", "incident_template_id"),
    "incident": ("INC", "incident_id"),
    "child_incident": ("CINC", "child_incident_mapping_id"),
    "problem": ("PRB", "problem_id"),
    "change": ("CHG", "change_id"),
    "change_request_mapping": ("CRM", "change_request_mapping_id"),
    "knowledge": ("KB", "knowledge_id"),
    "incident_knowledge": ("IKB", "incident_kb_id"),
    "incident_affected_cis": ("TASKCI", "incident_affected_cis_id"),
    "sla_definition": ("SLA", "sla_def_id"),
    "incident_sla": ("TSLA", "incident_sla_id"),
    "notification": ("NOTIF", "notification_id"),
}

#: Prose columns matched *fuzzily* (content overlap), not exactly, during DB-match — an LLM agent
#: can't reproduce free text verbatim. Keyed by collection name, like ``_ID_SPEC`` / ``_FK_FIELDS``.
#: ``short_description`` is prose the agent paraphrases, so it is matched fuzzily too. ``title`` stays exact.
_FREETEXT_FIELDS: Dict[str, List[str]] = {
    "notification": ["subject", "message"],
    "incident": ["short_description", "description", "worknotes", "resolution_notes", "close_notes"],
    "problem": ["short_description", "problem_statement", "worknotes", "workaround", "fix_notes"],
    "change": ["short_description", "description", "implementation_plan", "testing_plan", "close_notes"],
    "knowledge": ["body"],
    "service": ["description"],
    "service_offering": ["description"],
    "user_group": ["description"],
}

#: Composite-key collections -> ordered key segments as (segment prefix, target collection,
#: body field). The dict key is the segments joined by ':'; each segment must match its
#: prefix pattern, resolve to a PK in the target collection, AND equal the named body field.
_COMPOSITE_KEYS: Dict[str, List[Tuple[str, str, str]]] = {
    "role_permission": [("ROLE", "role", "role_id"), ("PERM", "permission", "perm_id")],
    "user_role": [
        ("USER", "users", "user_id"),
        ("ROLE", "role", "role_id"),
        ("ORG", "organization", "org_id"),
    ],
}

#: Foreign keys -> per collection, list of (field, target collection, target kind).
#: kind "pk" resolves against the target's dict keys; kind "name" resolves against the set of
#: ``role.name`` values (the ``users.role`` special case — roles stay data-driven, not hardcoded).
#: Denormalized ``*_display`` labels, ``notification.email``, timestamps, etc. are intentionally
#: absent (they are not foreign keys).
#: Loaded from the shared ``fk_spec.json`` (the single source of truth — see ``_FK_SPEC_PATH``),
#: so the gym and the TS review tool can never drift. JSON arrays become the ``(field, target,
#: kind)`` tuples the integrity check and ``slice_db_to_org`` consume.
_FK_FIELDS: Dict[str, List[Tuple[str, str, str]]] = {
    coll: [(field, target, kind) for field, target, kind in entries]
    for coll, entries in json.loads(_FK_SPEC_PATH.read_text(encoding="utf-8")).items()
}


class ItsmDB(DB):
    """In-memory ITSM database. One collection per table, keyed by primary key.

    Composite-key tables (role_permission, user_role) are keyed by their PK columns joined
    with ':' by the seed porter; the record still carries the individual columns.

    A ``model_validator`` enforces cross-record integrity (ID format + foreign keys) on every
    ``model_validate`` — i.e. seed load and post-delta. See ``_ID_SPEC`` / ``_FK_FIELDS`` /
    ``_COMPOSITE_KEYS`` for the declarative spec. Consequently a delta that deletes a record
    still referenced by another (no cascade) is rejected — order deletes leaf-first.
    """

    organization: Dict[str, Organization] = Field(default_factory=dict)
    role: Dict[str, Role] = Field(default_factory=dict)
    permission: Dict[str, Permission] = Field(default_factory=dict)
    role_permission: Dict[str, RolePermission] = Field(default_factory=dict)
    location: Dict[str, Location] = Field(default_factory=dict)
    users: Dict[str, User] = Field(default_factory=dict)
    user_role: Dict[str, UserRole] = Field(default_factory=dict)
    user_group: Dict[str, UserGroup] = Field(default_factory=dict)
    user_group_member: Dict[str, UserGroupMember] = Field(default_factory=dict)
    service: Dict[str, Service] = Field(default_factory=dict)
    service_offering: Dict[str, ServiceOffering] = Field(default_factory=dict)
    configuration_item: Dict[str, ConfigurationItem] = Field(default_factory=dict)
    incident_template: Dict[str, IncidentTemplate] = Field(default_factory=dict)
    incident: Dict[str, Incident] = Field(default_factory=dict)
    child_incident: Dict[str, ChildIncident] = Field(default_factory=dict)
    problem: Dict[str, Problem] = Field(default_factory=dict)
    change: Dict[str, Change] = Field(default_factory=dict)
    change_request_mapping: Dict[str, ChangeRequestMapping] = Field(default_factory=dict)
    knowledge: Dict[str, Knowledge] = Field(default_factory=dict)
    incident_knowledge: Dict[str, IncidentKnowledge] = Field(default_factory=dict)
    incident_affected_cis: Dict[str, IncidentAffectedCI] = Field(default_factory=dict)
    sla_definition: Dict[str, SLADefinition] = Field(default_factory=dict)
    incident_sla: Dict[str, IncidentSLA] = Field(default_factory=dict)
    notification: Dict[str, Notification] = Field(default_factory=dict)

    def freetext_fields(self) -> Dict[str, List[str]]:
        """Prose columns matched fuzzily (not exactly) during DB-match. See ``_FREETEXT_FIELDS``."""
        return _FREETEXT_FIELDS

    # -- integrity ----------------------------------------------------------
    def validate_integrity(self) -> None:
        """Check ID format and foreign keys across all collections.

        Collects every violation and raises a single ``ValueError`` listing them (or returns
        silently when the DB is clean). Pure and order-independent so the gold and predicted
        environments — built by different code paths — validate identically; also invoked by the
        ``model_validator`` on every ``model_validate``.
        """
        errors: List[str] = []

        # 1. ID format + dict-key/id-field agreement (non-composite collections).
        for coll, (prefix, id_field) in _ID_SPEC.items():
            pattern = re.compile(rf"^{prefix}_\d{{3,}}$")
            for key, rec in getattr(self, coll).items():
                if not pattern.fullmatch(key):
                    errors.append(f"{coll}: id {key!r} does not match {prefix}_<3+ digits>")
                rec_id = getattr(rec, id_field)
                if rec_id != key:
                    errors.append(f"{coll}: {id_field}={rec_id!r} != dict key {key!r}")

        # 2. Composite keys: segment format + resolution + agreement with body fields.
        for coll, segments in _COMPOSITE_KEYS.items():
            for key, rec in getattr(self, coll).items():
                parts = key.split(":")
                if len(parts) != len(segments):
                    errors.append(
                        f"{coll}: composite key {key!r} expected {len(segments)} segments"
                    )
                    continue
                for part, (prefix, target, body_field) in zip(parts, segments):
                    if not re.fullmatch(rf"{prefix}_\d{{3,}}", part):
                        errors.append(f"{coll}: key {key!r} segment {part!r} bad format")
                    elif part not in getattr(self, target):
                        errors.append(
                            f"{coll}: key {key!r} segment {part!r} not found in {target}"
                        )
                    if getattr(rec, body_field) != part:
                        errors.append(
                            f"{coll}: key {key!r} segment {part!r} != {body_field}="
                            f"{getattr(rec, body_field)!r}"
                        )

        # 3. Foreign keys (None = unset, skipped). "name" kind resolves against role names.
        role_names = {r.name for r in self.role.values()}
        for coll, fks in _FK_FIELDS.items():
            for key, rec in getattr(self, coll).items():
                for field, target, kind in fks:
                    value = getattr(rec, field)
                    if value is None:
                        continue
                    if kind == "name":
                        if value not in role_names:
                            errors.append(
                                f"{coll}.{field}={value!r} (record {key}) "
                                f"matches no {target}.name"
                            )
                    elif value not in getattr(self, target):
                        errors.append(
                            f"{coll}.{field}={value!r} (record {key}) not found in {target}"
                        )

        if errors:
            raise ValueError(
                "ItsmDB integrity check failed:\n  - " + "\n  - ".join(errors)
            )

    @model_validator(mode="after")
    def _enforce_integrity(self) -> "ItsmDB":
        self.validate_integrity()
        return self


def slice_db_to_orgs(db: "ItsmDB", org_ids: Optional[Set[str]]) -> "ItsmDB":
    """Return a NEW ``ItsmDB`` scoped to a SET of orgs (the task's tenancy scope).

    ``org_ids`` is the set of orgs a task touches — a 1-element set for single-tenant, the
    ``{provider, client}`` pair for an MSP task, or ``None`` for no slice (flat / all orgs).

    Keep org-agnostic catalog rows (tables whose records have no ``org_id`` field — role,
    permission, role_permission) plus every row whose ``org_id`` is in ``org_ids`` (this also
    narrows ``organization`` to those orgs). Then prune dangling foreign-key references to a fixed
    point — chiefly ``child_incident``, which has no ``org_id`` of its own but points at two
    incidents; any mapping whose parent/child fell outside the scope is dropped. The result
    re-validates (the ``model_validator`` re-runs ``validate_integrity``), so the slice is
    guaranteed referentially consistent. Numbers/names that collide *outside* the scope now resolve
    unambiguously to the in-scope record.
    """
    if org_ids is None:
        return db  # no slice — flat / multi-org view
    raw = db.model_dump()
    # FK-CLOSURE (not prune): keep the scope's org rows, then transitively PULL IN every row they
    # reference, even across orgs. A self-contained org adds little; an MSP task (client incident
    # owned in the client org, worked by a provider agent) pulls in the provider assignee, the
    # assignment_group, and that group's manager — a complete, referentially-consistent subgraph.
    # (The old prune dropped the *referencer*, which on a cross-org seed collapsed everything.)
    keep: Dict[str, set] = {coll: set() for coll in raw}

    # 1. Anchors: org-scoped rows whose org is in scope.
    for coll, rows in raw.items():
        for rid, rec in rows.items():
            if isinstance(rec, dict) and rec.get("org_id") in org_ids:
                keep[coll].add(rid)

    # 2. Closure: add every pk-FK referent of a kept row, to a fixed point (cross-org included).
    changed = True
    while changed:
        changed = False
        for coll in list(keep):
            for rid in list(keep[coll]):
                rec = raw[coll][rid]
                for field, target, kind in _FK_FIELDS.get(coll, []):
                    if kind != "pk":
                        continue
                    v = rec.get(field)
                    if v is not None and v in raw.get(target, {}) and v not in keep[target]:
                        keep[target].add(v)
                        changed = True

    # 3. Org-agnostic rows (no org_id field): pure catalog (role/permission/role_permission — no pk FK
    #    to org data) is always kept; FK-bearing joins (child_incident) are kept only when every pk-FK
    #    target already survived — so they never dangle and never drag out-of-scope rows back in.
    for coll, rows in raw.items():
        for rid, rec in rows.items():
            if not isinstance(rec, dict) or "org_id" in rec:
                continue
            if all(
                rec.get(field) is None or rec.get(field) in keep.get(target, set())
                for field, target, kind in _FK_FIELDS.get(coll, [])
                if kind == "pk"
            ):
                keep[coll].add(rid)

    sliced = {coll: {rid: rows[rid] for rid in keep[coll]} for coll, rows in raw.items()}
    return ItsmDB.model_validate(sliced)


def slice_db_to_org(db: "ItsmDB", org_id: str) -> "ItsmDB":
    """Back-compat single-org slice — a 1-element scope. See ``slice_db_to_orgs``."""
    return slice_db_to_orgs(db, {org_id})
