"""Task-gate tests (verifier v2, P6): broken gold must be caught, offline and at scoring time.

The motivating defect (QA-audited, shipped in production tasks): a gold ``update_incident``
passed ``work_notes`` — the tool's parameter is ``worknotes`` — so the gold write silently
never happened and every agent was graded against the untouched seed value.
"""

from __future__ import annotations

from enterprise_worlds.data_model.tasks import Action, Task
from enterprise_worlds.domains.itsm import environment as itsm_env
from enterprise_worlds.evaluator.evaluator_env import calculate_db_reward
from enterprise_worlds.evaluator.task_gate import validate_task, validate_tasks

BASE = {
    "id": "gate-test/task_001",
    "scenario": {
        "persona": {
            "identity": {
                "user_id": "USER_001", "first_name": "Test", "last_name": "Operator",
                "email": "test.operator@techcorp.com", "role": "admin",
            },
            "personality": "terse",
            "role_description": "IT operator",
            "known_info": {},
        },
        "task_description": "Update the incident worknotes.",
        "simulator_guidance": "If asked anything, confirm.",
    },
    "evaluation_criteria": {"actions": [], "nl_assertions": []},
}


def _task(actions: list[dict], guidance: str = "If asked anything, confirm.") -> Task:
    data = {**BASE, "scenario": {**BASE["scenario"], "simulator_guidance": guidance}}
    data["evaluation_criteria"] = {"actions": actions, "nl_assertions": []}
    return Task.model_validate(data)


def _env_ctor(db_delta=None):
    return itsm_env.get_environment(db_delta=db_delta, acting_user_id="USER_001")


def test_wrong_param_name_is_caught_by_signature_check():
    # The audited work_notes bug: right tool, wrong parameter name.
    task = _task([{"name": "update_incident", "arguments": {"incident_id": "INC_003", "work_notes": "x"}}])
    issues = validate_task(lambda db_delta=None: _env_ctor(db_delta), task)
    kinds = {i.kind for i in issues if i.severity == "error"}
    assert "gold-signature" in kinds
    assert any("work_notes" in i.detail for i in issues)


def test_unknown_tool_is_caught():
    task = _task([{"name": "frobnicate_incident", "arguments": {}}])
    issues = validate_task(lambda db_delta=None: _env_ctor(db_delta), task)
    assert any(i.kind == "gold-signature" and "unknown tool" in i.detail for i in issues)


def test_failing_gold_action_is_caught_by_replay():
    # Well-formed signature, but the referenced record doesn't exist -> replay error.
    task = _task([{"name": "update_incident", "arguments": {"incident_id": "INC_99999", "status": "in_progress"}}])
    issues = validate_task(lambda db_delta=None: _env_ctor(db_delta), task)
    assert any(i.kind == "gold-replay" for i in issues)


def test_empty_guidance_is_a_warning_not_error():
    task = _task([], guidance="")
    issues = validate_task(lambda db_delta=None: _env_ctor(db_delta), task)
    assert any(i.kind == "empty-guidance" and i.severity == "warning" for i in issues)
    assert not any(i.severity == "error" for i in issues)


def test_duplicate_ids_reported():
    t1, t2 = _task([]), _task([])
    report = validate_tasks([t1, t2], lambda t: (lambda db_delta=None: _env_ctor(db_delta)))
    assert any(i.kind == "duplicate-id" for i in report.errors)


def test_clean_task_passes_gate():
    task = _task([{"name": "update_incident", "arguments": {"incident_id": "INC_003", "status": "in_progress"}}])
    issues = validate_task(lambda db_delta=None: _env_ctor(db_delta), task)
    assert not [i for i in issues if i.severity == "error"], issues


def test_broken_gold_hard_fails_scoring():
    """P6a: calculate_db_reward refuses to score against a broken reference."""
    task = _task([{"name": "update_incident", "arguments": {"incident_id": "INC_99999", "status": "in_progress"}}])
    check = calculate_db_reward(_env_ctor, task, agent_tool_calls=[])
    assert not check.gold_replay_ok
    assert any("GOLD REPLAY BROKEN" in m for m in check.mismatches)
