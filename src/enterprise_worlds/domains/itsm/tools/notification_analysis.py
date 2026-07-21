"""Notification-analysis tools (4) — faithful port of the ITSM MCP's notification_analysis category.

Pure read/aggregate tools over the ``notification`` table: count by incident / status / type,
and the average notifications per incident.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class NotificationAnalysisToolsMixin(ItsmToolsBase):
    """Notification analytics tools (read-only aggregates)."""

    @is_tool(ToolType.READ)
    def count_notifications_by_incident(
        self, incident_id: Optional[List[str]] = None
    ) -> dict:
        """Count notifications grouped by incident.

        With no filter, every incident that has at least one notification is returned with its
        count. When ``incident_id`` is supplied, each requested incident is returned with its
        count (incidents with no notifications appear with a count of 0), in the order given.

        Args:
            incident_id: Optional list of incident ids to limit the count to.

        Returns:
            A dict ``{"metrics": {incident_id: count}}``.
        """
        if incident_id is not None:
            self._validate_incident_ids(incident_id)
            metrics: Dict[str, int] = {iid: 0 for iid in incident_id}
            wanted = set(incident_id)
            for notif in self.db.notification.values():
                if notif.incident_id in wanted:
                    metrics[notif.incident_id] += 1
        else:
            metrics = {}
            for notif in self.db.notification.values():
                metrics[notif.incident_id] = metrics.get(notif.incident_id, 0) + 1
        return {"metrics": metrics}

    @is_tool(ToolType.READ)
    def count_notifications_by_status(
        self, status: Optional[List[str]] = None
    ) -> dict:
        """Count notifications grouped by status.

        Args:
            status: Optional list of statuses to filter by. Accepted values: queued, sent,
                delivered, opened, failed.

        Returns:
            A dict ``{"metrics": {status: count}}``.
        """
        # The reference validates each enum-typed filter element before grouping.
        if status is not None:
            for value in status:
                self._check_enum("status", value, enums.NOTIFICATION_STATUS)
        return self._count_by_field("status", status)

    @is_tool(ToolType.READ)
    def count_notifications_by_type(
        self, type: Optional[List[str]] = None
    ) -> dict:
        """Count notifications grouped by type.

        Args:
            type: Optional list of notification types to filter by. Accepted values: report,
                update, alert, reminder, solution_proposal, other.

        Returns:
            A dict ``{"metrics": {type: count}}``.
        """
        # The reference validates each enum-typed filter element before grouping.
        if type is not None:
            for value in type:
                self._check_enum("type", value, enums.NOTIFICATION_TYPE)
        return self._count_by_field("type", type)

    @is_tool(ToolType.READ)
    def average_notifications_by_incident(
        self, incident_id: Optional[List[str]] = None
    ) -> dict:
        """Calculate the average number of notifications per incident.

        The average is taken over the incidents that have at least one notification (the divisor
        excludes incidents with zero notifications). Returns 0.0 when no in-scope incident has a
        notification. The result is rounded to two decimal places.

        Args:
            incident_id: Optional list of incident ids to limit the calculation to.

        Returns:
            A dict ``{"average": float}``.
        """
        if incident_id is not None:
            self._validate_incident_ids(incident_id)
        counts: Dict[str, int] = {}
        wanted = set(incident_id) if incident_id is not None else None
        for notif in self.db.notification.values():
            if wanted is not None and notif.incident_id not in wanted:
                continue
            counts[notif.incident_id] = counts.get(notif.incident_id, 0) + 1
        if not counts:
            return {"average": 0.0}
        total = sum(counts.values())
        average = round(total / len(counts), 2)
        return {"average": average}

    # ------------------------------------------------------------------ helpers
    def _validate_incident_ids(self, incident_id: List[str]) -> None:
        """Reject any incident_id reference that does not exist (batch FK gate).

        Mirrors the reference's ``_validate_incident_ids``: collects the unknown ids (deduped,
        preserving input order) and raises a single
        ``Invalid incident_id reference(s): <ids>`` error. An empty list passes (no references
        to validate), matching the reference's truthiness-based filter handling.
        """
        invalid: List[str] = []
        for iid in incident_id:
            if iid not in self.db.incident and iid not in invalid:
                invalid.append(iid)
        if invalid:
            raise ItsmError(
                "Invalid incident_id reference(s): " + ", ".join(invalid),
                code="INVALID_REFERENCE",
                field="incident_id",
            )

    def _count_by_field(self, field: str, values: Optional[List[str]]) -> dict:
        """Mirror the original MCP's status/type grouping (incl. its upper-case mismatch bug).

        With no filter the server returns an empty ``metrics`` map. With a filter it groups the
        notifications' (lower-case) field values but keys/compares against the upper-cased filter
        values, so each requested value comes back upper-cased with a count of 0.
        """
        if values is None:
            return {"metrics": {}}
        counts: Dict[str, int] = {}
        for notif in self.db.notification.values():
            stored = getattr(notif, field)
            if stored is not None:
                counts[stored] = counts.get(stored, 0) + 1
        metrics: Dict[str, int] = {}
        for value in values:
            metrics[value.upper()] = counts.get(value.upper(), 0)
        return {"metrics": metrics}
