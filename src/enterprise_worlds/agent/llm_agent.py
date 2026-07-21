"""litellm tool-calling agent.

The ``LLMAgent`` class, using Enterprise-Worlds' in-process conventions: tools are passed as OpenAI schema
dicts, and the litellm call goes
through ``utils.llm_utils.generate``. It follows the domain policy, makes tool calls, threads
the full conversation each turn, and produces a closing summary.
"""

from copy import deepcopy
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field

from enterprise_worlds.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from enterprise_worlds.utils.llm_utils import generate

#: Combines an instruction/policy structure (one-action-per-turn, valid JSON) with Enterprise-Worlds'
#: domain-specific hints (use ids not display names, close with a summary).
AGENT_INSTRUCTION = """
You are an enterprise-operations support agent. Help the user according to the <policy> below,
using the available tools.

In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

Guidance:
- Make one tool call at a time and use the tool results to decide the next step.
- Use exact record identifiers returned by the tools (e.g. user/group ids), not display names.
- When the user's request is fully handled, reply with a short summary of what you changed.

Always follow the policy. Always make sure you generate valid JSON only.
""".strip()

SYSTEM_PROMPT = """
<instructions>
{agent_instruction}
</instructions>
<policy>
{domain_policy}
</policy>
""".strip()


class LLMAgentState(BaseModel):
    """The state of the agent: a constant system prompt plus a growing conversation."""

    system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)


def is_valid_agent_history_message(message: Message) -> bool:
    """A message is a valid agent-history entry if it could have been seen by the agent:
    an assistant message, a non-tool-call user message, or a tool response to the assistant."""
    return (
        isinstance(message, AssistantMessage)
        or (isinstance(message, UserMessage) and not message.is_tool_call())
        or (isinstance(message, ToolMessage) and message.requestor == "assistant")
    )


class LLMAgent:
    def __init__(
        self,
        policy: str,
        tool_schemas: list[dict],
        llm: str,
        llm_args: Optional[dict] = None,
    ):
        self.domain_policy = policy
        self.tool_schemas = tool_schemas
        self.llm = llm
        # Default to deterministic decoding; deepcopy so per-run mutation (e.g. set_seed) can't
        # leak into a caller-shared dict.
        self.llm_args = deepcopy(llm_args) if llm_args is not None else {"temperature": 0.0}

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            agent_instruction=AGENT_INSTRUCTION, domain_policy=self.domain_policy
        )

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> LLMAgentState:
        if message_history is None:
            message_history = []
        assert all(is_valid_agent_history_message(m) for m in message_history), (
            "Message history must contain only AssistantMessage, non-tool-call UserMessage, "
            "or assistant ToolMessage."
        )
        return LLMAgentState(
            system_messages=[SystemMessage(content=self.system_prompt)],
            messages=message_history,
        )

    def generate_next_message(self, message, state: LLMAgentState):
        # A MultiToolMessage carries the responses to several parallel tool calls; expand it so
        # every tool result is threaded into the history (a partial set would be malformed).
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)
        resp = generate(
            model=self.llm,
            messages=state.system_messages + state.messages,
            tools=self.tool_schemas,
            **self.llm_args,
        )
        state.messages.append(resp)
        return resp, state

    def set_seed(self, seed: int):
        """Set the decoding seed for reproducible LLM outputs."""
        if self.llm is None:
            raise ValueError("LLM is not set")
        if self.llm_args.get("seed") is not None:
            logger.warning(f"Seed is already set to {self.llm_args['seed']}, resetting it to {seed}")
        self.llm_args["seed"] = seed
