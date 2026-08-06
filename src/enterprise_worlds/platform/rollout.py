"""``rollout.*`` — episodes as async jobs.

``rollout.submit`` returns immediately; trials run on a worker thread so the stdin loop stays
responsive to ``status``. This is where the miner-era inversion lands: the gym runs the user
simulator, the NL judge and the free-text judge **itself**, through its own litellm, with
credentials the runner injects as environment variables. There is no callback channel; the gym
never initiates a request. (``bridge.py`` keeps monkeypatching ``generate`` on the miner side —
that path is untouched.)

Job semantics worth stating once:

- ``idempotencyKey`` identifies the *work*: re-submitting a key that is known returns the same
  ``jobId`` and starts nothing, which is what stops a platform restart from double-spending tokens.
- A gym failure inside a trial ends the **job** in ``error`` with a message; ``submit`` itself does
  not throw for it.
- Cancel actually stops spending: the flag is checked before every trial, before every agent LLM
  turn, and before the judges run — an in-flight provider call is the only thing it cannot
  interrupt.
- Jobs live in memory and die with the process. Finished jobs are retired: a bounded number of
  terminal jobs is kept (a k=16 result carries full transcripts), and a result's payload is
  released once collected.

The worker thread never touches stdout; only the dispatch loop writes to the wire. The gym's frozen
clock is thread-local, so freezing it in the worker cannot race the session clock on the dispatch
thread.
"""
from __future__ import annotations

import json
import threading
from functools import partial
from typing import Any, Dict, List, Optional

from enterprise_worlds.agent.llm_agent import LLMAgent
from enterprise_worlds.config import DEFAULT_LLM_NL_JUDGE, DEFAULT_LLM_USER
from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.domains.itsm.environment import get_environment
from enterprise_worlds.evaluator.evaluator import evaluate_task
from enterprise_worlds.evaluator.evaluator_env import _build_gold_env
from enterprise_worlds.evaluator.text_match_strategy import TextMatchConfig
from enterprise_worlds.orchestrator.orchestrator import Orchestrator
from enterprise_worlds.platform import state as _state
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.serialize import jsonable, to_transcript
from enterprise_worlds.platform.wire import WireErrorCode
from enterprise_worlds.user.user_simulator import UserSimulator
from enterprise_worlds.utils.clock import DEFAULT_NOW, reset_now, set_now
from enterprise_worlds.utils.hash_utils import get_dict_hash

DEFAULT_MAX_STEPS = 16

#: Finished jobs kept addressable. Oldest-terminal is evicted beyond this — a k=16 result carries
#: full transcripts, and holding every one forever is a slow leak in a long-lived container.
MAX_TERMINAL_JOBS = 8

# Human-authored prose fields excluded from the structure-only part of the db diff, mirroring the
# free-text set the evaluator judges semantically rather than byte-compares.
FREE_TEXT_FIELDS = frozenset({
    "short_description", "description", "problem_statement", "worknotes",
    "resolution_notes", "close_notes", "workaround", "fix_notes",
    "implementation_plan", "testing_plan", "title", "body", "subject", "message",
})


class _Cancelled(Exception):
    """Raised inside a worker when its job's cancel flag is set."""


class _CancellableAgent:
    """Wraps the LLM agent so a cancelled job stops before its next model call, not after k trials."""

    def __init__(self, inner: Any, cancel: threading.Event) -> None:
        self._inner = inner
        self._cancel = cancel

    def get_init_state(self):
        return self._inner.get_init_state()

    def generate_next_message(self, message, agent_state):
        if self._cancel.is_set():
            raise _Cancelled()
        return self._inner.generate_next_message(message, agent_state)


def _litellm_model(model: str) -> str:
    """The contract's ``provider:model`` spelling as litellm's ``provider/model``.

    A model already carrying a ``/`` is a litellm string (e.g. ``bedrock/...:0``, whose colon is
    part of the model id) and passes through untouched; so does one with no colon at all.
    """
    if "/" in model or ":" not in model:
        return model
    provider, rest = model.split(":", 1)
    return f"{provider}/{rest}"


# ── diagnostics (ported from the miner bridge, which pioneered the shapes the review UI reads) ──

