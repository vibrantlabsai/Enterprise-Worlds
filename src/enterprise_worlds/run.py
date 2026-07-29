"""Run evals: tie agent <-> user simulator <-> environment <-> evaluator.

A small, registry-backed runner. Only the ``itsm`` domain is wired today; add
an entry to ``DOMAINS`` to register more.

Supports **k trials per task** (``run_domain(..., k=...)``) with optional seeding, and reports
both the mean reward and the ``pass^k`` reliability curve. ``save_run_dir`` writes a
structured run directory (compact ``summary.json`` + per task/trial trajectory files).
"""

import math
from pathlib import Path
from typing import Callable, Optional, Union

from pydantic import BaseModel

from enterprise_worlds.agent.llm_agent import LLMAgent
from enterprise_worlds.config import (
    DEFAULT_LLM_NL_JUDGE,
    DEFAULT_LLM_USER,
)
from enterprise_worlds.data_model.message import Message
from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.domains.itsm import environment as itsm_environment
from enterprise_worlds.environment.environment import Environment
from enterprise_worlds.evaluator.evaluator import RewardInfo, evaluate_task
from enterprise_worlds.evaluator.text_match_strategy import TextMatchConfig
from enterprise_worlds.orchestrator.orchestrator import Orchestrator
from enterprise_worlds.utils.clock import DEFAULT_NOW, reset_now, set_now
from enterprise_worlds.utils.io_utils import dump_file

DEFAULT_LLM_AGENT = "gpt-4o"


class DomainSpec(BaseModel):
    name: str
    get_environment: Callable[..., Environment]
    get_tasks: Callable[[], list[Task]]
    policy_path: Path

    model_config = {"arbitrary_types_allowed": True}


DOMAINS: dict[str, DomainSpec] = {
    "itsm": DomainSpec(
        name="itsm",
        get_environment=itsm_environment.get_environment,
        get_tasks=itsm_environment.get_tasks,
        policy_path=itsm_environment.ITSM_POLICY_PATH,
    ),
}


def list_domains() -> list[str]:
    return list(DOMAINS)


def get_domain(name: str) -> DomainSpec:
    if name not in DOMAINS:
        raise ValueError(f"Unknown domain {name!r}. Available: {list_domains()}")
    return DOMAINS[name]


def _pass_hat_k(n: int, c: int, k: int) -> float:
    """Unbiased estimate that a random size-k subset of n trials (c passing) all pass.

    The HumanEval ``pass^k`` estimator: ``C(c, k) / C(n, k)``.
    """
    if k > n or math.comb(n, k) == 0:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


class TaskResult(BaseModel):
    """Outcome of a single task trial (serialisable for --save-to / run dir)."""

    task_id: str
    trial: int = 0
    reward: float
    reward_info: RewardInfo
    stopped: bool
    num_tool_calls: int
    trajectory: list[Message]
    error: Optional[str] = None  # set if the trial crashed (e.g. provider error); reward is 0


class RunResults(BaseModel):
    domain: str
    agent_llm: str
    user_llm: str
    judge_llm: str
    k: int = 1
    results: list[TaskResult]  # flat list of every task x trial outcome

    @property
    def avg_reward(self) -> float:
        """Mean reward across all task x trial runs."""
        if not self.results:
            return 0.0
        return sum(r.reward for r in self.results) / len(self.results)

    def rewards_by_task(self) -> dict[str, list[float]]:
        by_task: dict[str, list[float]] = {}
        for r in self.results:
            by_task.setdefault(r.task_id, []).append(r.reward)
        return by_task

    def avg_pass_hat_k(self) -> dict[int, float]:
        """``pass^j`` for j=1..k, averaged over tasks (a trial 'passes' when reward >= 1.0)."""
        by_task = self.rewards_by_task()
        if not by_task:
            return {}
        out: dict[int, float] = {}
        for j in range(1, self.k + 1):
            vals = []
            for rewards in by_task.values():
                n = len(rewards)
                c = sum(1 for x in rewards if x >= 1.0)
                vals.append(_pass_hat_k(n, c, j))
            out[j] = sum(vals) / len(vals)
        return out


def run_task(
    domain: str,
    task: Task,
    agent_llm: str = DEFAULT_LLM_AGENT,
    user_llm: str = DEFAULT_LLM_USER,
    judge_llm: str = DEFAULT_LLM_NL_JUDGE,
    max_steps: int = 12,
    seed: Optional[int] = None,
    trial: int = 0,
    db_text_match: Optional[TextMatchConfig] = None,
) -> TaskResult:
    """Run and evaluate a single task trial end to end.

    ``seed`` (when set) is applied to the agent LLM for reproducibility — best-effort, since
    not every provider honours it (e.g. Bedrock/Anthropic drop it).
    """
    spec = get_domain(domain)

    # The task owns "now": freeze the env clock to it so the live run, the gold-action replay, and
    # the predicted-state rebuild all stamp identical timestamps (created_on/updated_on, and a new
    # incident SLA's start_time). Restored afterwards so cross-task runs don't leak the clock.
    set_now(task.current_time or DEFAULT_NOW)
    try:
        # Bake the task's acting user into a constructor so the live run, the gold-action replay,
        # and the predicted-state rebuild all use the same environment.
        def env_ctor(db_delta=None):
            return spec.get_environment(
                db_delta=db_delta,
                acting_user_id=task.acting_user_id,
                org_id=task.org_id,
                org_ids=task.org_ids,
                seed_db=task.seed_db,
            )

        env = env_ctor(db_delta=task.initial_state_delta)
        agent = LLMAgent(env.get_policy(), env.get_tool_schemas(), agent_llm)
        if seed is not None:
            agent.set_seed(seed)
        user_sim = _make_user(task, user_llm)

        run = Orchestrator(agent, user_sim, env, max_steps=max_steps).run()
        reward_info = evaluate_task(
            env_ctor,
            task,
            trajectory=run.trajectory,
            final_env=env,
            nl_llm=judge_llm,
            db_text_match=db_text_match,
        )
        return TaskResult(
            task_id=task.id,
            trial=trial,
            reward=reward_info.reward,
            reward_info=reward_info,
            stopped=run.stopped,
            num_tool_calls=len(run.agent_tool_calls),
            trajectory=run.trajectory,
        )
    finally:
        reset_now()


