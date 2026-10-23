"""--log-dir must hold exactly one run: reuse is refused unless --overwrite-log-dir (offline)."""

from __future__ import annotations

import argparse
import json

import pytest

import enterprise_worlds.cli as cli
import enterprise_worlds.run as run
from enterprise_worlds.evaluator.evaluator import RewardInfo
from enterprise_worlds.run import TaskResult, prepare_run_dir


class _Task:
    def __init__(self, tid):
        self.id = tid


def _patch_eval(mocker, reward, task_ids=("A", "B"), interrupt_after=None):
    """Fake run_task returning ``reward``; optionally raise KeyboardInterrupt after N trials."""
    calls = []

    def fake_run_task(domain, task, trial=0, seed=None, **kw):
        if interrupt_after is not None and len(calls) == interrupt_after:
            raise KeyboardInterrupt
        calls.append((task.id, trial))
        return TaskResult(task_id=task.id, trial=trial, reward=reward,
                          reward_info=RewardInfo(reward=reward), stopped=True,
                          num_tool_calls=1, trajectory=[])

    mocker.patch.object(run, "run_task", fake_run_task)
    spec = mocker.Mock()
    spec.get_tasks.return_value = [_Task(t) for t in task_ids]
    mocker.patch.object(run, "get_domain", return_value=spec)
    return calls


def _eworlds_run(*argv):
    parser = argparse.ArgumentParser()
    cli.add_run_args(parser)
    cli.run_run(parser.parse_args(list(argv)))


def _snapshot(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def _trial_rewards(d):
    return {str(p.relative_to(d)): json.loads(p.read_text())["reward"]
            for p in sorted(d.glob("*/trial_*.json"))}


def test_fresh_missing_or_unrelated_dirs_are_accepted(tmp_path):
    prepare_run_dir(tmp_path / "does-not-exist")
    prepare_run_dir(tmp_path)                               # empty
    (tmp_path / ".gitkeep").write_text("")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "trial_plan.json").write_text("{}")  # not a trial_<i>.json
    before = _snapshot(tmp_path)
    prepare_run_dir(tmp_path)
    assert _snapshot(tmp_path) == before


def test_reused_dir_is_refused_before_any_model_call(tmp_path, mocker, capsys):
    out = tmp_path / "runs" / "opus"
    _patch_eval(mocker, 1.0)
    _eworlds_run("--k", "4", "--log-dir", str(out))
    before = _snapshot(out)

    calls = _patch_eval(mocker, 0.0)
    with pytest.raises(SystemExit) as exc:
        _eworlds_run("--k", "2", "--log-dir", str(out))

    assert "already contains results from a previous run" in str(exc.value.code)
    assert "--overwrite-log-dir" in str(exc.value.code)
    assert calls == []                                      # no trial ran, no API cost
    assert _snapshot(out) == before                         # previous run untouched


def test_overwrite_replaces_previous_run_without_stale_trials(tmp_path, mocker):
    """k=4 then k=2 into the same dir: trials 0-1 overlap, 2-3 must not survive."""
    out = tmp_path / "run"
    _patch_eval(mocker, 1.0)
    _eworlds_run("--k", "4", "--log-dir", str(out))
    assert len(_trial_rewards(out)) == 8

    _patch_eval(mocker, 0.0)
    _eworlds_run("--k", "2", "--log-dir", str(out), "--overwrite-log-dir")

    summary = json.loads((out / "summary.json").read_text())
    assert summary["k"] == 2 and summary["num_runs"] == 4
    assert _trial_rewards(out) == {f"{t}/trial_{i}.json": 0.0 for t in "AB" for i in range(2)}


def test_overwrite_with_fewer_tasks_removes_emptied_task_dirs(tmp_path, mocker):
    out = tmp_path / "run"
    _patch_eval(mocker, 1.0, task_ids=("A", "B"))
    _eworlds_run("--log-dir", str(out))
    _patch_eval(mocker, 1.0, task_ids=("A",))
    _eworlds_run("--log-dir", str(out), "--overwrite-log-dir")

    assert not (out / "B").exists()
    assert list(_trial_rewards(out)) == ["A/trial_0.json"]


def test_overwrite_deletes_only_run_artifacts(tmp_path, mocker):
    out = tmp_path / "run"
    _patch_eval(mocker, 1.0)
    _eworlds_run("--log-dir", str(out))
    (out / "NOTES.md").write_text("keep me")
    (out / "A" / "review.txt").write_text("keep me too")

    _patch_eval(mocker, 0.0, task_ids=("C",))
    _eworlds_run("--log-dir", str(out), "--overwrite-log-dir")

    assert (out / "NOTES.md").read_text() == "keep me"
    assert (out / "A" / "review.txt").read_text() == "keep me too"
    assert not (out / "A" / "trial_0.json").exists() and not (out / "B").exists()
    assert list(_trial_rewards(out)) == ["C/trial_0.json"]


