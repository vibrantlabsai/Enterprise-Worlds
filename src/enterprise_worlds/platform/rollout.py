"""``rollout.*`` — episodes as async jobs.

``rollout.submit`` returns immediately; trials run on a worker thread so the stdin loop stays
responsive to ``status``. This is where the miner-era inversion lands: the gym runs the user
simulator, the NL judge and the free-text judge **itself**, through its own litellm, with
credentials the runner injects as environment variables. There is no callback channel; the gym
never initiates a request. (``bridge.py`` keeps monkeypatching ``generate`` on the miner side —
that path is untouched.)

Job semantics worth stating once:

- **A trial that fails to run is a per-run ``error`` entry, not the job's death.** The contract's
  ``RolloutRunOutcome.error`` exists exactly so trial N's provider fault does not discard trials
  0..N-1 — which is also how ``run_domain`` and the miner bridge behave. The job itself turns
  ``error`` only for rollout-level faults: an unbuildable world, a failed oracle replay, a result
  the wire cannot carry.
- ``idempotencyKey`` identifies work **in flight**: re-submitting a live key joins the running job
  (no double spend); a key whose job has finished is forgotten, so a re-submission re-runs rather
  than serving a stale or unrecoverable result — matching the platform's own pinned semantics.
- Everything checkable at submit fails at submit (``INVALID_PARAMS``): the task payload, model and
  config fields. A malformed request is the caller's bug and must not present as a gym-side
  rollout failure.
- Cancel actually stops spending: checked before every trial, before every agent and user-sim LLM
  turn, and before and after the judges — an in-flight provider call is the only thing it cannot
  interrupt. A cancel that lands during the last trial still ends the job ``cancelled``.
- Jobs live in memory and die with the process. Collecting a result evicts the job entirely, so a
  later call answers an honest 3001 instead of a half-dead status. Uncollected results are bounded
  (transcripts are heavy) and payload-free terminal statuses are bounded separately (they are
  tiny), evicted oldest-first.

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
from enterprise_worlds.config import DEFAULT_DB_TEXT_MATCH, DEFAULT_LLM_NL_JUDGE, DEFAULT_LLM_USER
from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.domains.itsm.environment import get_environment
from enterprise_worlds.evaluator.evaluator import evaluate_task
from enterprise_worlds.evaluator.evaluator_env import _build_gold_env
from enterprise_worlds.evaluator.text_match_strategy import TextMatchConfig
from enterprise_worlds.orchestrator.orchestrator import Orchestrator
from enterprise_worlds.platform import state as _state
from enterprise_worlds.platform.errors import WireFailure
from enterprise_worlds.platform.serialize import jsonable, stringify, to_transcript
from enterprise_worlds.platform.wire import WireErrorCode
from enterprise_worlds.user.user_simulator import UserSimulator
from enterprise_worlds.utils.clock import DEFAULT_NOW, reset_now, set_now
from enterprise_worlds.utils.hash_utils import get_dict_hash

#: Matches what the platform sends (rollout.ts ITSM_MAX_STEPS); the CLI's own default is 12.
DEFAULT_MAX_STEPS = 16

#: Uncollected results kept (each carries k full transcripts). Beyond this the oldest whole job is
#: evicted — the tokens were spent, but unbounded retention is a leak in a long-lived container.
MAX_RETAINED_RESULTS = 8
#: Payload-free terminal statuses kept (error/cancelled — a dict of a few strings each). Bounded
#: separately and generously so a burst of fast-failing jobs cannot destroy uncollected results,
#: and an error message survives long enough to be observed.
MAX_TERMINAL_STATUSES = 64
#: Live jobs accepted before submit pushes back. Each is a worker thread holding an environment;
#: unbounded acceptance is how thread exhaustion (and its zombie-job fallout) becomes reachable.
MAX_ACTIVE_JOBS = 16

_FREE_TEXT_STRATEGIES = ("exact", "fuzzy", "llm")

# Human-authored prose fields excluded from the structure-only part of the db diff, mirroring the
# free-text set the evaluator judges semantically rather than byte-compares.
FREE_TEXT_FIELDS = frozenset({
    "short_description", "description", "problem_statement", "worknotes",
    "resolution_notes", "close_notes", "workaround", "fix_notes",
    "implementation_plan", "testing_plan", "title", "body", "subject", "message",
})


class _Cancelled(Exception):
    """Raised inside a worker when its job's cancel flag is set."""


