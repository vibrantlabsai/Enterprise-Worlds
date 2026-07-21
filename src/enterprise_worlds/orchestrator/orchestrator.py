"""Minimal run loop: agent <-> user simulator <-> environment.

Enough to drive one task end-to-end and
produce a trajectory (plus the agent's tool calls) for the evaluator. The agent
is any object exposing ``generate_next_message(message, state) -> (AssistantMessage, state)``
and ``get_init_state()`` — a real litellm agent is item 1's concern.
"""

from typing import Protocol

from enterprise_worlds.data_model.message import AssistantMessage, Message, MultiToolMessage, ToolCall
from enterprise_worlds.environment.environment import Environment
from enterprise_worlds.user.base import STOP
from enterprise_worlds.user.user_simulator import UserSimulator

#: Fixed greeting the agent opens every conversation with (no LLM call).
DEFAULT_FIRST_AGENT_MESSAGE = "Hi! How can I help you today?"


def _is_agent_stop(message: Message) -> bool:
    """The agent can end the episode by emitting the STOP token (e.g. the gym `done()` action)."""
    content = getattr(message, "content", None)
    return bool(content and STOP in content)


class Agent(Protocol):
    def get_init_state(self): ...
    def generate_next_message(self, message, state) -> tuple[AssistantMessage, object]: ...


class RunResult:
    def __init__(self, trajectory: list[Message], agent_tool_calls: list[ToolCall], stopped: bool):
        self.trajectory = trajectory
        self.agent_tool_calls = agent_tool_calls
        self.stopped = stopped


class Orchestrator:
    def __init__(
        self,
        agent: Agent,
        user: UserSimulator,
        environment: Environment,
        max_steps: int = 20,
    ):
        self.agent = agent
        self.user = user
        self.environment = environment
        self.max_steps = max_steps

    def run(self) -> RunResult:
        trajectory: list[Message] = []
        agent_tool_calls: list[ToolCall] = []

        agent_state = self.agent.get_init_state()
        user_state = self.user.get_init_state()

        # The agent opens with a fixed greeting (no LLM call). This guarantees
        # the user simulator always has a non-system message to respond to — required by
        # providers like Anthropic, which reject a system-only message list.
        last_agent_msg: Message = AssistantMessage(content=DEFAULT_FIRST_AGENT_MESSAGE)
        trajectory.append(last_agent_msg)

        stopped = False
        steps = 0

        while steps < self.max_steps:
            steps += 1
            # User responds to the agent's last message; may end the conversation.
            user_msg, user_state = self.user.generate_next_message(last_agent_msg, user_state)
            trajectory.append(user_msg)
            if self.user.is_stop(user_msg):
                stopped = True
                break

            # Agent replies; drain any tool calls until it produces a text reply for the user.
            agent_msg, agent_state = self.agent.generate_next_message(user_msg, agent_state)
            trajectory.append(agent_msg)
            while agent_msg.is_tool_call() and steps < self.max_steps:
                steps += 1
                tool_msgs = []
                for tc in agent_msg.tool_calls or []:
                    agent_tool_calls.append(tc)
                    tm = self.environment.get_response(tc)
                    trajectory.append(tm)
                    tool_msgs.append(tm)
                # Hand the agent every tool result: a single ToolMessage for one call, or a
                # MultiToolMessage for parallel calls (so no result is dropped from its state).
                agent_input = tool_msgs[0] if len(tool_msgs) == 1 else MultiToolMessage(tool_messages=tool_msgs)
                agent_msg, agent_state = self.agent.generate_next_message(agent_input, agent_state)
                trajectory.append(agent_msg)
            last_agent_msg = agent_msg

            # The agent may end the conversation itself (e.g. the gym policy calls done()).
            if _is_agent_stop(agent_msg):
                stopped = True
                break

        return RunResult(trajectory=trajectory, agent_tool_calls=agent_tool_calls, stopped=stopped)
