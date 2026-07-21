"""Location tools (5) — faithful port of the ITSM MCP's location category.

Covers location CRUD/search: add, get-by-id, find-by-name, list-with-filters, and update.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from enterprise_worlds.domains.itsm.data_model import Location
from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.environment.toolkit import ToolType, is_tool


class LocationToolsMixin(ItsmToolsBase):
    """Location management tools."""

    # ------------------------------------------------------------------ helpers
    def _loc_require_location(self, location_id: str) -> Location:
        loc = self.db.location.get(location_id)
        if loc is None:
            raise ItsmError(
                f"Location not found with identifier '{location_id}'",
                code="RESOURCE_NOT_FOUND", field="location_id",
            )
        return loc

    def _check_unique_name(self, name: str, exclude_id: Optional[str] = None) -> None:
        """Names are globally unique (across all orgs). Raise if another location uses it."""
        for loc in self.db.location.values():
            if loc.location_id == exclude_id:
                continue
            if loc.name == name:
                raise ItsmError(
                    f"A location with name '{name}' already exists",
                    code="DUPLICATE_LOCATION_NAME", field="name",
                )

    @staticmethod
    def _loc_parse_dt(value: str) -> Optional[datetime]:
        """Parse an ISO-8601 date or datetime string, tolerating a trailing 'Z'."""
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

    # ------------------------------------------------------------------ writes
    @is_tool(ToolType.WRITE)
    def add_location(
        self,
        name: str,
        city: str,
        country: str,
        active: Optional[bool] = None,
        plot_no: Optional[str] = None,
        street: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Location:
        """Create a new location in the system.

        Auto-generates the location_id (LOC_001, LOC_002, ...) and timestamps. The org is
        inherited from the acting user. The name must be globally unique.

        Args:
            name: Location name (must be unique).
            city: City name (cannot be empty).
            country: Country (cannot be empty).
            active: Whether the location is active; defaults to True.
            plot_no: Plot/building number.
            street: Street address.
            state: State/province/region.

        Returns:
            The newly created location.
        """
        if not name:
            raise ItsmError("name cannot be empty", field="name")
        if not city:
            raise ItsmError("city cannot be empty", field="city")
        if not country:
            raise ItsmError("country cannot be empty", field="country")
        self._check_unique_name(name)

        location_id, _ = self._make_id(self.db.location, "LOC")
        now = self._now()
        location = Location(
            location_id=location_id,
            name=name,
            org_id=self._acting_org(),
            plot_no=plot_no,
            street=street,
            city=city,
            state=state,
            country=country,
            active=True if active is None else active,
            created_on=now,
            updated_on=now,
        )
        self.db.location[location_id] = location
        return location

    @is_tool(ToolType.WRITE)
    def update_location(
        self,
        location_id: str,
        name: Optional[str] = None,
        active: Optional[bool] = None,
        plot_no: Optional[str] = None,
        street: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        country: Optional[str] = None,
    ) -> Location:
        """Update specific fields of an existing location.

        At least one field besides location_id must be provided. If every provided value
        already equals the current value, an error is raised (idempotency check). name, city,
        and country are whitespace-trimmed before validation/compare/store and cannot be set to
        empty (or whitespace-only) strings.

        Args:
            location_id: Unique identifier of the location to update.
            name: Updated location name (must be unique).
            active: Updated active status.
            plot_no: Updated plot/building number.
            street: Updated street address.
            city: Updated city name (cannot be empty if provided).
            state: Updated state/province/region.
            country: Updated country (cannot be empty if provided).

        Returns:
            The updated location.
        """
        provided = {
            "name": name, "active": active, "plot_no": plot_no, "street": street,
            "city": city, "state": state, "country": country,
        }
        provided = {k: v for k, v in provided.items() if v is not None}
        if not provided:
            raise ItsmError("At least one field must be provided for update", field="location_id")

        # The reference strips leading/trailing whitespace from name/city/country before
        # validating, comparing (no-changes), and storing. plot_no/street/state are stored raw.
        for _field in ("name", "city", "country"):
            if _field in provided:
                provided[_field] = provided[_field].strip()

        if "name" in provided and provided["name"] == "":
            raise ItsmError("Name cannot be empty", field="name")
        if "city" in provided and provided["city"] == "":
            raise ItsmError("City cannot be empty", field="city")
        if "country" in provided and provided["country"] == "":
            raise ItsmError("Country cannot be empty", field="country")

        location = self._loc_require_location(location_id)

        if "name" in provided:
            self._check_unique_name(provided["name"], exclude_id=location_id)

        # No-changes-detected: all provided values already equal the current values.
        if all(getattr(location, field) == value for field, value in provided.items()):
            raise ItsmError(
                "No changes detected - all provided values are the same as current values",
                code="NO_CHANGES_DETECTED",
            )

        for field, value in provided.items():
            setattr(location, field, value)
        location.updated_on = self._now()
        return location

    # ------------------------------------------------------------------ reads
    @is_tool(ToolType.READ)
    def get_location_by_id(self, location_id: str) -> Location:
        """Get a location by its unique identifier.

        Args:
            location_id: Location's unique identifier (format: LOC_001, LOC_002, ...).

        Returns:
            The matching location.
        """
        return self._loc_require_location(location_id)

    @is_tool(ToolType.READ)
    def find_location_by_given_name(self, name: str) -> Location:
        """Find a location by its exact name (case-sensitive).

        Leading/trailing whitespace on the supplied name is trimmed before lookup, mirroring
        the reference's name-route normalization.

        Args:
            name: Name of the location to find.

        Returns:
            The matching location.
        """
        lookup = name.strip()
        for loc in self.db.location.values():
            if loc.name == lookup:
                return loc
        identifier = lookup if lookup else "empty name"
        raise ItsmError(
            f"Location not found with identifier '{identifier}'",
            code="RESOURCE_NOT_FOUND", field="name",
        )

    @is_tool(ToolType.READ)
    def find_locations(
        self,
        name: Optional[str] = None,
        city: Optional[str] = None,
        country: Optional[str] = None,
        active: Optional[bool] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> dict:
        """List locations with optional filters; without filters, returns all locations.

        Text filters (name, city, country) use case-insensitive partial matching. Date
        filters are inclusive ISO-8601 bounds on created_on. Passing an empty string for any
        filter is rejected.

        Args:
            name: Filter by location name (partial match).
            city: Filter by city (partial match).
            country: Filter by country (partial match).
            active: Filter by active status (True/False).
            created_after: Only locations created on/after this ISO 8601 date.
            created_before: Only locations created on/before this ISO 8601 date.

        Returns:
            A mapping with 'locations' (the matching locations) and 'total_count'.
        """
        for field, value in (
            ("name", name), ("city", city), ("country", country),
            ("created_after", created_after), ("created_before", created_before),
        ):
            if value == "":
                raise ItsmError(
                    f"Filter parameter '{field}' cannot be empty. Please provide a valid "
                    "value or omit the parameter.",
                    code="EMPTY_FILTER_VALUE", field=field,
                )

        after_dt = self._loc_parse_dt(created_after) if created_after else None
        before_dt = self._loc_parse_dt(created_before) if created_before else None

        out: List[Location] = []
        for loc in self.db.location.values():
            if name is not None and name.lower() not in (loc.name or "").lower():
                continue
            if city is not None and city.lower() not in (loc.city or "").lower():
                continue
            if country is not None and country.lower() not in (loc.country or "").lower():
                continue
            if active is not None and loc.active != active:
                continue
            if after_dt is not None or before_dt is not None:
                created = self._loc_parse_dt(loc.created_on) if loc.created_on else None
                if created is None:
                    continue
                if after_dt is not None and created < after_dt:
                    continue
                if before_dt is not None and created > before_dt:
                    continue
            out.append(loc)
        return {"locations": out, "total_count": len(out)}
