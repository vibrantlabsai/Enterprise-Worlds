"""DB-based match (item 5a).

Computes the expected ("gold") DB by replaying the task's gold ``actions`` through the real
tools on a fresh seed+delta environment, then compares it to the run's final DB record by record:
**structured fields exact, free-text fields fuzzy** (an LLM can't reproduce prose like a
notification subject verbatim — see ``compare_dbs`` and ``utils.text_match``).
"""

from datetime import datetime
from typing import Any, Callable, Optional

from loguru import logger
from pydantic import BaseModel, Field

from enterprise_worlds.data_model.message import Message, ToolCall
from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.environment.db import DB
from enterprise_worlds.environment.environment import Environment
from enterprise_worlds.evaluator.text_match_strategy import FreeTextVerdict, TextMatchConfig, judge_free_text
from enterprise_worlds.utils.text_match import fuzzy_text_match


class DBCheck(BaseModel):
    db_match: bool
    reward: float
    mismatches: list[str] = Field(default_factory=list)  # first divergences, for debugging
    # Per-field semantic-equivalence verdicts (with reasoning) for every free-text pair the LLM
    # judge decided — so a caller can see WHY a prose field was (not) accepted, not just db_match.
    text_judgments: list[FreeTextVerdict] = Field(default_factory=list)


def _build_gold_env(environment_constructor: Callable[..., Environment], task: Task) -> Environment:
    gold_env = environment_constructor(db_delta=task.initial_state_delta)
    for action in task.evaluation_criteria.actions:
        try:
            gold_env.make_tool_call(action.name, **action.arguments)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"gold action {action.name} failed: {e}")
    return gold_env


def _predicted_env(
    environment_constructor: Callable[..., Environment],
    task: Task,
    final_env: Optional[Environment],
    agent_tool_calls: Optional[list[ToolCall]],
) -> Environment:
    """The DB state the agent actually produced.

    Default: the live ``final_env`` the run already mutated. Offline fallback:
    rebuild from recorded ``agent_tool_calls`` on a fresh seed+delta env.
    """
    if final_env is not None:
        return final_env
    if agent_tool_calls is None:
        raise ValueError("Provide either final_env or agent_tool_calls.")
    env = environment_constructor(db_delta=task.initial_state_delta)
    for tc in agent_tool_calls:
        try:
            env.make_tool_call(tc.name, **tc.arguments)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"replayed agent call {tc.name} failed: {e}")
    return env


def tool_calls_from_trajectory(trajectory: list[Message]) -> list[ToolCall]:
    """Extract assistant tool calls from a trajectory (for offline re-eval)."""
    calls: list[ToolCall] = []
    for msg in trajectory:
        tcs = getattr(msg, "tool_calls", None)
        if tcs and getattr(msg, "role", None) == "assistant":
            calls.extend(tcs)
    return calls


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
)


def _as_instant(value: Any) -> Optional[datetime]:
    """Parse a timestamp string to a datetime, or None if it isn't one."""
    if not isinstance(value, str):
        return None
    v = value.strip().removesuffix("Z")
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _values_equal(gv: Any, pv: Any) -> bool:
    """Exact equality, or the same instant in a different timestamp serialization."""
    if gv == pv:
        return True
    g = _as_instant(gv)
    return g is not None and g == _as_instant(pv)


def _compare_record(
    prefix: str,
    gold: dict,
    pred: dict,
    freetext: list[str],
    baseline: dict,
    cfg: TextMatchConfig,
    strategy: str,
    mismatches: list[str],
    pending: list[dict],
) -> None:
    """Compare two record dicts; append divergences to ``mismatches``.

    Non-free-text fields must be equal (the same instant in a different timestamp format counts
    as equal). A free-text field is only checked when the gold actually
    *changed* it from ``baseline`` (the pre-action seed+delta value); if the gold left a prose
    field untouched, the agent's value there is unconstrained. How a changed free-text field is
    judged depends on ``strategy``: ``exact`` (==), ``fuzzy`` (content overlap), or ``llm`` —
    which defers to a batched semantic judge by appending ``{field, gold, pred}`` to ``pending``
    (decided once for the whole DB, so empty/None cases are resolved here to save judge tokens).
    """
    for field in sorted(set(gold) | set(pred)):
        gv, pv = gold.get(field), pred.get(field)
        if field in freetext:
            if gv == baseline.get(field):
                continue  # gold didn't set this prose field; don't constrain the agent
            if strategy == "exact":
                if gv != pv:
                    mismatches.append(f"{prefix} {field}: {gv!r} != {pv!r}")
            elif strategy == "llm":
                gold_has = bool(gv and str(gv).strip())
                pred_has = bool(pv and str(pv).strip())
                if not gold_has:
                    continue  # no text requirement
                if not pred_has:
                    mismatches.append(f"{prefix} {field}: {gv!r} !~ '' (empty)")
                else:
                    pending.append({"key": f"{prefix} {field}", "field": field, "gold": gv, "pred": pv})
            elif not fuzzy_text_match(gv, pv, cfg.threshold):
                mismatches.append(f"{prefix} {field}: {gv!r} !~ {pv!r}")
        elif not _values_equal(gv, pv):
            mismatches.append(f"{prefix} {field}: {gv!r} != {pv!r}")


