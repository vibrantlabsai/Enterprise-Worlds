"""Configurable free-text comparison strategy for DB-match.

During ``db_match`` (``evaluator/evaluator_env.py``) structural fields (ids, enums, links,
numerics) are always compared exactly, but the prose columns a domain marks free-text
(``DB.freetext_fields()``) can be compared three ways, chosen **at eval time**:

- ``exact``  — strict equality.
- ``fuzzy``  — token-overlap recall ≥ threshold (``utils.text_match.fuzzy_text_match``); LLM-free.
- ``llm``    — semantic-equivalence judge: one batched ``generate`` call decides, per pair, whether
  PRED conveys the same meaning **and the same material facts** as GOLD, ignoring wording/format.

``llm`` is the default — an agent legitimately paraphrases prose, so lexical comparison
under-rewards correct work. It needs a judge model; when none is available the config falls back
to ``fuzzy`` so LLM-free callers (the gym's own unit tests) stay deterministic.

``generate`` is module-level on purpose: the conversational bridge monkey-patches
``text_match_strategy.generate`` (alongside the user-sim / nl-judge modules) to route the judge
call out to the TS side, exactly as it does for ``evaluator_nl``.
"""

import json
from typing import Literal, Optional

from loguru import logger
from pydantic import BaseModel

from enterprise_worlds.config import DEFAULT_DB_TEXT_MATCH, DEFAULT_LLM_NL_JUDGE_ARGS
from enterprise_worlds.data_model.message import SystemMessage, UserMessage
from enterprise_worlds.utils.llm_utils import generate
from enterprise_worlds.utils.text_match import DB_FUZZY_THRESHOLD

TextMatchStrategy = Literal["exact", "fuzzy", "llm"]


class TextMatchConfig(BaseModel):
    """How free-text fields are compared during ``db_match`` (structural fields stay exact)."""

    strategy: TextMatchStrategy = DEFAULT_DB_TEXT_MATCH
    threshold: float = DB_FUZZY_THRESHOLD  # fuzzy only
    llm: Optional[str] = None  # llm only; judge model (defaults to the nl judge when unset)
    llm_args: Optional[dict] = None

    def effective_strategy(self) -> TextMatchStrategy:
        """``llm`` degrades to ``fuzzy`` when no judge model is configured (deterministic fallback)."""
        if self.strategy == "llm" and not self.llm:
            logger.warning("text-match strategy 'llm' has no judge model; falling back to 'fuzzy'")
            return "fuzzy"
        return self.strategy


JUDGE_SYSTEM_PROMPT = """
You decide whether an agent's free-text database field is acceptable given a reference.

For each numbered pair you get the field name, a GOLD text (a reference an ideal agent might write)
and a PRED text (what the agent actually produced). Judge LENIENTLY and DIRECTIONALLY: GOLD is one
acceptable phrasing, not the only one. Mark `equivalent` true when PRED faithfully conveys GOLD's
material facts for that field's purpose. Differences in wording, phrasing, tone, formatting, ordering,
length, and ADDED detail are all fine and must be ignored — extra correct information in PRED never
makes it non-equivalent.

Mark `equivalent` false ONLY when PRED:
  - CONTRADICTS a material fact in GOLD (a wrong entity, name, number, identifier, status, cause,
    action, or outcome), OR
  - OMITS a material fact from GOLD that is essential to this field's purpose (so PRED would mislead
    a reader or fail the field's job without it).
A non-essential omission, a paraphrase, or merely saying less than GOLD while staying accurate is
still equivalent. When genuinely in doubt, prefer `equivalent` true.

Respond with a JSON object EXACTLY of the form:
{
  "results": [
    {"index": 0, "equivalent": true, "reasoning": "<short reasoning>"}
  ]
}
Return one entry per input pair, keyed by its index.
""".strip()


class FreeTextVerdict(BaseModel):
    """One free-text field's semantic-equivalence verdict from the LLM judge, WITH the judge's
    reasoning — so a caller can see WHY a prose field was (not) accepted, not just the boolean.
    ``key`` is the ``collection/record_id field`` locator the structural walk produced."""

    key: str
    field: str
    gold: Optional[str] = None
    pred: Optional[str] = None
    equivalent: bool
    reasoning: Optional[str] = None


def judge_free_text(
    pairs: list[dict],
    llm: str,
    llm_args: Optional[dict] = None,
) -> list[FreeTextVerdict]:
    """Batch-judge semantic equivalence of ``(field, gold, pred)`` pairs in one ``generate`` call.

    ``pairs`` items: ``{"key": str, "field": str, "gold": str, "pred": str}``. Returns one
    :class:`FreeTextVerdict` per pair (by position), carrying the judge's ``equivalent`` verdict and
    its ``reasoning``; unparseable / missing verdicts default to ``equivalent=False`` (conservative —
    a real failure is never silently passed) with a ``reasoning`` that names the parse failure, so a
    judge that returns junk is visibly distinguishable from a genuine non-equivalence.
    """
    if not pairs:
        return []
    llm_args = llm_args if llm_args is not None else dict(DEFAULT_LLM_NL_JUDGE_ARGS)

    payload = [
        {"index": i, "field": p["field"], "gold": p["gold"], "pred": p["pred"]}
        for i, p in enumerate(pairs)
    ]
    user_prompt = "pairs:\n" + json.dumps(payload, indent=2, default=str)
    response = generate(
        model=llm,
        messages=[
            SystemMessage(content=JUDGE_SYSTEM_PROMPT),
            UserMessage(content=user_prompt),
        ],
        **llm_args,
    )
    return _parse_judge_response(response.content, pairs)


def _as_text(value: object) -> Optional[str]:
    return None if value is None else str(value)


def _parse_judge_response(content: Optional[str], pairs: list[dict]) -> list[FreeTextVerdict]:
    def verdict(i: int, equivalent: bool, reasoning: Optional[str]) -> FreeTextVerdict:
        p = pairs[i]
        return FreeTextVerdict(
            key=str(p.get("key", p["field"])), field=str(p["field"]),
            gold=_as_text(p.get("gold")), pred=_as_text(p.get("pred")),
            equivalent=equivalent, reasoning=reasoning,
        )

    # Conservative default: every pair non-equivalent until the judge says otherwise.
    verdicts = [verdict(i, False, None) for i in range(len(pairs))]
    try:
        data = json.loads(_extract_json(content or ""))
        results = data["results"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"could not parse free-text judge response: {e}")
        for v in verdicts:
            v.reasoning = "unparseable judge output"
        return verdicts
    for r in results:
        try:
            idx = int(r["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(pairs):
            verdicts[idx] = verdict(idx, bool(r.get("equivalent", False)), r.get("reasoning"))
    return verdicts


def _extract_json(text: str) -> str:
    """Strip ``<think>`` blocks / markdown fences, then fall back to the outermost ``{...}`` span."""
    import re

    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text
