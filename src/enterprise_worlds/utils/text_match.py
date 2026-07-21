"""Fuzzy matching for free-text fields during DB-match.

An LLM agent never reproduces prose (a notification subject, an incident worknote) verbatim, so
comparing such columns for exact equality is wrong. Instead we require the gold's content to be
*covered* by the agent's text — deterministic, stdlib-only, no embeddings.
"""

import re

#: Default overlap threshold: at least half of the gold's content tokens must appear in the
#: agent's text. Tuned for "agent is usually more verbose than the gold".
DB_FUZZY_THRESHOLD = 0.5

# Tiny stop-word set so short, mostly-boilerplate gold strings still discriminate on content.
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the to was were will "
    "with your you we our us this".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _content_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def text_overlap(gold: str, pred: str) -> float:
    """Recall of gold's content tokens in ``pred`` (0..1). 1.0 if gold has no content tokens."""
    gold_tokens = _content_tokens(gold)
    if not gold_tokens:
        return 1.0
    pred_tokens = _content_tokens(pred)
    return len(gold_tokens & pred_tokens) / len(gold_tokens)


def fuzzy_text_match(
    gold: str | None, pred: str | None, threshold: float = DB_FUZZY_THRESHOLD
) -> bool:
    """Does ``pred`` cover enough of ``gold``'s content to count as a match?

    - gold empty/None  -> match (the gold imposes no text requirement)
    - pred empty/None while gold has content -> no match (agent omitted the field)
    - otherwise        -> ``text_overlap(gold, pred) >= threshold``
    """
    gold_has = bool(gold and gold.strip())
    pred_has = bool(pred and pred.strip())
    if not gold_has:
        return True
    if not pred_has:
        return False
    return text_overlap(gold, pred) >= threshold  # type: ignore[arg-type]
