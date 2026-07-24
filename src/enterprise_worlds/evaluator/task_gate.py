"""Offline task-file validation gate (verifier v2, item P6b).

A task's gold actions ARE its reward spec: a gold action that silently fails (wrong parameter
name, missing record, invalid enum) produces a wrong reference DB and every agent is then
mis-scored against it. QA audits over itsm-v1 found exactly this shipped in production tasks
(a gold ``update_incident`` passing ``work_notes`` where the tool takes ``worknotes``).

``validate_tasks`` checks, per task, everything that can be verified without an LLM:

- **gold-signature** — every gold action names a real tool and only parameters that tool
  accepts (catches the ``work_notes`` class directly, before replay even runs);
- **gold-replay** — the gold actions replay cleanly on the task's seed(+delta) env;
- **gold-determinism** — replaying twice produces the same DB hash;
- **duplicate-id** — task ids are unique across the suite;
- **empty-guidance** (warning) — ``simulator_guidance`` is empty, which QA measured as the
  strongest predictor of sim-improvisation reward noise (the sim invents approvals or
  instructions the fixed gold cannot represent).

Run it from the CLI: ``eops validate-tasks --domain itsm [--tasks-file path]``.
"""

from __future__ import annotations

import inspect
from typing import Callable, Optional

from pydantic import BaseModel, Field

from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.environment.environment import Environment


class TaskIssue(BaseModel):
    task_id: str
    kind: str  # gold-signature | gold-replay | gold-determinism | duplicate-id | empty-guidance
    severity: str  # "error" | "warning"
    detail: str


class GateReport(BaseModel):
    total: int
    errors: list[TaskIssue] = Field(default_factory=list)
    warnings: list[TaskIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_gold_signatures(env: Environment, task: Task) -> list[TaskIssue]:
    """Every gold action must name a real tool and pass only params the tool accepts."""
    issues: list[TaskIssue] = []
    for action in task.evaluation_criteria.actions:
        fn = getattr(env.tools, action.name, None)
        if fn is None or not callable(fn):
            issues.append(TaskIssue(
                task_id=task.id, kind="gold-signature", severity="error",
                detail=f"gold action names unknown tool {action.name!r}",
            ))
            continue
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            continue  # tool takes **kwargs; nothing to check
        for arg in action.arguments:
            if arg not in params:
                issues.append(TaskIssue(
                    task_id=task.id, kind="gold-signature", severity="error",
                    detail=(
                        f"{action.name} does not accept parameter {arg!r} "
                        f"(accepts: {sorted(p for p in params if p != 'self')})"
                    ),
                ))
    return issues


def _replay_hash(env_ctor: Callable[..., Environment], task: Task) -> tuple[str, list[str]]:
    env = env_ctor(db_delta=task.initial_state_delta)
    errors: list[str] = []
    for action in task.evaluation_criteria.actions:
        try:
            env.make_tool_call(action.name, **action.arguments)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{action.name}: {type(e).__name__}: {e}")
    return env.tools.db.get_hash(), errors


def validate_task(
    env_ctor: Callable[..., Environment],
    task: Task,
    check_determinism: bool = True,
) -> list[TaskIssue]:
    issues: list[TaskIssue] = []

    probe_env = env_ctor(db_delta=task.initial_state_delta)
    issues.extend(_check_gold_signatures(probe_env, task))

    if task.evaluation_criteria.actions:
        h1, replay_errors = _replay_hash(env_ctor, task)
        for e in replay_errors:
            issues.append(TaskIssue(
                task_id=task.id, kind="gold-replay", severity="error",
                detail=f"gold action failed on replay: {e}",
            ))
        if check_determinism and not replay_errors:
            h2, _ = _replay_hash(env_ctor, task)
            if h1 != h2:
                issues.append(TaskIssue(
                    task_id=task.id, kind="gold-determinism", severity="error",
                    detail="replaying the gold actions twice produced different DB hashes",
                ))

    guidance = (task.scenario.simulator_guidance or "").strip()
    if not guidance:
        issues.append(TaskIssue(
            task_id=task.id, kind="empty-guidance", severity="warning",
            detail="simulator_guidance is empty — the sim will improvise at gold-adjacent "
                   "decision points, making reward depend on sim sampling",
        ))
    return issues


def validate_tasks(
    tasks: list[Task],
    env_ctor_for: Callable[[Task], Callable[..., Environment]],
    check_determinism: bool = True,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> GateReport:
    report = GateReport(total=len(tasks))

    seen: dict[str, int] = {}
    for t in tasks:
        seen[t.id] = seen.get(t.id, 0) + 1
    for tid, n in seen.items():
        if n > 1:
            report.errors.append(TaskIssue(
                task_id=tid, kind="duplicate-id", severity="error",
                detail=f"task id appears {n} times",
            ))

    for i, task in enumerate(tasks):
        for issue in validate_task(env_ctor_for(task), task, check_determinism):
            (report.errors if issue.severity == "error" else report.warnings).append(issue)
        if on_progress is not None:
            on_progress(i + 1, len(tasks))
    return report
