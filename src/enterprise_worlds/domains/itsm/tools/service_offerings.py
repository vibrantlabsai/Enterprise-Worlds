"""Service offering tools (4) — faithful port of the ITSM MCP's service_offering category.

Covers service-offering search/lookup, registration, and update.

Note on naming: the tool-facing API uses ``parent`` for what the schema stores as the
``business_service`` column.
"""

from __future__ import annotations

from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import ServiceOffering
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class ServiceOfferingToolsMixin(ItsmToolsBase):
    """Service offering management tools."""

    # ------------------------------------------------------------------ helpers
    def _validate_service_offering_enums(
        self,
        *,
        status=None,
        service_classification=None,
        business_criticality=None,
        used_for=None,
    ) -> None:
        """Reject out-of-set enum values, mirroring the reference's request-body enum gate."""
        self._check_enum("status", status, enums.SERVICE_STATUS)
        self._check_enum(
            "service_classification", service_classification, enums.SERVICE_CLASSIFICATION
        )
        self._check_enum(
            "business_criticality", business_criticality, enums.SERVICE_BUSINESS_CRITICALITY
        )
        self._check_enum("used_for", used_for, enums.SERVICE_USED_FOR)

    def _so_require_service(self, service_id: Optional[str], field: str = "parent") -> None:
        """Raise if a non-None service id does not exist (FK on ``business_service``)."""
        if service_id is not None and service_id not in self.db.service:
            raise ItsmError(
                f"Service with ID '{service_id}' not found",
                code="SERVICE_NOT_FOUND", field=field,
            )

    def _so_require_owner(self, user_id: Optional[str], field: str = "owned_by") -> None:
        """Raise if a non-None user id does not exist (FK on ``owned_by``)."""
        if user_id is not None and user_id not in self.db.users:
            raise ItsmError(
                f"User with ID '{user_id}' not found",
                code="USER_NOT_FOUND", field=field,
            )

    def _so_require_unique_name(
        self, name: str, exclude_id: Optional[str] = None, compare_to: Optional[str] = None
    ) -> None:
        """Raise if another service offering already uses the name.

        ``compare_to`` (defaults to ``name``) is the value matched against stored names — update
        passes the *stripped* name here so a whitespace-padded duplicate is still detected — while
        the raw ``name`` is echoed in the error message (mirrors the reference).
        """
        target = compare_to if compare_to is not None else name
        for so in self.db.service_offering.values():
            if so.name == target and so.service_offering_id != exclude_id:
                raise ItsmError(
                    f"A service offering with name '{name}' already exists",
                    code="DUPLICATE_SERVICE_OFFERING_NAME", field="name",
                )

    def _require_offering(self, service_offering_id: str) -> ServiceOffering:
        """Return the offering or raise a not-found error."""
        so = self.db.service_offering.get(service_offering_id)
        if so is None:
            raise ItsmError(
                f"Service Offering not found with identifier '{service_offering_id}'",
                code="NOT_FOUND",
            )
        return so

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def register_new_service_offering(
        self,
        name: str,
        owned_by: str,
        parent: str,
        short_description: str,
        used_for: Optional[str] = None,
        status: Optional[str] = None,
        service_classification: Optional[str] = None,
        business_criticality: Optional[str] = None,
    ) -> ServiceOffering:
        """Register a new service offering. The name must be unique.

        Args:
            name: Service offering name (required, unique).
            owned_by: Owner user id (required); must reference an existing user.
            parent: Parent business service id (required); must reference an existing service.
            short_description: Short description of the service offering (required).
            used_for: Purpose/usage (production, QA, test, development); defaults to 'production'.
            status: Status (operational, non-operational, repair_in_progress, ready, retired);
                defaults to 'operational'.
            service_classification: Classification (business, technology-management, application);
                defaults to 'business'.
            business_criticality: Criticality (critical, somewhat-critical, less-critical,
                not-critical); defaults to 'critical'.

        Returns:
            The created service offering.
        """
        # Enum validation first (the reference validates the request body before FK/uniqueness).
        self._validate_service_offering_enums(
            status=status, service_classification=service_classification,
            business_criticality=business_criticality, used_for=used_for,
        )
        self._so_require_owner(owned_by)
        self._so_require_service(parent)
        self._so_require_unique_name(name)

        offering_id, _ = self._make_id(self.db.service_offering, "SVCOFF")
        now = self._now()
        offering = ServiceOffering(
            service_offering_id=offering_id,
            name=name,
            short_description=short_description,
            owned_by=owned_by,
            business_service=parent,
            org_id=self._acting_org(),
            used_for=used_for or "production",
            status=status or "operational",
            service_classification=service_classification or "business",
            business_criticality=business_criticality or "critical",
            description=None,  # MCP never derives description on create (only the seed has it)
            created_on=now,
            updated_on=now,
        )
        self.db.service_offering[offering_id] = offering
        return offering

    @is_tool(ToolType.WRITE)
    def update_service_offering(
        self,
        service_offering_id: str,
        name: Optional[str] = None,
        owned_by: Optional[str] = None,
        used_for: Optional[str] = None,
        status: Optional[str] = None,
        service_classification: Optional[str] = None,
        business_criticality: Optional[str] = None,
        short_description: Optional[str] = None,
        parent: Optional[str] = None,
    ) -> ServiceOffering:
        """Update a service offering. Only fields you pass are changed; at least one is required.

        The name must remain unique if provided. Raises if every provided field already has its
        current value (idempotency validation).

        Args:
            service_offering_id: Id of the service offering to update (required).
            name: Updated service offering name (must stay unique).
            owned_by: Updated owner user id; must reference an existing user.
            used_for: Updated usage (production, QA, test, development).
            status: Updated status (operational, non-operational, repair_in_progress, ready,
                retired).
            service_classification: Updated classification (business, technology-management,
                application).
            business_criticality: Updated criticality (critical, somewhat-critical, less-critical,
                not-critical).
            short_description: Updated short description.
            parent: Updated parent business service id; must reference an existing service.

        Returns:
            The updated service offering.
        """
        # Enum validation first (the reference validates the request body before FK/existence).
        self._validate_service_offering_enums(
            status=status, service_classification=service_classification,
            business_criticality=business_criticality, used_for=used_for,
        )
        # map exposed arg name -> stored column name
        updates = {
            "name": name,
            "owned_by": owned_by,
            "used_for": used_for,
            "status": status,
            "service_classification": service_classification,
            "business_criticality": business_criticality,
            "short_description": short_description,
            "business_service": parent,
        }
        provided = {field: value for field, value in updates.items() if value is not None}
        if not provided:
            raise ItsmError("At least one field must be provided for update")

        # The reference strips the free-text columns before comparing/persisting (FK columns
        # owned_by/business_service are NOT stripped — they are validated against the raw value).
        for field in ("name", "short_description"):
            if field in provided and isinstance(provided[field], str):
                provided[field] = provided[field].strip()

        offering = self._require_offering(service_offering_id)

        # Name uniqueness compares the stripped value but echoes the raw input in the message.
        if name is not None:
            self._so_require_unique_name(
                name, exclude_id=service_offering_id, compare_to=provided["name"]
            )
        self._so_require_owner(owned_by)
        self._so_require_service(parent)

        if all(getattr(offering, field) == value for field, value in provided.items()):
            shown = ", ".join(f"{f}='{v}'" for f, v in provided.items())
            raise ItsmError(
                "No changes detected. All provided fields already have the same values: "
                f"{shown}. Please provide different values or omit unchanged fields.",
                code="NO_CHANGES_DETECTED",
            )

        # NOT NULL columns reject a value that is empty after stripping: the reference maps
        # ''/whitespace -> None and the DB rejects it (IntegrityError surfaced as INTERNAL_ERROR).
        for field in ("name", "short_description"):
            if provided.get(field) == "":
                raise ItsmError(
                    "Failed to update service offering: NOT NULL constraint failed: "
                    f"service_offering.{field}",
                    code="INTERNAL_ERROR", field=field,
                )

        for field, value in provided.items():
            setattr(offering, field, value)
        offering.updated_on = self._now()
        return offering

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def find_service_offering_by_name(self, name: str) -> ServiceOffering:
        """Find a service offering by its exact name.

        Args:
            name: The exact name of the service offering to find.

        Returns:
            The matching service offering.
        """
        # The reference strips the name before lookup (and echoes the stripped value on a miss).
        stripped = name.strip()
        for so in self.db.service_offering.values():
            if so.name == stripped:
                return so
        raise ItsmError(
            f"Service Offering not found with identifier '{stripped}'", code="NOT_FOUND",
        )

    @is_tool(ToolType.READ)
    def find_service_offerings(
        self,
        service_offering_id: Optional[str] = None,
        name: Optional[str] = None,
        owned_by: Optional[str] = None,
        used_for: Optional[str] = None,
        status: Optional[str] = None,
        service_classification: Optional[str] = None,
        business_criticality: Optional[str] = None,
        short_description: Optional[str] = None,
        parent: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List service offerings, optionally filtered. All filters are ANDed; omitted ignored.

        ``name``, ``short_description``, ``owned_by`` and ``parent`` are partial,
        case-insensitive matches; the remaining filters are exact. ``parent`` matches the stored
        business service.

        Args:
            service_offering_id: Filter by service offering id (exact match).
            name: Filter by name (partial, case-insensitive).
            owned_by: Filter by owner user id (partial, case-insensitive).
            used_for: Filter by usage (production, QA, test, development).
            status: Filter by status (operational, non-operational, repair_in_progress, ready,
                retired).
            service_classification: Filter by classification (business, technology-management,
                application).
            business_criticality: Filter by criticality (critical, somewhat-critical,
                less-critical, not-critical).
            short_description: Filter by short description (partial, case-insensitive).
            parent: Filter by parent business service id (partial, case-insensitive).
            created_after: Only offerings created strictly after this ISO 8601 timestamp.
            created_before: Only offerings created on/before this ISO 8601 timestamp.

        Returns:
            A dict with the matching offerings under 'service_offerings' (created_on-descending)
            and their 'total_count'.
        """
        # The reference validates enum-typed filters too (invalid value -> error, not empty result).
        self._validate_service_offering_enums(
            status=status, service_classification=service_classification,
            business_criticality=business_criticality, used_for=used_for,
        )
        # exact-match filters mapped to stored attribute names (id + enum-typed columns)
        eq_filters = {
            "service_offering_id": service_offering_id,
            "used_for": used_for,
            "status": status,
            "service_classification": service_classification,
            "business_criticality": business_criticality,
        }
        active = {k: v for k, v in eq_filters.items() if v is not None}
        out: List[ServiceOffering] = []
        for so in self.db.service_offering.values():
            if any(getattr(so, attr) != val for attr, val in active.items()):
                continue
            # owned_by / parent are substring (LIKE), case-insensitive — not exact.
            if name is not None and name.lower() not in (so.name or "").lower():
                continue
            if short_description is not None and (
                short_description.lower() not in (so.short_description or "").lower()
            ):
                continue
            if owned_by is not None and owned_by.lower() not in (so.owned_by or "").lower():
                continue
            if parent is not None and parent.lower() not in (so.business_service or "").lower():
                continue
            # created_after is exclusive of the boundary; created_before is inclusive (live server).
            if created_after is not None and (so.created_on or "") <= created_after:
                continue
            if created_before is not None and (so.created_on or "") > created_before:
                continue
            out.append(so)
        # Mirror the reference's ``ORDER BY created_on DESC``; ties fall back to id-descending so
        # ordering stays deterministic where timestamps coincide.
        out.sort(key=lambda s: (s.created_on or "", s.service_offering_id), reverse=True)
        return {"service_offerings": out, "total_count": len(out)}
