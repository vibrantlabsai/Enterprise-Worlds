"""Deterministic hashing of DB state for evaluation.

Serialize to canonical
JSON (sorted keys, str fallback for non-JSON types) and SHA-256 it. Two DBs that
serialize to the same canonical JSON hash equal, regardless of dict ordering.
"""

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def get_dict_hash(data: dict[str, Any]) -> str:
    """Get a stable SHA-256 hash of a dict."""
    canonical = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_pydantic_hash(model: BaseModel) -> str:
    """Get a stable SHA-256 hash of a pydantic model."""
    return get_dict_hash(model.model_dump())
