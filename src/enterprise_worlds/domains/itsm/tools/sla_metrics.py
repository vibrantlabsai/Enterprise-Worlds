"""SLA metric tools (2) — faithful port of the ITSM MCP's sla_metrics category.

Covers aggregations over ``sla_definition.target_mins`` grouped by priority; both tools
return ``{"metrics": {<PRIORITY>: value}}`` keyed by the uppercased requested priority.
"""

from __future__ import annotations

from typing import List, Optional

from enterprise_worlds.domains.itsm.enums import SLA_APPLIES_TO_PRIORITY
from enterprise_worlds.domains.itsm.tools._base import ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class SLAMetricToolsMixin(ItsmToolsBase):
    """SLA metric aggregation tools."""

    def _validate_priorities(self, priority: Optional[List[str]]) -> None:
        """Reject any requested priority outside the canonical SLA set.

        The reference validates ``priority`` at the request boundary (case-sensitive) against the
        SLA priority enum ``{critical, high, moderate, low}`` and rejects anything else (e.g.
        ``urgent``, ``planning``, or a wrong-case ``CRITICAL``) before computing any metric. An
        omitted/empty list is allowed (no items to validate).
        """
        for p in priority or []:
            self._check_enum("priority", p, SLA_APPLIES_TO_PRIORITY)

    def _target_mins_for(self, priority_upper: str) -> List[int]:
        """target_mins of every sla_definition whose applies_to_priority equals the key.

        The MCP compares the (uppercased) requested priority directly against the stored
        ``applies_to_priority`` value, so seeds that store lowercase priorities yield no matches.
        """
        return [
            sla.target_mins
            for sla in self.db.sla_definition.values()
            if sla.applies_to_priority == priority_upper
        ]

    @is_tool(ToolType.READ)
    def total_sum_of_target_mins_per_priority(
        self, priority: Optional[List[str]] = None
    ) -> dict:
        """Compute the total sum of target minutes grouped by priority.

        Args:
            priority: Optional list of priorities to filter by. Accepted values: critical, high,
                moderate, low (case-sensitive; any other value is rejected). When omitted, the
                metrics object is empty.

        Returns:
            An object ``{"metrics": {<PRIORITY>: sum}}`` keyed by the uppercased priority, where
            each value is the summed target minutes of matching SLA definitions.
        """
        self._validate_priorities(priority)
        metrics: dict = {}
        for p in priority or []:
            key = p.upper()
            metrics[key] = sum(self._target_mins_for(key))
        return {"metrics": metrics}

    @is_tool(ToolType.READ)
    def average_target_mins_per_priority(
        self, priority: Optional[List[str]] = None
    ) -> dict:
        """Compute the average of target minutes grouped by priority.

        Args:
            priority: Optional list of priorities to filter by. Accepted values: critical, high,
                moderate, low (case-sensitive; any other value is rejected). When omitted, the
                metrics object is empty.

        Returns:
            An object ``{"metrics": {<PRIORITY>: average}}`` keyed by the uppercased priority,
            where each value is the average target minutes of matching SLA definitions rounded to
            two decimal places (0.0 when there are no matches).
        """
        self._validate_priorities(priority)
        metrics: dict = {}
        for p in priority or []:
            key = p.upper()
            values = self._target_mins_for(key)
            metrics[key] = round(sum(values) / len(values), 2) if values else 0.0
        return {"metrics": metrics}
