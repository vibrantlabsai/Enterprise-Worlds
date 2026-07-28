"""Reward-channel regression fixtures (Tier 1).

Each fixture reproduces a reward-channel defect class measured by the itsm-v1 QA audits as an
END-TO-END ``evaluate_task`` scoring (gold actions + recorded agent tool calls), and asserts
the fixed verdict — including the negative controls that prove the fixes did not over-reach
(the deferred benign-extras class must still fail).

All LLM-free: db-channel only (``skip_nl_assertions=True``), free-text compared with the
``exact`` strategy so prose divergence is deterministic.
"""

from __future__ import annotations

import pytest

from enterprise_worlds.data_model.message import ToolCall
from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.domains.itsm import environment as itsm_env
from enterprise_worlds.evaluator.evaluator import evaluate_task
from enterprise_worlds.evaluator.text_match_strategy import TextMatchConfig
from enterprise_worlds.utils.clock import reset_now, set_now

EXACT = TextMatchConfig(strategy="exact")

BASE_TASK = {
    "id": "v2-fixture/task",
    "scenario": {
        "persona": {
            "identity": {
                "user_id": "USER_001", "first_name": "Test", "last_name": "Operator",
                "email": "test.operator@techcorp.com", "role": "admin",
            },
            "personality": "terse",
            "role_description": "IT operator",
            "known_info": {},
        },
        "task_description": "Do the thing.",
        "simulator_guidance": "Confirm anything proposed.",
    },
    "evaluation_criteria": {"actions": [], "nl_assertions": []},
}


@pytest.fixture(autouse=True)
def _frozen_clock():
    set_now("2024-06-01 00:00:00")
    yield
    reset_now()


def _task(actions: list[dict]) -> Task:
    data = {**BASE_TASK, "evaluation_criteria": {"actions": actions, "nl_assertions": []}}
    return Task.model_validate(data)


def _env_ctor(db_delta=None):
    return itsm_env.get_environment(db_delta=db_delta, acting_user_id="USER_001")


def _score(task: Task, agent_calls: list[dict]) -> float:
    calls = [ToolCall(name=c["name"], arguments=c["arguments"], requestor="assistant")
             for c in agent_calls]
    info = evaluate_task(
        _env_ctor, task, trajectory=[], agent_tool_calls=calls,
        skip_nl_assertions=True, db_text_match=EXACT,
    )
    return info.reward


NOTIFY_GOLD = {
    "name": "send_notification",
    "arguments": {"incident_id": "INC_003", "email": "christina.oliver@carsonllc.com",
                  "type": "update", "subject": "Work resumed on INC0000003",
                  "message": "The replacement part arrived; work has resumed."},
}


def test_free_text_sole_diff_is_advisory():
    """P1 (a58413_iter_012 class): only the prose differs — advisory, never gating."""
    task = _task([NOTIFY_GOLD])
    agent = [{**NOTIFY_GOLD, "arguments": {**NOTIFY_GOLD["arguments"],
              "subject": "Update on your incident INC0000003",
              "message": "Good news: the part we were waiting on arrived, so we've resumed."}}]
    assert _score(task, agent) == 1.0


def test_notification_status_is_not_a_tool_parameter():
    """P3 (3f5459_iter_007 class): delivery status is system-managed — the parameter no longer
    exists, so neither the agent schema nor a tool call can set it; the DB value is always
    'sent' and gold and agent can never disagree on it."""
    env = _env_ctor()
    for tool in ("send_notification", "update_notification"):
        schema = next(s for s in env.get_tool_schemas() if s["function"]["name"] == tool)
        assert "status" not in schema["function"]["parameters"]["properties"]
    with pytest.raises(TypeError):
        env.make_tool_call("send_notification", incident_id="INC_003",
                           email="christina.oliver@carsonllc.com", status="queued")
    notif = env.make_tool_call("send_notification", incident_id="INC_003",
                               email="christina.oliver@carsonllc.com", type="update",
                               subject="s", message="m")
    assert notif.status == "sent"


def test_state_long_form_matches():
    """P4 (dada77_iter_010 class): gold 'TX', agent heard and stored 'Texas' — same referent."""
    gold = {"name": "add_location", "arguments": {
        "name": "Austin Hub", "city": "Austin", "country": "USA", "state": "TX", "active": True}}
    task = _task([gold])
    agent = [{**gold, "arguments": {**gold["arguments"], "state": "Texas"}}]
    assert _score(task, agent) == 1.0


def test_broken_gold_invalidates():
    """P6 (iter_030 class): gold uses a wrong parameter name — scoring refuses to grade anyone
    against the silently-wrong reference DB."""
    broken_gold = {"name": "update_incident",
                   "arguments": {"incident_id": "INC_003", "work_notes": "reassigned to L2"}}
    correct_agent = [{"name": "update_incident",
                      "arguments": {"incident_id": "INC_003", "worknotes": "reassigned to L2"}}]
    task = _task([broken_gold])

    calls = [ToolCall(name=c["name"], arguments=c["arguments"], requestor="assistant")
             for c in correct_agent]
    info = evaluate_task(_env_ctor, task, trajectory=[], agent_tool_calls=calls,
                         skip_nl_assertions=True, db_text_match=EXACT)
    assert info.reward == 0.0 and not info.db_check.gold_replay_ok
    assert any("GOLD REPLAY BROKEN" in m for m in info.db_check.mismatches)


def test_negative_control_gold_null_extra_value_still_fails():
    """Illinois class (deferred benign-extras): gold left state unset, agent supplied a correct
    value. The verifier must NOT fix this — same referent isn't the question; gold is null."""
    gold = {"name": "add_location", "arguments": {
        "name": "Chicago Branch Office", "city": "Chicago", "country": "USA", "active": True}}
    task = _task([gold])
    agent = [{**gold, "arguments": {**gold["arguments"], "state": "Illinois"}}]
    assert _score(task, agent) == 0.0


def test_negative_control_wrong_structured_value_still_fails():
    """Wrong recipient is a genuine agent error."""
    task = _task([NOTIFY_GOLD])
    agent = [{**NOTIFY_GOLD, "arguments": {**NOTIFY_GOLD["arguments"],
              "email": "joanne.simpson@servicenow.com"}}]
    assert _score(task, agent) == 0.0


def test_clean_run_passes():
    task = _task([NOTIFY_GOLD])
    assert _score(task, [NOTIFY_GOLD]) == 1.0