def _strip_free_text(dump: dict) -> dict:
    out: Dict[str, Any] = {}
    for coll, recs in dump.items():
        if isinstance(recs, dict):
            out[coll] = {
                rid: {k: v for k, v in rec.items() if k not in FREE_TEXT_FIELDS}
                if isinstance(rec, dict) else rec
                for rid, rec in recs.items()
            }
        else:
            out[coll] = recs
    return out


def _db_field_diff(gold_dump: dict, pred_dump: dict) -> dict:
    """Per-collection structural diff: ``only_in_gold``/``only_in_pred`` ids and per-field
    ``{gold, pred, free_text}`` for shared records. Identical collections are omitted."""
    diff: Dict[str, Any] = {}
    for coll in sorted(set(gold_dump) | set(pred_dump)):
        gold_coll = gold_dump.get(coll) or {}
        pred_coll = pred_dump.get(coll) or {}
        if not isinstance(gold_coll, dict) or not isinstance(pred_coll, dict):
            continue
        only_in_gold = sorted(set(gold_coll) - set(pred_coll))
        only_in_pred = sorted(set(pred_coll) - set(gold_coll))
        field_diffs: Dict[str, dict] = {}
        for rid in sorted(set(gold_coll) & set(pred_coll)):
            g_rec = gold_coll[rid] or {}
            p_rec = pred_coll[rid] or {}
            fields = sorted(set(g_rec) | set(p_rec)) if isinstance(g_rec, dict) and isinstance(p_rec, dict) else []
            rec_fd = {
                f: {"gold": g_rec.get(f), "pred": p_rec.get(f), "free_text": f in FREE_TEXT_FIELDS}
                for f in fields if g_rec.get(f) != p_rec.get(f)
            }
            if rec_fd:
                field_diffs[rid] = rec_fd
        if only_in_gold or only_in_pred or field_diffs:
            diff[coll] = {
                "only_in_gold": only_in_gold,
                "only_in_pred": only_in_pred,
                "field_diffs": field_diffs,
            }
    return diff


def _run_oracle(env_ctor, task: Task) -> dict:
    """Replay the gold actions on a fresh env: the target DB and each gold call's outcome."""
    env = env_ctor(db_delta=task.initial_state_delta)
    node_executions = []
    for action in task.evaluation_criteria.actions:
        entry: Dict[str, Any] = {"tool": action.name, "arguments": action.arguments}
        try:
            result = env.make_tool_call(action.name, **(action.arguments or {}))
            entry["success"] = True
            entry["result"] = jsonable(result)
        except Exception as e:  # noqa: BLE001 - a gold-action failure is data
            entry["success"] = False
            entry["error"] = f"{type(e).__name__}: {e}"
        node_executions.append(entry)
    return {
        "success": all(n["success"] for n in node_executions),
        "node_executions": node_executions,
        "final_db_state": env.tools.db.model_dump(),
    }


# ── the rollout itself ────────────────────────────────────────────────────────

class _Job:
    def __init__(self, job_id: str, task_raw: Any, config: dict) -> None:
        self.job_id = job_id
        self.task_raw = task_raw
        self.config = config
        self.state = "queued"
        self.trials_done = 0
        self.trials_total = int(config["kRuns"])
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.collected = False
        self.cancel = threading.Event()


