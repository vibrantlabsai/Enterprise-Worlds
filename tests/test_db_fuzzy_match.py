"""Free-text DB-match: structured fields exact, prose columns per the configured strategy
(exact | fuzzy | llm). Structural compare stays exact regardless of strategy."""

from __future__ import annotations

import json

from enterprise_worlds.data_model.message import AssistantMessage
from enterprise_worlds.domains.itsm.data_model import Notification
from enterprise_worlds.domains.itsm.environment import get_environment
import enterprise_worlds.evaluator.text_match_strategy as _tms
from enterprise_worlds.evaluator.evaluator_env import compare_dbs
from enterprise_worlds.evaluator.text_match_strategy import TextMatchConfig
from enterprise_worlds.utils.text_match import fuzzy_text_match, text_overlap


def test_fuzzy_text_match():
    gold = "Update on INC0000003 (Printer connectivity issue)"
    verbose = "Update on your incident INC0000003 — printer connectivity work has resumed"
    assert text_overlap(gold, verbose) >= 0.5
    assert fuzzy_text_match(gold, verbose)                 # agent superset of gold
    assert fuzzy_text_match(None, None)                    # both empty -> no requirement
    assert fuzzy_text_match("", "anything")                # empty gold -> match
    assert not fuzzy_text_match(gold, None)                # gold has content, pred empty
    assert not fuzzy_text_match(gold, "vacation request approved")  # unrelated


def _db_with_notification(**overrides):
    db = get_environment().tools.db.model_copy(deep=True)
    fields = dict(
        notification_id="NOTIF_900", incident_id="INC_003", org_id="ORG_001",
        email="carlos.rodriguez@techcorp.com", type="update", status="queued",
        subject="Work resumed on INC0000003", message="the replacement part arrived",
        created_on="2024-06-01T00:00:00", updated_on="2024-06-01T00:00:00",
    )
    fields.update(overrides)
    db.notification["NOTIF_900"] = Notification(**fields)
    return db


def test_compare_dbs_freetext_is_fuzzy():
    gold = _db_with_notification()
    # Different prose, same meaning -> still matches.
    paraphrase = _db_with_notification(
        subject="Update: work on INC0000003 has resumed now",
        message="the part we were waiting on has arrived",
    )
    matched, mismatches = compare_dbs(gold, paraphrase)
    assert matched, mismatches


def test_compare_dbs_structured_is_exact():
    gold = _db_with_notification()
    for over in ({"email": "aisha.williams@techcorp.com"}, {"type": "alert"}, {"status": "sent"}):
        matched, mismatches = compare_dbs(gold, _db_with_notification(**over))
        assert not matched, f"{over} should not match"
        assert any("NOTIF_900" in m for m in mismatches)


def test_compare_dbs_unrelated_freetext_fails():
    gold = _db_with_notification()
    matched, _ = compare_dbs(gold, _db_with_notification(subject="vacation request approved"))
    assert not matched


def test_compare_dbs_extra_or_missing_row_fails():
    gold = _db_with_notification()
    base = get_environment().tools.db                 # no NOTIF_900
    matched, mismatches = compare_dbs(gold, base)
    assert not matched and any("missing" in m for m in mismatches)


def _stub_judge(monkeypatch, equivalent: bool):
    """Patch the batched judge's ``generate`` to mark every pair ``equivalent``."""
    def fake_generate(model=None, messages=None, **kwargs):
        # Echo back a verdict per pair the judge was asked about.
        pairs = json.loads((messages[-1].content or "").split("pairs:\n", 1)[1])
        results = [{"index": p["index"], "equivalent": equivalent} for p in pairs]
        return AssistantMessage(content=json.dumps({"results": results}))
    monkeypatch.setattr(_tms, "generate", fake_generate)


def test_llm_strategy_semantic_match(monkeypatch):
    # Lexically-divergent prose the fuzzy matcher would REJECT, but the semantic judge accepts.
    gold = _db_with_notification(subject="Server outage in datacenter A has been resolved")
    pred = _db_with_notification(subject="Good news — the datacenter A machines are back online")
    cfg = TextMatchConfig(strategy="llm", llm="stub")
    assert not fuzzy_text_match(gold.notification["NOTIF_900"].subject,
                                pred.notification["NOTIF_900"].subject)  # fuzzy would fail
    _stub_judge(monkeypatch, equivalent=True)
    assert compare_dbs(gold, pred, cfg=cfg)[0]                          # judge says equivalent
    _stub_judge(monkeypatch, equivalent=False)
    matched, mismatches = compare_dbs(gold, pred, cfg=cfg)
    assert not matched and any("judge" in m for m in mismatches)        # judge says not


def test_llm_strategy_structural_still_exact(monkeypatch):
    # Even with the judge approving all prose, a structural field mismatch still fails.
    _stub_judge(monkeypatch, equivalent=True)
    gold = _db_with_notification()
    matched, _ = compare_dbs(gold, _db_with_notification(status="sent"), cfg=TextMatchConfig(strategy="llm", llm="stub"))
    assert not matched


def test_llm_strategy_empty_pred_fails_without_judging(monkeypatch):
    # gold has prose, pred is empty -> resolved deterministically (no judge call needed).
    called = {"n": 0}
    def fake_generate(*a, **k):
        called["n"] += 1
        return AssistantMessage(content='{"results":[]}')
    monkeypatch.setattr(_tms, "generate", fake_generate)
    # baseline = pred's NOTIF_900 row, so only the gold's changed subject is considered; gold has
    # prose, pred is empty -> resolved deterministically, no pair reaches the batched judge.
    base = _db_with_notification(subject="", message="")
    gold = _db_with_notification(subject="Outage resolved", message="")
    matched, _ = compare_dbs(gold, base, baseline_db=base, cfg=TextMatchConfig(strategy="llm", llm="stub"))
    assert not matched and called["n"] == 0


def test_exact_strategy_rejects_paraphrase():
    gold = _db_with_notification()
    pred = _db_with_notification(subject="Update: work on INC0000003 has resumed now")
    assert not compare_dbs(gold, pred, cfg=TextMatchConfig(strategy="exact"))[0]


def test_freetext_unchanged_by_gold_is_ignored():
    # gold leaves INC_003.worknotes at its seed value; the agent overwrote it with an unrelated
    # note. Since the task never set worknotes, the agent's value must NOT be penalised.
    base = get_environment().tools.db
    gold = base.model_copy(deep=True)                 # unchanged vs baseline
    pred = base.model_copy(deep=True)
    pred.incident["INC_003"].worknotes = "agent added a transition note about the vendor part"
    assert compare_dbs(gold, pred, baseline_db=base)[0]      # ignored -> match
    assert not compare_dbs(gold, pred)[0]                    # no baseline -> (incorrectly) fails
