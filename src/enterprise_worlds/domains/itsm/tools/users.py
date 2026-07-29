"""User tools (6) — faithful port of the ITSM MCP's users category.

Covers user CRUD and the various user lookups (by id, email, name) plus filtered listing.
Note: ``add_new_user`` ignores the ``user_name`` arg (it derives ``<first>.<last>`` lowercased),
and ``update_user_details`` accepts but never stores the ``user_name`` / ``company_id`` args.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import User
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool

# Email/phone format gates mirror the reference's pydantic request-body field validators
# (``apis/users/router``): a malformed address/number is rejected with INVALID_EMAIL_FORMAT /
# INVALID_PHONE_FORMAT before any FK/uniqueness/lookup logic. The email regex requires a TLD of
# at least 2 chars (e.g. 'a@b.c' / 'x@y.z' are rejected; 'a@b.co' is accepted); the phone regex
# is the reference's ``+<1-3 digit cc>-<3>-<3>-<4>`` shape (e.g. '+1-415-555-0101').
_USR_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_USR_PHONE_RE = re.compile(r"^\+\d{1,3}-\d{3}-\d{3}-\d{4}$")


class UserToolsMixin(ItsmToolsBase):
    """User management tools."""

    # ----------------------------------------------------------------- helpers
    def _validate_user_enums(self, *, role=None) -> None:
        """Reject out-of-set enum values, mirroring the reference's request-body enum gate."""
        self._check_enum("role", role, enums.USER_ROLE)

    def _validate_user_contact_formats(
        self, *, email: Optional[str] = None, phone: Optional[str] = None
    ) -> None:
        """Reject malformed email/phone, mirroring the reference's pydantic field validators.

        Runs at the request boundary (before FK/uniqueness checks); ``None`` (field not supplied)
        is skipped. Email must match ``user@domain.tld``; phone must be ``+<cc>-XXX-XXX-XXXX``.
        """
        if email is not None and not _USR_EMAIL_RE.match(email):
            raise ItsmError(
                f"Invalid email format provided: '{email}'",
                code="INVALID_EMAIL_FORMAT", field="email",
            )
        if phone is not None and not _USR_PHONE_RE.match(phone):
            raise ItsmError(
                f"Invalid phone format provided: '{phone}'",
                code="INVALID_PHONE_FORMAT", field="phone",
            )

    @staticmethod
    def _gen_user_name(first_name: str, last_name: str) -> str:
        """Login name = ``<first>.<last>`` lowercased (mirrors the MCP)."""
        return f"{first_name}.{last_name}".lower()

    @staticmethod
    def _gen_static_token(user_id: str) -> str:
        """A ``token_<hash>`` auth token, DETERMINISTIC from user_id.

        The live MCP mints a random token via ``secrets``; we cannot (and need not) match that
        exact value. We derive it deterministically so gold-action replay reproduces the same
        DB state for hash matching (the token differs from the original MCP's, which is irreducible).
        """
        return f"token_{hashlib.sha256(user_id.encode()).hexdigest()}"

    def _usr_require_location(self, location_id: Optional[str]) -> None:
        if location_id is not None and location_id not in self.db.location:
            raise ItsmError(
                f"Location with ID '{location_id}' not found",
                code="LOCATION_NOT_FOUND", field="location_id",
            )

    @staticmethod
    def _usr_parse_dt(value: str) -> Optional[datetime]:
        """Parse an ISO date/datetime filter value; None if unparseable."""
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def add_new_user(
        self,
        first_name: str,
        last_name: str,
        email: str,
        phone: str,
        role: str,
        active: bool,
        user_name: Optional[str] = None,
        location_id: Optional[str] = None,
    ) -> User:
        """Create a new user.

        The system auto-generates the user_id (USER_001, USER_002, ...), the user_name
        (from first.last, lowercased — any supplied user_name is ignored), and a static
        auth token. The org_id is inherited from the acting user.

        Args:
            first_name: User's first name (1-60 characters) (required).
            last_name: User's last name (1-60 characters) (required).
            email: User's email address; must be unique within the org and valid (required).
            phone: User's phone number, format '+1-415-555-0101'; must be unique (required).
            role: User's role; one of admin, manager, agent, reporter (required).
            active: Whether the user account is active (required).
            user_name: Login name (ignored; always generated from first.last).
            location_id: Location identifier.

        Returns:
            The newly created user.
        """
        # Request-body validation first (the reference validates the body before FK/existence
        # checks): enum-typed role, then email/phone format.
        self._validate_user_enums(role=role)
        self._validate_user_contact_formats(email=email, phone=phone)
        self._usr_require_location(location_id)

        org_id = self._acting_org()
        for u in self.db.users.values():
            if u.email == email and u.org_id == org_id:
                raise ItsmError(
                    f"A user with email '{email}' already exists in your organization",
                    code="DUPLICATE_EMAIL", field="email",
                )
        for u in self.db.users.values():
            if u.phone == phone:
                raise ItsmError(
                    f"A user with phone '{phone}' already exists",
                    code="DUPLICATE_PHONE", field="phone",
                )

        user_id, _ = self._make_id(self.db.users, "USER")
        now = self._now()
        user = User(
            user_id=user_id,
            user_name=self._gen_user_name(first_name, last_name),
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            role=role,
            active=active,
            static_token=self._gen_static_token(user_id),
            org_id=org_id,
            location_id=location_id,
            created_on=now,
            updated_on=now,
        )
        self.db.users[user_id] = user
        return user

    @is_tool(ToolType.WRITE)
    def update_user_details(
        self,
        user_id: str,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        active: Optional[bool] = None,
        phone: Optional[str] = None,
        role: Optional[str] = None,
        user_name: Optional[str] = None,
        company_id: Optional[str] = None,
        location_id: Optional[str] = None,
    ) -> User:
        """Update specific fields of an existing user.

        At least one updatable field (email, first_name, last_name, active, phone, role or
        location_id) must be provided. Updating a field to its current value is rejected
        (no changes detected). The user_name and company_id arguments are accepted but never
        stored. user_name is not regenerated when the name changes.

        Args:
            user_id: Id of the user to update (required).
            email: Updated email address; must be unique within the org and valid.
            first_name: Updated first name (1-60 characters).
            last_name: Updated last name (1-60 characters).
            active: Updated active status.
            phone: Updated phone number, format '+1-415-555-0101'; must be unique.
            role: Updated role; one of admin, manager, agent, reporter.
            user_name: Updated login name (accepted but ignored).
            company_id: Updated company identifier (accepted but ignored).
            location_id: Updated location identifier.

        Returns:
            The updated user.
        """
        # Request-body validation first (the reference validates the body before FK/existence
        # checks): enum-typed role, then email/phone format (only for supplied fields).
        self._validate_user_enums(role=role)
        self._validate_user_contact_formats(email=email, phone=phone)
        self._reject_empty("first_name", first_name)
        self._reject_empty("last_name", last_name)
        user = self.db.users.get(user_id)
        if user is None:
            raise ItsmError(
                f"User not found with identifier '{user_id}'",
                code="NOT_FOUND", field=None,
            )

        # Only these fields are processed for the update / change-detection.
        candidates = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "active": active,
            "phone": phone,
            "role": role,
            "location_id": location_id,
        }
        provided = {k: v for k, v in candidates.items() if v is not None}
        if not provided:
            raise ItsmError(
                "At least one field must be provided for update",
                code="NO_FIELDS_PROVIDED", field=None,
            )

        # Email/phone uniqueness against other users (email is org-scoped, phone global).
        if email is not None and email != user.email:
            for u in self.db.users.values():
                if u.user_id != user_id and u.email == email and u.org_id == user.org_id:
                    raise ItsmError(
                        f"Failed to update user: A user with email '{email}' already exists",
                        code="INTERNAL_ERROR", field=None,
                    )
        if phone is not None and phone != user.phone:
            for u in self.db.users.values():
                if u.user_id != user_id and u.phone == phone:
                    # The reference surfaces a phone collision on update as the DB-constraint error
                    # (the email path stays INTERNAL_ERROR, matching the reference).
                    raise ItsmError(
                        "Failed to update user due to database constraint",
                        code="DATABASE_ERROR", field=None,
                    )
        if location_id is not None and location_id != user.location_id:
            self._usr_require_location(location_id)

        # Change detection: every provided value matching current -> reject.
        # The reference surfaces a fixed top-level message (the per-field detail lives in a nested
        # detail the gym does not model); code is NO_CHANGES_DETECTED.
        changed = {k: v for k, v in provided.items() if getattr(user, k) != v}
        if not changed:
            raise ItsmError(
                "No changes detected - all provided values are the same as current values",
                code="NO_CHANGES_DETECTED", field=None,
            )

        for field, value in changed.items():
            setattr(user, field, value)
        user.updated_on = self._now()
        return user

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def get_user(self, user_id: str) -> User:
        """Return the user with the given user id.

        Args:
            user_id: User's unique identifier, e.g. 'USER_003'.

        Returns:
            The matching user.
        """
        user = self.db.users.get(user_id)
        if user is None:
            raise ItsmError(
                f"User not found with identifier '{user_id}'",
                code="NOT_FOUND", field=None,
            )
        return user

    @is_tool(ToolType.READ)
    def get_user_using_email(self, email: str) -> User:
        """Return the user with the given email address.

        The email is format-validated first; a malformed address is rejected before lookup.

        Args:
            email: User's email address.

        Returns:
            The matching user.
        """
        # The reference validates email FORMAT first; a malformed address is rejected with
        # INVALID_EMAIL_FORMAT before any lookup (so it never falls through to NOT_FOUND).
        if not _USR_EMAIL_RE.match(email or ""):
            raise ItsmError(
                f"Invalid email format provided: '{email}'",
                code="INVALID_EMAIL_FORMAT", field="email",
            )
        for user in self.db.users.values():
            if user.email == email:
                return user
        raise ItsmError(
            f"User not found with identifier '{email}'",
            code="NOT_FOUND", field=None,
        )

    @is_tool(ToolType.READ)
    def get_user_using_name(self, first_name: str, last_name: str) -> User:
        """Return the user matching the given first and last name (case-insensitive).

        Args:
            first_name: User's first name.
            last_name: User's last name.

        Returns:
            The matching user.
        """
        fl, ll = first_name.lower(), last_name.lower()
        for user in self.db.users.values():
            if user.first_name.lower() == fl and user.last_name.lower() == ll:
                return user
        raise ItsmError(
            f"User not found with identifier '{first_name} {last_name}'",
            code="NOT_FOUND", field=None,
        )

    @is_tool(ToolType.READ)
    def list_users(
        self,
        email: Optional[str] = None,
        user_id: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        phone: Optional[str] = None,
        role: Optional[str] = None,
        active: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List users, optionally filtered. Empty-string filters are ignored.

        Results are ordered by created_on ascending and each user is returned as a serialized
        dict (the static auth token is omitted).

        Args:
            email: Filter by email address (exact match).
            user_id: Filter by user id (exact match).
            first_name: Filter by first name (case-insensitive, partial match).
            last_name: Filter by last name (case-insensitive, partial match).
            phone: Filter by phone number (exact match).
            role: Filter by role (admin, manager, agent, reporter).
            active: Filter by active status ('true' or 'false').
            created_after: Only users created strictly after this ISO date/datetime.
            created_before: Only users created on/before this ISO date/datetime.

        Returns:
            A dict with the matching users and their total count.
        """
        # The reference validates enum-typed filters too (invalid value -> error, not empty result).
        self._validate_user_enums(role=role)
        after_dt = self._usr_parse_dt(created_after) if created_after else None
        before_dt = self._usr_parse_dt(created_before) if created_before else None
        active_bool = None
        if active:
            active_bool = active.strip().lower() == "true"

        out: List[User] = []
        for user in self.db.users.values():
            if email and user.email != email:
                continue
            if user_id and user.user_id != user_id:
                continue
            if first_name and first_name.lower() not in user.first_name.lower():
                continue
            if last_name and last_name.lower() not in user.last_name.lower():
                continue
            if phone and user.phone != phone:
                continue
            if role and user.role != role:
                continue
            if active and user.active != active_bool:
                continue
            if after_dt is not None:
                created = self._usr_parse_dt(user.created_on or "")
                if created is None or not (created > after_dt):
                    continue
            if before_dt is not None:
                created = self._usr_parse_dt(user.created_on or "")
                if created is None or not (created <= before_dt):
                    continue
            out.append(user)
        # The reference orders results by created_on ascending (stable for equal timestamps) and
        # serializes each row via to_dict(), which omits the secret static_token field.
        out.sort(key=lambda u: (u.created_on or ""))
        return {
            "users": [u.model_dump(exclude={"static_token"}) for u in out],
            "total_count": len(out),
        }
