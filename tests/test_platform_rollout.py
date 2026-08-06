"""``rollout.*`` — async jobs, run modelless through fakes patched over the rollout module's seams.

The fakes stand in for the three things a real rollout pays for (the target agent, the user
simulator, the judges); everything else — the orchestrator, the environment, the tools, the
serialiser, the job machinery — is real. The gate fixture holds a trial open mid-flight so the
tests can observe `running`, join an in-flight idempotency key, and cancel before completion.
"""
from __future__ import annotations

import threading
import time

import pytest

import enterprise_worlds.platform.rollout as rollout_mod
from enterprise_worlds.data_model.message import AssistantMessage, ToolCall, UserMessage
from enterprise_worlds.domains.itsm.environment import get_tasks
from enterprise_worlds.evaluator.evaluator import RewardInfo
from enterprise_worlds.evaluator.evaluator_env import DBCheck
from enterprise_worlds.platform import wire
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.rollout import JobRegistry, _litellm_model
from enterprise_worlds.user.base import STOP

from test_platform_server import INITIALIZE, drive, req

CONFIG = {
    "targetModel": "sarvam:sarvam-105b",
    "kRuns": 2,
    "gymConfig": {"userModel": "anthropic:claude-sonnet-4-6", "judgeModel": "anthropic:claude-sonnet-4-6",
                  "maxSteps": 8, "db": "msp_db.json"},
}


def _task_payload() -> dict:
    task = next(
        t for t in get_tasks()
        if t.org_ids and len(t.org_ids) >= 2 and (t.seed_db or "msp_db.json") == "msp_db.json"
    ).model_dump()
    # Keep the fixture light: no gold actions → the oracle replay and gold rebuild are trivial.
    task["evaluation_criteria"] = {"actions": [], "nl_assertions": []}
    return task


class FakeUser:
    """Scripted counterparty: one request, then the stop token."""

    def __init__(self, scenario, llm=None):
        self.turns = 0

    def get_init_state(self):
        return None

    def generate_next_message(self, message, state):
        self.turns += 1
        content = "please add the site" if self.turns == 1 else STOP
        return UserMessage(content=content), state

    def is_stop(self, message):
        return STOP in (message.content or "")


class FakeAgent:
    """Scripted target: one tool call, then a text reply. Records what model it was given."""

    instances: list = []

    def __init__(self, policy, tool_schemas, llm):
        self.llm = llm
        self.n = 0
        FakeAgent.instances.append(self)

    def get_init_state(self):
        return {}

    def generate_next_message(self, message, state):
        self.n += 1
        if self.n == 1:
            call = ToolCall(id="c1", name="add_location",
                            arguments={"name": "Rollout Site", "city": "X", "country": "Y"})
            return AssistantMessage(tool_calls=[call]), state
        return AssistantMessage(content="done"), state


class GatedAgent(FakeAgent):
    """A FakeAgent that parks on its first turn until the test releases it."""

    started = threading.Event()
    release = threading.Event()

    def generate_next_message(self, message, state):
        GatedAgent.started.set()
        assert GatedAgent.release.wait(timeout=10), "test never released the gate"
        return super().generate_next_message(message, state)


def _fake_evaluate(env_ctor, task, trajectory, final_env, nl_llm=None, db_text_match=None, **kw):
    return RewardInfo(reward=1.0, db_check=DBCheck(db_match=True, reward=1.0))


@pytest.fixture()
def modelless(monkeypatch):
    """A rollout module that spends no tokens."""
    FakeAgent.instances = []
    GatedAgent.started = threading.Event()
    GatedAgent.release = threading.Event()
    monkeypatch.setattr(rollout_mod, "LLMAgent", FakeAgent)
    monkeypatch.setattr(rollout_mod, "UserSimulator", FakeUser)
    monkeypatch.setattr(rollout_mod, "evaluate_task", _fake_evaluate)
    return monkeypatch


def _wait(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _wait_terminal(reg, job_id):
    assert _wait(lambda: reg.status(job_id)["state"] in ("done", "error", "cancelled"))
    return reg.status(job_id)


# ── the happy path and its shapes ─────────────────────────────────────────────

def test_submit_runs_k_trials_and_the_result_has_the_contract_shape(modelless):
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), CONFIG)
    status = _wait_terminal(reg, job_id)
    assert status == {"state": "done", "trialsDone": 2, "trialsTotal": 2}

    result = reg.result(job_id)
    assert result["targetModel"] == "sarvam:sarvam-105b"  # echoed as sent, not as litellm spells it
    assert result["kRuns"] == 2 and result["passRate"] == 1.0
    assert [r["runIdx"] for r in result["perRun"]] == [0, 1]
    for run in result["perRun"]:
        assert run["passed"] is True and run["toolCalls"] == 1
        diag = run["diagnostics"]
        assert diag["db_match"] is True
        assert set(diag) >= {"db_match", "nl_checks", "db_text_judgments", "final_state"}
        assert "Rollout Site" in str(diag["final_state"]["location"])
    assert result["artifacts"]["oracle"]["success"] is True
    assert "users" in result["artifacts"]["initial_db"]
    # The target agent got the litellm spelling; the wire keeps the platform's.
    assert {a.llm for a in FakeAgent.instances} == {"sarvam/sarvam-105b"}


