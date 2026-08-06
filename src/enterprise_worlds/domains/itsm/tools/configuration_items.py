"""Configuration item tools (4) — faithful port of the ITSM MCP's CI category.

Covers CI registration/update plus serial-number and filtered lookups.
"""

from __future__ import annotations

from typing import List, Optional

from enterprise_worlds.domains.itsm import enums
from enterprise_worlds.domains.itsm.data_model import ConfigurationItem
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class ConfigurationItemToolsMixin(ItsmToolsBase):
    """Configuration item management tools."""

    # ------------------------------------------------------------------ helpers
    def _validate_ci_enums(self, *, status=None) -> None:
        """Reject out-of-set enum values, mirroring the reference's request-body enum gate."""
        self._check_enum("status", status, enums.CI_STATUS)

    def _validate_ci_cost(self, cost: Optional[float]) -> None:
        """Reject a negative cost, mirroring the reference request schema's ``ge=0`` bound.

        The original rejects ``cost < 0`` as a request-body validation error before any FK or
        existence check (the gym does not enforce JSON-schema bounds at call time, so this guard
        is required for parity).
        """
        if cost is not None and cost < 0:
            raise ItsmError(
                "Input should be greater than or equal to 0",
                code="VALIDATION_ERROR",
                field="cost",
            )

    def _ci_require_location(self, location_id: Optional[str]) -> None:
        if location_id is not None and location_id not in self.db.location:
            raise ItsmError(
                "Invalid reference: The owner_id or location_id value does not exist in the "
                "database",
                code="INVALID_REFERENCE",
                field="location_id",
            )

    @staticmethod
    def _cost_repr(value: object) -> str:
        """Render a cost value the way the original ServiceNow MCP does in the no-changes message."""
        try:
            return f"Decimal('{float(value):.2f}')"
        except (TypeError, ValueError):
            return repr(value)

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def register_configuration_item(
        self,
        name: str,
        owner_id: str,
        location_id: str,
        serial_number: str,
        status: str,
        cost: float,
    ) -> ConfigurationItem:
        """Register a new configuration item in the system.

        Args:
            name: CI name (1-120 characters, unique across all CIs).
            owner_id: User id who owns the CI.
            location_id: Location id where the CI is located.
            serial_number: Serial number (1-60 characters, unique across all CIs).
            status: CI status (in_use, in_stock, maintenance, retired, disposed).
            cost: Cost of the CI (must be >= 0).

        Returns:
            The newly created configuration item.
        """
        # Enum validation first (the reference validates the request body before FK checks).
        self._validate_ci_enums(status=status)
        # Body-level numeric validation (cost >= 0) is rejected before any FK check.
        self._validate_ci_cost(cost)
        # Validation order mirrors the original MCP: owner -> name -> serial -> location.
        self._require_user(owner_id, "owner_id")
        for ci in self.db.configuration_item.values():
            if ci.name == name:
                raise ItsmError(
                    f"A configuration item with name '{name}' already exists",
                    field="name",
                )
        for ci in self.db.configuration_item.values():
            if ci.serial_number == serial_number:
                raise ItsmError(
                    f"A configuration item with serial_number '{serial_number}' already exists",
                    code="DUPLICATE_SERIAL_NUMBER",
                    field="serial_number",
                )
        self._ci_require_location(location_id)

        ci_id, _ = self._make_id(self.db.configuration_item, "CI")
        now = self._now()
        ci = ConfigurationItem(
            configuration_item_id=ci_id,
            name=name,
            serial_number=serial_number,
            owner_id=owner_id,
            location_id=location_id,
            org_id=self._acting_org(),
            status=status,
            cost=cost,
            created_on=now,
            updated_on=now,
        )
        self.db.configuration_item[ci_id] = ci
        return ci

    @is_tool(ToolType.WRITE)
    def update_configuration_item(
        self,
        configuration_item_id: str,
        owner_id: Optional[str] = None,
        location_id: Optional[str] = None,
        serial_number: Optional[str] = None,
        status: Optional[str] = None,
        cost: Optional[float] = None,
        name: Optional[str] = None,
    ) -> ConfigurationItem:
        """Update specific fields of an existing configuration item.

        At least one field besides ``configuration_item_id`` must be provided. Updating a checked
        field (name, serial_number, owner_id, location_id, cost) to its current value yields a
        "no changes detected" error unless another field actually changes; ``status`` is always
        applied when provided.

        Args:
            configuration_item_id: Unique identifier of the CI to update.
            owner_id: Updated owner user id.
            location_id: Updated location id.
            serial_number: Updated serial number (must remain unique).
            status: Updated CI status (in_use, in_stock, maintenance, retired, disposed).
            cost: Updated cost (must be >= 0).
            name: Updated CI name.

        Returns:
            The updated configuration item.
        """
        # Enum validation first (the reference validates the request body before FK checks).
        self._validate_ci_enums(status=status)
        # Body-level numeric validation (cost >= 0) is rejected before the CI existence check,
        # mirroring the reference's request-schema validation order.
        self._validate_ci_cost(cost)
        # ``serial_number`` carries a min_length=1 constraint (rejects '' outright); ``name`` does
        # not (the reference stores an empty name as-is), so only serial_number is guarded here.
        self._reject_empty("serial_number", serial_number)
        ci = self.db.configuration_item.get(configuration_item_id)
        if ci is None:
            raise ItsmError(
                f"Configuration item not found with identifier '{configuration_item_id}'",
                code="NOT_FOUND",
                field="configuration_item_id",
            )

        provided = {
            "owner_id": owner_id, "location_id": location_id, "serial_number": serial_number,
            "status": status, "cost": cost, "name": name,
        }
        if all(v is None for v in provided.values()):
            raise ItsmError(
                "At least one field must be provided for update",
                field="configuration_item_id",
            )

        # Owner validation fires first; the original MCP reports it as the CI not being found.
        if owner_id is not None and owner_id not in self.db.users:
            raise ItsmError(
                f"Configuration item not found with identifier '{configuration_item_id}'",
                code="NOT_FOUND",
                field="configuration_item_id",
            )

        # Serial uniqueness (against other CIs) before location existence.
        if serial_number is not None:
            for other_id, other in self.db.configuration_item.items():
                if other_id != configuration_item_id and other.serial_number == serial_number:
                    raise ItsmError(
                        f"A configuration item with serial_number '{serial_number}' already "
                        f"exists",
                        code="DUPLICATE_SERIAL_NUMBER",
                        field="serial_number",
                    )

        if location_id is not None and location_id not in self.db.location:
            raise ItsmError(
                "Invalid reference: The location_id value does not exist in the database",
                code="INVALID_REFERENCE",
                field="location_id",
            )

        # No-changes detection: status is excluded; checked-field order is
        # name, serial_number, owner_id, location_id, cost.
        checked = [
            ("name", name, ci.name),
            ("serial_number", serial_number, ci.serial_number),
            ("owner_id", owner_id, ci.owner_id),
            ("location_id", location_id, ci.location_id),
            ("cost", cost, ci.cost),
        ]
        unchanged: List[str] = []
        changes = {}
        for field, new_value, current in checked:
            if new_value is None:
                continue
            if field == "cost":
                same = current is not None and float(new_value) == float(current)
            else:
                same = new_value == current
            if same:
                if field == "cost":
                    unchanged.append(f"{field} (already {self._cost_repr(current)})")
                else:
                    unchanged.append(f"{field} (already {current!r})")
            else:
                changes[field] = new_value

        if status is None and not changes:
            raise ItsmError(
                "No changes detected for fields: " + ", ".join(unchanged),
                code="NO_CHANGES_DETECTED",
                field="configuration_item_id",
            )

        self._check_configuration_item_transition(ci, status)
        for field, new_value in changes.items():
            setattr(ci, field, new_value)
        if status is not None:
            ci.status = status
        ci.updated_on = self._now()
        return ci

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def find_configuration_item_by_serial_number(
        self, serial_number: str
    ) -> ConfigurationItem:
        """Retrieve a configuration item by its (unique) serial number.

        Args:
            serial_number: Configuration item serial number (exact match).

        Returns:
            The matching configuration item.
        """
        for ci in self.db.configuration_item.values():
            if ci.serial_number == serial_number:
                return ci
        raise ItsmError(
            f"Configuration item not found with identifier '{serial_number}'",
            code="NOT_FOUND",
            field="serial_number",
        )

    @is_tool(ToolType.READ)
    def find_configuration_items(
        self,
        configuration_item_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        location_id: Optional[str] = None,
        serial_number: Optional[str] = None,
        status: Optional[str] = None,
        cost: Optional[float] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List configuration items with optional filters (all ANDed; omitted filters ignored).

        Args:
            configuration_item_id: Filter by configuration item id (exact match).
            owner_id: Filter by owner user id (exact match).
            location_id: Filter by location id (exact match).
            serial_number: Filter by serial number (exact match).
            status: Filter by status (in_use, in_stock, maintenance, retired, disposed).
            cost: Filter by cost (exact match).
            created_after: Only CIs created after this ISO 8601 timestamp.
            created_before: Only CIs created before this ISO 8601 timestamp.

        Returns:
            A dict with the matching CIs under 'configuration_items' (id-descending) and their
            'total_count'.
        """
        # The reference validates enum-typed filters too (invalid value -> error, not empty result).
        self._validate_ci_enums(status=status)
        eq_filters = {
            "configuration_item_id": configuration_item_id, "owner_id": owner_id,
            "location_id": location_id, "serial_number": serial_number, "status": status,
        }
        active = {k: v for k, v in eq_filters.items() if v is not None}
        out: List[ConfigurationItem] = []
        for ci in self.db.configuration_item.values():
            if any(getattr(ci, attr) != val for attr, val in active.items()):
                continue
            if cost is not None and not (
                ci.cost is not None and float(ci.cost) == float(cost)
            ):
                continue
            # created_after is exclusive of the boundary; created_before is inclusive.
            if created_after is not None and (ci.created_on or "") <= created_after:
                continue
            if created_before is not None and (ci.created_on or "") > created_before:
                continue
            out.append(ci)
        # Mirror the reference's ``ORDER BY created_on DESC``; ties fall back to id-descending so
        # ordering stays deterministic (and unchanged where timestamps coincide).
        out.sort(key=lambda c: (c.created_on or "", c.configuration_item_id), reverse=True)
        return {"configuration_items": out, "total_count": len(out)}