def run_domain(
    domain: str,
    task_ids: Optional[list[str]] = None,
    num_tasks: Optional[int] = None,
    agent_llm: str = DEFAULT_LLM_AGENT,
    user_llm: str = DEFAULT_LLM_USER,
    judge_llm: str = DEFAULT_LLM_NL_JUDGE,
    max_steps: int = 12,
    k: int = 1,
    seed: Optional[int] = None,
    on_result: Optional[Callable[[TaskResult], None]] = None,
    db_text_match: Optional[TextMatchConfig] = None,
) -> RunResults:
    """Run and evaluate a set of tasks for a domain, ``k`` trials each.

    When ``seed`` is given, trial ``i`` of every task uses ``seed + i`` (reproducible); otherwise
    trials vary only by sampling temperature.
    """
    spec = get_domain(domain)
    tasks = spec.get_tasks()
    if task_ids:
        wanted = set(task_ids)
        tasks = [t for t in tasks if t.id in wanted]
    if num_tasks is not None:
        tasks = tasks[:num_tasks]

    results: list[TaskResult] = []
    for task in tasks:
        for trial in range(k):
            trial_seed = None if seed is None else seed + trial
            try:
                result = run_task(
                    domain,
                    task,
                    agent_llm=agent_llm,
                    user_llm=user_llm,
                    judge_llm=judge_llm,
                    max_steps=max_steps,
                    seed=trial_seed,
                    trial=trial,
                    db_text_match=db_text_match,
                )
            except Exception as e:  # noqa: BLE001 - one trial's failure shouldn't abort the batch
                result = TaskResult(
                    task_id=task.id,
                    trial=trial,
                    reward=0.0,
                    reward_info=RewardInfo(reward=0.0),
                    stopped=False,
                    num_tool_calls=0,
                    trajectory=[],
                    error=f"{type(e).__name__}: {e}",
                )
            results.append(result)
            if on_result is not None:
                on_result(result)

    return RunResults(
        domain=domain,
        agent_llm=agent_llm,
        user_llm=user_llm,
        judge_llm=judge_llm,
        k=k,
        results=results,
    )


def dump_trial_result(out_dir: Union[str, Path], r: TaskResult) -> Path:
    """Write one trial's full TaskResult to ``<out_dir>/<task_id>/trial_<i>.json`` and return it.

    Used both incrementally (per trial, as it completes — so a crashed/interrupted run keeps the
    trajectories already produced) and by ``save_run_dir`` at the end.
    """
    task_dir = Path(out_dir) / r.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / f"trial_{r.trial}.json"
    dump_file(path, r.model_dump())
    return path


def save_run_dir(results: RunResults, out_dir: Union[str, Path]) -> Path:
    """Write a structured run directory.

    Layout::

        <out_dir>/summary.json              # metadata + metrics + per-task pass^k (no trajectories)
        <out_dir>/<task_id>/trial_<i>.json  # full TaskResult (trajectory + reward_info) per trial
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    by_task = results.rewards_by_task()
    per_task = []
    errors_by_task: dict[str, list[str]] = {}
    for r in results.results:
        if r.error:
            errors_by_task.setdefault(r.task_id, []).append(r.error)
    for tid, rewards in by_task.items():
        c = sum(1 for x in rewards if x >= 1.0)
        n = len(rewards)
        per_task.append({
            "task_id": tid,
            "rewards": rewards,
            "passes": c,
            "trials": n,
            "pass^k": {str(j): _pass_hat_k(n, c, j) for j in range(1, results.k + 1)},
            "errors": errors_by_task.get(tid, []),
        })

    summary = {
        "domain": results.domain,
        "agent_llm": results.agent_llm,
        "user_llm": results.user_llm,
        "judge_llm": results.judge_llm,
        "k": results.k,
        "num_tasks": len(by_task),
        "num_runs": len(results.results),
        "avg_reward": results.avg_reward,
        "avg_pass^k": {str(j): v for j, v in results.avg_pass_hat_k().items()},
        "per_task": per_task,
    }
    dump_file(out / "summary.json", summary)

    for r in results.results:
        dump_trial_result(out, r)

    return out


def _make_user(task: Task, user_llm: str):
    from enterprise_worlds.user.user_simulator import UserSimulator

    return UserSimulator(task.scenario, llm=user_llm)
