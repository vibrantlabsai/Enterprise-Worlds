"""NL-assertion evaluation (item 5b).

An LLM judge grades each natural-language assertion against the conversation
trajectory.
"""

import json
from typing import Optional

from loguru import logger
from pydantic import BaseModel

from enterprise_worlds.config import DEFAULT_LLM_NL_JUDGE, DEFAULT_LLM_NL_JUDGE_ARGS
from enterprise_worlds.data_model.message import Message, SystemMessage, UserMessage, render_trajectory
from enterprise_worlds.utils.llm_utils import generate

JUDGE_SYSTEM_PROMPT = """
You are grading whether a conversation met a list of expected outcomes.

TASK
- You are given a conversation and a list of expected outcomes.
- Grade each expected outcome independently as met or not met, based only on the
  conversation.

FORMAT
Respond with a JSON object exactly of the form:
{
  "results": [
    {"expectedOutcome": "<the assertion>", "reasoning": "<short reasoning>", "metExpectation": true or false}
  ]
}
""".strip()


class NLAssertionCheck(BaseModel):
    nl_assertion: str
    met: bool
    reasoning: Optional[str] = None


class NLCheck(BaseModel):
    checks: list[NLAssertionCheck]
    reward: float


def evaluate_nl_assertions(
    trajectory: list[Message],
    nl_assertions: list[str],
    llm: Optional[str] = None,
    llm_args: Optional[dict] = None,
) -> NLCheck:
    if not nl_assertions:
        return NLCheck(checks=[], reward=1.0)

    llm = llm or DEFAULT_LLM_NL_JUDGE
    llm_args = llm_args if llm_args is not None else dict(DEFAULT_LLM_NL_JUDGE_ARGS)

    user_prompt = (
        f"conversation:\n{render_trajectory(trajectory)}\n\n"
        f"expectedOutcomes:\n{json.dumps(nl_assertions, indent=2)}"
    )
    response = generate(
        model=llm,
        messages=[
            SystemMessage(content=JUDGE_SYSTEM_PROMPT),
            UserMessage(content=user_prompt),
        ],
        **llm_args,
    )

    checks = _parse_judge_response(response.content, nl_assertions)
    reward = 1.0 if checks and all(c.met for c in checks) else 0.0
    return NLCheck(checks=checks, reward=reward)


def _parse_judge_response(content: Optional[str], nl_assertions: list[str]) -> list[NLAssertionCheck]:
    try:
        data = json.loads(_extract_json(content or ""))
        results = data["results"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"could not parse NL judge response: {e}")
        return [NLAssertionCheck(nl_assertion=a, met=False, reasoning="unparseable judge output") for a in nl_assertions]

    checks = []
    for r in results:
        checks.append(
            NLAssertionCheck(
                nl_assertion=r.get("expectedOutcome", ""),
                met=bool(r.get("metExpectation", False)),
                reasoning=r.get("reasoning"),
            )
        )
    return checks


def _extract_json(text: str) -> str:
    """Best-effort extraction of the JSON object from a judge response.

    Robust to reasoning models that wrap output in ``<think>...</think>`` blocks and to
    markdown code fences: strip both, then fall back to the outermost ``{...}`` span.
    """
    import re

    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = _strip_code_fence(text)
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()
