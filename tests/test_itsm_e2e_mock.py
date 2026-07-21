"""End-to-end pipeline test without any LLM (deterministic, no API key / no oracle).

Drives the real Orchestrator -> Environment -> Evaluator with a scripted agent that replays a
task's gold actions and a scripted user that opens then stops. Proves the full loop (turn
taking, tool dispatch, DB-match + NL evaluation) scores a correct trajectory as reward 1.0.
The NL-assertion judge is mocked to "all met" so no network call is made.
"""

from __future__ import annotations

import json

import pytest

from enterprise_worlds.data_model.message import AssistantMessage, ToolCall, UserMessage
from enterprise_worlds.domains.itsm import environment as itsm_env
from enterprise_worlds.evaluator.evaluator import evaluate_task
from enterprise_worlds.orchestrator.orchestrator import Orchestrator
from enterprise_worlds.user.base import STOP

TASKS = itsm_env.get_tasks()


class ScriptedAgent:
    """Emits the task's gold actions one tool call per turn, then a closing summary."""

    def __init__(self, task):
        self.actions = task.evaluation_criteria.actions

    def get_init_state(self):
        return {"i": 0}

    def generate_next_message(self, message, state):
        i = state["i"]
        if i < len(self.actions):
            a = self.actions[i]
            state["i"] = i + 1
            return AssistantMessage(
                tool_calls=[ToolCall(id=f"c{i}", name=a.name, arguments=a.arguments)]
            ), state
        return AssistantMessage(content="All requested changes are complete."), state


class ScriptedUser:
    """Responds to the agent's greeting with the task description, then stops."""

    def __init__(self, task):
        self.desc = task.scenario.task_description

    def get_init_state(self):
        return {"opened": False}

    @staticmethod
    def is_stop(message) -> bool:
        return STOP in (message.content or "")

    def generate_next_message(self, agent_message, state):
        if not state["opened"]:
            state["opened"] = True
            return UserMessage(content=self.desc), state
        return UserMessage(content=STOP), state


def _env_ctor_for(task):
    def ctor(db_delta=None):
        return itsm_env.get_environment(
            db_delta=db_delta, acting_user_id=task.acting_user_id,
        )

    return ctor


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_e2e_gold_trajectory_scores_full_reward(task, mocker):
    # Mock the NL-assertion judge to mark all assertions met (no network).
    judged = json.dumps({
        "results": [{"expectedOutcome": a, "reasoning": "ok", "metExpectation": True}
                    for a in task.evaluation_criteria.nl_assertions]
    })
    mocker.patch(
        "enterprise_worlds.evaluator.evaluator_nl.generate",
        return_value=AssistantMessage(content=judged),
    )

    ctor = _env_ctor_for(task)
    env = ctor(db_delta=task.initial_state_delta)
    run = Orchestrator(ScriptedAgent(task), ScriptedUser(task), env, max_steps=40).run()

    # The agent's gold tool calls were dispatched against the env; the user stopped.
    assert run.stopped, f"{task.id}: conversation did not terminate"
    assert len(run.agent_tool_calls) == len(task.evaluation_criteria.actions)

    reward_info = evaluate_task(ctor, task, trajectory=run.trajectory, final_env=env)
    # Tasks with gold actions are DB-matched; NL-only tasks gate on the (mocked-all-met) judge.
    if task.evaluation_criteria.actions:
        assert reward_info.db_check is not None and reward_info.db_check.db_match, \
            f"{task.id}: final DB did not match gold"
    assert reward_info.reward == 1.0, f"{task.id}: reward {reward_info.reward} != 1.0"
