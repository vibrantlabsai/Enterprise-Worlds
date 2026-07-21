"""Notification tools (6) — faithful port of the ITSM MCP's notification category.

Covers notification search (by id/incident/email/type/status/date-range), the email- and
incident-scoped finders, and send/update/delete write tools.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import Notification
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool

# Pragmatic e-mail shape gate (local@domain.tld); mirrors the reference's INVALID_EMAIL_FORMAT 400.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotificationToolsMixin(ItsmToolsBase):
    """Notification management tools."""

    # ------------------------------------------------------------------ helpers
    def _validate_notification_enums(self, *, type=None, status=None) -> None:
        """Reject out-of-set enum values, mirroring the reference's request-body enum gate."""
        self._check_enum("type", type, enums.NOTIFICATION_TYPE)
        self._check_enum("status", status, enums.NOTIFICATION_STATUS)

    def _acting_email(self) -> Optional[str]:
        """Email of the acting (token) user, used for the send-to-self guard."""
        if self.acting_user_id and self.acting_user_id in self.db.users:
            return self.db.users[self.acting_user_id].email
        return None

    def _require_email_user(self, email: str) -> None:
        """Validate that ``email`` belongs to an existing user (global, any org)."""
        for user in self.db.users.values():
            if user.email == email:
                return
        raise ItsmError(
            f"User with email '{email}' not found or does not belong to your organization",
            code="USER_EMAIL_NOT_FOUND",
            field="email",
        )

    def _require_notification(self, notification_id: str) -> Notification:
        notif = self.db.notification.get(notification_id)
        if notif is None:
            raise ItsmError(
                f"Notification not found with identifier '{notification_id}'",
                code="RESOURCE_NOT_FOUND",
                field=None,
            )
        return notif

    @staticmethod
    def _parse_filter_date(value: str) -> Optional[datetime]:
        """Parse an ISO-8601 date or datetime (tolerating a trailing 'Z'); None if unparseable."""
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1]
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text + "T00:00:00")
            except ValueError:
                return None

    def _validate_filter_date(self, field: str, value: Optional[str]) -> None:
        """Reject a non-ISO date filter, mirroring the reference's INVALID_DATE_FORMAT 400."""
        if value and self._parse_filter_date(value) is None:
            raise ItsmError(
                f"Invalid date format for {field}: '{value}'. Expected ISO 8601 format "
                "(e.g., '2024-01-15T09:00:00' or '2024-01-15')",
                code="INVALID_DATE_FORMAT",
                field=field,
            )

    def _validate_email_format(self, email: str) -> None:
        """Reject a malformed e-mail address, mirroring the reference's INVALID_EMAIL_FORMAT 400."""
        if not _EMAIL_RE.match(email or ""):
            raise ItsmError(
                f"Invalid email format provided: '{email}'",
                code="INVALID_EMAIL_FORMAT",
                field="email",
            )

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def send_notification(
        self,
        incident_id: str,
        email: str,
        type: Optional[str] = None,
        status: Optional[str] = None,
        subject: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Notification:
        """Create and send a new notification associated with an incident.

        Args:
            incident_id: Id of the associated incident (required).
            email: Recipient email address; must belong to an existing user (required).
            type: Notification type (alert, update, reminder, report, solution_proposal,
                other); defaults to 'other'.
            status: Notification status (queued, sent, delivered, opened, failed); defaults
                to 'queued'.
            subject: Subject of the notification.
            message: Message content of the notification.

        Returns:
            The created notification.
        """
        # Enum validation first (the reference validates the request body before FK checks).
        self._validate_notification_enums(type=type, status=status)
        self._require_incident(incident_id)
        self._require_email_user(email)
        acting_email = self._acting_email()
        if acting_email is not None and email == acting_email:
            raise ItsmError(
                "Cannot send notification to yourself",
                code="CANNOT_SEND_TO_SELF",
                field="email",
            )

        notification_id, _ = self._make_id(self.db.notification, "NOTIF")
        now = self._now()
        notification = Notification(
            notification_id=notification_id,
            incident_id=incident_id,
            org_id=self._acting_org(),
            email=email,
            subject=subject,
            message=message,
            type=type or "other",
            status=status or "queued",
            created_on=now,
            updated_on=now,
        )
        self.db.notification[notification_id] = notification
        return notification

    @is_tool(ToolType.WRITE)
    def update_notification(
        self,
        notification_id: str,
        incident_id: Optional[str] = None,
        email: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        subject: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Notification:
        """Update an existing notification. Only the fields you pass are changed.

        Args:
            notification_id: Id of the notification to update (required).
            incident_id: New associated incident id; validated for existence if supplied.
            email: New recipient email; must belong to an existing user if supplied.
            type: New notification type (alert, update, reminder, report,
                solution_proposal, other).
            status: New notification status (queued, sent, delivered, opened, failed).
            subject: New subject.
            message: New message content.

        Returns:
            The updated notification.
        """
        # Enum validation first (the reference validates the request body before FK checks).
        self._validate_notification_enums(type=type, status=status)
        # ``subject``/``message`` carry a min_length=1 constraint — '' is rejected at the request
        # boundary (before the notification-existence check), not stored or treated as a no-op.
        self._reject_empty("subject", subject)
        self._reject_empty("message", message)
        notification = self._require_notification(notification_id)
        updates = {
            "incident_id": incident_id,
            "email": email,
            "type": type,
            "status": status,
            "subject": subject,
            "message": message,
        }
        provided = {k: v for k, v in updates.items() if v is not None}
        if not provided:
            raise ItsmError("At least one field must be provided for update")
        if incident_id is not None:
            self._require_incident(incident_id)
        if email is not None:
            self._require_email_user(email)
        # A no-op update (every provided field already equal) is rejected, not silently re-stamped.
        changed = {k: v for k, v in provided.items() if getattr(notification, k) != v}
        if not changed:
            raise ItsmError("No changes detected", code="NO_CHANGES_DETECTED")
        for field, value in changed.items():
            setattr(notification, field, value)
        notification.updated_on = self._now()
        return notification

    @is_tool(ToolType.WRITE)
    def delete_notifications(self, notification_id: str) -> dict:
        """Delete a notification by id.

        Note: the upstream MCP registers this tool but does not wire a router endpoint for it,
        so it never deletes anything and the server returns an "API endpoint not found" error.
        This port mirrors that behaviour exactly (raises; no state change).

        Args:
            notification_id: The id of the notification to delete (e.g. 'NOTIF_001').

        Returns:
            A success message with the deleted notification id (never reached: see note).
        """
        raise ItsmError(
            "API endpoint not found for tool: delete_notifications. "
            "Check that router function name matches tool name.",
            code="API_ENDPOINT_NOT_FOUND",
        )

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def find_notifications(
        self,
        notification_id: Optional[str] = None,
        incident_id: Optional[str] = None,
        email: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        created_before: Optional[str] = None,
        created_after: Optional[str] = None,
    ) -> dict:
        """Search notifications using various filters; all are optional and ANDed together.

        Null values for any filter are ignored. The ``email`` filter is a case-insensitive
        substring match; an empty ``email`` is ignored. Empty-string id filters and unknown
        id/date/enum values are rejected (the reference pre-validates the request before querying).
        Date filters compare against ``created_on``: ``created_after`` is exclusive (> value) and
        ``created_before`` is inclusive (<= value).

        Args:
            notification_id: Filter by notification id.
            incident_id: Filter by associated incident id.
            email: Filter by recipient email address (case-insensitive substring match).
            type: Filter by notification type (alert, update, reminder, report,
                solution_proposal, other).
            status: Filter by notification status (queued, sent, delivered, opened, failed).
            created_before: Only notifications created on/before this ISO timestamp.
            created_after: Only notifications created strictly after this ISO timestamp.

        Returns:
            A mapping with the matching notifications and their count
            ({'notifications': [...], 'count': N}).
        """
        # The reference validates enum-typed filters too (invalid/empty value -> error, not empty
        # result); this fires first, matching the reference's request-body parse order.
        self._validate_notification_enums(type=type, status=status)
        # Empty-string id filters are rejected (email empty is silently ignored, per the reference).
        if notification_id == "":
            raise ItsmError(
                "notification_id cannot be empty. Please provide a valid notification ID "
                "or omit this parameter.",
                code="MISSING_REQUIRED_VALUE",
                field="notification_id",
            )
        if incident_id == "":
            raise ItsmError(
                "incident_id cannot be empty. Please provide a valid incident ID "
                "or omit this parameter.",
                code="MISSING_REQUIRED_VALUE",
                field="incident_id",
            )
        # Reference-existence checks for the id filters.
        if notification_id and notification_id not in self.db.notification:
            raise ItsmError(
                f"Notification with ID '{notification_id}' not found or does not "
                "belong to your organization",
                code="NOTIFICATION_NOT_FOUND",
                field="notification_id",
            )
        if incident_id and incident_id not in self.db.incident:
            raise ItsmError(
                f"Incident with ID '{incident_id}' not found",
                code="INCIDENT_NOT_FOUND",
                field="incident_id",
            )
        # Date-format validation (the boundary comparison below is unchanged).
        self._validate_filter_date("created_after", created_after)
        self._validate_filter_date("created_before", created_before)

        eq_filters = {
            "notification_id": notification_id,
            "incident_id": incident_id,
            "type": type,
            "status": status,
        }
        active = {k: v for k, v in eq_filters.items() if v}
        # ``email`` is matched case-insensitively as a substring (reference uses ilike '%email%').
        email_needle = email.lower() if email else None
        out: List[Notification] = []
        for notif in self.db.notification.values():
            if any(getattr(notif, attr) != val for attr, val in active.items()):
                continue
            if email_needle is not None and email_needle not in (notif.email or "").lower():
                continue
            if created_after and (notif.created_on or "") <= created_after:
                continue
            if created_before and (notif.created_on or "") > created_before:
                continue
            out.append(notif)
        return {"notifications": out, "count": len(out)}

    @is_tool(ToolType.READ)
    def find_notifications_for_email(self, email: str) -> dict:
        """Retrieve notifications sent to a specific email address.

        Args:
            email: Email address to search for.

        Returns:
            A mapping with the matching notifications and their count
            ({'notifications': [...], 'count': N}).
        """
        # The reference rejects malformed addresses with a 400 before the lookup.
        self._validate_email_format(email)
        out = [n for n in self.db.notification.values() if n.email == email]
        if not out:
            raise ItsmError(
                f"Notifications not found with identifier 'email={email}'",
                code="RESOURCE_NOT_FOUND",
                field=None,
            )
        return {"notifications": out, "count": len(out)}

    @is_tool(ToolType.READ)
    def find_notifications_sent_for_incident(self, incident_id: str) -> dict:
        """Retrieve notifications related to a specific incident.

        Args:
            incident_id: Incident id to search for.

        Returns:
            A mapping with the matching notifications and their count
            ({'notifications': [...], 'count': N}).
        """
        out = [n for n in self.db.notification.values() if n.incident_id == incident_id]
        if not out:
            raise ItsmError(
                f"Notifications not found with identifier 'incident_id={incident_id}'",
                code="RESOURCE_NOT_FOUND",
                field=None,
            )
        return {"notifications": out, "count": len(out)}
