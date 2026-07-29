"""Delta — a structural patch applied to a domain DB at task-load time (item 7).

A delta is keyed ``collection -> record_id -> EntityOp`` where each ``EntityOp``
is exactly one of three explicit verbs so intent is unambiguous:

- ``set``    — merge fields into an existing record (record must exist)
- ``create`` — insert a new record (record must not exist)
- ``delete`` — remove a record

The delta is applied on top of the seed ``db.json`` before a run starts.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from enterprise_worlds.environment.db import DB


class EntityOp(BaseModel):
    """Exactly one of `set` / `create` / `delete` on a single record."""

    model_config = ConfigDict(extra="forbid")

    set: dict[str, Any] | None = Field(
        default=None,
        description="Merge these fields into the existing record (record must exist).",
    )
    create: dict[str, Any] | None = Field(
        default=None,
        description="Create a new record with these fields (record must not exist).",
    )
    delete: Literal[True] | None = Field(
        default=None,
        description="Remove the record. Only `true` is allowed.",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "EntityOp":
        ops = [n for n in ("set", "create", "delete") if getattr(self, n) is not None]
        if len(ops) != 1:
            raise ValueError(
                f"EntityOp must specify exactly one of set/create/delete (got {ops or 'none'})"
            )
        return self

    @property
    def kind(self) -> Literal["set", "create", "delete"]:
        for n in ("set", "create", "delete"):
            if getattr(self, n) is not None:
                return n  # type: ignore[return-value]
        raise RuntimeError("unreachable — validator should have caught this")

    @property
    def payload(self) -> dict[str, Any] | Literal[True]:
        return getattr(self, self.kind)


class Delta(RootModel[dict[str, dict[str, EntityOp]]]):
    """A patch applied to a domain DB at task-load time.

    Top-level keys are DB collection names (e.g. 'incidents', 'users') —
    they must match the field names on the domain's DB Pydantic model.
    Inner keys are record IDs. Each value is exactly one EntityOp.
    """

    def __iter__(self):
        return iter(self.root.items())

    def __getitem__(self, collection: str) -> dict[str, EntityOp]:
        return self.root[collection]

    def __contains__(self, collection: str) -> bool:
        return collection in self.root

    @property
    def record_count(self) -> int:
        return sum(len(v) for v in self.root.values())


def apply_delta(db: DB, delta: Delta | dict | None) -> DB:
    """Return a NEW typed DB with ``delta`` applied on top of ``db``.

    Works on the dict form of the DB so each verb is explicit, then re-validates
    through the DB model — a malformed delta (e.g. a bad enum value, an unknown
    collection) fails loudly here at load time rather than silently at hash time.
    The input ``db`` is never mutated.
    """
    if delta is None:
        return db.model_copy(deep=True)
    if isinstance(delta, dict):
        delta = Delta.model_validate(delta)

    raw = db.model_dump()
    valid_collections = set(raw.keys())

    for collection, records in delta:
        if collection not in valid_collections:
            raise ValueError(
                f"delta references unknown collection {collection!r}; "
                f"DB has {sorted(valid_collections)}"
            )
        coll: dict[str, Any] = raw[collection]
        for record_id, op in records.items():
            if op.kind == "delete":
                coll.pop(record_id, None)
            elif op.kind == "create":
                if record_id in coll:
                    raise ValueError(
                        f"create on existing record {collection}/{record_id}"
                    )
                coll[record_id] = dict(op.payload)  # type: ignore[arg-type]
            else:  # set — shallow field-merge
                if record_id not in coll:
                    raise ValueError(
                        f"set on missing record {collection}/{record_id}"
                    )
                coll[record_id] = {**coll[record_id], **op.payload}  # type: ignore[dict-item]

    return type(db).model_validate(raw)
