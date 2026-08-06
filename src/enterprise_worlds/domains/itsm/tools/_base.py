"""Shared base for the ITSM toolkit: DB access, deterministic IDs, validation helpers.

Category mixins (incidents, users, …) provide the ``@is_tool`` methods and rely on the
helpers defined here. The concrete ``ItsmTools`` (in ``__init__.py``) combines all mixins with
this base so the toolkit metaclass collects every tool.

All write tools mutate the in-memory ``ItsmDB`` only. IDs and timestamps are generated
deterministically (sequential suffix + frozen clock) so gold-action replay reproduces the same
DB state for hash matching. Behaviour mirrors the live MCP (see ``docs/itsm_build_spec.md``).
"""

from __future__ import annotations

from typing import Any, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import ItsmDB
from enterprise_worlds.environment.toolkit import ToolKitBase
from enterprise_worlds.utils.clock import get_now


class ItsmError(ValueError):
    """A domain validation error. Carries an optional code/field for richer rendering.

    Raised by tools and surfaced to the agent as a tool error (mirrors the MCP's typed errors).
    """

    def __init__(self, message: str, code: str = "VALIDATION_ERROR", field: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.field = field


class ItsmToolsBase(ToolKitBase):
    """Base toolkit holding the ITSM DB + shared helpers (no tools of its own)."""

    db: ItsmDB

    def __init__(
        self, db: ItsmDB, acting_user_id: Optional[str] = None, org_id: Optional[str] = None
    ) -> None:
        super().__init__(db)
        # The authenticated caller (from the task's user context). Used for org scoping;
        # defaults to the first admin in the seed when not provided.
        self.acting_user_id = acting_user_id
        # The task's org. When set the env is single-tenant (DB sliced to this org), so this is
        # the authoritative scope; otherwise org is derived from the acting user.
        self.org_id = org_id

    # -- time ---------------------------------------------------------------
    @staticmethod
    def _now() -> str:
        return get_now()

    # -- id generation ------------------------------------------------------
    @staticmethod
    def _next_seq(collection: dict) -> int:
        """Next sequential integer for ``<PREFIX>_<NNN>`` keys (1 if empty)."""
        seqs = []
        for key in collection:
            tail = key.split("_")[-1]
            if tail.isdigit():
                seqs.append(int(tail))
        return (max(seqs) + 1) if seqs else 1

    @classmethod
    def _make_id(cls, collection: dict, prefix: str, width: int = 3) -> tuple[str, int]:
        """Return (new_id, seq) like ('INC_024', 24) for the given prefix."""
        seq = cls._next_seq(collection)
        return f"{prefix}_{seq:0{width}d}", seq

    # -- org scoping --------------------------------------------------------
    def _acting_org(self) -> str:
        """Org of the acting user; falls back to the first admin's org, else ORG_001."""
        if self.acting_user_id and self.acting_user_id in self.db.users:
            return self.db.users[self.acting_user_id].org_id
        for u in self.db.users.values():
            if u.role == "admin":
                return u.org_id
        any_user = next(iter(self.db.users.values()), None)
        return any_user.org_id if any_user else "ORG_001"

    def _org_of_user(self, user_id: str) -> str:
        user = self.db.users.get(user_id)
        if user is None:
            raise ItsmError(f"User with ID '{user_id}' not found", field="user_id")
        return user.org_id

    # -- enum validation ----------------------------------------------------
    def _check_enum(
        self, field: str, value: Optional[str], allowed: "frozenset[str]"
    ) -> None:
        """Reject a value outside its canonical set, mirroring the reference's enum gate.

        ``None`` (field not supplied) is always allowed. The check is case-sensitive — the
        reference rejects wrong-case values rather than normalizing them (the one field that does
        normalize, ``incident_sla.stage``, lower()s before calling this). Canonical sets live in
        ``enterprise_worlds.domains.itsm.enums``.
        """
        if value is not None and value not in allowed:
            allowed_str = ", ".join(repr(v) for v in sorted(allowed))
            raise ItsmError(
                f"Invalid value '{value}' for '{field}'. Must be one of: {allowed_str}",
                code="INVALID_ENUM_VALUE",
                field=field,
            )

    def _reject_empty(self, field: str, value: Optional[str]) -> None:
        """Reject an empty string for a ``min_length=1``-constrained field.

        Mirrors the reference's pydantic request-body field constraint: such a field rejects ``''``
        outright (``string_too_short``) rather than normalizing it to None/clearing the column —
        so the rejection fires even when other valid fields are present in the same call. ``None``
        (field not supplied) is skipped.
        """
        if value == "":
            raise ItsmError(
                "String should have at least 1 character",
                code="VALIDATION_ERROR",
                field=field,
            )

    # -- lifecycle transition validation ------------------------------------
    def _assert_transition(
        self, entity: str, table: "dict[str, frozenset[str]]", current: str, new: Optional[str]
    ) -> None:
        if new is None or new == current:
            return
        if new not in table.get(current, frozenset()):
            raise ItsmError(
                f"{entity} status transition {current!r} -> {new!r} is not permitted by policy",
                code="TRANSITION_NOT_ALLOWED",
                field="status",
            )

    def _assert_required_on(
        self, entity: str, required_on: dict, new: str, record: Any, updates: dict
    ) -> None:
        for field in required_on.get(new, ()):
            value = updates.get(field)
            if value is None:
                value = getattr(record, field, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ItsmError(
                    f"{entity} status {new!r} requires {field}",
                    code="VALIDATION_ERROR",
                    field=field,
                )

    def _check_incident_transition(self, record: Any, updates: dict) -> None:
        """Enforce the §3.3 incident lifecycle and its resolve-time required fields."""
        new = updates.get("status")
        self._assert_transition("incident", enums.INCIDENT_TRANSITIONS, record.status, new)
        if new is not None and new != record.status:
            self._assert_required_on("incident", enums.INCIDENT_REQUIRED_ON, new, record, updates)

    def _check_change_transition(self, record: Any, updates: dict) -> None:
        """Enforce the §4.2 change lifecycle and its schedule/close required fields."""
        new = updates.get("status")
        self._assert_transition("change", enums.CHANGE_TRANSITIONS, record.status, new)
        if new is not None and new != record.status:
            self._assert_required_on("change", enums.CHANGE_REQUIRED_ON, new, record, updates)

    def _check_configuration_item_transition(self, record: Any, new_status: Optional[str]) -> None:
        """Enforce the §5.1 CI lifecycle."""
        self._assert_transition(
            "configuration_item", enums.CI_TRANSITIONS, record.status, new_status
        )

    def _check_problem_transition(self, record: Any, new_status: Optional[str]) -> None:
        """§4.3 defines a final state but no ordered table, so only closed is guarded."""
        if new_status is not None and new_status != record.status and record.status == "closed":
            raise ItsmError(
                "problem is closed (terminal); reopening is not permitted by policy",
                code="TRANSITION_NOT_ALLOWED",
                field="status",
            )

    # -- existence checks ---------------------------------------------------
    def _require_user(self, user_id: Optional[str], field: str) -> None:
        if user_id is not None and user_id not in self.db.users:
            raise ItsmError(f"User with ID '{user_id}' not found", field=field)

    def _require_group(self, group_id: Optional[str], field: str = "assignment_group") -> None:
        if group_id is not None and group_id not in self.db.user_group:
            raise ItsmError(f"Group with ID '{group_id}' not found", field=field)

    def _require_ci(self, ci_id: Optional[str], field: str = "configuration_item") -> None:
        if ci_id is not None and ci_id not in self.db.configuration_item:
            raise ItsmError(f"Configuration item with ID '{ci_id}' not found", field=field)

    def _require_incident(self, incident_id: str, field: str = "incident_id"):
        inc = self.db.incident.get(incident_id)
        if inc is None:
            raise ItsmError(f"Incident with ID '{incident_id}' not found", field=field)
        return inc

    def _require_service(self, service_id: Optional[str], field: str = "service") -> None:
        if service_id is not None and service_id not in self.db.service:
            raise ItsmError(f"Service with ID '{service_id}' not found", field=field)

    def _require_service_offering(
        self, offering_id: Optional[str], field: str = "service_offering"
    ) -> None:
        if offering_id is not None and offering_id not in self.db.service_offering:
            raise ItsmError(f"Service offering with ID '{offering_id}' not found", field=field)

    def _require_problem(self, problem_id: Optional[str], field: str = "problem") -> None:
        if problem_id is not None and problem_id not in self.db.problem:
            raise ItsmError(f"Problem with ID '{problem_id}' not found", field=field)

    def _require_change(self, change_id: Optional[str], field: str = "change_request") -> None:
        if change_id is not None and change_id not in self.db.change:
            raise ItsmError(f"Change request with ID '{change_id}' not found", field=field)

    def _require_incident_template(
        self, template_id: Optional[str], field: str = "incident_template"
    ) -> None:
        if template_id is not None and template_id not in self.db.incident_template:
            raise ItsmError(f"Incident template with ID '{template_id}' not found", field=field)
