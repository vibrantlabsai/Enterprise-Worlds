"""Gym shapes → contract shapes.

The contract splits what every gym has (transcripts, pass/fail) from what only this gym understands
(diagnostics). The transcript mapping here must be exact in one subtle way: the gym's message models
declare ``tool_calls: Optional[list[ToolCall]] = None`` and are serialised with pydantic's
``model_dump()`` WITHOUT ``exclude_none`` — so a plain text turn carries an explicit
``toolCalls: null``, while a tool-result message (whose model has no such field) omits the key
entirely. **null and absent are different, and the difference must survive**: the platform stores
transcripts as given, and collapsing null to absent silently rewrites every transcript at rest.
"""
from __future__ import annotations

from typing import Any, List

from enterprise_worlds.data_model.message import Message


def jsonable(value: Any) -> Any:
    """Coerce a tool result (often a pydantic model) into JSON-serialisable data."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


def to_transcript(trajectory: List[Message]) -> List[dict]:
    """The contract's ``TranscriptMessage[]`` for a gym trajectory.

    A rename, not a reshape: the gym spells tool calls ``tool_calls`` and the contract spells them
    ``toolCalls``; each call's ``{id, name, arguments, requestor}`` already matches
    ``TranscriptToolCall``. Everything else — ``content: null``, a tool message's ``id``/
    ``requestor``/``error``, and the null-vs-absent distinction above — passes through untouched.
    """
    out: List[dict] = []
    for msg in trajectory:
        dump = msg.model_dump()
        if "tool_calls" in dump:
            dump["toolCalls"] = dump.pop("tool_calls")
        out.append(dump)
    return out
