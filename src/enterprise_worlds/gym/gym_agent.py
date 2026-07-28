"""Gymnasium-compatible RL/training interface for Enterprise-Worlds.

A thin wrapper over the existing eval stack that exposes the standard Gymnasium ``reset``/``step`` loop. The learner
policy plays the **agent**; a ``UserSimulator`` plays the user; reward is the (sparse, terminal)
evaluator score.

Mechanism: the real ``Orchestrator`` runs in a background daemon thread. A
controllable ``GymAgent`` is plugged in as the agent — when the orchestrator asks it to act, it
*blocks* until the env's ``step(action)`` supplies the next action (via a threading Event). So
``reset()`` advances the thread to the first agent turn, and each ``step()`` injects one action
and advances to the next agent turn (or episode end).

    import gymnasium as gym
    from enterprise_worlds.gym import register_gym_agent, EW_ENV_ID
    register_gym_agent()
    env = gym.make(EW_ENV_ID, domain="itsm", task_id="msp_band_iter_004")
    obs, info = env.reset()
    obs, reward, terminated, truncated, info = env.step('list_users(first_name="Aisha")')
    ...
    obs, reward, terminated, truncated, info = env.step("done()")   # ends the episode
"""

from __future__ import annotations

import ast
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional

import gymnasium as gym
from gymnasium.envs.registration import register
from loguru import logger

from enterprise_worlds.data_model.message import AssistantMessage, Message, ToolCall, render_trajectory
from enterprise_worlds.evaluator.evaluator import evaluate_task
from enterprise_worlds.orchestrator.orchestrator import Orchestrator
from enterprise_worlds.user.base import STOP
from enterprise_worlds.user.user_simulator import UserSimulator

EW_ENV_ID = "enterprise-worlds-v0"

#: A `done` tool descriptor advertised to the policy so it knows how to end the episode.
DONE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "done",
        "description": "Call this when the task is complete to end the episode.",
        "parameters": {"type": "object", "properties": {}},
    },
}


class TauSpace(gym.spaces.Space):
    """Degenerate string space: actions/observations are plain strings."""

    def sample(self, *args, **kwargs) -> str:
        raise NotImplementedError("Sampling is not supported for the Enterprise-Worlds gym (string space).")

    def contains(self, x: Any) -> bool:
        return isinstance(x, str)


# ---------------------------------------------------------------------------- action parsing
def _gen_call_id() -> str:
    return "gym_call"


def _parse_func_args(argstr: str) -> dict:
    """Parse `a='x', b=2` (keyword args) into a dict via the AST (no eval)."""
    argstr = argstr.strip()
    if not argstr:
        return {}
    call = ast.parse(f"_f({argstr})", mode="eval").body
    args: dict = {}
    for kw in getattr(call, "keywords", []):
        args[kw.arg] = ast.literal_eval(kw.value)
    return args


def parse_action_string(action: str) -> AssistantMessage:
    """Parse an action string into an AssistantMessage.

    Accepts: plain text, a JSON tool call ``{"name":..,"arguments":{..}}``, a functional tool
    call ``name(a='x', b=2)``, or ``done()`` / the STOP token (ends the episode).
    """
    s = (action or "").strip()
    if s in ("done", "done()") or STOP in s:
        return AssistantMessage(content=STOP)
    if s.startswith("{"):
        data = json.loads(s)
        if isinstance(data, dict) and "name" in data:
            if data["name"] == "done":
                return AssistantMessage(content=STOP)
            return AssistantMessage(tool_calls=[ToolCall(
                id=_gen_call_id(), name=data["name"], arguments=data.get("arguments", {}))])
        raise ValueError("JSON action must be a tool call with a 'name' field")
    m = re.match(r"^([A-Za-z_]\w*)\s*\((.*)\)$", s, re.S)
    if m:
        name, argstr = m.group(1), m.group(2)
        if name == "done":
            return AssistantMessage(content=STOP)
        return AssistantMessage(tool_calls=[ToolCall(
            id=_gen_call_id(), name=name, arguments=_parse_func_args(argstr))])
    return AssistantMessage(content=action)  # plain text message to the user


# ---------------------------------------------------------------------------- controllable agent
@dataclass
class GymAgentState:
    messages: list = field(default_factory=list)


class GymAgent:
    """An Orchestrator-compatible agent whose turns are supplied externally via ``set_action``.

    Synchronises with the gym env through a single Event (``_turn``): cleared = it is the agent's
    turn and ``generate_next_message`` is blocked waiting for an action; set = an action has been
    provided / not the agent's turn.
    """

    def __init__(self, tool_schemas: List[dict], policy: str):
        self.tool_schemas = tool_schemas
        self.policy = policy
        self._observation: Optional[list[Message]] = None
        self._next_action: Optional[AssistantMessage] = None
        self._lock = threading.Lock()
        self._turn = threading.Event()
        self._turn.set()  # not the agent's turn until generate_next_message is entered

    def get_init_state(self) -> GymAgentState:
        return GymAgentState()

    @property
    def observation(self) -> list[Message]:
        return self._observation or []

    @property
    def is_agent_turn(self) -> bool:
        return not self._turn.is_set()

    def set_action(self, action_msg: AssistantMessage) -> None:
        with self._lock:
            if self._turn.is_set():
                raise RuntimeError("It is not the agent's turn to act.")
            self._next_action = action_msg
            self._turn.set()

    def generate_next_message(self, message, state: GymAgentState):
        with self._lock:
            self._turn.clear()  # it is now the agent's turn; we will block for the action
            if message is not None:
                state.messages.append(message)
            self._observation = list(state.messages)
        self._turn.wait()  # block until set_action() provides the next action
        with self._lock:
            resp = self._next_action
            self._next_action = None
        state.messages.append(resp)
        return resp, state