def test_repeated_overwrite_runs_stay_consistent(tmp_path, mocker):
    out = tmp_path / "run"
    for reward in (1.0, 0.0, 1.0):
        _patch_eval(mocker, reward)
        _eworlds_run("--k", "2", "--log-dir", str(out), "--overwrite-log-dir")
        summary = json.loads((out / "summary.json").read_text())
        assert summary["avg_reward"] == reward
        assert set(_trial_rewards(out).values()) == {reward} and len(_trial_rewards(out)) == 4


def test_interrupted_run_keeps_partial_trials_and_blocks_silent_resume(tmp_path, mocker):
    """Incremental logging still keeps finished trials; a later run can't silently mix into them."""
    out = tmp_path / "run"
    _patch_eval(mocker, 1.0, interrupt_after=3)
    with pytest.raises(KeyboardInterrupt):
        _eworlds_run("--k", "2", "--log-dir", str(out))
    partial = _snapshot(out)
    assert len(_trial_rewards(out)) == 3 and not (out / "summary.json").exists()

    calls = _patch_eval(mocker, 0.0)
    with pytest.raises(SystemExit):
        _eworlds_run("--k", "2", "--log-dir", str(out))
    assert calls == [] and _snapshot(out) == partial


def test_save_to_is_unaffected(tmp_path, mocker):
    """--save-to rewrites one file in full, so it can't mix runs and needs no guard."""
    blob = tmp_path / "results.json"
    for reward in (1.0, 0.0):
        _patch_eval(mocker, reward)
        _eworlds_run("--save-to", str(blob))
    assert {r["reward"] for r in json.loads(blob.read_text())["results"]} == {0.0}


# ── overwrite safety ─────────────────────────────────────────────────────────────────────


def test_overwrite_never_deletes_through_a_symlinked_task_dir(tmp_path, mocker):
    elsewhere = tmp_path / "other-run" / "A"
    elsewhere.mkdir(parents=True)
    (elsewhere / "trial_0.json").write_text('{"reward": 1.0}')
    out = tmp_path / "run"
    out.mkdir()
    (out / "summary.json").write_text("{}")
    (out / "A").symlink_to(elsewhere, target_is_directory=True)
    before = _snapshot(tmp_path / "other-run")

    calls = _patch_eval(mocker, 0.0)
    with pytest.raises(SystemExit) as exc:
        _eworlds_run("--log-dir", str(out), "--overwrite-log-dir")

    assert "symlinked task dirs" in str(exc.value.code)
    assert calls == []
    assert _snapshot(tmp_path / "other-run") == before      # nothing outside the log dir touched
    assert (out / "summary.json").exists()                  # and nothing inside it either


def test_overwrite_removes_symlinked_files_not_their_targets(tmp_path):
    kept = tmp_path / "keep.json"
    kept.write_text("{}")
    out = tmp_path / "run"
    (out / "A").mkdir(parents=True)
    (out / "A" / "trial_0.json").symlink_to(kept)
    (out / "summary.json").symlink_to(kept)

    prepare_run_dir(out, overwrite=True)
    assert kept.read_text() == "{}"
    assert not (out / "A").exists() and not (out / "summary.json").is_symlink()


def test_failed_overwrite_reports_cleanly_and_stays_detectable(tmp_path, mocker):
    out = tmp_path / "run"
    _patch_eval(mocker, 1.0)
    _eworlds_run("--k", "2", "--log-dir", str(out))

    real_unlink = run.Path.unlink
    deleted = []

    def flaky_unlink(self, missing_ok=False):
        if len(deleted) == 2:
            raise PermissionError(13, "Permission denied", str(self))
        deleted.append(self)
        return real_unlink(self, missing_ok=missing_ok)

    mocker.patch.object(run.Path, "unlink", flaky_unlink)
    calls = _patch_eval(mocker, 0.0)
    with pytest.raises(SystemExit) as exc:
        _eworlds_run("--log-dir", str(out), "--overwrite-log-dir")
    mocker.stopall()

    assert "could not clear previous results" in str(exc.value.code)
    assert "Nothing was run" in str(exc.value.code)
    assert calls == []
    calls = _patch_eval(mocker, 0.0)                         # leftovers still block a plain rerun
    with pytest.raises(SystemExit):
        _eworlds_run("--log-dir", str(out))
    assert calls == []
