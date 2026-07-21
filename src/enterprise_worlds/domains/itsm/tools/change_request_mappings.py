"""Change request mapping tools (5) — faithful port of the ITSM MCP's change_request_mappings.

Covers linking a change to an incident and/or problem (``map_change_request``), listing and
finding mappings, and filtered deletion.
"""

from __future__ import annotations

from typing import List, Optional

from enterprise_worlds.domains.itsm.data_model import ChangeRequestMapping
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class ChangeRequestMappingToolsMixin(ItsmToolsBase):
    """Change request mapping management tools."""

    # ------------------------------------------------------------------ helpers
    def _crm_require_change(self, change_id: str) -> None:
        if change_id not in self.db.change:
            raise ItsmError(f"Change '{change_id}' not found", field="change_id")

    def _crm_require_incident(self, incident_id: str) -> None:
        # Local existence check: the change-request-mapping manager raises
        # "Incident '<id>' not found" (no "with ID"), unlike _base._require_incident.
        if incident_id not in self.db.incident:
            raise ItsmError(f"Incident '{incident_id}' not found", field="incident_id")

    def _crm_require_problem(self, problem_id: str) -> None:
        if problem_id not in self.db.problem:
            raise ItsmError(f"Problem '{problem_id}' not found", field="problem_id")

    @staticmethod
    def _crm_sort_desc(
        mappings: List[ChangeRequestMapping],
    ) -> List[ChangeRequestMapping]:
        # Mirror the manager's order_by(created_at.desc()). ISO-8601 timestamps sort
        # lexicographically == chronologically; sorted() is stable for ties.
        return sorted(mappings, key=lambda m: m.created_at or "", reverse=True)

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def map_change_request(
        self,
        change_id: str,
        incident_id: Optional[str] = None,
        problem_id: Optional[str] = None,
    ) -> ChangeRequestMapping:
        """Create a mapping between a change and either an incident or a problem.

        At least one of ``incident_id`` or ``problem_id`` must be supplied; supplying both
        creates a single mapping row linking the change to both. The mapping is scoped to the
        caller's organization.

        Args:
            change_id: Change id to map (e.g. 'CHG_001'). Must exist.
            incident_id: Incident id to link (optional if problem_id supplied). Must exist.
            problem_id: Problem id to link (optional if incident_id supplied). Must exist.

        Returns:
            The created change request mapping.
        """
        if incident_id is None and problem_id is None:
            raise ItsmError(
                "Either incident_id or problem_id must be provided",
                field="incident_id",
            )

        # Existence validation: change first, then incident, then problem.
        self._crm_require_change(change_id)
        if incident_id is not None:
            self._crm_require_incident(incident_id)
        if problem_id is not None:
            self._crm_require_problem(problem_id)

        org_id = self._acting_org()

        # Duplicate detection, scoped to org: incident link first, then problem link.
        if incident_id is not None:
            for m in self.db.change_request_mapping.values():
                if (m.org_id == org_id and m.change_id == change_id
                        and m.incident_id == incident_id):
                    raise ItsmError(
                        f"Mapping for change '{change_id}' and incident '{incident_id}' "
                        f"already exists",
                        code="DUPLICATE_MAPPING",
                    )
        if problem_id is not None:
            for m in self.db.change_request_mapping.values():
                if (m.org_id == org_id and m.change_id == change_id
                        and m.problem_id == problem_id):
                    raise ItsmError(
                        f"Mapping for change '{change_id}' and problem '{problem_id}' "
                        f"already exists",
                        code="DUPLICATE_MAPPING",
                    )

        mapping_id, _ = self._make_id(self.db.change_request_mapping, "CRM")
        now = self._now()
        mapping = ChangeRequestMapping(
            change_request_mapping_id=mapping_id,
            change_id=change_id,
            incident_id=incident_id,
            problem_id=problem_id,
            org_id=org_id,
            created_at=now,
            updated_at=now,
        )
        self.db.change_request_mapping[mapping_id] = mapping
        return mapping

    @is_tool(ToolType.WRITE)
    def delete_change_request_mappings(
        self,
        change_request_mapping_id: Optional[str] = None,
        change_id: Optional[str] = None,
        incident_id: Optional[str] = None,
        problem_id: Optional[str] = None,
    ) -> dict:
        """Delete change request mappings matching the given filters (AND logic).

        At least one filter must be provided. When multiple filters are given, ALL must match
        (AND logic) to guard against accidental bulk deletion. Raises if no mapping matches.

        Args:
            change_request_mapping_id: Specific mapping id to delete (CRM_###).
            change_id: Delete mappings for this change.
            incident_id: Delete mappings linked to this incident.
            problem_id: Delete mappings linked to this problem.

        Returns:
            A summary with deleted_count, deleted_mappings, and a message.
        """
        if (change_request_mapping_id is None and change_id is None
                and incident_id is None and problem_id is None):
            raise ItsmError(
                "At least one filter must be provided",
                field="change_request_mapping_id",
            )

        matches = []
        for mid, m in self.db.change_request_mapping.items():
            if (change_request_mapping_id is not None
                    and m.change_request_mapping_id != change_request_mapping_id):
                continue
            if change_id is not None and m.change_id != change_id:
                continue
            if incident_id is not None and m.incident_id != incident_id:
                continue
            if problem_id is not None and m.problem_id != problem_id:
                continue
            matches.append((mid, m))

        if not matches:
            parts = []
            if change_request_mapping_id is not None:
                parts.append(f"change_request_mapping_id={change_request_mapping_id}")
            if change_id is not None:
                parts.append(f"change_id={change_id}")
            if incident_id is not None:
                parts.append(f"incident_id={incident_id}")
            if problem_id is not None:
                parts.append(f"problem_id={problem_id}")
            raise ItsmError(
                "No change request mappings found matching the provided parameters: "
                + ", ".join(parts)
            )

        deleted_mappings = [m.model_dump() for _, m in matches]
        for mid, _ in matches:
            del self.db.change_request_mapping[mid]

        if len(matches) == 1:
            message = (
                "Successfully deleted change request mapping "
                f"'{matches[0][1].change_request_mapping_id}'"
            )
        else:
            message = f"Successfully deleted {len(matches)} change request mapping(s)"

        return {
            "deleted_count": len(matches),
            "deleted_mappings": deleted_mappings,
            "message": message,
        }

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def list_change_request_mappings(
        self,
        change_request_mapping_id: Optional[str] = None,
        change_id: Optional[str] = None,
        incident_id: Optional[str] = None,
        problem_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List change request mappings, optionally filtered. All filters are ANDed.

        Omitting all filters returns every mapping. Results span all organizations.

        Args:
            change_request_mapping_id: Filter by exact mapping id (CRM_###).
            change_id: Filter by change id (CHG_###).
            incident_id: Filter by incident id (INC_###).
            problem_id: Filter by problem id (PRB_###).
            created_after: Only mappings created on/after this ISO timestamp.
            created_before: Only mappings created on/before this ISO timestamp.

        Returns:
            A dict with the matching mappings and a total_count.
        """
        out: List[ChangeRequestMapping] = []
        for m in self.db.change_request_mapping.values():
            if (change_request_mapping_id is not None
                    and m.change_request_mapping_id != change_request_mapping_id):
                continue
            if change_id is not None and m.change_id != change_id:
                continue
            if incident_id is not None and m.incident_id != incident_id:
                continue
            if problem_id is not None and m.problem_id != problem_id:
                continue
            if created_after is not None and (m.created_at or "") < created_after:
                continue
            if created_before is not None and (m.created_at or "") > created_before:
                continue
            out.append(m)
        out = self._crm_sort_desc(out)
        return {
            "change_request_mappings": out,
            "total_count": len(out),
        }

    @is_tool(ToolType.READ)
    def find_change_request_mappings_for_incident(self, incident_id: str) -> dict:
        """Retrieve all change request mappings tied to a specific incident.

        Args:
            incident_id: Incident id (INC_###) whose change mappings should be returned.

        Returns:
            A dict with the matching mappings and a total_count.
        """
        self._crm_require_incident(incident_id)
        out = self._crm_sort_desc(
            [m for m in self.db.change_request_mapping.values()
             if m.incident_id == incident_id]
        )
        return {
            "change_request_mappings": out,
            "total_count": len(out),
        }

    @is_tool(ToolType.READ)
    def find_change_request_mappings_for_problem(self, problem_id: str) -> dict:
        """Retrieve all change request mappings tied to a specific problem.

        Args:
            problem_id: Problem id (PRB_###) whose change mappings should be returned.

        Returns:
            A dict with the matching mappings and a total_count.
        """
        self._crm_require_problem(problem_id)
        out = self._crm_sort_desc(
            [m for m in self.db.change_request_mapping.values()
             if m.problem_id == problem_id]
        )
        return {
            "change_request_mappings": out,
            "total_count": len(out),
        }
