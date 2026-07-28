"""Framework unit tests (offline, no LLM / no oracle).

Covers the domain-agnostic machinery the whole benchmark rests on: typed tool-schema
generation, the deterministic clock, delta application + its error paths, DB hashing,
message<->litellm conversion, and the evaluator's multiplicative reward combination.
"""

from __future__ import annotations

import json
from typing import List, Literal, Optional

import pytest

from enterprise_worlds.data_model.message import (
    AssistantMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from enterprise_worlds.environment.tool import build_tool_schema
from enterprise_worlds.environment.toolkit import ToolKitBase, ToolType, is_tool
from enterprise_worlds.utils.clock import DEFAULT_NOW, get_now, reset_now, set_now
from enterprise_worlds.utils.hash_utils import get_dict_hash, get_pydantic_hash
from enterprise_worlds.utils.llm_utils import _parse_args, to_litellm_messages


# --------------------------------------------------------------------- schema gen
class _DemoKit(ToolKitBase):
    @is_tool(ToolType.WRITE)
    def make_thing(
        self,
        name: str,
        level: Literal["low", "high"] = "low",
        tags: Optional[List[str]] = None,
        count: int = 0,
        enabled: bool = False,
    ) -> str:
        """Make a thing.

        Args:
            name: The thing's name.
            level: Severity level.
            tags: Optional tags.
            count: How many.
            enabled: Whether enabled.

        Returns:
            The id.
        """
        return "ok"


def test_tool_schema_types_and_required():
    fn = build_tool_schema(_DemoKit().make_thing)["function"]
    props = fn["parameters"]["properties"]
    assert fn["name"] == "make_thing"
    assert fn["description"] == "Make a thing."
    assert props["name"] == {"type": "string", "description": "The thing's name."}
    assert props["level"]["enum"] == ["low", "high"]
    assert props["tags"] == {"type": "array", "items": {"type": "string"}, "description": "Optional tags."}
    assert props["count"]["type"] == "integer"
    assert props["enabled"]["type"] == "boolean"
    # only the param without a default / not Optional is required
    assert fn["parameters"]["required"] == ["name"]


def test_toolkit_collects_and_filters_tools():
    kit = _DemoKit()
    assert kit.has_tool("make_thing")
    assert [s["function"]["name"] for s in kit.get_tool_schemas()] == ["make_thing"]
    assert kit.get_tool_schemas(include=["nonexistent"]) == []


# ------------------------------------------------------------------------- clock
def test_clock_default_set_reset():
    reset_now()
    assert get_now() == DEFAULT_NOW
    # An ISO "T"-separated current_time is canonicalised to the seed format ("YYYY-MM-DD HH:MM:SS")
    # so freshly stamped timestamps are byte-identical to seed rows.
    set_now("2030-01-02T03:04:05")
    assert get_now() == "2030-01-02 03:04:05"
    reset_now()
    assert get_now() == DEFAULT_NOW


# ------------------------------------------------------------------------- delta
def _small_db():
    from enterprise_worlds.domains.itsm.environment import get_environment
    return get_environment().tools.db


def test_delta_set_create_delete():
    from enterprise_worlds.environment.delta import apply_delta
    db = _small_db()
    before = db.get_hash()
    out = apply_delta(db, {"incident": {"INC_003": {"set": {"priority": "critical"}}}})
    assert out.incident["INC_003"].priority == "critical"
    assert db.incident["INC_003"].priority != "critical", "input DB must not be mutated"
    assert out.get_hash() != before
    # create a fresh, dependency-free incident (its payload must satisfy the FK + enum +
    # id-format validators that now run on every apply_delta)
    new_incident = {
        "incident_id": "INC_900", "number": "INC0000900", "short_description": "delta test",
        "caller_id": "USER_001", "org_id": "ORG_001", "status": "new",
        "category": "inquiry-help", "impact": "low", "urgency": "low", "priority": "low",
    }
    out2 = apply_delta(db, {"incident": {"INC_900": {"create": new_incident}}})
    assert "INC_900" in out2.incident
    # delete it again (deleting INC_003 instead would now fail — incident_sla TSLA_004 references it)
    out3 = apply_delta(out2, {"incident": {"INC_900": {"delete": True}}})
    assert "INC_900" not in out3.incident


def test_delta_error_paths():
    from enterprise_worlds.environment.delta import apply_delta
    db = _small_db()
    with pytest.raises(ValueError):  # set on a missing record
        apply_delta(db, {"incident": {"NOPE_999": {"set": {"priority": "low"}}}})
    with pytest.raises(ValueError):  # create on an existing record
        apply_delta(db, {"incident": {"INC_003": {"create": {"x": 1}}}})
    with pytest.raises(ValueError):  # unknown collection
        apply_delta(db, {"not_a_table": {"X": {"set": {"a": 1}}}})
    with pytest.raises(ValueError):  # EntityOp must be exactly one verb
        apply_delta(db, {"incident": {"INC_003": {"set": {"a": 1}, "delete": True}}})


# -------------------------------------------------------------------------- hash
def test_dict_hash_is_order_independent_and_pydantic_hash_matches():
    assert get_dict_hash({"a": 1, "b": 2}) == get_dict_hash({"b": 2, "a": 1})
    assert get_dict_hash({"a": 1}) != get_dict_hash({"a": 2})
    db = _small_db()
    assert db.get_hash() == get_pydantic_hash(db)
    # stable across reload, changes on mutation
    db2 = _small_db()
    assert db.get_hash() == db2.get_hash()
    db2.incident["INC_003"].priority = "critical"
    assert db2.get_hash() != db.get_hash()


# ----------------------------------------------------------------- message conv
def test_to_litellm_messages_shapes():
    msgs = [
        SystemMessage(content="sys"),
        UserMessage(content="hi"),
        AssistantMessage(content=None, tool_calls=[ToolCall(id="c1", name="t", arguments={"x": 1})]),
        ToolMessage(id="c1", content="result"),
    ]
    out = to_litellm_messages(msgs)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "hi"}
    tc = out[2]["tool_calls"][0]
    assert tc["function"]["name"] == "t"
    assert json.loads(tc["function"]["arguments"]) == {"x": 1}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "result"}