class JobRegistry:
    """Rollout jobs by id, run on one worker thread each."""

    def __init__(self, max_terminal: int = MAX_TERMINAL_JOBS) -> None:
        self._jobs: Dict[str, _Job] = {}
        self._by_key: Dict[str, str] = {}
        self._terminal_order: List[str] = []
        self._max_terminal = max_terminal
        self._counter = 0
        self._lock = threading.Lock()

    # ── protocol surface ──────────────────────────────────────────────────

    def submit(self, task_raw: Any, config: Any, idempotency_key: Any = None) -> str:
        if not isinstance(config, dict) or not isinstance(config.get("targetModel"), str):
            raise WireFailure(WireErrorCode.INVALID_PARAMS, "config.targetModel must be a string",
                              kind="invalid_params", retryable=False)
        k = config.get("kRuns")
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise WireFailure(WireErrorCode.INVALID_PARAMS, "config.kRuns must be a positive integer",
                              kind="invalid_params", retryable=False)
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise WireFailure(WireErrorCode.INVALID_PARAMS, "idempotencyKey must be a string",
                              kind="invalid_params", retryable=False)
        with self._lock:
            # The key identifies the work, not the request: a known key joins the existing job —
            # whether in flight or already terminal — rather than starting (and paying for) a second run.
            if idempotency_key is not None and idempotency_key in self._by_key:
                return self._by_key[idempotency_key]
            self._counter += 1
            job = _Job(f"j{self._counter}", task_raw, config)
            self._jobs[job.job_id] = job
            if idempotency_key is not None:
                self._by_key[idempotency_key] = job.job_id
        threading.Thread(target=self._work, args=(job,), daemon=True,
                         name=f"rollout-{job.job_id}").start()
        return job.job_id

    def status(self, job_id: Any) -> dict:
        job = self._get(job_id)
        out: Dict[str, Any] = {"state": job.state, "trialsDone": job.trials_done,
                               "trialsTotal": job.trials_total}
        if job.error is not None:
            out["error"] = job.error
        return out

    def result(self, job_id: Any) -> dict:
        job = self._get(job_id)
        with self._lock:
            if job.state == "cancelled":
                raise WireFailure(WireErrorCode.JOB_CANCELLED,
                                  f"job {job_id!r} was cancelled; there is no result",
                                  kind="job_cancelled", retryable=False)
            if job.state == "error":
                raise WireFailure(WireErrorCode.JOB_NOT_COMPLETE,
                                  f"job {job_id!r} failed: {job.error}",
                                  kind="job_not_complete", retryable=False)
            if job.state != "done":
                raise WireFailure(WireErrorCode.JOB_NOT_COMPLETE,
                                  f"job {job_id!r} is {job.state}; poll rollout.status",
                                  kind="job_not_complete", retryable=True)
            if job.result is None:
                # Collected earlier and the payload released (see _work). The status survives; the
                # transcripts do not — re-fetching means re-running, which is the caller's call.
                raise WireFailure(WireErrorCode.JOB_NOT_FOUND,
                                  f"job {job_id!r} was already collected and its payload released",
                                  kind="job_not_found", retryable=False)
            result, job.result, job.collected = job.result, None, True
            return result

    def cancel(self, job_id: Any) -> None:
        job = self._get(job_id)
        # Idempotent, and a no-op on a job that already reached a terminal state.
        job.cancel.set()

    # ── internals ─────────────────────────────────────────────────────────

    def _get(self, job_id: Any) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise WireFailure(WireErrorCode.JOB_NOT_FOUND, f"no rollout job {job_id!r}",
                              kind="job_not_found", retryable=False)
        return job

    def _work(self, job: _Job) -> None:
        try:
            if job.cancel.is_set():
                raise _Cancelled()
            job.state = "running"
            result = _run_rollout(job)
            # A result the wire cannot carry (a NaN score from a judge, an unserialisable object)
            # must fail HERE, while the job can still turn to `error` — failing at collection time
            # would release nothing and leave the caller retrying a reply that can never encode.
            json.dumps(result, allow_nan=False)
            with self._lock:
                job.result = result
                job.state = "done"
                self._retire(job)
        except _Cancelled:
            with self._lock:
                job.state = "cancelled"
                self._retire(job)
        except Exception as e:  # noqa: BLE001 - a gym failure ends the job, never the process
            with self._lock:
                job.error = f"{type(e).__name__}: {e}"
                job.state = "error"
                self._retire(job)

    def _retire(self, job: _Job) -> None:
        """Bound the terminal set (caller holds the lock). Evicted jobs stop being addressable —
        an honest 3001 afterwards beats pretending a released result could still be served."""
        self._terminal_order.append(job.job_id)
        while len(self._terminal_order) > self._max_terminal:
            evicted = self._terminal_order.pop(0)
            self._jobs.pop(evicted, None)
            for key, jid in list(self._by_key.items()):
                if jid == evicted:
                    del self._by_key[key]


