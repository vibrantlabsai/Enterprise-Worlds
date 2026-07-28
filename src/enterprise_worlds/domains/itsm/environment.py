"""ITSM environment + task loader factory."""

import os
from pathlib import Path
from typing import Iterable, Optional

from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.domains.itsm.data_model import ItsmDB, slice_db_to_orgs
from enterprise_worlds.domains.itsm.tools import ItsmTools
from enterprise_worlds.environment.delta import Delta, apply_delta
from enterprise_worlds.environment.environment import Environment
from enterprise_worlds.utils.io_utils import load_file

_DATA_DIR = Path(__file__).resolve().parents[3].parent / "data" / "itsmbench"
#: Default seed file. The data dir holds multiple seeds (e.g. ``msp_db.json``, ``single_tenant_db.json``);
#: a run selects one via the ``EW_ITSM_DB`` env var (filename; the legacy ``EOPS_ITSM_DB`` spelling is
#: still honoured), so the DB is chosen at run time rather than by editing code. ``ITSM_DB_PATH`` is the
#: default-file path (used by tests / as the fallback).
_DEFAULT_DB_FILE = "msp_db.json"
ITSM_DB_PATH = _DATA_DIR / _DEFAULT_DB_FILE
ITSM_POLICY_PATH = _DATA_DIR / "policy.md"
ITSM_TASKS_PATH = _DATA_DIR / "tasks.json"
ITSM_TASKS_DIR = _DATA_DIR / "tasks"


def _itsm_db_path(seed_db: Optional[str] = None) -> Path:
    """Resolve the seed db path.

    ``seed_db`` is the task's own seed (a filename in the data dir) and wins when set — the suite is
    mixed-seed, so the seed belongs to the task rather than the run. Otherwise ``EW_ITSM_DB``
    selects it (``EOPS_ITSM_DB`` is the legacy spelling, kept working for existing consumers).
    """
    name = (
        seed_db
        or os.environ.get("EW_ITSM_DB")
        or os.environ.get("EOPS_ITSM_DB")
        or _DEFAULT_DB_FILE
    )
    # Filename only: a seed is picked *from* the data dir, never an arbitrary path out of it.
    return _DATA_DIR / Path(name).name

DOMAIN_NAME = "itsm"


def get_environment(
    db_delta: Optional[Delta | dict] = None,
    acting_user_id: Optional[str] = None,
    org_id: Optional[str] = None,
    org_ids: Optional[Iterable[str]] = None,
    seed_db: Optional[str] = None,
) -> Environment:
    """Build a fresh ITSM environment: load the seed DB and apply the task delta (item 7).

    ``acting_user_id`` is the authenticated caller from the task context. ``org_ids`` is the task's
    **tenancy scope** — the set of orgs its data lives in (a 1-element set for single-tenant, the
    ``{provider, client}`` pair for an MSP task). When given, the DB is sliced to that set (after the
    delta) so numbers/names that collide *outside* the scope resolve unambiguously. ``org_id`` is the
    legacy single-org alias (treated as a 1-element scope). Both ``None`` ⇒ no slice (multi-org).
    ``seed_db`` is the task's seed world (see ``_itsm_db_path``).
    """
    db = ItsmDB.load(_itsm_db_path(seed_db))
    db = apply_delta(db, db_delta)
    scope = set(org_ids) if org_ids is not None else ({org_id} if org_id is not None else None)
    if scope is not None:
        db = slice_db_to_orgs(db, scope)
    policy = ITSM_POLICY_PATH.read_text(encoding="utf-8") if ITSM_POLICY_PATH.exists() else ""
    return Environment(
        domain_name=DOMAIN_NAME,
        policy=policy,
        tools=ItsmTools(db, acting_user_id=acting_user_id, org_id=org_id),
    )


def get_tasks() -> list[Task]:
    """Load and validate the ITSM tasks (item 6).

    Tasks are gathered from two optional sources and merged: ``tasks.json`` (a JSON list of
    tasks) and a ``tasks/`` directory (each ``*.json`` file holds one task object or a list of
    them). Duplicate task ids across the sources are rejected.

    ``EW_ITSM_TASKS`` (a JSON file or a directory) overrides both sources when set, so a run can
    evaluate an arbitrary task set without touching the shipped ``tasks.json`` — the run-time
    parallel to ``EW_ITSM_DB``. The legacy ``EOPS_ITSM_TASKS`` spelling is still honoured.
    """
    raw: list[dict] = []
    override = os.environ.get("EW_ITSM_TASKS") or os.environ.get("EOPS_ITSM_TASKS")
    if override:
        p = Path(override)
        sources = sorted(p.glob("*.json")) if p.is_dir() else [p]
        for fp in sources:
            data = load_file(fp)
            raw.extend(data if isinstance(data, list) else [data])
    else:
        if ITSM_TASKS_PATH.exists():
            raw.extend(load_file(ITSM_TASKS_PATH))
        if ITSM_TASKS_DIR.is_dir():
            for fp in sorted(ITSM_TASKS_DIR.glob("*.json")):
                data = load_file(fp)
                raw.extend(data if isinstance(data, list) else [data])

    tasks = [Task.model_validate(t) for t in raw]
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise ValueError(f"duplicate task id {t.id!r} across tasks.json / tasks/")
        seen.add(t.id)
    return tasks
