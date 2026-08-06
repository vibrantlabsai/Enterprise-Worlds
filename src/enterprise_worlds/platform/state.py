"""``gym.materializeState`` — the world a task starts from.

Order is **load → apply delta → slice to org_ids**, and it is not interchangeable: slicing first
would drop the rows the delta is about to create. The order is not re-derived here — building the
world is delegated to the gym's own ``get_environment``, which already does exactly that and is the
reference implementation the platform's TypeScript port is held to.

A scoped task whose ``fk_spec.json`` is missing must FAIL, not degrade: an org-row filter without
foreign-key closure produces a world silently missing the out-of-scope records a cross-org MSP task
turns on. In practice the gym cannot even import without the file (``_FK_FIELDS`` loads at import
time), but the explicit guard here keeps the failure honest — and testable — if that ever changes.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import ValidationError

from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.domains.itsm import data_model as _itsm_data
from enterprise_worlds.domains.itsm import environment as _itsm_env
from enterprise_worlds.environment.environment import Environment
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.wire import WireErrorCode
from enterprise_worlds.utils.clock import DEFAULT_NOW, _to_seed_format, reset_now, set_now


def decode_task(raw: Any) -> Task:
    """Validate an inbound task payload into the gym's own :class:`Task`.

    The platform stores tasks in this gym's native shape; ``task_id`` is accepted as a spelling of
    ``id`` because the miner-side runner uses it. A payload that does not validate is the caller's
    fault, not a gym fault — hence ``INVALID_PARAMS`` rather than an internal error.
    """
    if not isinstance(raw, dict):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "task must be an object",
                          kind="invalid_params", retryable=False)
    if "id" not in raw and "task_id" in raw:
        raw = {**raw, "id": raw["task_id"]}
    try:
        return Task.model_validate(raw)
    except ValidationError as e:
        raise WireFailure(WireErrorCode.INVALID_PARAMS, f"invalid task: {e}",
                          kind="invalid_params", retryable=False) from e


def check_fk_spec(scope: Optional[set]) -> None:
    """Refuse to build a scoped world without the foreign-key spec (see module docstring)."""
    if scope and not _itsm_data._FK_SPEC_PATH.exists():
        raise FileNotFoundError(
            "fk_spec.json is missing: a task scoped to org_ids requires foreign-key closure, "
            "and slicing without it would silently drop cross-org records "
            f"(expected at {_itsm_data._FK_SPEC_PATH})"
        )


def build_environment(
    *,
    db_delta: Any = None,
    acting_user_id: Optional[str] = None,
    org_id: Optional[str] = None,
    org_ids: Optional[list] = None,
    seed_db: Optional[str] = None,
    clock: Optional[str] = None,
) -> Environment:
    """One world, built the way the gym itself builds it, under the given frozen clock."""
    scope = set(org_ids) if org_ids is not None else ({org_id} if org_id is not None else None)
    check_fk_spec(scope)
    set_now(clock or DEFAULT_NOW)
    try:
        return _itsm_env.get_environment(
            db_delta=db_delta, acting_user_id=acting_user_id,
            org_id=org_id, org_ids=org_ids, seed_db=seed_db,
        )
    finally:
        reset_now()


def materialize_state(raw_task: Any) -> Dict[str, Any]:
    """The ``StateSnapshot`` for a task: its starting world, plus how it was built."""
    task = decode_task(raw_task)
    clock = task.current_time or DEFAULT_NOW
    env = build_environment(
        db_delta=task.initial_state_delta, acting_user_id=task.acting_user_id,
        org_id=task.org_id, org_ids=task.org_ids, seed_db=task.seed_db, clock=clock,
    )
    return {
        "state": env.tools.db.model_dump(),
        "provenance": {
            "seed_db": _itsm_env._itsm_db_path(task.seed_db).name,
            "org_ids": sorted(task.org_ids) if task.org_ids else ([task.org_id] if task.org_id else None),
            "delta_applied": task.initial_state_delta is not None,
            # The RESOLVED clock — the canonicalised form every timestamp in `state` is stamped
            # with — not the ISO spelling the task requested it in.
            "clock": _to_seed_format(clock),
        },
    }
