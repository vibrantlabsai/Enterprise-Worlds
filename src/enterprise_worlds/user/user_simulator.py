"""User simulator (item 4).

A stateless LLM that role-plays a human user from a (persona, task_description)
scenario.
"""

from typing import Optional

from enterprise_worlds.config import DEFAULT_LLM_USER, DEFAULT_LLM_USER_ARGS
from enterprise_worlds.data_model.message import AssistantMessage, SystemMessage, UserMessage
from enterprise_worlds.data_model.tasks import Scenario
from enterprise_worlds.user.base import STOP, UserState
from enterprise_worlds.utils.llm_utils import generate

SYSTEM_PROMPT = """
You are role-playing a human user contacting an IT support agent. Stay fully in
character as the person described below. You are the USER, not the agent — never
call tools, never act as support staff.

Guidelines:
- Reveal information progressively: share details only when the agent asks for them.
- Do not invent facts (ids, emails, numbers). Only use the facts in <known_info> below;
  if asked for something not listed there, say you don't have it handy.
- Keep messages short and natural, the way a real person would talk.
- When your goal has been accomplished (or the agent makes clear it cannot be),
  end the conversation by replying with exactly: {stop_token}

<persona>
Name: {name}
Role: {role_description}
Personality: {personality}
</persona>

<known_info>
Facts you know and can share when the agent asks for them (don't volunteer them all at once):
{known_info}
</known_info>

<scenario>
{task_description}
</scenario>

<how_to_behave>
{simulator_guidance}
</how_to_behave>
""".strip()


class UserSimulator:
    def __init__(
        self,
        scenario: Scenario,
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
    ):
        self.scenario = scenario
        self.llm = llm or DEFAULT_LLM_USER
        self.llm_args = llm_args if llm_args is not None else dict(DEFAULT_LLM_USER_ARGS)

    @property
    def system_prompt(self) -> str:
        known = self.scenario.persona.known_info
        known_info = "\n".join(f"- {k}: {v}" for k, v in known.items()) or "- (nothing in particular)"
        return SYSTEM_PROMPT.format(
            stop_token=STOP,
            name=self.scenario.persona.name,
            role_description=self.scenario.persona.role_description or "(unspecified)",
            personality=self.scenario.persona.personality,
            known_info=known_info,
            task_description=self.scenario.task_description,
            simulator_guidance=self.scenario.simulator_guidance or "(behave naturally in character)",
        )

    def get_init_state(self) -> UserState:
        return UserState(system_messages=[SystemMessage(content=self.system_prompt)])

    @staticmethod
    def is_stop(message: UserMessage) -> bool:
        return STOP in (message.content or "")

    def generate_next_message(
        self, agent_message: Optional[AssistantMessage], state: UserState
    ) -> tuple[UserMessage, UserState]:
        """Produce the next user turn in response to the agent's message."""
        if agent_message is not None:
            state.messages.append(agent_message)
        # Roles are flipped: the simulator sees the agent as the "user".
        llm_messages = list(state.system_messages) + state.flip_roles()
        response = generate(model=self.llm, messages=llm_messages, **self.llm_args)
        user_message = UserMessage(content=response.content)
        state.messages.append(user_message)
        return user_message, state
