"""NL-assertion evaluation (item 5b).

An LLM judge grades each natural-language assertion against the conversation
trajectory.
"""

import json
from typing import TYPE_CHECKING, Optional

from loguru import logger
from pydantic import BaseModel

from enterprise_worlds.config import DEFAULT_LLM_NL_JUDGE, DEFAULT_LLM_NL_JUDGE_ARGS
from enterprise_worlds.data_model.message import Message, SystemMessage, UserMessage, render_trajectory
from enterprise_worlds.utils.llm_utils import generate

if TYPE_CHECKING:
    from enterprise_worlds.environment.db import DB

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

# Verifier v2: appended when a final_database_state section is present. QA audits measured the
# transcript-only judge crediting state claims from agent narration (SLA "paused" with no tool
# call; used_as='resolution' accepted as "applied"; links never created marked met) — state
# claims must be grounded in the state itself.
STATE_GROUNDING_RULES = """

GRADING RULES
- Claims about database state (statuses, links, field values, recipients, enum values) MUST be
  verified against the final_database_state section — the conversation is NOT evidence for
  state. If the state contradicts the assertion, it is not met, regardless of what the agent
  said or claimed to have done.
- Enum values are exact: "applied" is not "resolution"; "advanced to assess" means the final
  status is assess, not a state beyond it.
- Exception: notification delivery status is system-managed (always recorded as sent) — grade
  notification claims on existence, recipient, type, and content, never on queued/sent/
  delivered wording in the assertion.
- Only dialogue-behavior claims (the agent asked, confirmed, communicated, or was told
  something) are graded from the conversation.
""".rstrip()


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
    final_db: Optional["DB"] = None,
) -> NLCheck:
    """Grade the assertions; with ``final_db`` given, state claims are graded against the
    final DB (transcript stays evidence for dialogue-behavior claims only)."""
    if not nl_assertions:
        return NLCheck(checks=[], reward=1.0)

    llm = llm or DEFAULT_LLM_NL_JUDGE
    llm_args = llm_args if llm_args is not None else dict(DEFAULT_LLM_NL_JUDGE_ARGS)

    system_prompt = JUDGE_SYSTEM_PROMPT
    db_section = ""
    if final_db is not None:
        db_section = (
            "final_database_state (authoritative for state claims):\n"
            f"{_relevant_db_dump(final_db, nl_assertions)}\n\n"
        )
        system_prompt = JUDGE_SYSTEM_PROMPT + STATE_GROUNDING_RULES

    user_prompt = (
        f"conversation:\n{render_trajectory(trajectory)}\n\n"
        + db_section
        + f"expectedOutcomes:\n{json.dumps(nl_assertions, indent=2)}"
    )
    response = generate(
        model=llm,
        messages=[
            SystemMessage(content=system_prompt),
            UserMessage(content=user_prompt),
        ],
        **llm_args,
    )

    checks = _parse_judge_response(response.content, nl_assertions)
    reward = 1.0 if checks and all(c.met for c in checks) else 0.0
    return NLCheck(checks=checks, reward=reward)


_DB_DUMP_CAP = 30_000  # chars; past this, fall back to referenced records + counts


def _relevant_db_dump(db: "DB", nl_assertions: list[str]) -> str:
    """Compact JSON dump of the collections the assertions plausibly reference.

    A collection is relevant when its name (singular or plural-ish) appears in any assertion,
    or when one of its record ids does. If the filtered dump still exceeds ``_DB_DUMP_CAP``,
    degrade to per-collection record counts plus only the directly referenced records — the
    judge needs the rows the assertions talk about, not the whole world.
    """
    text = " ".join(nl_assertions).lower()
    full = db.model_dump()
    picked: dict[str, dict] = {}
    for coll, records in full.items():
        if not isinstance(records, dict):
            continue
        name_hit = coll.lower().rstrip("s") in text or coll.lower() in text
        id_hits = {rid for rid in records if rid.lower() in text}
        if name_hit:
            picked[coll] = records
        elif id_hits:
            picked[coll] = {rid: records[rid] for rid in id_hits}
    dump = json.dumps(picked, default=str)
    if len(dump) <= _DB_DUMP_CAP:
        return dump
    # Too big: keep only directly referenced records, summarize the rest.
    slim: dict[str, object] = {}
    for coll, records in picked.items():
        referenced = {rid: rec for rid, rec in records.items() if rid.lower() in text}
        slim[coll] = referenced or f"<{len(records)} records; none directly referenced>"
    return json.dumps(slim, default=str)[:_DB_DUMP_CAP]


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
