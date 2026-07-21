"""Incident affected-CI tools (3) — faithful port of the ITSM MCP's incident_affected_cis category.

Covers linking a configuration item to an incident (creating a ``TASKCI_xxx`` mapping), listing
those mappings with optional filters, and bulk-removing them by filter.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from enterprise_worlds.domains.itsm.data_model import IncidentAffectedCI
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class IncidentAffectedCIToolsMixin(ItsmToolsBase):
    """Incident affected configuration-item (CI) management tools."""

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _iac_parse_dt(value: str) -> Optional[datetime]:
        """Parse an ISO timestamp (``T`` or space separated) into a datetime, or None."""
        try:
            return datetime.fromisoformat(value.replace(" ", "T"))
        except (ValueError, AttributeError):
            return None

    def _matches(
        self,
        mapping: IncidentAffectedCI,
        incident_affected_cis_id: Optional[str],
        configuration_item: Optional[str],
        incident_id: Optional[str],
        created_before: Optional[str],
        created_after: Optional[str],
    ) -> bool:
        """Whether a mapping satisfies all supplied (non-None) filters (ANDed)."""
        if (
            incident_affected_cis_id is not None
            and mapping.incident_affected_cis_id != incident_affected_cis_id
        ):
            return False
        if configuration_item is not None and mapping.configuration_item != configuration_item:
            return False
        if incident_id is not None and mapping.incident_id != incident_id:
            return False
        # created_after is strictly greater; created_before is inclusive.
        mapping_dt = self._iac_parse_dt(mapping.created_on) if mapping.created_on else None
        if created_after is not None:
            bound = self._iac_parse_dt(created_after)
            if bound is not None and (mapping_dt is None or not (mapping_dt > bound)):
                return False
        if created_before is not None:
            bound = self._iac_parse_dt(created_before)
            if bound is not None and (mapping_dt is None or not (mapping_dt <= bound)):
                return False
        return True

    @staticmethod
    def _identifier_str(
        incident_affected_cis_id: Optional[str],
        incident_id: Optional[str],
        configuration_item: Optional[str],
        created_before: Optional[str],
        created_after: Optional[str],
    ) -> str:
        """Build the filter identifier string used in the MCP's NOT_FOUND error message."""
        parts = []
        if incident_affected_cis_id is not None:
            parts.append(f"incident_affected_cis_id='{incident_affected_cis_id}'")
        if incident_id is not None:
            parts.append(f"incident_id='{incident_id}'")
        if configuration_item is not None:
            parts.append(f"configuration_item='{configuration_item}'")
        if created_before is not None:
            parts.append(f"created_before='{created_before}'")
        if created_after is not None:
            parts.append(f"created_after='{created_after}'")
        return ", ".join(parts)

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def link_affected_ci_to_incident(
        self, configuration_item: str, incident_id: str
    ) -> IncidentAffectedCI:
        """Attach a configuration item to an incident, creating a TASKCI_xxx link.

        Args:
            configuration_item: Existing CI id within the caller's organization (e.g., 'CI_001').
            incident_id: Existing incident id within the same org (e.g., 'INC_001').

        Returns:
            The created incident-CI mapping.
        """
        if configuration_item not in self.db.configuration_item:
            raise ItsmError(
                f"Configuration item '{configuration_item}' not found",
                field="configuration_item",
            )
        if incident_id not in self.db.incident:
            raise ItsmError(f"Incident '{incident_id}' not found", field="incident_id")
        for m in self.db.incident_affected_cis.values():
            if m.configuration_item == configuration_item and m.incident_id == incident_id:
                raise ItsmError(
                    f"Configuration item '{configuration_item}' is already linked to "
                    f"incident '{incident_id}'",
                    field="configuration_item",
                )

        mapping_id, _ = self._make_id(self.db.incident_affected_cis, "TASKCI")
        now = self._now()
        mapping = IncidentAffectedCI(
            incident_affected_cis_id=mapping_id,
            configuration_item=configuration_item,
            incident_id=incident_id,
            org_id=self._acting_org(),
            created_on=now,
            updated_on=now,
        )
        self.db.incident_affected_cis[mapping_id] = mapping
        return mapping

    @is_tool(ToolType.WRITE)
    def remove_affected_ci_from_incident(
        self,
        incident_affected_cis_id: Optional[str] = None,
        configuration_item: Optional[str] = None,
        incident_id: Optional[str] = None,
        created_before: Optional[str] = None,
        created_after: Optional[str] = None,
    ) -> dict:
        """Delete incident-CI links matching the given filters (at least one required).

        Args:
            incident_affected_cis_id: Specific mapping id to delete (TASKCI_###).
            configuration_item: Remove mappings for this CI.
            incident_id: Remove mappings for this incident.
            created_before: Delete mappings created on/before this ISO timestamp.
            created_after: Delete mappings created strictly after this ISO timestamp.

        Returns:
            A summary with the number of mappings removed.
        """
        # A no-filter call is rejected (the reference requires >=1 filter); otherwise an empty
        # call would match — and delete — every mapping.
        if all(
            v is None
            for v in (incident_affected_cis_id, configuration_item, incident_id,
                      created_before, created_after)
        ):
            raise ItsmError("At least one filter must be provided", code="VALIDATION_ERROR")
        to_remove = [
            mid
            for mid, m in self.db.incident_affected_cis.items()
            if self._matches(
                m, incident_affected_cis_id, configuration_item, incident_id,
                created_before, created_after,
            )
        ]
        if not to_remove:
            identifier = self._identifier_str(
                incident_affected_cis_id, incident_id, configuration_item,
                created_before, created_after,
            )
            raise ItsmError(
                f"Incident affected CI mapping not found with identifier '{identifier}'",
                code="NOT_FOUND",
            )
        for mid in to_remove:
            del self.db.incident_affected_cis[mid]
        return {"deleted_count": len(to_remove)}

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def list_incident_affected_cis(
        self,
        incident_affected_cis_id: Optional[str] = None,
        configuration_item: Optional[str] = None,
        incident_id: Optional[str] = None,
        created_before: Optional[str] = None,
        created_after: Optional[str] = None,
    ) -> dict:
        """List configuration items linked to incidents. All filters optional (ANDed).

        Args:
            incident_affected_cis_id: Specific mapping id (TASKCI_###).
            configuration_item: Filter by CI id.
            incident_id: Filter by incident id.
            created_before: Return mappings created on/before this ISO timestamp.
            created_after: Return mappings created strictly after this ISO timestamp.

        Returns:
            A dict with the matching mappings (newest first) and the total count.
        """
        matches = [
            m
            for m in self.db.incident_affected_cis.values()
            if self._matches(
                m, incident_affected_cis_id, configuration_item, incident_id,
                created_before, created_after,
            )
        ]
        matches.sort(key=lambda m: (m.created_on or ""), reverse=True)
        return {
            "incident_affected_cis": matches,
            "total_count": len(matches),
        }
