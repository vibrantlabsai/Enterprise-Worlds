"""Offline task-suite validation (no LLM, no oracle).

For every ported ITSM task: confirm it loads/validates, its gold actions replay cleanly on its
seed (+ delta) through the real tools, and the DB-match evaluator scores the gold trajectory as
a perfect match (reward 1.0). This proves the task + evaluation pipeline are internally
consistent and deterministic — the foundation the gold-action DB-hash benchmark relies on.
"""

from __future__ import annotations

import pytest

from enterprise_worlds.data_model.message import ToolCall
from enterprise_worlds.domains.itsm import environment as itsm_env
from enterprise_worlds.evaluator.evaluator_env import calculate_db_reward

TASKS = itsm_env.get_tasks()


def _env_ctor_for(task):
    def ctor(db_delta=None):
        return itsm_env.get_environment(
            db_delta=db_delta,
            acting_user_id=task.acting_user_id,
        )

    return ctor


def test_there_are_tasks():
    assert TASKS, "no ITSM tasks loaded"


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_gold_actions_replay_and_db_match(task):
    gold_calls = [
        ToolCall(name=a.name, arguments=a.arguments, requestor="assistant")
        for a in task.evaluation_criteria.actions
    ]
    if not gold_calls:
        pytest.skip(f"{task.id}: NL-only task (no gold actions to DB-match)")
    # Predicted == gold trajectory => DB-match must be perfect (validates replay + determinism).
    db_check = calculate_db_reward(_env_ctor_for(task), task, agent_tool_calls=gold_calls)
    assert db_check.db_match, f"{task.id}: gold-action replay did not match the gold DB state"
    assert db_check.reward == 1.0


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_gold_actions_are_deterministic(task):
    if not task.evaluation_criteria.actions:
        pytest.skip(f"{task.id}: NL-only task (no gold actions)")
    ctor = _env_ctor_for(task)

    def replay():
        env = ctor(db_delta=task.initial_state_delta)
        for a in task.evaluation_criteria.actions:
            env.make_tool_call(a.name, **a.arguments)
        return env.get_db_hash()

    assert replay() == replay(), f"{task.id}: gold-action replay is non-deterministic"
