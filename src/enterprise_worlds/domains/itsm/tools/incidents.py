"""Incident tools (14) — faithful port of the ITSM MCP's incident category.

Covers incident CRUD/search, priority/assignment-group aggregates, and the child-incident
relationship tools.
"""

from __future__ import annotations

from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import ChildIncident, Incident
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool

# Non-enum NOT NULL incident columns exposed as ``update_incident`` text params. Passing '' for
# one is normalized to None by the reference, which then trips the column's NOT NULL constraint
# (VALIDATION_ERROR). The enum NOT NULL columns (status/category/impact/urgency/priority) are
# already rejected by the enum gate when given '', so they need no separate guard here.
_INC_NOT_NULL_TEXT = frozenset({"number", "short_description", "caller_id"})


class IncidentToolsMixin(ItsmToolsBase):
    """Incident management tools."""

    # ------------------------------------------------------------------ helpers
    def _validate_incident_enums(
        self,
        *,
        status=None,
        category=None,
        impact=None,
        urgency=None,
        priority=None,
        channel=None,
        on_hold_reason=None,
        resolution_code=None,
    ) -> None:
        """Reject out-of-set enum values, mirroring the reference's request-body enum gate."""
        self._check_enum("status", status, enums.INCIDENT_STATUS)
        self._check_enum("category", category, enums.INCIDENT_CATEGORY)
        self._check_enum("impact", impact, enums.INCIDENT_IMPACT)
        self._check_enum("urgency", urgency, enums.INCIDENT_URGENCY)
        self._check_enum("priority", priority, enums.INCIDENT_PRIORITY)
        self._check_enum("channel", channel, enums.INCIDENT_CHANNEL)
        self._check_enum("on_hold_reason", on_hold_reason, enums.INCIDENT_ON_HOLD_REASON)
        self._check_enum("resolution_code", resolution_code, enums.INCIDENT_RESOLUTION_CODE)

    @staticmethod
    def _blank_to_none(value: Optional[str]) -> Optional[str]:
        """Map an empty-string FK value to ``None`` (the reference's ``normalize_field``)."""
        return None if value == "" else value

    def _collect_ref_errors(
        self,
        *,
        caller_id: Optional[str] = None,
        assigned_to: Optional[str] = None,
        assignment_group: Optional[str] = None,
        resolved_by: Optional[str] = None,
        service: Optional[str] = None,
        service_offering: Optional[str] = None,
        configuration_item: Optional[str] = None,
        problem: Optional[str] = None,
        change_request: Optional[str] = None,
        caused_by_change: Optional[str] = None,
        incident_template: Optional[str] = None,
    ) -> List[str]:
        """Validate every referenced entity, collecting ALL errors in the reference's order.

        Mirrors the original manager's ``_validate_referenced_entities``, which appends each bad
        reference to a list and raises one combined ``ValueError`` — vs. the port's old fail-fast
        (which stopped at the first bad ref). ``parent_incident`` is intentionally absent: the
        reference normalizes it but does not existence-check it.
        """
        checks = (
            (self._require_user, caller_id, "caller_id"),
            (self._require_user, assigned_to, "assigned_to"),
            (self._require_group, assignment_group, "assignment_group"),
            (self._require_user, resolved_by, "resolved_by"),
            (self._require_service, service, "service"),
            (self._require_service_offering, service_offering, "service_offering"),
            (self._require_ci, configuration_item, "configuration_item"),
            (self._require_problem, problem, "problem"),
            (self._require_change, change_request, "change_request"),
            (self._require_change, caused_by_change, "caused_by_change"),
            (self._require_incident_template, incident_template, "incident_template"),
        )
        errors: List[str] = []
        for check, value, field in checks:
            try:
                check(value, field)
            except ItsmError as exc:
                errors.append(str(exc))
        return errors

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def create_incident(
        self,
        caller_id: str,
        short_description: str,
        service: Optional[str] = None,
        service_offering: Optional[str] = None,
        configuration_item: Optional[str] = None,
        channel: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        description: Optional[str] = None,
        impact: Optional[str] = None,
        urgency: Optional[str] = None,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
        assignment_group: Optional[str] = None,
        worknotes: Optional[str] = None,
        resolution_notes: Optional[str] = None,
        close_notes: Optional[str] = None,
        resolution_code: Optional[str] = None,
        parent_incident: Optional[str] = None,
        problem: Optional[str] = None,
        change_request: Optional[str] = None,
        caused_by_change: Optional[str] = None,
        resolved_by: Optional[str] = None,
        on_hold_reason: Optional[str] = None,
        incident_template: Optional[str] = None,
        resolved: Optional[str] = None,
    ) -> Incident:
        """Create a new incident.

        Args:
            caller_id: User id of the person reporting the incident (required).
            short_description: Brief summary of the incident (required).
            service: Affected business service id.
            service_offering: Affected service offering id.
            configuration_item: Affected configuration item id.
            channel: Channel the incident came in on (email, phone, self-service).
            status: Incident status; defaults to 'new'.
            category: Incident category; defaults to 'inquiry-help'.
            description: Detailed description.
            impact: Business impact (high, medium, low); defaults to 'low'.
            urgency: Urgency (high, medium, low); defaults to 'low'.
            priority: Priority (critical, high, moderate, low, planning); defaults to 'planning'.
            assigned_to: Assignee user id.
            assignment_group: Assignment group id.
            worknotes: Internal work notes.
            resolution_notes: Resolution notes.
            close_notes: Closure notes.
            resolution_code: Resolution code.
            parent_incident: Parent incident id.
            problem: Related problem id.
            change_request: Related change id.
            caused_by_change: Change id that caused this incident.
            resolved_by: User id that resolved the incident.
            on_hold_reason: Reason if the incident is on hold.
            incident_template: Incident template id used.
            resolved: Resolution timestamp.

        Returns:
            The created incident.
        """
        # Enum validation first (the reference validates the request body before manager FK checks).
        self._validate_incident_enums(
            status=status, category=category, impact=impact, urgency=urgency, priority=priority,
            channel=channel, on_hold_reason=on_hold_reason, resolution_code=resolution_code,
        )
        # Normalize empty-string FK fields to None (the reference's normalize_field over its 12
        # foreign-key fields) before validating/storing them.
        caller_id = self._blank_to_none(caller_id)
        assigned_to = self._blank_to_none(assigned_to)
        assignment_group = self._blank_to_none(assignment_group)
        resolved_by = self._blank_to_none(resolved_by)
        service = self._blank_to_none(service)
        service_offering = self._blank_to_none(service_offering)
        configuration_item = self._blank_to_none(configuration_item)
        problem = self._blank_to_none(problem)
        change_request = self._blank_to_none(change_request)
        caused_by_change = self._blank_to_none(caused_by_change)
        incident_template = self._blank_to_none(incident_template)
        parent_incident = self._blank_to_none(parent_incident)
        if not caller_id:
            raise ItsmError("caller_id is required", code="MISSING_FIELD", field="caller_id")

        # Collect ALL invalid references into one combined error (matches the reference's
        # aggregated VALIDATION_ERROR), instead of stopping at the first bad ref.
        errors = self._collect_ref_errors(
            caller_id=caller_id, assigned_to=assigned_to, assignment_group=assignment_group,
            resolved_by=resolved_by, service=service, service_offering=service_offering,
            configuration_item=configuration_item, problem=problem, change_request=change_request,
            caused_by_change=caused_by_change, incident_template=incident_template,
        )
        if errors:
            raise ItsmError("; ".join(errors), code="VALIDATION_ERROR")

        incident_id, seq = self._make_id(self.db.incident, "INC")
        now = self._now()
        incident = Incident(
            incident_id=incident_id,
            number=f"INC-{seq:06d}",
            short_description=short_description,
            caller_id=caller_id,
            service=service,
            service_offering=service_offering,
            configuration_item=configuration_item,
            assigned_to=assigned_to,
            assignment_group=assignment_group,
            resolved_by=resolved_by,
            problem=problem,
            change_request=change_request,
            caused_by_change=caused_by_change,
            incident_template=incident_template,
            parent_incident=parent_incident,
            org_id=self._org_of_user(caller_id),
            channel=channel,
            contact_type=None,  # MCP does not derive contact_type on create (only seed has it)
            status=status or "new",
            category=category or "inquiry-help",
            description=description,
            worknotes=worknotes,
            resolution_notes=resolution_notes,
            close_notes=close_notes,
            impact=impact or "low",
            urgency=urgency or "low",
            priority=priority or "planning",
            resolution_code=resolution_code,
            on_hold_reason=on_hold_reason,
            resolved=resolved,
            created_at=now,
            updated_at=now,
        )
        self.db.incident[incident_id] = incident
        return incident

    @is_tool(ToolType.WRITE)
    def update_incident(
        self,
        incident_id: str,
        number: Optional[str] = None,
        service: Optional[str] = None,
        service_offering: Optional[str] = None,
        configuration_item: Optional[str] = None,
        caller_id: Optional[str] = None,
        channel: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        short_description: Optional[str] = None,
        description: Optional[str] = None,
        impact: Optional[str] = None,
        urgency: Optional[str] = None,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
        assignment_group: Optional[str] = None,
        worknotes: Optional[str] = None,
        resolution_notes: Optional[str] = None,
        close_notes: Optional[str] = None,
        resolution_code: Optional[str] = None,
        parent_incident: Optional[str] = None,
        problem: Optional[str] = None,
        change_request: Optional[str] = None,
        caused_by_change: Optional[str] = None,
        resolved_by: Optional[str] = None,
        on_hold_reason: Optional[str] = None,
        incident_template: Optional[str] = None,
        resolved: Optional[str] = None,
    ) -> Incident:
        """Update an existing incident. Only the fields you pass are changed.

        Args:
            incident_id: Id of the incident to update (required).
            number: Incident number.
            service: Affected business service id.
            service_offering: Affected service offering id.
            configuration_item: Affected configuration item id.
            caller_id: Caller user id.
            channel: Channel (email, phone, self-service); also updates contact_type.
            status: Incident status.
            category: Incident category.
            short_description: Brief summary.
            description: Detailed description.
            impact: Business impact (high, medium, low).
            urgency: Urgency (high, medium, low).
            priority: Priority (critical, high, moderate, low, planning).
            assigned_to: Assignee user id.
            assignment_group: Assignment group id.
            worknotes: Internal work notes.
            resolution_notes: Resolution notes.
            close_notes: Closure notes.
            resolution_code: Resolution code.
            parent_incident: Parent incident id.
            problem: Related problem id.
            change_request: Related change id.
            caused_by_change: Change id that caused this incident.
            resolved_by: User id that resolved the incident.
            on_hold_reason: Reason if on hold.
            incident_template: Incident template id.
            resolved: Resolution timestamp.

        Returns:
            The updated incident.
        """
        self._validate_incident_enums(
            status=status, category=category, impact=impact, urgency=urgency, priority=priority,
            channel=channel, on_hold_reason=on_hold_reason, resolution_code=resolution_code,
        )
        incident = self._require_incident(incident_id)

        updates = {
            "number": number, "service": service, "service_offering": service_offering,
            "configuration_item": configuration_item, "caller_id": caller_id,
            "channel": channel, "status": status, "category": category,
            "short_description": short_description, "description": description,
            "impact": impact, "urgency": urgency, "priority": priority,
            "assigned_to": assigned_to, "assignment_group": assignment_group,
            "worknotes": worknotes, "resolution_notes": resolution_notes,
            "close_notes": close_notes, "resolution_code": resolution_code,
            "parent_incident": parent_incident, "problem": problem,
            "change_request": change_request, "caused_by_change": caused_by_change,
            "resolved_by": resolved_by, "on_hold_reason": on_hold_reason,
            "incident_template": incident_template, "resolved": resolved,
        }
        # Empty-string handling mirrors the reference's incident "clear" model: an explicit '' is
        # normalized to None and the (nullable) column is CLEARED. A field is "provided" when its
        # arg is not None, so an explicit '' counts (and clears) — distinct from an omitted field.
        # Passing '' for a NOT NULL column (number/short_description/caller_id; the enum NOT NULL
        # columns are already rejected above) violates the column constraint -> VALIDATION_ERROR.
        provided = {k: v for k, v in updates.items() if v is not None}
        for k in list(provided):
            if provided[k] == "":
                if k in _INC_NOT_NULL_TEXT:
                    raise ItsmError(f"{k} cannot be empty", code="VALIDATION_ERROR", field=k)
                provided[k] = None

        # Collect ALL invalid references into one combined error (matches the reference's
        # aggregated VALIDATION_ERROR), instead of stopping at the first bad ref. Cleared (None)
        # references are skipped — clearing a link never triggers an existence check.
        errors = self._collect_ref_errors(
            caller_id=provided.get("caller_id"), assigned_to=provided.get("assigned_to"),
            assignment_group=provided.get("assignment_group"), resolved_by=provided.get("resolved_by"),
            service=provided.get("service"), service_offering=provided.get("service_offering"),
            configuration_item=provided.get("configuration_item"), problem=provided.get("problem"),
            change_request=provided.get("change_request"),
            caused_by_change=provided.get("caused_by_change"),
            incident_template=provided.get("incident_template"),
        )
        if errors:
            raise ItsmError("; ".join(errors), code="VALIDATION_ERROR")

        # A no-op update is rejected, not silently re-stamped — matches the reference's
        # "no changes detected" (both text and enum fields trigger it for incidents). An id-only
        # call (no updatable fields supplied) is the zero-field case of this: the reference also
        # surfaces it as NO_CHANGES_DETECTED with the empty "...for fields: " list, so the guard
        # fires whenever nothing actually changed — including when ``provided`` is empty.
        changed = {k: v for k, v in provided.items() if getattr(incident, k) != v}
        if not changed:
            raise ItsmError(
                "No changes detected for fields: " + ", ".join(provided),
                code="NO_CHANGES_DETECTED",
            )
        for field, value in changed.items():
            setattr(incident, field, value)
        incident.updated_at = self._now()
        return incident

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def find_incident_by_id(self, incident_id: str) -> Incident:
        """Return the incident with the given id.

        Args:
            incident_id: The incident id, e.g. 'INC_003'.

        Returns:
            The matching incident.
        """
        return self._require_incident(incident_id)

    @is_tool(ToolType.READ)
    def find_incident_by_number(self, number: str) -> Incident:
        """Return the incident with the given human-readable number.

        Args:
            number: The incident number, e.g. 'INC0000003'.

        Returns:
            The matching incident.
        """
        for inc in self.db.incident.values():
            if inc.number == number:
                return inc
        raise ItsmError(f"Incident with number '{number}' not found", field="number")

    @is_tool(ToolType.READ)
    def list_incidents(
        self,
        incident_id: Optional[str] = None,
        number: Optional[str] = None,
        service: Optional[str] = None,
        service_offering: Optional[str] = None,
        configuration_item: Optional[str] = None,
        called_id: Optional[str] = None,
        channel: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        short_description: Optional[str] = None,
        description: Optional[str] = None,
        impact: Optional[str] = None,
        urgency: Optional[str] = None,
        priority: Optional[str] = None,
        assigned_to: Optional[str] = None,
        assignment_group: Optional[str] = None,
        worknotes: Optional[str] = None,
        resolution_notes: Optional[str] = None,
        close_notes: Optional[str] = None,
        resolution_code: Optional[str] = None,
        parent_incident: Optional[str] = None,
        problem: Optional[str] = None,
        change_request: Optional[str] = None,
        caused_by_change: Optional[str] = None,
        resolved_by: Optional[str] = None,
        on_hold_reason: Optional[str] = None,
        incident_template: Optional[str] = None,
        resolved: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> List[Incident]:
        """List incidents, optionally filtered. All filters are ANDed; omitted filters ignored.

        Args:
            incident_id: Filter by incident id.
            number: Filter by incident number.
            service: Filter by service id.
            service_offering: Filter by service offering id.
            configuration_item: Filter by configuration item id.
            called_id: Filter by caller user id.
            channel: Filter by channel.
            status: Filter by status.
            category: Filter by category.
            short_description: Filter by short description.
            description: Filter by description.
            impact: Filter by impact.
            urgency: Filter by urgency.
            priority: Filter by priority.
            assigned_to: Filter by assignee.
            assignment_group: Filter by assignment group.
            worknotes: Filter by work notes.
            resolution_notes: Filter by resolution notes.
            close_notes: Filter by close notes.
            resolution_code: Filter by resolution code.
            parent_incident: Filter by parent incident.
            problem: Filter by related problem.
            change_request: Filter by related change.
            caused_by_change: Filter by causing change.
            resolved_by: Filter by resolver.
            on_hold_reason: Filter by on-hold reason.
            incident_template: Filter by incident template.
            resolved: Filter by resolved timestamp.
            created_after: Only incidents created on/after this ISO timestamp.
            created_before: Only incidents created on/before this ISO timestamp.

        Returns:
            The list of matching incidents.
        """
        # The reference validates enum-typed filters too (invalid value -> error, not empty result).
        self._validate_incident_enums(
            status=status, category=category, impact=impact, urgency=urgency, priority=priority,
            channel=channel, on_hold_reason=on_hold_reason, resolution_code=resolution_code,
        )
        # map exposed filter name -> incident attribute ('called_id' is the MCP's spelling)
        eq_filters = {
            "incident_id": incident_id, "number": number, "service": service,
            "service_offering": service_offering, "configuration_item": configuration_item,
            "caller_id": called_id, "channel": channel, "status": status, "category": category,
            "short_description": short_description, "description": description, "impact": impact,
            "urgency": urgency, "priority": priority, "assigned_to": assigned_to,
            "assignment_group": assignment_group, "worknotes": worknotes,
            "resolution_notes": resolution_notes, "close_notes": close_notes,
            "resolution_code": resolution_code, "parent_incident": parent_incident,
            "problem": problem, "change_request": change_request,
            "caused_by_change": caused_by_change, "resolved_by": resolved_by,
            "on_hold_reason": on_hold_reason, "incident_template": incident_template,
            "resolved": resolved,
        }
        active = {k: v for k, v in eq_filters.items() if v is not None}
        out: List[Incident] = []
        for inc in self.db.incident.values():
            if any(getattr(inc, attr) != val for attr, val in active.items()):
                continue
            # Lower bound is EXCLUSIVE for the incidents domain (live probe #9): keep only
            # created_at strictly greater than created_after. Upper bound stays inclusive (live).
            if created_after is not None and (inc.created_at or "") <= created_after:
                continue
            if created_before is not None and (inc.created_at or "") > created_before:
                continue
            out.append(inc)
        return out

    @is_tool(ToolType.READ)
    def get_incidents_assigned_to(
        self, assignment_group: Optional[str] = None, assigned_to: Optional[str] = None
    ) -> List[Incident]:
        """List incidents assigned to a user and/or assignment group.

        Args:
            assignment_group: Assignment group id to filter by.
            assigned_to: Assignee user id to filter by.

        Returns:
            The list of matching incidents.
        """
        out: List[Incident] = []
        for inc in self.db.incident.values():
            if assignment_group is not None and inc.assignment_group != assignment_group:
                continue
            if assigned_to is not None and inc.assigned_to != assigned_to:
                continue
            out.append(inc)
        return out

    @is_tool(ToolType.READ)
    def get_count_of_incident_priority_wise(
        self, priority_list: Optional[List[str]] = None
    ) -> dict:
        """Count incidents per priority.

        With a ``priority_list`` only those priorities are counted; when omitted (or empty) the
        reference counts ALL priorities present in the data (grouped), so we do the same.

        Args:
            priority_list: Optional priorities to count, e.g. ['high', 'critical']. Omit to count
                every priority present.

        Returns:
            A mapping of each priority to its incident count.
        """
        if priority_list:
            # The reference validates each requested priority against the enum set (case-sensitive)
            # and rejects invalid/wrong-case values rather than returning a zero count for them.
            for p in priority_list:
                self._check_enum("priority_list", p, enums.INCIDENT_PRIORITY)
            counts = {p: 0 for p in priority_list}
            for inc in self.db.incident.values():
                if inc.priority in counts:
                    counts[inc.priority] += 1
            return counts
        # No list: group over all priorities present (count-all behaviour).
        counts: dict = {}
        for inc in self.db.incident.values():
            if inc.priority is not None:
                counts[inc.priority] = counts.get(inc.priority, 0) + 1
        return counts

    @is_tool(ToolType.READ)
    def count_incident_for_assignment_group(
        self,
        assignment_group_id: str,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """Count incidents for an assignment group, optionally filtered.

        Args:
            assignment_group_id: The assignment group id.
            status: Optional status filter.
            priority: Optional priority filter.
            channel: Optional channel filter.

        Returns:
            A mapping of the group id to the matching incident count.
        """
        # Enum validation happens at the request body (before the handler's group lookup): an
        # invalid/wrong-case/empty filter is rejected rather than silently matching nothing.
        self._validate_incident_enums(status=status, priority=priority, channel=channel)
        # The reference 404s when the assignment group does not exist (it does not return 0).
        if assignment_group_id not in self.db.user_group:
            raise ItsmError(
                f"Assignment Group not found with identifier '{assignment_group_id}'",
                code="NOT_FOUND",
            )
        count = 0
        for inc in self.db.incident.values():
            if inc.assignment_group != assignment_group_id:
                continue
            if status is not None and inc.status != status:
                continue
            if priority is not None and inc.priority != priority:
                continue
            if channel is not None and inc.channel != channel:
                continue
            count += 1
        return {assignment_group_id: count}

    # --------------------------------------------------------- child incidents
    def _add_child(self, parent_incident: str, child_incident: str) -> ChildIncident:
        self._require_incident(parent_incident, "parent_incident")
        self._require_incident(child_incident, "child_incident")
        if parent_incident == child_incident:
            raise ItsmError(
                "An incident cannot be its own child", code="INVALID_RELATIONSHIP",
                field="child_incident",
            )
        for m in self.db.child_incident.values():
            if m.parent_incident == parent_incident and m.child_incident == child_incident:
                raise ItsmError(
                    f"Child incident relationship already exists: '{parent_incident}' -> "
                    f"'{child_incident}'", code="INVALID_RELATIONSHIP", field="child_incident",
                )
        # Reverse-relationship guard: reject A->B when B->A already exists (the reference forbids
        # cyclic parent/child links).
        for m in self.db.child_incident.values():
            if m.parent_incident == child_incident and m.child_incident == parent_incident:
                raise ItsmError(
                    f"Cannot create relationship '{parent_incident}' -> '{child_incident}': "
                    f"reverse relationship already exists ('{child_incident}' -> "
                    f"'{parent_incident}')",
                    code="INVALID_RELATIONSHIP", field="child_incident",
                )
        mapping_id, _ = self._make_id(self.db.child_incident, "CINC")
        now = self._now()
        mapping = ChildIncident(
            child_incident_mapping_id=mapping_id,
            parent_incident=parent_incident,
            child_incident=child_incident,
            created_at=now,
            updated_at=now,
        )
        self.db.child_incident[mapping_id] = mapping
        return mapping

    @is_tool(ToolType.WRITE)
    def add_child_incident(self, parent_incident: str, child_incident: str) -> ChildIncident:
        """Link a child incident to a parent incident.

        Args:
            parent_incident: The parent incident id.
            child_incident: The child incident id.

        Returns:
            The created parent-child mapping.
        """
        return self._add_child(parent_incident, child_incident)

    @is_tool(ToolType.WRITE)
    def add_child_incidents(
        self, parent_incident: str, child_incidents: List[str]
    ) -> List[ChildIncident]:
        """Link multiple child incidents to a parent incident.

        Args:
            parent_incident: The parent incident id.
            child_incidents: The child incident ids to link.

        Returns:
            The created parent-child mappings.
        """
        # All-or-nothing on existence (matches the reference): validate the parent, drop empty/
        # whitespace child ids, then collect EVERY missing child and reject before inserting any.
        self._require_incident(parent_incident, "parent_incident")
        cleaned = [c for c in child_incidents if c and c.strip()]
        missing = [c for c in cleaned if c not in self.db.incident]
        if missing:
            raise ItsmError(
                "The following child incidents do not exist: " + ", ".join(missing),
                code="CHILD_INCIDENTS_NOT_FOUND", field="child_incidents",
            )
        return [self._add_child(parent_incident, child) for child in cleaned]

    @is_tool(ToolType.WRITE)
    def update_child_incident(
        self, child_incident_mapping_id: str, parent_incident: str, child_incident: str
    ) -> ChildIncident:
        """Update an existing parent-child incident mapping.

        Args:
            child_incident_mapping_id: The mapping id to update.
            parent_incident: The new parent incident id.
            child_incident: The new child incident id.

        Returns:
            The updated mapping.
        """
        mapping = self.db.child_incident.get(child_incident_mapping_id)
        if mapping is None:
            raise ItsmError(
                f"Child incident mapping '{child_incident_mapping_id}' not found",
                field="child_incident_mapping_id",
            )
        self._require_incident(parent_incident, "parent_incident")
        self._require_incident(child_incident, "child_incident")
        # Self-link guard.
        if parent_incident == child_incident:
            raise ItsmError(
                "Cannot update relationship: parent and child incident cannot be the same "
                f"('{parent_incident}')",
                code="INVALID_RELATIONSHIP", field="child_incident",
            )
        # Duplicate guard: another mapping already has this exact direction.
        for m in self.db.child_incident.values():
            if m.child_incident_mapping_id == child_incident_mapping_id:
                continue
            if m.parent_incident == parent_incident and m.child_incident == child_incident:
                raise ItsmError(
                    f"Child incident relationship already exists: '{parent_incident}' -> "
                    f"'{child_incident}'", code="INVALID_RELATIONSHIP", field="child_incident",
                )
        # Reverse guard: another mapping already has the reverse direction.
        for m in self.db.child_incident.values():
            if m.child_incident_mapping_id == child_incident_mapping_id:
                continue
            if m.parent_incident == child_incident and m.child_incident == parent_incident:
                raise ItsmError(
                    f"Cannot update relationship '{parent_incident}' -> '{child_incident}': "
                    f"reverse relationship already exists in another mapping ('{child_incident}' "
                    f"-> '{parent_incident}')",
                    code="INVALID_RELATIONSHIP", field="child_incident",
                )
        # No-op guard: this mapping already holds the exact pair being set.
        if (mapping.parent_incident == parent_incident
                and mapping.child_incident == child_incident):
            raise ItsmError(
                f"Cannot update: relationship '{parent_incident}' -> '{child_incident}' already "
                f"exists for mapping '{child_incident_mapping_id}'",
                code="INVALID_RELATIONSHIP", field="child_incident",
            )
        mapping.parent_incident = parent_incident
        mapping.child_incident = child_incident
        mapping.updated_at = self._now()
        return mapping

    @is_tool(ToolType.WRITE)
    def remove_child_incident(
        self,
        child_incident_mapping_id: Optional[str] = None,
        parent_incident: Optional[str] = None,
        child_incident: Optional[str] = None,
    ) -> dict:
        """Remove parent-child incident mapping(s) matching the given filters.

        Args:
            child_incident_mapping_id: Remove the mapping with this id.
            parent_incident: Remove mappings with this parent.
            child_incident: Remove mappings with this child.

        Returns:
            A success message; raises if the mapping is not found.
        """
        # Identifier guard: require the mapping id OR both parent and child (the reference rejects
        # any other combination — including no args — instead of matching/deleting everything).
        if not child_incident_mapping_id and (not parent_incident or not child_incident):
            raise ItsmError(
                "Either child_incident_mapping_id or both parent_incident and child_incident "
                "must be provided",
                code="MISSING_IDENTIFIERS",
            )
        # Resolve a SINGLE mapping (id takes precedence), mirroring the reference's `.first()`.
        if child_incident_mapping_id:
            mapping = self.db.child_incident.get(child_incident_mapping_id)
            identifier = child_incident_mapping_id
        else:
            mapping = next(
                (m for m in self.db.child_incident.values()
                 if m.parent_incident == parent_incident
                 and m.child_incident == child_incident),
                None,
            )
            identifier = f"{parent_incident}-{child_incident}"
        if mapping is None:
            raise ItsmError(
                f"Child incident mapping '{identifier}' not found",
                code="MAPPING_NOT_FOUND",
            )
        del self.db.child_incident[mapping.child_incident_mapping_id]
        return {"message": "Child incident mapping removed successfully"}

    @is_tool(ToolType.READ)
    def list_child_incidents(self, parent_incident: str) -> List[ChildIncident]:
        """List the child-incident mappings for a parent incident.

        Args:
            parent_incident: The parent incident id.

        Returns:
            The child-incident mappings.
        """
        return [m for m in self.db.child_incident.values()
                if m.parent_incident == parent_incident]

    @is_tool(ToolType.READ)
    def find_parent_incident(self, child_incident: str) -> ChildIncident:
        """Find the parent-child mapping for a child incident.

        Args:
            child_incident: The child incident id.

        Returns:
            The mapping whose child is the given incident.
        """
        for m in self.db.child_incident.values():
            if m.child_incident == child_incident:
                return m
        raise ItsmError(
            f"No parent found for child incident '{child_incident}'", field="child_incident"
        )
