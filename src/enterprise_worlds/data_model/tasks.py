"""Task and evaluation-criteria models (items 6 + 7).

Trimmed to what items 4-7 need:
a user scenario (persona + task description), evaluation criteria (gold actions
+ NL assertions), and a per-task initial-state ``Delta`` (item 7).
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from enterprise_worlds.environment.delta import Delta


class Identity(BaseModel):
    """DB-projected persona identity — mirrors the seed ``users[user_id]`` row exactly."""

    user_id: str
    first_name: str
    last_name: str
    email: str
    role: str  # users.role enum: admin | manager | agent | reporter


class UserProfile(BaseModel):
    """The persona the user simulator role-plays (item 4 input)."""

    # DB-backed identity (who the operator is). ``identity.user_id`` doubles as the
    # authenticated caller for the tools (see ``Task.acting_user_id``).
    identity: Identity
    # Sim-only behavioral instruction (sampled preset), e.g. "impatient, terse".
    personality: str
    # Sim-only role/org flavor, e.g. "IT service manager, Infrastructure org".
    role_description: str = ""
    # Facts the user knows and can reveal when the agent asks (e.g. their ``user_id``, email,
    # an incident number). Free-form, so a task can carry whatever the persona would know.
    known_info: Dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        """Human-readable name, derived from the DB identity (kept for the sim prompt/CLI)."""
        return f"{self.identity.first_name} {self.identity.last_name}".strip()


class Scenario(BaseModel):
    """Everything passed to the user simulator."""

    persona: UserProfile
    task_description: str
    # Sim-only behavioral script (second person): how to open, progressive disclosure of known_info,
    # confirmation style, and how to respond to the agent's PROPOSE/ASK cascades — never shown to the agent.
    simulator_guidance: str = ""


class Action(BaseModel):
    """A gold tool call replayed to compute the expected DB state (item 6)."""

    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class EvaluationCriteria(BaseModel):
    """How a task is scored: gold actions (DB-hash) and NL assertions.

    The two criteria are combined multiplicatively, so a task passes only if every
    criterion it defines passes.
    """

    actions: List[Action] = Field(default_factory=list)
    nl_assertions: List[str] = Field(default_factory=list)


class Task(BaseModel):
    """A single benchmark task (items 6 + 7)."""

    id: str
    scenario: Scenario
    evaluation_criteria: EvaluationCriteria = Field(default_factory=EvaluationCriteria)
    # The task's tenancy scope: the set of orgs its data lives in. When set, the env is sliced
    # (FK-closed) to these orgs so numbers/names that collide *outside* the scope resolve
    # unambiguously. A 1-elt list is single-tenant; {provider, client} is the MSP cross-org case
    # (the acting operator may belong to a different org than the records). `org_id` is the legacy
    # single-org alias (a 1-elt scope); `org_ids` takes precedence when both are set.
    org_ids: Optional[List[str]] = None
    org_id: Optional[str] = None
    # item 7: collection -> record_id -> {set|create|delete}
    initial_state_delta: Optional[Delta] = None
    # Wall-clock "now" for this scenario (ISO 8601). Drives the env clock for the live run and the
    # gold replay; when None the gym's DEFAULT_NOW is used. Tool timestamps (created_on/updated_on,
    # and a new incident SLA's start_time) are stamped from it, so they are reproducible across runs.
    current_time: Optional[str] = None

    @property
    def acting_user_id(self) -> Optional[str]:
        """The authenticated caller for the run, taken from the persona's DB identity.

        Sets org scoping and the default attribution the tools use (requested_by on changes,
        opened_by on problems, sender email on notifications). When absent the tools fall back
        to the first admin / their own defaults.
        """
        return self.scenario.persona.identity.user_id
