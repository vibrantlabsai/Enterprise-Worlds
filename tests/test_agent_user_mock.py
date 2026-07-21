"""Agent + user-simulator unit tests with a mocked LLM (offline, no real API call).

Exercises the real LLMAgent and UserSimulator (and the litellm response parsing in llm_utils)
without hitting a provider, so the conversational machinery is covered in CI.
"""

from __future__ import annotations

from types import SimpleNamespace

from enterprise_worlds.agent.llm_agent import LLMAgent
from enterprise_worlds.data_model.message import AssistantMessage, ToolCall, UserMessage
from enterprise_worlds.data_model.tasks import Identity, UserProfile, Scenario
from enterprise_worlds.user.base import STOP
from enterprise_worlds.user.user_simulator import UserSimulator
from enterprise_worlds.utils.llm_utils import generate


def _fake_completion(content, tool_calls=None):
    tcs = None
    if tool_calls:
        tcs = [
            SimpleNamespace(id=tc["id"], function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]))
            for tc in tool_calls
        ]
    message = SimpleNamespace(content=content, tool_calls=tcs)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_llm_utils_parses_tool_calls(mocker):
    mocker.patch(
        "litellm.completion",
        return_value=_fake_completion(
            "let me look that up",
            [{"id": "call_1", "name": "get_incident", "arguments": '{"incident_id": "INC_003"}'}],
        ),
    )
    msg = generate("gpt-4o", [UserMessage(content="status of INC_003?")], tools=[{"type": "function"}])
    assert isinstance(msg, AssistantMessage)
    assert msg.content == "let me look that up"
    assert msg.tool_calls[0].name == "get_incident"
    assert msg.tool_calls[0].arguments == {"incident_id": "INC_003"}


def test_llm_utils_plain_text(mocker):
    mocker.patch("litellm.completion", return_value=_fake_completion("all done"))
    msg = generate("gpt-4o", [UserMessage(content="hi")])
    assert msg.content == "all done"
    assert not msg.is_tool_call()


def test_llm_utils_strips_think_blocks(mocker):
    # reasoning models inline <think>…</think>; it must be stripped so it can't pollute
    # content or trip the user-sim stop token it merely mentions while reasoning.
    mocker.patch(
        "litellm.completion",
        return_value=_fake_completion("<think>I should end with ###STOP###</think>\nHi, my VPN is down."),
    )
    msg = generate("gpt-4o", [UserMessage(content="hi")])
    assert msg.content == "Hi, my VPN is down."
    assert "###STOP###" not in (msg.content or "")


def test_agent_init_state_and_turn(mocker):
    agent = LLMAgent(policy="DOMAIN POLICY", tool_schemas=[], llm="gpt-4o")
    state = agent.get_init_state()
    assert state.system_messages[0].role == "system"
    assert "DOMAIN POLICY" in state.system_messages[0].content

    mocker.patch(
        "enterprise_worlds.agent.llm_agent.generate",
        return_value=AssistantMessage(content=None, tool_calls=[ToolCall(name="get_incident", arguments={})]),
    )
    msg, state = agent.generate_next_message(UserMessage(content="help"), state)
    assert msg.is_tool_call() and msg.tool_calls[0].name == "get_incident"
    # the incoming user message and the assistant reply are both recorded in state
    assert any(m.role == "user" and m.content == "help" for m in state.messages)
    assert state.messages[-1] is msg


def _scenario():
    return Scenario(
        persona=UserProfile(
            identity=Identity(user_id="USER_003", first_name="Dana", last_name="Lee", email="dana@techcorp.com", role="agent"),
            personality="terse",
        ),
        task_description="reset my VPN",
    )


def test_user_sim_system_prompt_and_stop():
    us = UserSimulator(_scenario(), llm="gpt-4o-mini")
    state = us.get_init_state()
    sys = state.system_messages[0].content
    assert "Dana" in sys and "reset my VPN" in sys and STOP in sys
    assert UserSimulator.is_stop(UserMessage(content=f"thanks {STOP}"))
    assert not UserSimulator.is_stop(UserMessage(content="still here"))


def test_user_sim_generates_and_records_turn(mocker):
    us = UserSimulator(_scenario(), llm="gpt-4o-mini")
    state = us.get_init_state()
    mocker.patch("enterprise_worlds.user.user_simulator.generate", return_value=AssistantMessage(content="my VPN is down"))
    agent_msg = AssistantMessage(content="How can I help?")
    user_msg, state = us.generate_next_message(agent_msg, state)
    assert user_msg.role == "user" and user_msg.content == "my VPN is down"
    # both the agent message and the produced user message are recorded in state
    assert any(m.content == "How can I help?" for m in state.messages)
    assert any(m.content == "my VPN is down" for m in state.messages)
