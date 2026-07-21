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
