"""Gymnasium-compatible RL/training interface for Enterprise-Worlds.

Requires the optional ``gymnasium`` dependency: ``uv pip install -e ".[gym]"``.
"""

from enterprise_worlds.gym.gym_agent import (
    EW_ENV_ID,
    AgentGymEnv,
    GymAgent,
    parse_action_string,
    register_gym_agent,
)

__all__ = [
    "EW_ENV_ID",
    "AgentGymEnv",
    "GymAgent",
    "parse_action_string",
    "register_gym_agent",
]