def _run_rollout(job: _Job) -> dict:
    """All k trials plus the once-per-rollout artifacts. Runs on the worker thread."""
    task = _state.decode_task(job.task_raw)
    config = job.config
    gym_config = config.get("gymConfig") or {}
    if not isinstance(gym_config, dict):
        raise ValueError("config.gymConfig must be an object")
    target_model = _litellm_model(config["targetModel"])
    user_model = _litellm_model(gym_config.get("userModel") or DEFAULT_LLM_USER)
    judge_model = _litellm_model(gym_config.get("judgeModel") or DEFAULT_LLM_NL_JUDGE)
    max_steps = gym_config.get("maxSteps") or DEFAULT_MAX_STEPS
    free_text = gym_config.get("freeTextMatch")
    db_text_match = TextMatchConfig(strategy=free_text) if free_text else None
    # The task's own seed wins (the suite is mixed-seed); the config's `db` is the run-level default.
    seed_db = task.seed_db or gym_config.get("db")

    scope = set(task.org_ids) if task.org_ids is not None else None
    _state.check_fk_spec(scope)

    # Worker-thread clock: thread-local, so this cannot race a session on the dispatch thread.
    set_now(task.current_time or DEFAULT_NOW)
    try:
        env_ctor = partial(
            get_environment, acting_user_id=task.acting_user_id,
            org_id=task.org_id, org_ids=task.org_ids, seed_db=seed_db,
        )
        initial_db = env_ctor(db_delta=task.initial_state_delta).tools.db.model_dump()
        oracle = _run_oracle(env_ctor, task)

        per_run: List[dict] = []
        for idx in range(job.trials_total):
            if job.cancel.is_set():
                raise _Cancelled()
            per_run.append(_run_trial(
                task, env_ctor, idx,
                target_model=target_model, user_model=user_model, judge_model=judge_model,
                max_steps=max_steps, db_text_match=db_text_match, cancel=job.cancel,
            ))
            job.trials_done = idx + 1

        passed = sum(1 for r in per_run if r["passed"])
        return {
            # Echoed as the platform named it, not as litellm spells it.
            "targetModel": config["targetModel"],
            "kRuns": job.trials_total,
            "passRate": passed / job.trials_total,
            "perRun": per_run,
            "artifacts": {"oracle": oracle, "initial_db": initial_db},
        }
    finally:
        reset_now()


def _run_trial(task: Task, env_ctor, idx: int, *, target_model: str, user_model: str,
               judge_model: str, max_steps: int, db_text_match: Optional[TextMatchConfig],
               cancel: threading.Event) -> dict:
    env = env_ctor(db_delta=task.initial_state_delta)
    agent = _CancellableAgent(LLMAgent(env.get_policy(), env.get_tool_schemas(), target_model), cancel)
    user = UserSimulator(task.scenario, llm=user_model)

    run = Orchestrator(agent, user, env, max_steps=max_steps).run()
    if cancel.is_set():
        raise _Cancelled()  # the episode is over, but the judges have not been paid for yet

    reward_info = evaluate_task(
        env_ctor, task, trajectory=run.trajectory, final_env=env,
        nl_llm=judge_model, db_text_match=db_text_match,
    )
    db = reward_info.db_check
    nl = reward_info.nl_check
    pred_dump = env.tools.db.model_dump()

    # Field-level structural diff vs the gold final DB. Diagnostic only: a diff failure must never
    # fail a trial that ran.
    db_diff: Optional[dict] = None
    try:
        gold_dump = _build_gold_env(env_ctor, task).tools.db.model_dump()
        db_diff = {
            "structural_match": get_dict_hash(_strip_free_text(gold_dump)) == get_dict_hash(_strip_free_text(pred_dump)),
            "collections": _db_field_diff(gold_dump, pred_dump),
        }
    except Exception:  # noqa: BLE001 - diagnostic only
        db_diff = None

    return {
        "runIdx": idx,
        "passed": reward_info.reward >= 1.0,
        "transcript": to_transcript(run.trajectory),
        "toolCalls": len(run.agent_tool_calls),
        # The keys the review UI reads — keep the names.
        "diagnostics": {
            "db_match": bool(db.db_match) if db is not None else True,
            "nl_checks": [
                {"assertion": c.nl_assertion, "met": c.met, "reasoning": c.reasoning}
                for c in (nl.checks if nl is not None else [])
            ],
            "db_text_judgments": [j.model_dump() for j in db.text_judgments] if db is not None else [],
            "final_state": pred_dump,
            **({"db_diff": db_diff} if db_diff is not None else {}),
        },
    }
