"""Environment — wraps a domain toolkit/DB and dispatches tool calls.

Trimmed: no user-side DB, no solo mode, no env assertions. Just enough to run tool calls during a task and
to be reconstructed as the "gold" environment during evaluation.
"""

from typing import Optional

from enterprise_worlds.data_model.message import ToolCall, ToolMessage
from enterprise_worlds.environment.toolkit import ToolKitBase


class Environment:
    def __init__(self, domain_name: str, policy: str, tools: ToolKitBase):
        self.domain_name = domain_name
        self.policy = policy
        self.tools = tools

    def get_policy(self) -> str:
        return self.policy

    def get_tool_schemas(self, include: Optional[list[str]] = None) -> list[dict]:
        return self.tools.get_tool_schemas(include=include)

    def get_tool_output_schemas(self, include: Optional[list[str]] = None) -> list[dict]:
        return self.tools.get_tool_output_schemas(include=include)

    def make_tool_call(self, tool_name: str, **kwargs):
        """Execute a tool call directly (used for gold-action replay)."""
        return self.tools.use_tool(tool_name, **kwargs)

    def get_response(self, tool_call: ToolCall) -> ToolMessage:
        """Execute a ToolCall, returning a ToolMessage (used in the run loop)."""
        error = False
        try:
            result = self.make_tool_call(tool_call.name, **tool_call.arguments)
            content = _stringify(result)
        except Exception as e:  # noqa: BLE001 - surfaced back to the agent
            content = f"Error: {e}"
            error = True
        return ToolMessage(
            id=tool_call.id,
            content=content,
            requestor=tool_call.requestor,
            error=error,
        )

    def get_db_hash(self) -> str:
        return self.tools.get_db_hash()


def _stringify(result) -> Optional[str]:
    if result is None:
        return None
    if hasattr(result, "model_dump_json"):
        return result.model_dump_json()
    if isinstance(result, list):
        return "[" + ", ".join(_stringify(r) or "null" for r in result) + "]"
    return str(result)