class _CancelGate:
    """Wraps a conversational participant so a cancelled job stops before its next model call.

    Both the target agent and the user simulator are gated: the orchestrator alternates them, so
    gating only the agent would let a cancelled job pay for one more full user-sim turn.
    """

    def __init__(self, inner: Any, cancel: threading.Event) -> None:
        self._inner = inner
        self._cancel = cancel

    def get_init_state(self):
        return self._inner.get_init_state()

    def generate_next_message(self, message, participant_state):
        if self._cancel.is_set():
            raise _Cancelled()
        return self._inner.generate_next_message(message, participant_state)

    def is_stop(self, message):
        return self._inner.is_stop(message)


def _litellm_model(model: str) -> str:
    """The contract's ``provider:model`` spelling as litellm's ``provider/model``.

    Only the FIRST colon is the provider separator; the rest of the id is untouched. A model
    already carrying a ``/`` is a litellm string (e.g. ``bedrock/...:0``, whose colon is part of
    the model id) and passes through, as does one with no colon at all.
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
    """Replay the gold actions on a fresh env: the target DB and each gold call's outcome.

    ``result`` is a STRING, as the miner bridge stores it and the platform's stored-evidence schema
    (``OracleNodeExecutionSchema``) requires — a structured value here fails re-import of promoted
    evidence and renders as ``[object Object]``.
    """
    env = env_ctor(db_delta=task.initial_state_delta)
    node_executions = []
    for action in task.evaluation_criteria.actions:
        entry: Dict[str, Any] = {"tool": action.name, "arguments": action.arguments}
        try:
            result = env.make_tool_call(action.name, **(action.arguments or {}))
            entry["success"] = True
            entry["result"] = stringify(result)
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
    def __init__(self, job_id: str, task: Task, config: dict, key: Optional[str]) -> None:
        self.job_id = job_id
        self.task = task
        self.config = config
        self.key = key
        self.state = "queued"
        self.trials_done = 0
        self.trials_total = int(config["kRuns"])
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.cancel = threading.Event()


class JobRegistry:
    """Rollout jobs by id, run on one worker thread each."""

    def __init__(self, max_results: int = MAX_RETAINED_RESULTS,
                 max_statuses: int = MAX_TERMINAL_STATUSES,
                 max_active: int = MAX_ACTIVE_JOBS) -> None:
        self._jobs: Dict[str, _Job] = {}
        self._by_key: Dict[str, str] = {}
        # Terminal jobs by kind, oldest first: results carry transcripts, statuses carry strings.
        self._results_order: List[str] = []
        self._statuses_order: List[str] = []
        self._max_results = max_results
        self._max_statuses = max_statuses
        self._max_active = max_active
        self._counter = 0
        self._lock = threading.Lock()

    # ── protocol surface ──────────────────────────────────────────────────

    def submit(self, task_raw: Any, config: Any, idempotency_key: Any = None) -> str:
        task, config = _validate_submit(task_raw, config, idempotency_key)
        with self._lock:
            # The key identifies work IN FLIGHT: joining a live job prevents a double spend, and a
            # finished job's key was dropped at its terminal transition, so a re-submission after
            # completion re-runs rather than answering from (or being stranded on) a dead job.
            if idempotency_key is not None and idempotency_key in self._by_key:
                return self._by_key[idempotency_key]
            active = sum(1 for j in self._jobs.values() if j.state in ("queued", "running"))
            if active >= self._max_active:
                raise WireFailure(WireErrorCode.INTERNAL_ERROR,
                                  f"{active} rollouts already in flight; retry later",
                                  kind="overloaded", retryable=True)
            self._counter += 1
            job = _Job(f"j{self._counter}", task, config, idempotency_key)
            self._jobs[job.job_id] = job
            if idempotency_key is not None:
                self._by_key[idempotency_key] = job.job_id
        try:
            threading.Thread(target=self._work, args=(job,), daemon=True,
                             name=f"rollout-{job.job_id}").start()
        except Exception:
            # A thread that never started leaves a permanently-queued zombie whose key would poison
            # every retry of this very submit. Unregister before surfacing the failure.
            with self._lock:
                self._jobs.pop(job.job_id, None)
                if idempotency_key is not None and self._by_key.get(idempotency_key) == job.job_id:
                    del self._by_key[idempotency_key]
            raise
        return job.job_id

    def status(self, job_id: Any) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise _job_not_found(job_id)
            # One coherent snapshot: state and error transition together under this lock in _work,
            # so reading them under it too cannot observe `running` carrying an error.
            out: Dict[str, Any] = {"state": job.state, "trialsDone": job.trials_done,
                                   "trialsTotal": job.trials_total}
            if job.error is not None:
                out["error"] = job.error
            return out

    def result(self, job_id: Any) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise _job_not_found(job_id)
            if job.state == "cancelled":
                raise WireFailure(WireErrorCode.JOB_CANCELLED,
                                  f"job {job_id!r} was cancelled; there is no result",
                                  kind="job_cancelled", retryable=False)
            if job.state == "error":
                # The job HAS finished — 3002's "not finished yet" would send a well-behaved client
                # into a poll-forever loop. This is the gym having failed, so say so.
                raise WireFailure(WireErrorCode.INTERNAL_ERROR,
                                  f"job {job_id!r} failed and will have no result",
                                  kind="job_failed", retryable=False,
                                  details={"jobId": job_id, "error": job.error})
            if job.state != "done":
                raise WireFailure(WireErrorCode.JOB_NOT_COMPLETE,
                                  f"job {job_id!r} is {job.state}; poll rollout.status",
                                  kind="job_not_complete", retryable=True)
            # Collection is consumption: the job is evicted whole, so every later call — status or
            # result — answers an honest 3001 rather than a done-but-empty half-state.
            result = job.result
            self._evict(job.job_id)
            return result

    def cancel(self, job_id: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise _job_not_found(job_id)
        # Idempotent, and a no-op on a job that already reached a terminal state.
        job.cancel.set()

    # ── internals ─────────────────────────────────────────────────────────

    def _work(self, job: _Job) -> None:
        try:
            if job.cancel.is_set():
                raise _Cancelled()
            job.state = "running"
            result = _run_rollout(job)
            # A cancel that landed during the final trial's judges must still win: serving the
            # payload of a cancelled job breaks "result afterwards is 3003".
            if job.cancel.is_set():
                raise _Cancelled()
            # A result the wire cannot carry (a NaN score from a judge, an unserialisable object)
            # must fail HERE, while the job can still turn to `error` — failing at collection time
            # would leave the caller retrying a reply that can never encode.
            json.dumps(result, allow_nan=False)
            self._finish(job, "done", result=result)
        except _Cancelled:
            self._finish(job, "cancelled")
        except WireFailure as e:
            self._finish(job, "error", error=str(e))
        except Exception as e:  # noqa: BLE001 - a rollout-level failure ends the job, never the process
            self._finish(job, "error", error=f"{type(e).__name__}: {e}")

    def _finish(self, job: _Job, state: str, *, result: Optional[dict] = None,
                error: Optional[str] = None) -> None:
        with self._lock:
            job.result = result
            job.error = error
            job.state = state
            # The key protects in-flight work only (see submit); a finished job releases it.
            if job.key is not None and self._by_key.get(job.key) == job.job_id:
                del self._by_key[job.key]
            order = self._results_order if state == "done" else self._statuses_order
            order.append(job.job_id)
            bound = self._max_results if state == "done" else self._max_statuses
            while len(order) > bound:
                self._evict(order[0])

    def _evict(self, job_id: str) -> None:
        """Remove a terminal job entirely (caller holds the lock). Gone means gone: a later call
        gets an honest 3001, never a status whose payload quietly stopped existing."""
        self._jobs.pop(job_id, None)
        for order in (self._results_order, self._statuses_order):
            if job_id in order:
                order.remove(job_id)


def _job_not_found(job_id: Any) -> WireFailure:
    return WireFailure(WireErrorCode.JOB_NOT_FOUND, f"no rollout job {job_id!r}",
                       kind="job_not_found", retryable=False)


def _validate_submit(task_raw: Any, config: Any, idempotency_key: Any) -> tuple:
    """Everything checkable without running anything, checked at submit (INVALID_PARAMS)."""
    if not isinstance(config, dict):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "config must be an object",
                          kind="invalid_params", retryable=False)
    target = config.get("targetModel")
    if not isinstance(target, str) or not target:
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "config.targetModel must be a non-empty string",
                          kind="invalid_params", retryable=False)
    k = config.get("kRuns")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "config.kRuns must be a positive integer",
                          kind="invalid_params", retryable=False)
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "idempotencyKey must be a string",
                          kind="invalid_params", retryable=False)
    gym_config = config.get("gymConfig")
    if gym_config is not None and not isinstance(gym_config, dict):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "config.gymConfig must be an object",
                          kind="invalid_params", retryable=False)
    gym_config = gym_config or {}
    free_text = gym_config.get("freeTextMatch")
    if free_text is not None and free_text not in _FREE_TEXT_STRATEGIES:
        raise WireFailure(WireErrorCode.INVALID_PARAMS,
                          f"gymConfig.freeTextMatch must be one of {_FREE_TEXT_STRATEGIES}",
                          kind="invalid_params", retryable=False)
    max_steps = gym_config.get("maxSteps")
    if max_steps is not None and (not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1):
        raise WireFailure(WireErrorCode.INVALID_PARAMS, "gymConfig.maxSteps must be a positive integer",
                          kind="invalid_params", retryable=False)
    return _state.decode_task(task_raw), config


def _run_rollout(job: _Job) -> dict:
    """All k trials plus the once-per-rollout artifacts. Runs on the worker thread.

    A trial that fails to run becomes its per-run ``error`` entry and the remaining trials still
    run; an exception escaping THIS function is a rollout-level fault and ends the job.
    """
    task = job.task
    config = job.config
    gym_config = config.get("gymConfig") or {}
    target_model = _litellm_model(config["targetModel"])
    user_model = _litellm_model(gym_config.get("userModel") or DEFAULT_LLM_USER)
    judge_model = _litellm_model(gym_config.get("judgeModel") or DEFAULT_LLM_NL_JUDGE)
    max_steps = gym_config.get("maxSteps") or DEFAULT_MAX_STEPS
    free_text = gym_config.get("freeTextMatch")
    db_text_match = TextMatchConfig(strategy=free_text) if free_text else None
    # The task's own seed wins (the suite is mixed-seed); the config's `db` is the run-level default.
    seed_db = task.seed_db or gym_config.get("db")

    scope = set(task.org_ids) if task.org_ids is not None else (
        {task.org_id} if task.org_id is not None else None)
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
            try:
                per_run.append(_run_trial(
                    task, env_ctor, idx,
                    target_model=target_model, user_model=user_model, judge_model=judge_model,
                    max_steps=max_steps, db_text_match=db_text_match, cancel=job.cancel,
                ))
            except _Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 - one trial's failure must not discard the others
                per_run.append({"runIdx": idx, "passed": False, "transcript": [],
                                "toolCalls": 0, "error": f"{type(e).__name__}: {e}"})
            job.trials_done = idx + 1

        passed = sum(1 for r in per_run if r["passed"])
        return {
            # Echoed as the platform named it, not as litellm spells it.
            "targetModel": config["targetModel"],
            "kRuns": job.trials_total,
            "passRate": passed / job.trials_total,
            "perRun": per_run,
            "artifacts": {
                "oracle": oracle,
                "initial_db": initial_db,
                # The evidence of record must show what actually judged it: without this, a caller
                # that omitted judgeModel could not tell the default judge decided the verdicts.
                "config_resolved": {
                    "targetModel": target_model,
                    "userModel": user_model,
                    "judgeModel": judge_model,
                    "maxSteps": max_steps,
                    "freeTextMatch": free_text or DEFAULT_DB_TEXT_MATCH,
                    "seedDb": seed_db or "default",
                },
            },
        }
    finally:
        reset_now()


def _run_trial(task: Task, env_ctor, idx: int, *, target_model: str, user_model: str,
               judge_model: str, max_steps: int, db_text_match: Optional[TextMatchConfig],
               cancel: threading.Event) -> dict:
    env = env_ctor(db_delta=task.initial_state_delta)
    agent = _CancelGate(LLMAgent(env.get_policy(), env.get_tool_schemas(), target_model), cancel)
    user = _CancelGate(UserSimulator(task.scenario, llm=user_model), cancel)

    run = Orchestrator(agent, user, env, max_steps=max_steps).run()
    if cancel.is_set():
        raise _Cancelled()  # the episode is over, but the judges have not been paid for yet

    reward_info = evaluate_task(
        env_ctor, task, trajectory=run.trajectory, final_env=env,
        nl_llm=judge_model, db_text_match=db_text_match,
    )
    if cancel.is_set():
        raise _Cancelled()  # a cancel during the judges must not record (or pay to diff) the trial
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