def _dump(rec):
    return rec.model_dump() if isinstance(rec, BaseModel) else rec


def compare_dbs(
    gold_db: DB,
    pred_db: DB,
    baseline_db: Optional[DB] = None,
    cfg: Optional[TextMatchConfig] = None,
    text_verdicts_out: Optional[list[FreeTextVerdict]] = None,
) -> tuple[bool, list[str]]:
    """Record-by-record DB comparison: structured fields exact, free-text fields per ``cfg``.

    Collections must have identical record-id sets (no missing / no extra rows). ``baseline_db``
    (the seed+delta DB before the gold actions) lets free-text fields the gold never changed be
    ignored. ``cfg`` (a ``TextMatchConfig``, default ``llm`` → ``fuzzy`` without a judge model)
    selects the free-text comparison; under ``llm`` all changed prose pairs are judged in one
    batched call after the structural walk. Returns ``(matched, mismatches)``. When
    ``text_verdicts_out`` is provided, the per-pair :class:`FreeTextVerdict`s from the judge
    (equivalent + reasoning) are appended to it for callers that want the judge's rationale.
    """
    cfg = cfg or TextMatchConfig()
    strategy = cfg.effective_strategy()
    freetext = gold_db.freetext_fields()
    mismatches: list[str] = []
    pending: list[dict] = []  # llm strategy: free-text pairs to judge in one batch
    for coll in type(gold_db).model_fields:
        gold_coll, pred_coll = getattr(gold_db, coll), getattr(pred_db, coll)
        if not isinstance(gold_coll, dict):
            if gold_coll != pred_coll:
                mismatches.append(f"{coll}: {gold_coll!r} != {pred_coll!r}")
            continue
        gold_ids, pred_ids = set(gold_coll), set(pred_coll)
        if gold_ids != pred_ids:
            if missing := sorted(gold_ids - pred_ids):
                mismatches.append(f"{coll}: missing {missing}")
            if extra := sorted(pred_ids - gold_ids):
                mismatches.append(f"{coll}: unexpected {extra}")
            continue
        ft = freetext.get(coll, [])
        base_coll = getattr(baseline_db, coll, {}) if baseline_db is not None else {}
        for rid in sorted(gold_ids):
            base = _dump(base_coll[rid]) if rid in base_coll else {}
            _compare_record(
                f"{coll}/{rid}", _dump(gold_coll[rid]), _dump(pred_coll[rid]),
                ft, base, cfg, strategy, mismatches, pending,
            )
    if pending:
        verdicts = judge_free_text(pending, cfg.llm, cfg.llm_args)  # type: ignore[arg-type]
        if text_verdicts_out is not None:
            text_verdicts_out.extend(verdicts)
        for pair, v in zip(pending, verdicts):
            if not v.equivalent:
                mismatches.append(f"{pair['key']}: {pair['gold']!r} !≈ {pair['pred']!r} (judge)")
    return (not mismatches), mismatches


def calculate_db_reward(
    environment_constructor: Callable[..., Environment],
    task: Task,
    final_env: Optional[Environment] = None,
    agent_tool_calls: Optional[list[ToolCall]] = None,
    text_match: Optional[TextMatchConfig] = None,
) -> DBCheck:
    gold_env = _build_gold_env(environment_constructor, task)
    pred_env = _predicted_env(environment_constructor, task, final_env, agent_tool_calls)
    # Baseline = seed+delta before any gold action, so free-text fields the gold never changed
    # don't constrain the agent.
    baseline_db = environment_constructor(db_delta=task.initial_state_delta).tools.db
    text_verdicts: list[FreeTextVerdict] = []
    match, mismatches = compare_dbs(
        gold_env.tools.db, pred_env.tools.db, baseline_db, text_match,
        text_verdicts_out=text_verdicts,
    )
    return DBCheck(
        db_match=match, reward=1.0 if match else 0.0,
        mismatches=mismatches[:20], text_judgments=text_verdicts,
    )
