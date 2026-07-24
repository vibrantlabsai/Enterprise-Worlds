"""k-trial eval: pass^k aggregation and structured run-dir logging (offline, mocked run_task)."""

from __future__ import annotations

import json

import enterprise_worlds.run as run
from enterprise_worlds.evaluator.evaluator import RewardInfo
from enterprise_worlds.run import TaskResult, _pass_hat_k, run_domain, save_run_dir


def test_pass_hat_k_estimator():
    assert _pass_hat_k(3, 3, 3) == 1.0
    assert _pass_hat_k(3, 0, 1) == 0.0
    assert abs(_pass_hat_k(3, 2, 1) - 2 / 3) < 1e-9   # c/n
    assert abs(_pass_hat_k(3, 2, 2) - 1 / 3) < 1e-9   # C(2,2)/C(3,2)
    assert _pass_hat_k(3, 2, 3) == 0.0                # C(2,3) == 0
    assert _pass_hat_k(2, 1, 3) == 0.0                # k > n


class _Task:
    def __init__(self, tid):
        self.id = tid


def _patch_domain_and_runtask(mocker, rewards_by_task):
    """task A/B return scripted per-trial rewards; no LLM is invoked."""
    def fake_run_task(domain, task, trial=0, seed=None, **kw):
        r = rewards_by_task[task.id][trial]
        return TaskResult(task_id=task.id, trial=trial, reward=r,
                          reward_info=RewardInfo(reward=r), stopped=True,
                          num_tool_calls=1, trajectory=[])
    mocker.patch.object(run, "run_task", fake_run_task)
    spec = mocker.Mock()
    spec.get_tasks.return_value = [_Task(t) for t in rewards_by_task]
    mocker.patch.object(run, "get_domain", return_value=spec)


def test_run_domain_k_trials_and_pass_hat_k(mocker):
    _patch_domain_and_runtask(mocker, {"A": [1.0, 1.0, 0.0], "B": [0.0, 0.0, 0.0]})
    res = run_domain("itsm", k=3, seed=0)

    assert res.k == 3
    assert len(res.results) == 6                       # 2 tasks x 3 trials
    assert {r.trial for r in res.results} == {0, 1, 2}
    assert abs(res.avg_reward - 2 / 6) < 1e-9

    pk = res.avg_pass_hat_k()
    # A: pass^1=2/3, ^2=1/3, ^3=0 ; B: all 0 -> averaged over the two tasks
    assert abs(pk[1] - 1 / 3) < 1e-9
    assert abs(pk[2] - 1 / 6) < 1e-9
    assert pk[3] == 0.0


def test_save_run_dir_layout(tmp_path, mocker):
    _patch_domain_and_runtask(mocker, {"A": [1.0, 0.0], "B": [1.0, 1.0]})
    res = run_domain("itsm", k=2)

    out = save_run_dir(res, tmp_path / "run")
    summary = json.loads((out / "summary.json").read_text())
    assert summary["k"] == 2 and summary["num_tasks"] == 2 and summary["num_runs"] == 4
    assert set(summary["avg_pass^k"]) == {"1", "2"}
    # every task x trial trajectory file exists
    for tid in ("A", "B"):
        for i in range(2):
            assert (out / tid / f"trial_{i}.json").exists()


# ── verifier v2: invalid trials (P5) ─────────────────────────────────────────────────────


def test_infra_error_classifier():
    from enterprise_worlds.run import _is_infra_error

    assert _is_infra_error(RuntimeError(
        'completion failed: 400 {"message":"This model does not support assistant message prefill."}'))
    assert _is_infra_error(RuntimeError("AccessDeniedException: Authentication failed"))
    assert _is_infra_error(TimeoutError("read timed out"))
    assert not _is_infra_error(ValueError("task has no gold actions"))
    assert not _is_infra_error(KeyError("INC_003"))


def test_invalid_runs_excluded_from_pass_hat_k(mocker):
    """A crashed trial says nothing about the agent: n shrinks, it doesn't count as a fail."""
    _patch_domain_and_runtask(mocker, {"A": [1.0, 1.0, 0.0]})
    res = run_domain("itsm", k=3, seed=0)
    # Manufacture the crash the aggregation should ignore: trial 2 becomes infra-invalid.
    res.results[2].invalid = True
    res.results[2].reward = 0.0

    assert res.invalid_runs == 1
    assert abs(res.avg_reward - 1.0) < 1e-9              # 2 valid trials, both passed
    pk = res.avg_pass_hat_k()
    assert pk[1] == 1.0                                  # n=2, c=2
    assert pk[2] == 1.0
    assert pk[3] == 0.0                                  # k=3 > n=2 valid trials


def test_infra_crash_in_run_domain_marks_invalid(mocker):
    def crashing_run_task(domain, task, trial=0, seed=None, **kw):
        raise RuntimeError("model does not support assistant message prefill (400)")
    mocker.patch.object(run, "run_task", crashing_run_task)
    spec = mocker.Mock()
    spec.get_tasks.return_value = [_Task("A")]
    mocker.patch.object(run, "get_domain", return_value=spec)

    res = run_domain("itsm", k=2)
    assert all(r.invalid for r in res.results)
    assert res.invalid_runs == 2
    assert res.avg_pass_hat_k() == {}                    # nothing valid to aggregate


def test_agent_exception_still_counts_as_failure(mocker):
    def bad_agent_run_task(domain, task, trial=0, seed=None, **kw):
        raise ValueError("agent produced malformed tool call")
    mocker.patch.object(run, "run_task", bad_agent_run_task)
    spec = mocker.Mock()
    spec.get_tasks.return_value = [_Task("A")]
    mocker.patch.object(run, "get_domain", return_value=spec)

    res = run_domain("itsm", k=2)
    assert all(not r.invalid for r in res.results)       # not infra -> real failure
    assert res.avg_pass_hat_k()[1] == 0.0


def test_save_run_dir_reports_invalid_runs(tmp_path, mocker):
    _patch_domain_and_runtask(mocker, {"A": [1.0, 0.0]})
    res = run_domain("itsm", k=2)
    res.results[1].invalid = True
    out = save_run_dir(res, tmp_path / "run")
    summary = json.loads((out / "summary.json").read_text())
    assert summary["invalid_runs"] == 1
    task_a = next(t for t in summary["per_task"] if t["task_id"] == "A")
    assert task_a["invalid_runs"] == 1 and task_a["trials"] == 1
