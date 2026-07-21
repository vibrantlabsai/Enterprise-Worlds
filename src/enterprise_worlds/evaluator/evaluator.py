"""Evaluator dispatcher (item 5).

Combines DB-based match (5a) and NL assertions (5b) into a single RewardInfo.
The combined reward is multiplicative over the criteria that the task defines,
so a task passes only if every defined criterion passes.
"""

from typing import Callable, Optional

from pydantic import BaseModel

from enterprise_worlds.data_model.message import Message, ToolCall
from enterprise_worlds.data_model.tasks import Task
from enterprise_worlds.environment.environment import Environment
from enterprise_worlds.evaluator.evaluator_env import DBCheck, calculate_db_reward
from enterprise_worlds.evaluator.evaluator_nl import NLCheck, evaluate_nl_assertions
from enterprise_worlds.evaluator.text_match_strategy import TextMatchConfig


class RewardInfo(BaseModel):
    reward: float
    db_check: Optional[DBCheck] = None
    nl_check: Optional[NLCheck] = None


def evaluate_task(
    environment_constructor: Callable[..., Environment],
    task: Task,
    trajectory: list[Message],
    final_env: Optional[Environment] = None,
    agent_tool_calls: Optional[list[ToolCall]] = None,
    nl_llm: Optional[str] = None,
    nl_llm_args: Optional[dict] = None,
    skip_nl_assertions: bool = False,
    db_text_match: Optional[TextMatchConfig] = None,
) -> RewardInfo:
    """Score a completed run against the task's evaluation criteria.

    The reward is the product of the two criteria the task may define: gold-action
    full-DB-hash match and the NL-assertion judge. A task passes (reward 1.0) only if
    every defined criterion passes.

    ``db_text_match`` selects how free-text DB fields are compared (default ``llm``); when its
    ``llm`` strategy has no judge model it inherits ``nl_llm`` / ``nl_llm_args``, so callers that
    already pass an NL judge get semantic free-text matching for free.
    """
    criteria = task.evaluation_criteria

    # gold-action full-DB-hash (computed whenever the task defines gold actions)
    db_check: Optional[DBCheck] = None
    if criteria.actions:
        text_match = db_text_match or TextMatchConfig()
        if text_match.strategy == "llm" and not text_match.llm:
            text_match = text_match.model_copy(update={"llm": nl_llm, "llm_args": nl_llm_args})
        db_check = calculate_db_reward(
            environment_constructor, task, final_env=final_env,
            agent_tool_calls=agent_tool_calls, text_match=text_match,
        )

    # NL-assertion judge. Skipped for RL/gym reward, where the judge LLM is unnecessary
    # overhead/non-determinism.
    nl_check: Optional[NLCheck] = None
    if criteria.nl_assertions and not skip_nl_assertions:
        nl_check = evaluate_nl_assertions(
            trajectory, criteria.nl_assertions, llm=nl_llm, llm_args=nl_llm_args
        )

    reward = (db_check.reward if db_check else 1.0) * (nl_check.reward if nl_check else 1.0)

    return RewardInfo(reward=reward, db_check=db_check, nl_check=nl_check)
