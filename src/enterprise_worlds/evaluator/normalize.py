"""Deterministic value normalization for db_match (verifier v2).

Structured fields are compared byte-exactly, but the generator writes canonical values into
gold (``state='TX'``, ``country='USA'``) while the agent only ever hears prose ("Austin,
Texas", "United States") and stores what it heard. QA audits over itsm-v1 measured runs
killed purely on surface form: TX != Texas, USA != United States, ``""`` != ``null``, and an
ISO ``'T'``-separated timestamp vs the seed's space-separated form of the same instant.

This module canonicalizes those specific surface forms before equality. It is deliberately
conservative: exact alias tables and format canonicalization only — no fuzzy matching, no
similarity thresholds — so a normalized match can never conflate genuinely different values.
Same-referent-different-content cases (e.g. CI display names 'Dell XPS 15' vs
'Dell XPS 15 - Marcus Li') are intentionally NOT normalized.
"""

import re
from typing import Optional

# USPS two-letter codes for states, districts, and territories.
_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}

# Common-name aliases only — additions must be unambiguous.
_COUNTRIES = {
    "united states": "US", "united states of america": "USA", "usa": "US", "us": "US",
    "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "japan": "JP", "germany": "DE", "france": "FR", "india": "IN", "canada": "CA",
    "australia": "AU", "singapore": "SG", "netherlands": "NL", "switzerland": "CH",
}

_TS_FIELD = re.compile(r"(_at|_on|_time|_date)$")
# 'YYYY-MM-DDTHH:MM:SS...' — the ISO 'T' separator the seeds write with a space instead.
_ISOISH = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def normalize_value(collection: str, field: str, value) -> Optional[str]:
    """Canonical comparison form of ``value`` for ``collection.field``.

    ``None`` and ``""`` collapse together (an omitted optional string and an explicitly
    empty one are the same statement). Non-string scalars pass through unchanged.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s == "":
        return None
    if (collection, field) == ("location", "state"):
        return _US_STATES.get(s.lower(), s.upper() if len(s) == 2 else s)
    if (collection, field) == ("location", "country"):
        return _COUNTRIES.get(s.lower(), s)
    if _TS_FIELD.search(field) and _ISOISH.match(s):
        return s.replace("T", " ", 1)
    return s


def values_equivalent(collection: str, field: str, gold, pred) -> bool:
    """True when gold and pred are the same value modulo canonical surface form."""
    if gold == pred:
        return True
    return normalize_value(collection, field, gold) == normalize_value(collection, field, pred)
