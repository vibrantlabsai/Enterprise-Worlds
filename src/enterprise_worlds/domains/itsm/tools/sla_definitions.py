"""SLA definition tools (4) — faithful port of the ITSM MCP's sla_definitions category.

Covers SLA-definition create, lookup-by-name, filtered list, and update.
"""

from __future__ import annotations

from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import SLADefinition
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class SLADefinitionToolsMixin(ItsmToolsBase):
    """SLA definition management tools."""

    # ------------------------------------------------------------------ helpers
    def _validate_sla_def_enums(
        self,
        *,
        metric=None,
        applies_to_priority=None,
    ) -> None:
        """Reject out-of-set enum values, mirroring the reference's request-body enum gate."""
        self._check_enum("metric", metric, enums.SLA_METRIC)
        self._check_enum("applies_to_priority", applies_to_priority, enums.SLA_APPLIES_TO_PRIORITY)

    def _sla_require_unique_name(self, name: str, exclude_id: Optional[str] = None) -> None:
        """Raise if another SLA definition (any org) already uses this exact name."""
        for sla in self.db.sla_definition.values():
            if sla.sla_def_id == exclude_id:
                continue
            if sla.name == name:
                raise ItsmError(
                    f"SLA definition with name '{name}' already exists",
                    code="DUPLICATE_NAME",
                    field="name",
                )

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def add_new_sla_definition(
        self,
        name: str,
        metric: str,
        target_mins: int,
        pause_on_pending: bool,
        active: bool,
        applies_to_priority: Optional[str] = None,
        schedule: Optional[str] = None,
    ) -> SLADefinition:
        """Create a new SLA definition.

        Args:
            name: Unique name for the SLA definition (<=120 chars).
            metric: SLA metric, either 'response' or 'resolution'.
            target_mins: Target time in minutes (>=1).
            pause_on_pending: Whether the SLA pauses on pending status.
            active: Whether the SLA definition is active.
            applies_to_priority: Priority this SLA applies to (critical, high, moderate, low).
            schedule: Schedule id (<=32 chars).

        Returns:
            The created SLA definition.
        """
        # Enum validation first (the reference validates the request body before FK/uniqueness checks).
        self._validate_sla_def_enums(metric=metric, applies_to_priority=applies_to_priority)
        self._sla_require_unique_name(name)

        sla_def_id, _ = self._make_id(self.db.sla_definition, "SLA")
        now = self._now()
        sla = SLADefinition(
            sla_def_id=sla_def_id,
            name=name,
            metric=metric,
            target_mins=target_mins,
            pause_on_pending=pause_on_pending,
            applies_to_priority=applies_to_priority,
            active=active,
            schedule=schedule,
            org_id=self._acting_org(),
            created_on=now,
            updated_on=now,
        )
        self.db.sla_definition[sla_def_id] = sla
        return sla

    @is_tool(ToolType.WRITE)
    def update_sla_definition(
        self,
        sla_def_id: str,
        name: Optional[str] = None,
        metric: Optional[str] = None,
        target_mins: Optional[int] = None,
        pause_on_pending: Optional[bool] = None,
        active: Optional[bool] = None,
        applies_to_priority: Optional[str] = None,
        schedule: Optional[str] = None,
    ) -> SLADefinition:
        """Update an existing SLA definition by id. Only the fields you pass are changed.

        An update that supplies no fields at all is rejected. If only non-enum fields are supplied
        and they all already equal their current values, no change is made and an error is raised;
        supplying an enum field (metric/applies_to_priority) always counts as a change. Renaming to
        a name used by a different SLA definition is rejected.

        Args:
            sla_def_id: Id of the SLA definition to update (required).
            name: New unique name for the SLA definition (<=120 chars).
            metric: SLA metric, either 'response' or 'resolution'.
            target_mins: Target time in minutes (>=1).
            pause_on_pending: Whether the SLA pauses on pending status.
            active: Whether the SLA definition is active.
            applies_to_priority: Priority this SLA applies to (critical, high, moderate, low).
            schedule: Schedule id (<=32 chars).

        Returns:
            The updated SLA definition.
        """
        self._validate_sla_def_enums(metric=metric, applies_to_priority=applies_to_priority)

        # The reference rejects an update that carries no field at all, and does so before the
        # not-found lookup (router-level guard returns "No fields provided for update").
        if all(
            v is None
            for v in (
                name, metric, target_mins, pause_on_pending, active, applies_to_priority, schedule
            )
        ):
            raise ItsmError("No fields provided for update")

        sla = self.db.sla_definition.get(sla_def_id)
        if sla is None:
            raise ItsmError(
                f"SLA definition not found with identifier '{sla_def_id}'",
                code="NOT_FOUND",
                field="sla_def_id",
            )

        if name is not None and name != sla.name:
            self._sla_require_unique_name(name, exclude_id=sla_def_id)

        # No-change detection considers ONLY these fields, in this fixed order (mirrors the
        # reference manager's field-processing order: strings name/schedule first, then the
        # numeric/boolean fields). The enum fields (metric/applies_to_priority) are deliberately
        # excluded from the comparison: the reference always re-applies them and treats their mere
        # presence as a change, so an update that supplies an enum field never trips the guard.
        tracked = (
            ("name", name),
            ("schedule", schedule),
            ("target_mins", target_mins),
            ("pause_on_pending", pause_on_pending),
            ("active", active),
        )
        changes_made = False
        unchanged_fields: List[str] = []
        for field, value in tracked:
            if value is None:
                continue
            if getattr(sla, field) != value:
                setattr(sla, field, value)
                changes_made = True
            else:
                unchanged_fields.append(f"{field} (already {getattr(sla, field)!r})")

        for field, value in (("metric", metric), ("applies_to_priority", applies_to_priority)):
            if value is not None:
                setattr(sla, field, value)
                changes_made = True

        if not changes_made and unchanged_fields:
            raise ItsmError(
                f"No changes detected for fields: {', '.join(unchanged_fields)}",
                code="NO_CHANGES_DETECTED",
            )

        if changes_made:
            sla.updated_on = self._now()
        return sla

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def find_sla_definition_by_name(self, name: str) -> SLADefinition:
        """Return the SLA definition with the given unique name.

        Args:
            name: Name of the SLA definition.

        Returns:
            The matching SLA definition.
        """
        for sla in self.db.sla_definition.values():
            if sla.name == name:
                return sla
        raise ItsmError(
            f"SLA definition not found with identifier '{name}'",
            code="NOT_FOUND",
            field="name",
        )

    @is_tool(ToolType.READ)
    def find_sla_definitions(
        self,
        sla_def_id: Optional[str] = None,
        name: Optional[str] = None,
        metric: Optional[str] = None,
        target_mins: Optional[int] = None,
        pause_on_pending: Optional[bool] = None,
        applies_to_priority: Optional[str] = None,
        active: Optional[bool] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List SLA definitions, applying optional filters. Returns all if no filters given.

        Empty strings and nulls on the free-text filters are ignored (return all without
        filtering); the enum filters (metric/applies_to_priority) are validated and reject any
        value outside their canonical set (including the empty string). Booleans filter on
        true/false. A ``target_mins`` of 0 is treated as no filter (out-of-range sentinel).
        ``created_after`` is exclusive; ``created_before`` is inclusive.

        Args:
            sla_def_id: Filter by sla_def_id.
            name: Filter by name.
            metric: Filter by metric ('response' or 'resolution').
            target_mins: Filter by target_mins.
            pause_on_pending: Filter by pause_on_pending.
            applies_to_priority: Filter by applies_to_priority (critical, high, moderate, low).
            active: Filter by active status.
            created_after: ISO timestamp; return SLA definitions created strictly after this value.
            created_before: ISO timestamp; return SLA definitions created on/before this value.

        Returns:
            A dict with the matching ``sla_definitions`` list and their ``total_count``.
        """
        # The reference validates enum-typed filters at the request boundary and rejects any value
        # outside the canonical set (uppercase, junk, or the empty string) before querying.
        self._validate_sla_def_enums(metric=metric, applies_to_priority=applies_to_priority)

        eq_filters = {
            "sla_def_id": sla_def_id,
            "name": name,
            "metric": metric,
            "target_mins": target_mins,
            "pause_on_pending": pause_on_pending,
            "applies_to_priority": applies_to_priority,
            "active": active,
        }
        active_filters = {}
        for k, v in eq_filters.items():
            if v is None or v == "":
                continue
            # The reference's should_apply_filter drops an integer 0 (out-of-range sentinel) so a
            # target_mins=0 filter is ignored. Booleans (a subclass of int) must still apply.
            if isinstance(v, int) and not isinstance(v, bool) and v == 0:
                continue
            active_filters[k] = v

        out: List[SLADefinition] = []
        for sla in self.db.sla_definition.values():
            if any(getattr(sla, attr) != val for attr, val in active_filters.items()):
                continue
            if created_after not in (None, "") and (sla.created_on or "") <= created_after:
                continue
            if created_before not in (None, "") and (sla.created_on or "") > created_before:
                continue
            out.append(sla)

        return {"sla_definitions": out, "total_count": len(out)}
