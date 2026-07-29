"""Minimal litellm wrapper.

No caching / langfuse / fine-tune parsing — just turn our message objects into
a litellm ``completion`` call and return an ``AssistantMessage``.
"""

import json
import re
from typing import Optional

from enterprise_worlds.data_model.message import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
)


def to_litellm_messages(messages: list[Message]) -> list[dict]:
    """Convert our message objects to litellm/OpenAI dict format."""
    out: list[dict] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            out.append({"role": "system", "content": msg.content or ""})
        elif isinstance(msg, ToolMessage):
            out.append(
                {"role": "tool", "tool_call_id": msg.id, "content": msg.content or ""}
            )
        else:  # user / assistant
            content = msg.content or ""
            tool_calls = getattr(msg, "tool_calls", None)
            # Providers reject empty/whitespace content on a non-tool message (e.g. a reasoning
            # model that returned only a <think> block, which strips to ""). Some require a
            # non-whitespace char (pattern '\S'), so use a minimal visible placeholder.
            if not content.strip() and not tool_calls:
                content = "..."
            entry: dict = {"role": msg.role, "content": content}
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for i, tc in enumerate(msg.tool_calls)  # type: ignore[union-attr]
                ]
            out.append(entry)
    return out


def generate(
    model: str,
    messages: list[Message],
    tools: Optional[list[dict]] = None,
    **kwargs,
) -> AssistantMessage:
    """Call the LLM and return an AssistantMessage.

    Imports litellm lazily so the rest of the package (and the LLM-free tests)
    work without litellm or an API key installed.
    """
    import litellm

    completion_kwargs: dict = {"model": model, "messages": to_litellm_messages(messages)}
    completion_kwargs.update(kwargs)
    if tools:
        completion_kwargs["tools"] = tools

    # Opus 4.8 deprecates the sampling params (temperature/top_p) and the Anthropic API rejects
    # them; litellm's model map doesn't yet know to drop them for this id, so strip them here.
    if "opus-4-8" in model:
        completion_kwargs.pop("temperature", None)
        completion_kwargs.pop("top_p", None)

    response = litellm.completion(**completion_kwargs)
    choice = response.choices[0].message

    tool_calls = None
    if getattr(choice, "tool_calls", None):
        tool_calls = [
            ToolCall(
                id=tc.id or "",
                name=tc.function.name,
                arguments=_parse_args(tc.function.arguments),
            )
            for tc in choice.tool_calls
        ]

    return AssistantMessage(content=_strip_think(choice.content), tool_calls=tool_calls)


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: Optional[str]) -> Optional[str]:
    """Remove ``<think>...</think>`` reasoning blocks from a model's content.

    Reasoning models (e.g. Sarvam-M, DeepSeek-R1) emit chain-of-thought inline. Left in, it
    pollutes the trajectory and breaks substring checks like the user-simulator stop token
    (which would otherwise match a '###STOP###' the model merely mentions while reasoning).
    """
    if not text:
        return text
    return _THINK_RE.sub("", text).strip()


def _parse_args(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        return json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {}
