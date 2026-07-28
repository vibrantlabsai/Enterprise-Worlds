"""NL-judge unit tests (first direct coverage of ``evaluator_nl``).

Focus: state grounding — the judge prompt must carry the final DB for state claims (QA audits
measured the transcript-only judge crediting narrated-but-never-executed actions) — plus the
parse/fallback behavior.
"""

from __future__ import annotations

import json

from enterprise_worlds.data_model.message import AssistantMessage, UserMessage
import enterprise_worlds.evaluator.evaluator_nl as _nl
from enterprise_worlds.evaluator.evaluator_nl import (
    STATE_GROUNDING_RULES,
    _relevant_db_dump,
    evaluate_nl_assertions,
)
from enterprise_worlds.domains.itsm.environment import get_environment

TRAJ = [UserMessage(content="please pause the SLA"), AssistantMessage(content="Done — SLA paused.")]
ASSERTIONS = ["The incident INC_003 SLA is paused."]


def _capture_judge(monkeypatch, met: bool = True):
    """Stub the judge LLM; capture the prompts it was shown."""
    seen: dict = {}

    def fake_generate(model=None, messages=None, **kwargs):
        seen["system"] = messages[0].content
        seen["user"] = messages[-1].content
        results = [
            {"expectedOutcome": a, "reasoning": "stub", "metExpectation": met}
            for a in ASSERTIONS
        ]
        return AssistantMessage(content=json.dumps({"results": results}))

    monkeypatch.setattr(_nl, "generate", fake_generate)
    return seen


def test_prompt_grounds_state_in_final_db(monkeypatch):
    seen = _capture_judge(monkeypatch)
    db = get_environment().tools.db
    evaluate_nl_assertions(TRAJ, ASSERTIONS, llm="stub", final_db=db)
    assert "final_database_state" in seen["user"]
    assert "INC_003" in seen["user"]  # the referenced record made it into the dump
    assert "MUST be" in seen["system"]  # grounding rules appended


def test_without_db_falls_back_to_transcript_only(monkeypatch):
    seen = _capture_judge(monkeypatch)
    evaluate_nl_assertions(TRAJ, ASSERTIONS, llm="stub", final_db=None)
    assert "final_database_state" not in seen["user"]
    assert STATE_GROUNDING_RULES.strip() not in seen["system"]


def test_relevant_db_dump_filters_and_caps():
    db = get_environment().tools.db
    dump = _relevant_db_dump(db, ["The incident INC_003 SLA is paused."])
    data = json.loads(dump)
    assert "incident" in data and "INC_003" in data["incident"]
    # Unreferenced collections with no name hit are excluded.
    assert "knowledge" not in data
    assert len(dump) <= _nl._DB_DUMP_CAP


def test_unparseable_judge_marks_all_not_met(monkeypatch):
    monkeypatch.setattr(
        _nl, "generate", lambda **kw: AssistantMessage(content="the dog ate my JSON")
    )
    check = evaluate_nl_assertions(TRAJ, ASSERTIONS, llm="stub")
    assert check.reward == 0.0
    assert all(not c.met for c in check.checks)


def test_reward_requires_all_met(monkeypatch):
    _capture_judge(monkeypatch, met=True)
    assert evaluate_nl_assertions(TRAJ, ASSERTIONS, llm="stub").reward == 1.0
    _capture_judge(monkeypatch, met=False)
    assert evaluate_nl_assertions(TRAJ, ASSERTIONS, llm="stub").reward == 0.0
