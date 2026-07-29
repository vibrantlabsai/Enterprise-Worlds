"""Conversation message types."""

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]
ToolRequestor = Literal["user", "assistant"]


class ToolCall(BaseModel):
    """A single tool call emitted by the agent (or user)."""

    id: str = Field(default="", description="Unique identifier for the tool call.")
    name: str = Field(description="The name of the tool.")
    arguments: dict = Field(description="The arguments of the tool.")
    requestor: ToolRequestor = Field(
        default="assistant", description="Who requested the tool call."
    )


class SystemMessage(BaseModel):
    role: Literal["system"] = "system"
    content: Optional[str] = None


class _ParticipantMessage(BaseModel):
    """Common base for user/assistant messages."""

    role: Role
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None

    def has_text_content(self) -> bool:
        return bool(self.content and self.content.strip())

    def is_tool_call(self) -> bool:
        return self.tool_calls is not None and len(self.tool_calls) > 0


class AssistantMessage(_ParticipantMessage):
    role: Literal["assistant"] = "assistant"


class UserMessage(_ParticipantMessage):
    role: Literal["user"] = "user"


class ToolMessage(BaseModel):
    """The response from executing a tool call."""

    id: str = Field(default="", description="Id of the tool call this responds to.")
    role: Literal["tool"] = "tool"
    content: Optional[str] = None
    requestor: ToolRequestor = "assistant"
    error: bool = False


Message = SystemMessage | AssistantMessage | UserMessage | ToolMessage


class MultiToolMessage(BaseModel):
    """A batch of tool responses handed to the agent in one turn.

    Transient agent-input wrapper for an assistant message that made several tool calls: the
    agent expands it into the individual ToolMessages in its state, so it never reaches the LLM
    or the rendered trajectory. Deliberately kept out of the ``Message`` union for that reason.
    """

    role: Literal["tool"] = "tool"
    tool_messages: list[ToolMessage] = Field(default_factory=list)


def render_trajectory(trajectory: list[Message]) -> str:
    """Render a trajectory as plain text for the NL-assertion judge."""
    lines: list[str] = []
    for msg in trajectory:
        if isinstance(msg, ToolMessage):
            lines.append(f"tool: {msg.content}")
        elif isinstance(msg, (UserMessage, AssistantMessage)):
            if msg.is_tool_call():
                for tc in msg.tool_calls or []:
                    args = json.dumps(tc.arguments)
                    lines.append(f"{msg.role} (tool call): {tc.name}({args})")
            if msg.content:
                lines.append(f"{msg.role}: {msg.content}")
        elif isinstance(msg, SystemMessage) and msg.content:
            lines.append(f"system: {msg.content}")
    return "\n".join(lines)