def test_parse_args():
    assert _parse_args('{"a": 1}') == {"a": 1}
    assert _parse_args({"a": 1}) == {"a": 1}
    assert _parse_args("not json") == {}


def test_to_litellm_messages_never_emits_empty_content():
    # empty/whitespace non-tool messages get a non-whitespace placeholder (providers require \S);
    # assistant messages WITH tool_calls may keep empty content.
    out = to_litellm_messages([
        UserMessage(content=""),                 # reasoning model stripped to nothing
        UserMessage(content="   "),
        AssistantMessage(content=None, tool_calls=[ToolCall(id="c", name="t", arguments={})]),
    ])
    assert out[0]["content"].strip() != ""
    assert out[1]["content"].strip() != ""
    assert out[2]["content"] == "" and out[2]["tool_calls"]  # tool message may have empty content


# --------------------------------------------------------------------- evaluator
def test_nl_judge_parses_think_wrapped_and_fenced_json():
    """Reasoning models wrap JSON in <think> blocks / code fences — the judge must still parse."""
    from enterprise_worlds.evaluator.evaluator_nl import _parse_judge_response
    content = (
        "<think>let me grade each one carefully</think>\n"
        '```json\n{"results": [{"expectedOutcome": "a", "metExpectation": true},'
        ' {"expectedOutcome": "b", "metExpectation": false}]}\n```'
    )
    checks = _parse_judge_response(content, ["a", "b"])
    assert [c.met for c in checks] == [True, False]


def test_evaluator_multiplicative_reward(mocker):
    """DB pass x NL fail => 0; DB pass x NL pass => 1 (real combination path, mocked judge)."""
    from enterprise_worlds.data_model.message import ToolCall as TC
    from enterprise_worlds.domains.itsm import environment as itsm_env
    from enterprise_worlds.evaluator.evaluator import evaluate_task

    task = itsm_env.get_tasks()[0]

    def ctor(db_delta=None):
        return itsm_env.get_environment(
            db_delta=db_delta, acting_user_id=task.acting_user_id,
            org_id=task.org_id, org_ids=task.org_ids, seed_db=task.seed_db,
        )

    final_env = ctor(db_delta=task.initial_state_delta)
    for a in task.evaluation_criteria.actions:  # produce the gold DB state
        final_env.make_tool_call(a.name, **a.arguments)

    def judge(met: bool):
        results = [{"expectedOutcome": a, "metExpectation": met}
                   for a in task.evaluation_criteria.nl_assertions]
        return AssistantMessage(content=json.dumps({"results": results}))

    # reward = DB-match x NL.  NL all-met -> reward 1.0
    mocker.patch("enterprise_worlds.evaluator.evaluator_nl.generate", return_value=judge(True))
    r = evaluate_task(ctor, task, trajectory=[], final_env=final_env)
    assert r.db_check.db_match and r.reward == 1.0

    # NL not-met -> DB(1) * NL(0) = 0
    mocker.patch("enterprise_worlds.evaluator.evaluator_nl.generate", return_value=judge(False))
    r = evaluate_task(ctor, task, trajectory=[], final_env=final_env)
    assert r.db_check.db_match and r.reward == 0.0