# ---------------------------------------------------------------------------- the gym env
class AgentGymEnv(gym.Env):
    """Play as the agent against the user simulator (Gymnasium ``reset``/``step``)."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        domain: str,
        task_id: str,
        max_steps: int = 100,
        user_llm: Optional[str] = None,
        user_llm_args: Optional[dict] = None,
    ):
        from enterprise_worlds.config import DEFAULT_LLM_USER, DEFAULT_LLM_USER_ARGS
        from enterprise_worlds.run import get_domain

        self.domain = domain
        self.task_id = task_id
        self.max_steps = max_steps
        self.user_llm = user_llm or DEFAULT_LLM_USER
        self.user_llm_args = user_llm_args if user_llm_args is not None else dict(DEFAULT_LLM_USER_ARGS)

        self._spec = get_domain(domain)
        tasks = {t.id: t for t in self._spec.get_tasks()}
        if task_id not in tasks:
            raise ValueError(f"Unknown task_id {task_id!r} in domain {domain!r}.")
        self._task = tasks[task_id]

        self._lock = threading.Lock()
        self._agent: Optional[GymAgent] = None
        self._env = None
        self._orchestrator: Optional[Orchestrator] = None
        self._thread: Optional[threading.Thread] = None
        self._done = threading.Event()
        self._run_result = None
        self.observation_space = TauSpace()
        self.action_space = TauSpace()

    # -- construction --------------------------------------------------------
    def _env_ctor(self, db_delta=None):
        return self._spec.get_environment(
            db_delta=db_delta, acting_user_id=self._task.acting_user_id,
            org_id=self._task.org_id, org_ids=self._task.org_ids,
            seed_db=self._task.seed_db,
        )

    def _build(self):
        env = self._env_ctor(db_delta=self._task.initial_state_delta)
        tool_schemas = env.get_tool_schemas()
        agent = GymAgent(tool_schemas, env.get_policy())
        user = UserSimulator(self._task.scenario, llm=self.user_llm, llm_args=self.user_llm_args)
        orchestrator = Orchestrator(agent, user, env, max_steps=self.max_steps)
        return env, agent, orchestrator

    def _run_orchestrator(self):
        result = None
        try:
            result = self._orchestrator.run()
        except Exception as e:  # noqa: BLE001 - surfaced to the main thread via _done
            logger.error(f"[{self.task_id}] orchestrator error: {e}")
        finally:
            self._run_result = result
            self._done.set()

    def _wait_for_agent_turn(self):
        while not self._done.is_set() and not self._agent.is_agent_turn:
            self._done.wait(timeout=0.01)

    # -- Gymnasium API -------------------------------------------------------
    def reset(self, seed: Optional[int] = None, options: Optional[dict] = None):
        super().reset(seed=seed)
        with self._lock:
            self._run_result = None
            self._done.clear()
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=1.0)
            self._env, self._agent, self._orchestrator = self._build()
            self._thread = threading.Thread(target=self._run_orchestrator, daemon=True)
            self._thread.start()
            self._wait_for_agent_turn()
            obs = "" if self._done.is_set() else render_trajectory(self._agent.observation)
            return obs, self._get_info()

    def step(self, action: str):
        if self._orchestrator is None:
            raise RuntimeError("Call reset() before step().")
        with self._lock:
            if self._done.is_set():
                return "", 0.0, True, False, self._get_info()
            try:
                action_msg = parse_action_string(action)
            except Exception as e:  # noqa: BLE001 - invalid action is recoverable, not terminal
                return f"Invalid action: {e}", 0.0, False, False, self._get_info()
            self._agent.set_action(action_msg)
            self._wait_for_agent_turn()
            terminated = self._done.is_set()
            obs = render_trajectory(self._agent.observation)
            reward, reward_info = self._get_reward()
            info = self._get_info()
            info["reward_info"] = reward_info
            return obs, reward, terminated, False, info

    def render(self):
        return render_trajectory(self._agent.observation) if self._agent else ""

    def close(self):
        if self._thread is not None and self._thread.is_alive():
            self._done.set()
            self._thread.join(timeout=1.0)

    # -- helpers -------------------------------------------------------------
    def _get_reward(self) -> tuple[float, Optional[dict]]:
        if self._run_result is None:
            return 0.0, None
        ri = evaluate_task(
            self._env_ctor, self._task, trajectory=self._run_result.trajectory,
            final_env=self._env, skip_nl_assertions=True,
        )
        return ri.reward, ri.model_dump()

    def _get_info(self) -> dict:
        tools = (self._agent.tool_schemas if self._agent else []) + [DONE_TOOL_SCHEMA]
        return {
            "task_id": self._task.id,
            "tools": tools,
            "policy": self._agent.policy if self._agent else "",
        }


def register_gym_agent() -> None:
    """Register the Enterprise-Worlds gym environment with Gymnasium (call once before ``gym.make``)."""
    register(id=EW_ENV_ID, entry_point="enterprise_worlds.gym.gym_agent:AgentGymEnv")