def test_the_transcript_round_trips_with_toolcalls_null_distinct_from_absent(modelless):
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    _wait_terminal(reg, job_id)
    result = reg.result(job_id)

    transcript = result["perRun"][0]["transcript"]
    greeting = transcript[0]
    assert greeting["role"] == "assistant"
    assert "toolCalls" in greeting and greeting["toolCalls"] is None  # explicit null, not absent
    tool_call_msg = next(m for m in transcript if m.get("toolCalls"))
    assert tool_call_msg["toolCalls"][0] == {
        "id": "c1", "name": "add_location",
        "arguments": {"name": "Rollout Site", "city": "X", "country": "Y"},
        "requestor": "assistant",
    }
    tool_result_msg = next(m for m in transcript if m["role"] == "tool")
    assert "toolCalls" not in tool_result_msg  # absent, not null
    assert tool_result_msg["id"] == "c1" and tool_result_msg["error"] is False

    # And the whole result survives the wire codec unchanged.
    decoded, err = wire.decode(wire.encode(wire.success(1, result)))
    assert err is None
    assert decoded["result"] == result


# ── job semantics ─────────────────────────────────────────────────────────────

def test_status_reports_running_while_a_trial_is_in_flight(modelless):
    modelless.setattr(rollout_mod, "LLMAgent", GatedAgent)
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    assert GatedAgent.started.wait(timeout=10)
    assert reg.status(job_id) == {"state": "running", "trialsDone": 0, "trialsTotal": 1}
    with pytest.raises(WireFailure) as exc:
        reg.result(job_id)
    assert exc.value.code == wire.WireErrorCode.JOB_NOT_COMPLETE
    assert exc.value.retryable is True
    GatedAgent.release.set()
    assert _wait_terminal(reg, job_id)["state"] == "done"


def test_an_in_flight_idempotency_key_joins_the_run_instead_of_doubling_it(modelless):
    modelless.setattr(rollout_mod, "LLMAgent", GatedAgent)
    reg = JobRegistry()
    first = reg.submit(_task_payload(), dict(CONFIG, kRuns=1), idempotency_key="work-1")
    assert GatedAgent.started.wait(timeout=10)
    second = reg.submit(_task_payload(), dict(CONFIG, kRuns=1), idempotency_key="work-1")
    assert second == first
    GatedAgent.release.set()
    _wait_terminal(reg, first)
    # One job, one run: the second submit constructed no second agent.
    assert len(FakeAgent.instances) == 1


def test_cancel_stops_the_work_and_result_is_job_cancelled(modelless):
    modelless.setattr(rollout_mod, "LLMAgent", GatedAgent)
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), dict(CONFIG, kRuns=2))
    assert GatedAgent.started.wait(timeout=10)
    reg.cancel(job_id)
    GatedAgent.release.set()
    assert _wait_terminal(reg, job_id)["state"] == "cancelled"
    with pytest.raises(WireFailure) as exc:
        reg.result(job_id)
    assert exc.value.code == wire.WireErrorCode.JOB_CANCELLED
    # The cancelled job never reached trial 2: at most one agent (trial 1's) was ever built.
    assert len(FakeAgent.instances) == 1


def test_a_gym_failure_inside_a_trial_ends_the_job_in_error_not_the_process(modelless):
    class ExplodingAgent(FakeAgent):
        def generate_next_message(self, message, state):
            raise RuntimeError("provider melted")

    modelless.setattr(rollout_mod, "LLMAgent", ExplodingAgent)
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))  # does not throw
    status = _wait_terminal(reg, job_id)
    assert status["state"] == "error"
    assert "provider melted" in status["error"]
    with pytest.raises(WireFailure) as exc:
        reg.result(job_id)
    assert exc.value.code == wire.WireErrorCode.JOB_NOT_COMPLETE


def test_unknown_job_is_job_not_found(modelless):
    reg = JobRegistry()
    for call in (lambda: reg.status("j404"), lambda: reg.result("j404"), lambda: reg.cancel("j404")):
        with pytest.raises(WireFailure) as exc:
            call()
        assert exc.value.code == wire.WireErrorCode.JOB_NOT_FOUND


def test_finished_jobs_are_retired_and_collected_payloads_released(modelless):
    reg = JobRegistry(max_terminal=1)
    first = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    _wait_terminal(reg, first)
    second = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    _wait_terminal(reg, second)

    # The bound evicted the older terminal job entirely — an honest 3001, not a stale status.
    with pytest.raises(WireFailure) as exc:
        reg.status(first)
    assert exc.value.code == wire.WireErrorCode.JOB_NOT_FOUND

    # Collecting releases the payload; a re-collect says so instead of pretending.
    assert reg.result(second)["kRuns"] == 1
    with pytest.raises(WireFailure) as exc:
        reg.result(second)
    assert exc.value.code == wire.WireErrorCode.JOB_NOT_FOUND
    assert "released" in str(exc.value)


def test_bad_config_is_rejected_at_submit(modelless):
    reg = JobRegistry()
    for config in ({}, {"targetModel": "m"}, {"targetModel": "m", "kRuns": 0},
                   {"targetModel": "m", "kRuns": True}):
        with pytest.raises(WireFailure) as exc:
            reg.submit(_task_payload(), config)
        assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS, config


def test_model_spelling_translation():
    assert _litellm_model("anthropic:claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"
    assert _litellm_model("bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0") == \
        "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"  # the colon is part of the model id
    assert _litellm_model("gpt-4o") == "gpt-4o"


def test_rollout_dispatch_over_the_wire(modelless):
    replies = drive(
        INITIALIZE,
        req(2, "rollout.submit", {"task": _task_payload(), "config": dict(CONFIG, kRuns=1)}),
        req(3, "rollout.status", {"jobId": "j1"}),
    )
    assert replies[1]["result"] == {"jobId": "j1"}
    status = replies[2]["result"]
    assert status["state"] in ("queued", "running", "done")
    assert status["trialsTotal"] == 1
