"""Gym interface tests (offline — mocked user LLM, no real API call, no oracle).

Verifies the Gymnasium reset/step contract, action parsing, and that driving a task's gold
actions through the gym yields a sparse terminal reward of 1.0 (gold-action DB-match). Skipped
if ``gymnasium`` is not installed.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("gymnasium")  # optional [gym] extra
import gymnasium as gym

from enterprise_worlds.data_model.message import AssistantMessage
from enterprise_worlds.domains.itsm import environment as itsm_env
from enterprise_worlds.gym.gym_agent import AgentGymEnv, parse_action_string
from enterprise_worlds.user.base import STOP

TASK_ID = "itsm_restore_retired_ci_001"


def test_parse_action_string():
    assert parse_action_string("Hello, how can I help?").content == "Hello, how can I help?"
    assert parse_action_string("done()").content == STOP
    assert parse_action_string("done").content == STOP

    fc = parse_action_string('list_users(first_name="Aisha", active=True)')
    assert fc.tool_calls[0].name == "list_users"
    assert fc.tool_calls[0].arguments == {"first_name": "Aisha", "active": True}

    jc = parse_action_string('{"name": "get_user", "arguments": {"user_id": "USER_001"}}')
    assert jc.tool_calls[0].name == "get_user"
    assert jc.tool_calls[0].arguments == {"user_id": "USER_001"}


def test_register_and_make():
    from enterprise_worlds.gym import EW_ENV_ID, register_gym_agent

    register_gym_agent()
    env = gym.make(EW_ENV_ID, domain="itsm", task_id=TASK_ID, disable_env_checker=True)
    assert env is not None
    env.close()


def test_agent_gym_episode(mocker):
    # The user simulator runs in the orchestrator thread; mock its LLM so no real call is made
    # (and it never emits STOP, so only the agent's done() ends the episode).
    mocker.patch(
        "enterprise_worlds.user.user_simulator.generate",
        return_value=AssistantMessage(content="Please go ahead and take care of it."),
    )
    task = next(t for t in itsm_env.get_tasks() if t.id == TASK_ID)
    env = AgentGymEnv(domain="itsm", task_id=TASK_ID, max_steps=50)

    obs, info = env.reset()
    assert isinstance(obs, str) and isinstance(info, dict)
    assert any(t["function"]["name"] == "done" for t in info["tools"])  # done tool advertised
    assert info["policy"]  # the agent policy is exposed to the learner

    # Drive the task's gold actions (as JSON tool calls). Reward is sparse: 0 until terminal.
    for a in task.evaluation_criteria.actions:
        obs, reward, terminated, truncated, info = env.step(
            json.dumps({"name": a.name, "arguments": a.arguments})
        )
        assert isinstance(obs, str) and isinstance(reward, float)
        assert isinstance(terminated, bool) and isinstance(truncated, bool)
        assert reward == 0.0 and terminated is False

    # End the episode — gold actions reproduce the gold DB state, so terminal reward is 1.0.
    obs, reward, terminated, truncated, info = env.step("done()")
    assert terminated is True and truncated is False
    assert reward == 1.0, f"expected terminal reward 1.0, got {reward}"
    assert info["reward_info"]["db_check"]["db_match"] is True
    env.close()


def test_step_before_reset_raises():
    env = AgentGymEnv(domain="itsm", task_id=TASK_ID)
    with pytest.raises(RuntimeError):
        env.step("done()")
