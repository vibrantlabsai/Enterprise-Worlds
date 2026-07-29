"""User-simulator base types."""

from pydantic import BaseModel, Field

from enterprise_worlds.data_model.message import (
    AssistantMessage,
    Message,
    SystemMessage,
    UserMessage,
)

#: Token the user emits when the conversation is complete.
STOP = "###STOP###"


class UserState(BaseModel):
    """Conversation state held by the user simulator."""

    system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)

    def flip_roles(self) -> list[Message]:
        """Return ``messages`` with user/assistant roles swapped.

        The simulator is an LLM producing the *user* turn, so from its point of
        view the agent is the "user" and itself is the "assistant".
        """
        flipped: list[Message] = []
        for msg in self.messages:
            if isinstance(msg, AssistantMessage):
                flipped.append(UserMessage(content=msg.content))
            elif isinstance(msg, UserMessage):
                flipped.append(AssistantMessage(content=msg.content))
            else:
                flipped.append(msg)
        return flipped
