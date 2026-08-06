"""``rollout.*`` — async jobs, run modelless through fakes patched over the rollout module's seams.

The fakes stand in for the three things a real rollout pays for (the target agent, the user
simulator, the judges); everything else — the orchestrator, the environment, the tools, the
serialiser, the job machinery — is real. The gate fixtures hold a trial (or its judge phase) open
mid-flight so the tests can observe `running`, join an in-flight idempotency key, and land a cancel
at the exact moments the adversarial review proved were unprotected.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import enterprise_worlds.platform.rollout as rollout_mod
from enterprise_worlds.data_model.message import AssistantMessage, ToolCall, UserMessage
from enterprise_worlds.domains.itsm.environment import ITSM_DB_PATH, get_tasks
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
        assert set(diag) >= {"db_match", "nl_checks", "db_text_judgments", "final_state", "db_diff"}
        assert "Rollout Site" in str(diag["final_state"]["location"])
        # The trial added a location the (empty) gold replay did not: the diff must say so.
        assert diag["db_diff"]["structural_match"] is False
    assert result["artifacts"]["oracle"]["success"] is True
    assert "users" in result["artifacts"]["initial_db"]
    # The evidence of record shows what actually ran it — litellm spellings, resolved defaults.
    assert result["artifacts"]["config_resolved"] == {
        "targetModel": "sarvam/sarvam-105b",
        "userModel": "anthropic/claude-sonnet-4-6",
        "judgeModel": "anthropic/claude-sonnet-4-6",
        "maxSteps": 8,
        "freeTextMatch": "llm",
        "seedDb": "msp_db.json",
    }
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


def test_oracle_results_are_strings_as_stored_evidence_requires(modelless):
    # The platform's OracleNodeExecutionSchema types `result` as a string; a structured value here
    # fails re-import of promoted evidence.
    payload = _task_payload()
    payload["evaluation_criteria"] = {
        "actions": [{"name": "add_location",
                     "arguments": {"name": "Gold Site", "city": "X", "country": "Y"}}],
        "nl_assertions": [],
    }
    reg = JobRegistry()
    job_id = reg.submit(payload, dict(CONFIG, kRuns=1))
    _wait_terminal(reg, job_id)
    (node,) = reg.result(job_id)["artifacts"]["oracle"]["node_executions"]
    assert node["success"] is True
    assert isinstance(node["result"], str) and "Gold Site" in node["result"]


def test_config_db_is_the_fallback_seed_when_the_task_names_none(modelless):
    payload = _task_payload()
    payload["seed_db"] = None
    payload["org_ids"] = None
    payload["org_id"] = None
    config = dict(CONFIG, kRuns=1,
                  gymConfig=dict(CONFIG["gymConfig"], db="single_tenant_db.json"))
    reg = JobRegistry()
    job_id = reg.submit(payload, config)
    _wait_terminal(reg, job_id)
    result = reg.result(job_id)
    single_tenant = json.loads((ITSM_DB_PATH.parent / "single_tenant_db.json").read_text(encoding="utf-8"))
    assert set(result["artifacts"]["initial_db"]["organization"]) == set(single_tenant["organization"])
    assert result["artifacts"]["config_resolved"]["seedDb"] == "single_tenant_db.json"


# ── fault isolation ───────────────────────────────────────────────────────────

def test_a_failing_trial_is_a_per_run_error_entry_not_the_jobs_death(modelless):
    class SecondTrialExplodes(FakeAgent):
        def generate_next_message(self, message, state):
            if len(FakeAgent.instances) == 2:
                raise RuntimeError("provider melted")
            return super().generate_next_message(message, state)

    modelless.setattr(rollout_mod, "LLMAgent", SecondTrialExplodes)
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), dict(CONFIG, kRuns=2))
    status = _wait_terminal(reg, job_id)
    # Trial 2's provider fault must not discard trial 1's paid-for evidence.
    assert status == {"state": "done", "trialsDone": 2, "trialsTotal": 2}
    result = reg.result(job_id)
    assert result["passRate"] == 0.5
    ok, failed = result["perRun"]
    assert ok["passed"] is True and ok["transcript"] and "error" not in ok
    assert failed["passed"] is False and failed["transcript"] == []
    assert "provider melted" in failed["error"]


def test_an_unbuildable_world_is_a_rollout_level_fault_and_the_job_errors(modelless):
    payload = _task_payload()
    payload["seed_db"] = "nope.json"
    reg = JobRegistry()
    job_id = reg.submit(payload, dict(CONFIG, kRuns=1))  # submit itself does not throw
    status = _wait_terminal(reg, job_id)
    assert status["state"] == "error"
    assert "nope.json" in status["error"]
    # The job HAS finished: not 3002's "not finished yet", but the gym having failed — with the
    # error in details, not just prose.
    with pytest.raises(WireFailure) as exc:
        reg.result(job_id)
    assert exc.value.code == wire.WireErrorCode.INTERNAL_ERROR
    assert exc.value.kind == "job_failed"
    assert "nope.json" in exc.value.details["error"]


def test_everything_checkable_at_submit_fails_at_submit(modelless):
    reg = JobRegistry()
    bad = [
        ({"not": "a task"}, CONFIG),
        (_task_payload(), {}),
        (_task_payload(), {"targetModel": "", "kRuns": 1}),
        (_task_payload(), {"targetModel": "m", "kRuns": 0}),
        (_task_payload(), {"targetModel": "m", "kRuns": True}),
        (_task_payload(), dict(CONFIG, gymConfig={"freeTextMatch": "semantic"})),
        (_task_payload(), dict(CONFIG, gymConfig={"maxSteps": 0})),
    ]
    for task, config in bad:
        with pytest.raises(WireFailure) as exc:
            reg.submit(task, config)
        assert exc.value.code == wire.WireErrorCode.INVALID_PARAMS, (task, config)
    assert reg._jobs == {}  # nothing half-registered


# ── cancellation ──────────────────────────────────────────────────────────────

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


def test_a_cancel_landing_during_the_judges_still_ends_the_job_cancelled(modelless):
    # The adversarial repro: cancel while evaluate_task runs on the LAST trial used to let the job
    # finish `done` and serve its payload.
    judge_started, judge_release = threading.Event(), threading.Event()

    def gated_evaluate(*args, **kwargs):
        judge_started.set()
        assert judge_release.wait(timeout=10), "test never released the judge gate"
        return _fake_evaluate(*args, **kwargs)

    modelless.setattr(rollout_mod, "evaluate_task", gated_evaluate)
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    assert judge_started.wait(timeout=10)
    reg.cancel(job_id)
    judge_release.set()
    assert _wait_terminal(reg, job_id)["state"] == "cancelled"
    with pytest.raises(WireFailure) as exc:
        reg.result(job_id)
    assert exc.value.code == wire.WireErrorCode.JOB_CANCELLED


# ── idempotency and lifecycle ─────────────────────────────────────────────────

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


def test_a_finished_key_is_forgotten_so_resubmission_reruns(modelless):
    # The platform's pinned semantics: dedup must NOT join a finished job — that would serve a
    # stale (or, post-collection, unrecoverable) result. A crashed hub re-submitting after a
    # collect must get fresh work, not a dead id.
    reg = JobRegistry()
    first = reg.submit(_task_payload(), dict(CONFIG, kRuns=1), idempotency_key="work-1")
    _wait_terminal(reg, first)
    second = reg.submit(_task_payload(), dict(CONFIG, kRuns=1), idempotency_key="work-1")
    assert second != first
    _wait_terminal(reg, second)
    assert len(FakeAgent.instances) == 2  # it really re-ran


def test_collection_consumes_the_job_entirely(modelless):
    reg = JobRegistry()
    job_id = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    _wait_terminal(reg, job_id)
    assert reg.result(job_id)["kRuns"] == 1
    # Gone means gone: status AND result answer an honest 3001, never a done-but-empty half-state.
    for call in (lambda: reg.status(job_id), lambda: reg.result(job_id)):
        with pytest.raises(WireFailure) as exc:
            call()
        assert exc.value.code == wire.WireErrorCode.JOB_NOT_FOUND


def test_uncollected_results_are_bounded_but_fast_failures_cannot_evict_them(modelless):
    reg = JobRegistry(max_results=1, max_statuses=2)
    done_1 = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    _wait_terminal(reg, done_1)
    # A burst of failing jobs (bogus seed) fills the STATUS bound without touching the result.
    for _ in range(3):
        payload = _task_payload()
        payload["seed_db"] = "nope.json"
        failed = reg.submit(payload, dict(CONFIG, kRuns=1))
        _wait_terminal(reg, failed)
    assert reg.result(done_1)["kRuns"] == 1  # the uncollected result survived the failure burst

    # The RESULT bound still holds: a second uncollected done job evicts the first whole.
    done_2 = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    _wait_terminal(reg, done_2)
    done_3 = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    _wait_terminal(reg, done_3)
    with pytest.raises(WireFailure) as exc:
        reg.status(done_2)
    assert exc.value.code == wire.WireErrorCode.JOB_NOT_FOUND
    assert reg.result(done_3)["kRuns"] == 1


def test_a_thread_start_failure_leaves_no_zombie_and_no_poisoned_key(modelless):
    class BoomThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    modelless.setattr(rollout_mod.threading, "Thread", BoomThread)
    reg = JobRegistry()
    with pytest.raises(RuntimeError, match="can't start new thread"):
        reg.submit(_task_payload(), dict(CONFIG, kRuns=1), idempotency_key="work-1")
    # No permanently-queued corpse, and the key is free for the retry this failure will cause.
    assert reg._jobs == {} and reg._by_key == {}


def test_submit_pushes_back_when_too_many_rollouts_are_in_flight(modelless):
    modelless.setattr(rollout_mod, "LLMAgent", GatedAgent)
    reg = JobRegistry(max_active=1)
    first = reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    assert GatedAgent.started.wait(timeout=10)
    with pytest.raises(WireFailure) as exc:
        reg.submit(_task_payload(), dict(CONFIG, kRuns=1))
    assert exc.value.code == wire.WireErrorCode.INTERNAL_ERROR
    assert exc.value.retryable is True
    GatedAgent.release.set()
    _wait_terminal(reg, first)


def test_unknown_job_is_job_not_found(modelless):
    reg = JobRegistry()
    for call in (lambda: reg.status("j404"), lambda: reg.result("j404"), lambda: reg.cancel("j404")):
        with pytest.raises(WireFailure) as exc:
            call()
        assert exc.value.code == wire.WireErrorCode.JOB_NOT_FOUND


def test_model_spelling_translation():
    assert _litellm_model("anthropic:claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"
    assert _litellm_model("bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0") == \
        "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"  # the colon is part of the model id
    assert _litellm_model("azure:deployment:name") == "azure/deployment:name"  # first colon only
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
