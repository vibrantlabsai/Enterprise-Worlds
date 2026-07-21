"""Incident-template tools (4) — faithful port of the ITSM MCP's incident_templates category.

Covers incident-template create / get-by-name / list / update. The MCP nests the scalar
template fields inside a ``change_request_values`` JSON object on input (and in its read
returns); internally each scalar maps to a column on the flat ``incident_template`` row.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import IncidentTemplate
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class IncidentTemplateToolsMixin(ItsmToolsBase):
    """Incident-template management tools."""

    # The recognised ``change_request_values`` keys (sorted; mirrors the reference's allow-list
    # and the order it renders them in the rejection message).
    _ALLOWED_CRV_KEYS = (
        "caller_id",
        "category",
        "channel",
        "configuration_item",
        "impact",
        "priority",
        "service",
        "service_offering",
        "short_description",
        "urgency",
    )

    # ----------------------------------------------------------- private helpers
    def _check_unknown_crv_keys(self, crv: Dict[str, Any]) -> None:
        """Reject ``change_request_values`` keys outside the recognised set.

        The live server rejects unknown keys with a single ``VALIDATION_ERROR``; the port
        previously ignored them silently. Unknown keys are listed sorted, followed by the full
        allow-list.
        """
        unknown = sorted(k for k in crv if k not in self._ALLOWED_CRV_KEYS)
        if unknown:
            raise ItsmError(
                "Invalid fields in change_request_values: "
                + ", ".join(unknown)
                + ". Allowed fields: "
                + ", ".join(self._ALLOWED_CRV_KEYS),
                code="VALIDATION_ERROR",
            )

    def _validate_template_enums(self, crv: Dict[str, Any]) -> None:
        """Reject out-of-set enum values supplied in ``change_request_values``.

        Mirrors the reference's request-body enum gate. The enum-typed template fields are nested
        inside ``change_request_values``, so they are validated from that dict. Empty strings are
        this category's "not provided" sentinel (ignored on update, defaulted on create), so they
        are coerced to ``None`` and skipped by ``_check_enum`` (which already no-ops on None).
        Note: the template channel set excludes 'web' (unlike ``incident.channel``).
        """
        self._check_enum("impact", crv.get("impact") or None, enums.INCIDENT_IMPACT)
        self._check_enum("urgency", crv.get("urgency") or None, enums.INCIDENT_URGENCY)
        self._check_enum("priority", crv.get("priority") or None, enums.INCIDENT_PRIORITY)
        self._check_enum("category", crv.get("category") or None, enums.INCIDENT_CATEGORY)
        self._check_enum("channel", crv.get("channel") or None, enums.TEMPLATE_CHANNEL)

    def _tmpl_require_service(self, service_id: Optional[str], field: str = "service") -> None:
        if service_id is not None and service_id not in self.db.service:
            raise ItsmError(f"Service with ID '{service_id}' not found", field=field)

    def _tmpl_require_service_offering(
        self, offering_id: Optional[str], field: str = "service_offering"
    ) -> None:
        if offering_id is not None and offering_id not in self.db.service_offering:
            raise ItsmError(
                f"Service offering with ID '{offering_id}' not found", field=field
            )

    def _template_parent_consistency_error(
        self, service_id: Optional[str], offering_id: Optional[str]
    ) -> Optional[str]:
        """Return the service<->offering parent-consistency error, or None if consistent/skipped.

        The reference only runs this cross-check when BOTH the (effective) service and offering
        resolve to existing rows: if the service does not exist its existence error is what
        surfaces and the parent-check is skipped. The message names the offering's actual parent
        service (mirrors live).
        """
        if not service_id or not offering_id:
            return None
        if service_id not in self.db.service:
            return None
        offering = self.db.service_offering.get(offering_id)
        if offering is None:
            return None
        if offering.business_service == service_id:
            return None
        svc = self.db.service.get(service_id)
        svc_name = svc.name if svc else service_id
        parent = self.db.service.get(offering.business_service)
        parent_name = parent.name if parent else offering.business_service
        return (
            f"Service offering '{offering_id}' ({offering.name}) does not belong to service "
            f"'{service_id}' ({svc_name}). Service offering belongs to service "
            f"'{offering.business_service}' ({parent_name})"
        )

    def _collect_template_ref_errors(
        self,
        caller_id: Optional[str],
        service: Optional[str],
        service_offering: Optional[str],
        configuration_item: Optional[str],
        eff_service: Optional[str],
        eff_offering: Optional[str],
    ) -> List[str]:
        """Validate every referenced entity, collecting ALL errors in the reference's order.

        The live server aggregates referenced-entity failures (caller_id, service,
        service_offering, configuration_item) together with the service<->offering
        parent-consistency check into one ``VALIDATION_ERROR`` instead of stopping at the first
        bad ref. ``None`` values (field not provided, or cleared on update) are skipped by the
        underlying existence checks.
        """
        errors: List[str] = []
        checks = (
            (self._require_user, caller_id, "caller_id"),
            (self._tmpl_require_service, service, "service"),
            (self._tmpl_require_service_offering, service_offering, "service_offering"),
            (self._require_ci, configuration_item, "configuration_item"),
        )
        for fn, value, field in checks:
            try:
                fn(value, field)
            except ItsmError as exc:
                errors.append(str(exc))
        parent_err = self._template_parent_consistency_error(eff_service, eff_offering)
        if parent_err:
            errors.append(parent_err)
        return errors

    def _name_exists(self, name: str, exclude_id: Optional[str] = None) -> bool:
        for tid, tmpl in self.db.incident_template.items():
            if tid == exclude_id:
                continue
            if tmpl.name == name:
                return True
        return False

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def create_new_incident_template(
        self,
        name: str,
        change_request_values: Dict[str, Any],
        active: Optional[bool] = None,
    ) -> IncidentTemplate:
        """Create a new incident template.

        The system generates ``incident_template_id`` (TMPL_001, TMPL_002, …) and timestamps.
        ``impact``/``urgency`` default to ``low`` and ``priority`` defaults to ``planning`` when
        omitted; ``active`` defaults to true. ``org_id`` is inherited from the acting user.

        Args:
            name: Template name (1-120 characters, must be unique).
            change_request_values: JSON object of template fields. Required keys: ``caller_id``
                and ``short_description``. Optional keys: ``channel``, ``category``, ``impact``,
                ``urgency``, ``priority``, ``configuration_item``, ``service``,
                ``service_offering``.
            active: Whether the template is active; defaults to true.

        Returns:
            The created incident template.
        """
        crv = change_request_values or {}
        # Enum validation first (the reference validates the request body before manager FK checks).
        self._validate_template_enums(crv)
        caller_id = crv.get("caller_id")
        short_description = crv.get("short_description")
        if caller_id is None or caller_id == "":
            raise ItsmError(
                "change_request_values must contain 'caller_id'",
                code="MISSING_FIELD",
                field="change_request_values.caller_id",
            )
        if short_description is None or short_description == "":
            raise ItsmError(
                "change_request_values must contain 'short_description'",
                code="MISSING_FIELD",
                field="change_request_values.short_description",
            )

        if self._name_exists(name):
            raise ItsmError(
                f"An incident template with name '{name}' already exists",
                code="DUPLICATE_TEMPLATE_NAME",
                field="name",
            )

        # Unknown change_request_values keys are rejected (live), after the name-unique check.
        self._check_unknown_crv_keys(crv)

        configuration_item = crv.get("configuration_item")
        service = crv.get("service")
        service_offering = crv.get("service_offering")
        # Aggregate all referenced-entity + parent-consistency failures into one error.
        errors = self._collect_template_ref_errors(
            caller_id, service, service_offering, configuration_item, service, service_offering
        )
        if errors:
            raise ItsmError("; ".join(errors), code="VALIDATION_ERROR")

        template_id, _ = self._make_id(self.db.incident_template, "TMPL")
        now = self._now()
        template = IncidentTemplate(
            incident_template_id=template_id,
            name=name,
            active=True if active is None else active,
            caller_id=caller_id,
            channel=crv.get("channel"),
            short_description=short_description,
            category=crv.get("category"),
            impact=crv.get("impact") or "low",
            urgency=crv.get("urgency") or "low",
            priority=crv.get("priority") or "planning",
            configuration_item=configuration_item,
            service=service,
            service_offering=service_offering,
            org_id=self._acting_org(),
            created_at=now,
            updated_at=now,
        )
        self.db.incident_template[template_id] = template
        return template

    @is_tool(ToolType.WRITE)
    def update_incident_template(
        self,
        incident_template_id: str,
        name: Optional[str] = None,
        change_request_values: Optional[Dict[str, Any]] = None,
        active: Optional[bool] = None,
    ) -> IncidentTemplate:
        """Update specific fields of an existing incident template.

        Empty strings ("") are ignored (the field is not updated). For the nullable fields
        (``channel``, ``category``, ``configuration_item``, ``service``, ``service_offering``)
        an explicit ``null`` clears the field. At least one field must carry a meaningful value,
        and updating a field to its current value raises a "no changes detected" error.

        Args:
            incident_template_id: Unique identifier of the template to update (required).
            name: Updated template name (1-120 chars, unique). Empty string = ignore.
            change_request_values: JSON object of fields to update. May include ``caller_id``,
                ``short_description``, ``channel``, ``category``, ``impact``, ``urgency``,
                ``priority``, ``configuration_item``, ``service``, ``service_offering``. Empty
                string ignores a field; ``null`` clears a nullable field.
            active: Updated active status; ``null`` = ignore, true/false = update.

        Returns:
            The updated incident template.
        """
        crv = change_request_values or {}
        # Enum validation first (the reference validates the request body before manager FK checks).
        self._validate_template_enums(crv)

        # Collect the proposed changes (post-empty-string-filtering). Each entry maps a record
        # attribute -> (new_value, is_tracked_for_no_change, format_style).
        # format_style: 'q' -> quoted repr (already 'x'), 'p' -> plain (already x).
        proposed: List[tuple] = []

        if name is not None and name != "":
            proposed.append(("name", name, True, "q"))

        # change_request_values scalar fields
        def crv_provided(key: str) -> bool:
            return key in crv and crv[key] != ""

        if crv_provided("caller_id"):
            proposed.append(("caller_id", crv["caller_id"], True, "q"))
        if crv_provided("short_description"):
            proposed.append(("short_description", crv["short_description"], True, "q"))
        # channel/category: empty string ignored; null clears; null is the only no-change-checked
        # state for these two, non-null values update without a no-change check.
        if "channel" in crv and crv["channel"] != "":
            checked = crv["channel"] is None
            proposed.append(("channel", crv["channel"], checked, "q"))
        if "category" in crv and crv["category"] != "":
            checked = crv["category"] is None
            proposed.append(("category", crv["category"], checked, "q"))
        # impact/urgency/priority: never subject to the no-change check.
        if crv_provided("impact"):
            proposed.append(("impact", crv["impact"], False, "p"))
        if crv_provided("urgency"):
            proposed.append(("urgency", crv["urgency"], False, "p"))
        if crv_provided("priority"):
            proposed.append(("priority", crv["priority"], False, "p"))
        # nullable FK fields: empty string ignored; null clears; no-change checked for any value.
        for key in ("configuration_item", "service", "service_offering"):
            if key in crv and crv[key] != "":
                proposed.append((key, crv[key], True, "p"))

        if active is not None:
            proposed.append(("active", active, True, "p"))

        if not proposed:
            raise ItsmError(
                "At least one field must be provided for update", code="VALIDATION_ERROR"
            )

        template = self.db.incident_template.get(incident_template_id)
        if template is None:
            raise ItsmError(
                f"Incident template not found with identifier '{incident_template_id}'",
                code="NOT_FOUND",
                field="incident_template_id",
            )

        # Unknown change_request_values keys are rejected (live), after the existence check.
        self._check_unknown_crv_keys(crv)

        # FK existence + service<->offering parent-consistency, aggregated in the reference's
        # order (caller_id, service, service_offering, configuration_item, then parent-check).
        # Only PROVIDED refs are existence-checked; the parent-check uses the effective values.
        prop = {attr: value for attr, value, _, _ in proposed}
        errors = self._collect_template_ref_errors(
            prop.get("caller_id"),
            prop.get("service"),
            prop.get("service_offering"),
            prop.get("configuration_item"),
            prop.get("service", template.service),
            prop.get("service_offering", template.service_offering),
        )
        if errors:
            raise ItsmError("; ".join(errors), code="VALIDATION_ERROR")

        # Duplicate-name check (excluding the template itself).
        for attr, value, _, _ in proposed:
            if attr == "name" and self._name_exists(value, exclude_id=incident_template_id):
                raise ItsmError(
                    f"An incident template with name '{value}' already exists",
                    code="DUPLICATE_TEMPLATE_NAME",
                    field="name",
                )

        # No-change detection: if every change actually applied would be a no-op, error. The
        # error only fires when nothing would change; a single real change lets the call proceed
        # (unchanged tracked fields are simply re-set to their value).
        changed_any = False
        unchanged_msgs: List[str] = []
        for attr, value, tracked, style in proposed:
            current = getattr(template, attr)
            if value == current:
                if tracked:
                    if style == "q":
                        unchanged_msgs.append(f"{attr} (already '{current}')")
                    else:
                        unchanged_msgs.append(f"{attr} (already {current})")
            else:
                changed_any = True

        if not changed_any:
            raise ItsmError(
                "No changes detected for fields: " + ", ".join(unchanged_msgs),
                code="NO_CHANGES_DETECTED",
            )

        for attr, value, _, _ in proposed:
            setattr(template, attr, value)
        template.updated_at = self._now()
        return template

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def get_incident_template_by_name(self, name: str) -> IncidentTemplate:
        """Retrieve an incident template by its exact (case-sensitive) name.

        Args:
            name: Incident template name (exact match, case-sensitive).

        Returns:
            The matching incident template.
        """
        for tmpl in self.db.incident_template.values():
            if tmpl.name == name:
                return tmpl
        raise ItsmError(
            f"Incident template not found with identifier '{name}'",
            code="NOT_FOUND",
            field="name",
        )

    @is_tool(ToolType.READ)
    def get_incident_templates(
        self,
        incident_template_id: Optional[str] = None,
        name: Optional[str] = None,
        active: Optional[bool] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List incident templates with optional filters (newest first).

        All provided filters are ANDed. ``created_after`` is strict (>) and ``created_before``
        is inclusive (<=). Results are ordered by ``created_at`` descending.

        Args:
            incident_template_id: Filter by exact template id.
            name: Filter by template name (exact match).
            active: Filter by active status (true or false).
            created_after: Only templates created strictly after this ISO timestamp.
            created_before: Only templates created on/before this ISO timestamp.

        Returns:
            A dict with 'incident_templates' (the matching templates, newest first) and
            'total_count'.
        """
        out: List[IncidentTemplate] = []
        for tmpl in self.db.incident_template.values():
            if incident_template_id is not None and tmpl.incident_template_id != incident_template_id:
                continue
            if name is not None and tmpl.name != name:
                continue
            if active is not None and tmpl.active != active:
                continue
            if created_after is not None and not ((tmpl.created_at or "") > created_after):
                continue
            if created_before is not None and not ((tmpl.created_at or "") <= created_before):
                continue
            out.append(tmpl)
        out.sort(key=lambda t: (t.created_at or ""), reverse=True)
        return {"incident_templates": out, "total_count": len(out)}
