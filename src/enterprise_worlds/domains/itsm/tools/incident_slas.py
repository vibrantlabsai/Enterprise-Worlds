"""Incident SLA tools (5) — faithful port of the ITSM MCP's incident_slas category.

Covers incident-SLA CRUD/search plus the stage-wise breached count aggregate. The
``incident_sla`` table links an incident to an SLA definition and tracks its lifecycle stage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import IncidentSLA
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class IncidentSLAToolsMixin(ItsmToolsBase):
    """Incident SLA management tools."""

    # ------------------------------------------------------------------ helpers
    def _normalize_stage(self, stage: Optional[str]) -> Optional[str]:
        """Normalize then validate an incident-SLA stage, returning the canonical value.

        The reference normalizes case for this one field (e.g. 'Breached' -> 'breached'), so we
        strip()/lower() before validating against ``enums.INCIDENT_SLA_STAGE`` and storing the
        normalized lowercase value. ``None`` (field not supplied) passes through unchanged.
        """
        if stage is None:
            return None
        stage = stage.strip().lower()
        self._check_enum("stage", stage, enums.INCIDENT_SLA_STAGE)
        return stage

    def _require_sla_def(self, sla_def_id: str) -> None:
        if sla_def_id not in self.db.sla_definition:
            raise ItsmError(f"SLA definition '{sla_def_id}' not found", field="sla_def_id")

    def _require_incident_sla(self, incident_sla_id: str) -> IncidentSLA:
        record = self.db.incident_sla.get(incident_sla_id)
        if record is None:
            raise ItsmError(
                f"Incident SLA '{incident_sla_id}' not found", field="incident_sla_id"
            )
        return record

    def _require_incident_fk(self, incident_id: str) -> None:
        """Incident FK existence with the reference's exact message.

        The shared ``_require_incident`` raises ``Incident with ID '<id>' not found``; the
        incident-SLA manager raises ``Incident '<id>' not found`` (no "with ID"). We keep a local
        check so this category matches the reference wording without touching the shared helper.
        """
        if incident_id not in self.db.incident:
            raise ItsmError(f"Incident '{incident_id}' not found", field="incident_id")

    @staticmethod
    def _normalize_sla_ts(value: Optional[str]) -> Optional[str]:
        """Normalize a stored ISO timestamp to naive-UTC isoformat, mirroring the reference.

        The reference parses every persisted timestamp and converts it to naive UTC: a trailing
        'Z' or an explicit offset is converted to UTC and the tzinfo dropped (e.g.
        '2024-05-02T08:30:00+05:30' -> '2024-05-02T03:00:00'; '...Z' -> the same instant without
        'Z'). Naive inputs round-trip unchanged. ``None`` passes through; an unparseable value is
        returned verbatim (the live schema layer rejects those upstream, so this never persists junk
        beyond what the port already accepted).
        """
        if value is None:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.isoformat()

    @staticmethod
    def _render_sla_filter_ts(value: str) -> str:
        """Render a filter timestamp the way the reference's not-found identifier does.

        Unlike persisted values, delete-filter timestamps are parsed by the request schema and
        stringified WITHOUT UTC conversion: tz-aware inputs keep their offset and the 'T' becomes a
        space (e.g. '2020-01-01T00:00:00+05:30' -> '2020-01-01 00:00:00+05:30'). Unparseable values
        are returned verbatim.
        """
        try:
            return str(datetime.fromisoformat(value))
        except (ValueError, TypeError):
            return value

    def _delete_not_found_identifier(
        self,
        incident_sla_id: Optional[str],
        incident_id: Optional[str],
        sla_def_id: Optional[str],
        stage: Optional[str],
        has_breached: Optional[bool],
        created_before: Optional[str],
        created_after: Optional[str],
    ) -> str:
        """Build the comma-joined ``key=value`` identifier for a zero-match delete error.

        Mirrors the reference: provided filters are rendered in signature order using ``repr`` of
        the (raw) value — so strings keep their quotes, booleans render as ``True``/``False``, and
        the un-normalized ``stage`` is shown verbatim. Timestamps are parsed/stringified via
        :meth:`_render_sla_filter_ts` before the ``repr``.
        """
        parts: List[str] = []
        for key, value in (
            ("incident_sla_id", incident_sla_id),
            ("incident_id", incident_id),
            ("sla_def_id", sla_def_id),
            ("stage", stage),
            ("has_breached", has_breached),
            (
                "created_before",
                self._render_sla_filter_ts(created_before) if created_before is not None else None,
            ),
            (
                "created_after",
                self._render_sla_filter_ts(created_after) if created_after is not None else None,
            ),
        ):
            if value is not None:
                parts.append(f"{key}={value!r}")
        return ", ".join(parts)

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def link_new_incident_sla(
        self,
        incident_id: str,
        sla_def_id: str,
        start_time: Optional[str] = None,
        stage: Optional[str] = None,
        has_breached: Optional[bool] = None,
        breach_time: Optional[str] = None,
        completed_time: Optional[str] = None,
    ) -> IncidentSLA:
        """Create a new incident SLA record linking an incident to an SLA definition.

        Args:
            incident_id: Incident identifier the SLA applies to (required).
            sla_def_id: SLA definition identifier (required).
            start_time: Start time of the SLA, ISO timestamp. Defaults to the current time
                (the env clock) when omitted, since a newly linked SLA starts now.
            stage: Lifecycle stage (in_progress, paused, completed, cancelled, breached);
                defaults to 'in_progress'.
            has_breached: Whether the SLA has breached; defaults to False.
            breach_time: Breach time, ISO timestamp.
            completed_time: Completion time, ISO timestamp.

        Returns:
            The created incident SLA record.
        """
        # An explicit empty start_time is rejected (the reference requires a value); an omitted
        # start_time still auto-stamps from the env clock (see default handling below).
        if start_time is not None and not start_time.strip():
            raise ItsmError("start_time cannot be empty", field="start_time")
        # Order mirrors the original MCP: incident FK -> sla_def FK -> uniqueness -> stage.
        self._require_incident_fk(incident_id)
        self._require_sla_def(sla_def_id)
        org_id = self._acting_org()
        for record in self.db.incident_sla.values():
            if (
                record.org_id == org_id
                and record.incident_id == incident_id
                and record.sla_def_id == sla_def_id
            ):
                raise ItsmError(
                    f"SLA '{sla_def_id}' is already linked to incident '{incident_id}'",
                    field="sla_def_id",
                )
        stage = self._normalize_stage(stage)
        # The reference normalizes every persisted timestamp to naive UTC (see _normalize_sla_ts).
        start_time = self._normalize_sla_ts(start_time)
        breach_time = self._normalize_sla_ts(breach_time)
        completed_time = self._normalize_sla_ts(completed_time)

        incident_sla_id, _ = self._make_id(self.db.incident_sla, "TSLA")
        now = self._now()
        record = IncidentSLA(
            incident_sla_id=incident_sla_id,
            incident_id=incident_id,
            sla_def_id=sla_def_id,
            org_id=org_id,
            stage=stage or "in_progress",
            has_breached=has_breached if has_breached is not None else False,
            start_time=start_time if start_time is not None else now,
            breach_time=breach_time,
            completed_time=completed_time,
            created_on=now,
            updated_on=now,
        )
        self.db.incident_sla[incident_sla_id] = record
        return record

    @is_tool(ToolType.WRITE)
    def update_incident_sla_details(
        self,
        incident_sla_id: str,
        incident_id: Optional[str] = None,
        sla_def_id: Optional[str] = None,
        stage: Optional[str] = None,
        has_breached: Optional[bool] = None,
        start_time: Optional[str] = None,
        breach_time: Optional[str] = None,
        completed_time: Optional[str] = None,
    ) -> IncidentSLA:
        """Update an existing incident SLA record. Only the fields you pass are changed.

        Args:
            incident_sla_id: Id of the incident SLA record to update (required).
            incident_id: New incident identifier.
            sla_def_id: New SLA definition identifier.
            stage: New lifecycle stage (in_progress, paused, completed, cancelled, breached).
            has_breached: Update the breach flag.
            start_time: New start time, ISO timestamp.
            breach_time: New breach time, ISO timestamp.
            completed_time: New completion time, ISO timestamp.

        Returns:
            The updated incident SLA record.
        """
        # Order mirrors the original MCP: record existence -> required-field -> FK -> uniqueness ->
        # no-op detection. A pure-id call (no updatable field) and a call that changes nothing are
        # both rejected, matching the reference.
        record = self._require_incident_sla(incident_sla_id)
        stage = self._normalize_stage(stage)
        # Normalize timestamps to naive UTC (matches the reference) BEFORE no-op detection so a
        # re-supplied equivalent timestamp (e.g. '...Z') is correctly seen as "no change".
        start_time = self._normalize_sla_ts(start_time)
        breach_time = self._normalize_sla_ts(breach_time)
        completed_time = self._normalize_sla_ts(completed_time)
        updates = {
            "incident_id": incident_id,
            "sla_def_id": sla_def_id,
            "stage": stage,
            "has_breached": has_breached,
            "start_time": start_time,
            "breach_time": breach_time,
            "completed_time": completed_time,
        }
        provided = {k: v for k, v in updates.items() if v is not None}
        if not provided:
            raise ItsmError("At least one field must be provided for update")
        if incident_id is not None:
            self._require_incident_fk(incident_id)
        if sla_def_id is not None:
            self._require_sla_def(sla_def_id)
        # Re-check the (org, incident, sla_def) uniqueness if either side of the pair changes.
        if incident_id is not None or sla_def_id is not None:
            new_incident = incident_id or record.incident_id
            new_sla_def = sla_def_id or record.sla_def_id
            for other in self.db.incident_sla.values():
                if (
                    other.incident_sla_id != incident_sla_id
                    and other.org_id == record.org_id
                    and other.incident_id == new_incident
                    and other.sla_def_id == new_sla_def
                ):
                    raise ItsmError(
                        f"SLA '{new_sla_def}' is already linked to incident '{new_incident}'",
                        field="sla_def_id",
                    )
        # Field-by-field change detection. The reference compares the parsed (string) stage against
        # the stored Enum member, so a supplied ``stage`` is ALWAYS counted as a change even when its
        # value is unchanged (verified live: a stage-only no-op SUCCEEDS, while a
        # has_breached / timestamp / id no-op raises). Every other field counts only when its new
        # value differs from the current one.
        changed = {
            k: v
            for k, v in provided.items()
            if k == "stage" or getattr(record, k) != v
        }
        if not changed:
            # Bare ValueError in the reference -> generic VALIDATION_ERROR envelope (verified live),
            # not the NO_CHANGES_DETECTED code some other categories use.
            raise ItsmError("No changes detected for the provided incident SLA fields")
        for field, value in changed.items():
            setattr(record, field, value)
        record.updated_on = self._now()
        return record

    @is_tool(ToolType.WRITE)
    def delete_incident_slas(
        self,
        incident_sla_id: Optional[str] = None,
        incident_id: Optional[str] = None,
        sla_def_id: Optional[str] = None,
        stage: Optional[str] = None,
        has_breached: Optional[bool] = None,
        created_before: Optional[str] = None,
        created_after: Optional[str] = None,
    ) -> dict:
        """Delete incident SLA records matching the given filters (all ANDed).

        At least one filter must be supplied. Deletion is not scoped to the caller's org.

        Args:
            incident_sla_id: Delete the record with this id.
            incident_id: Delete records for this incident.
            sla_def_id: Delete records for this SLA definition.
            stage: Delete records in this stage.
            has_breached: Delete records with this breach flag.
            created_before: Delete records created on or before this ISO timestamp.
            created_after: Delete records created on or after this ISO timestamp.

        Returns:
            A summary mapping with the number of records deleted.
        """
        # A no-filter delete is rejected (the reference requires >=1 filter); without this guard an
        # empty call would match — and wipe — every row.
        if all(
            v is None
            for v in (incident_sla_id, incident_id, sla_def_id, stage, has_breached,
                      created_before, created_after)
        ):
            raise ItsmError("At least one filter must be provided", code="VALIDATION_ERROR")
        # Validate/normalize the stage for matching, but keep the RAW value for the not-found
        # identifier (the reference renders the un-normalized filter there, e.g. stage='Paused').
        normalized_stage = self._normalize_stage(stage)
        to_remove: List[str] = []
        for record_id, record in self.db.incident_sla.items():
            if incident_sla_id is not None and record_id != incident_sla_id:
                continue
            if incident_id is not None and record.incident_id != incident_id:
                continue
            if sla_def_id is not None and record.sla_def_id != sla_def_id:
                continue
            if normalized_stage is not None and record.stage != normalized_stage:
                continue
            if has_breached is not None and record.has_breached != has_breached:
                continue
            if created_before is not None and (record.created_on or "") > created_before:
                continue
            if created_after is not None and (record.created_on or "") < created_after:
                continue
            to_remove.append(record_id)
        # Zero match is a 404 in the reference (router), not a silent {deleted_count: 0} success.
        if not to_remove:
            identifier = self._delete_not_found_identifier(
                incident_sla_id, incident_id, sla_def_id, stage, has_breached,
                created_before, created_after,
            )
            raise ItsmError(
                f"Incident SLA record not found with identifier '{identifier}'",
                code="NOT_FOUND",
            )
        for record_id in to_remove:
            del self.db.incident_sla[record_id]
        return {"deleted_count": len(to_remove)}

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def find_incident_slas(
        self,
        incident_sla_id: Optional[str] = None,
        incident_id: Optional[str] = None,
        sla_def_id: Optional[str] = None,
        stage: Optional[str] = None,
        has_breached: Optional[bool] = None,
        created_before: Optional[str] = None,
        created_after: Optional[str] = None,
    ) -> dict:
        """List incident SLA records, optionally filtered. All filters are ANDed.

        Results are not scoped to the caller's org and are ordered by created_on descending.

        Args:
            incident_sla_id: Filter by SLA record id.
            incident_id: Filter by incident id.
            sla_def_id: Filter by SLA definition id.
            stage: Filter by stage (in_progress, paused, completed, cancelled, breached).
            has_breached: Filter by breach flag.
            created_before: Only records created on or before this ISO timestamp.
            created_after: Only records created on or after this ISO timestamp.

        Returns:
            A mapping with the matching records under 'incident_slas' and their 'total_count'.
        """
        stage = self._normalize_stage(stage)
        out: List[IncidentSLA] = []
        for record in self.db.incident_sla.values():
            if incident_sla_id is not None and record.incident_sla_id != incident_sla_id:
                continue
            if incident_id is not None and record.incident_id != incident_id:
                continue
            if sla_def_id is not None and record.sla_def_id != sla_def_id:
                continue
            if stage is not None and record.stage != stage:
                continue
            if has_breached is not None and record.has_breached != has_breached:
                continue
            if created_before is not None and (record.created_on or "") > created_before:
                continue
            if created_after is not None and (record.created_on or "") < created_after:
                continue
            out.append(record)
        out.sort(key=lambda r: (r.created_on or ""), reverse=True)
        return {"incident_slas": out, "total_count": len(out)}

    @is_tool(ToolType.READ)
    def find_stage_wise_breached_incident_sla_counts(
        self, stages: Optional[List[str]] = None
    ) -> dict:
        """Return counts of breached incident SLA records grouped by stage.

        Only records with has_breached=True are counted. Counts are not scoped to the caller's
        org. Stages with no breached records are omitted. The returned keys are the SLAStage
        enum names (e.g. 'SLAStage.IN_PROGRESS').

        Args:
            stages: Optional list of stages to limit the aggregation
                (in_progress, paused, completed, cancelled, breached).

        Returns:
            A mapping with per-stage breached counts under 'counts'.
        """
        if stages is not None:
            stages = [self._normalize_stage(stage) for stage in stages]
        counts: dict = {}
        for record in self.db.incident_sla.values():
            if not record.has_breached:
                continue
            if stages is not None and record.stage not in stages:
                continue
            key = f"SLAStage.{record.stage.upper()}"
            counts[key] = counts.get(key, 0) + 1
        return {"counts": counts}
