"""Free-text DB-match: structured fields exact (modulo canonical surface form), free-text
columns per the configured strategy (exact | fuzzy | llm). Free-text divergences are advisory —
reported, never gating — so ``db_match`` is fully deterministic."""

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


def test_compare_dbs_unrelated_freetext_is_advisory():
    # Prose is agent-unobservable: even an unrelated subject never gates — but it IS reported.
    gold = _db_with_notification()
    advisory: list[str] = []
    matched, _ = compare_dbs(
        gold, _db_with_notification(subject="vacation request approved"),
        advisory_out=advisory,
    )
    assert matched and advisory


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


def test_llm_strategy_semantic_judge_is_advisory(monkeypatch):
    # Lexically-divergent prose the fuzzy matcher would REJECT: the semantic judge's verdict is
    # recorded, but never gates db_match either way.
    gold = _db_with_notification(subject="Server outage in datacenter A has been resolved")
    pred = _db_with_notification(subject="Good news — the datacenter A machines are back online")
    cfg = TextMatchConfig(strategy="llm", llm="stub")
    assert not fuzzy_text_match(gold.notification["NOTIF_900"].subject,
                                pred.notification["NOTIF_900"].subject)  # fuzzy would fail
    _stub_judge(monkeypatch, equivalent=True)
    advisory: list[str] = []
    matched, _ = compare_dbs(gold, pred, cfg=cfg, advisory_out=advisory)
    assert matched and not advisory                                      # judge says equivalent
    _stub_judge(monkeypatch, equivalent=False)
    advisory = []
    matched, mismatches = compare_dbs(gold, pred, cfg=cfg, advisory_out=advisory)
    assert matched and not mismatches                                    # still doesn't gate
    assert any("judge" in a for a in advisory)                           # ...but is reported


def test_llm_strategy_structural_still_exact(monkeypatch):
    # Even with the judge approving all prose, a structural field mismatch still fails.
    _stub_judge(monkeypatch, equivalent=True)
    gold = _db_with_notification()
    matched, _ = compare_dbs(gold, _db_with_notification(email="aisha.williams@techcorp.com"),
                             cfg=TextMatchConfig(strategy="llm", llm="stub"))
    assert not matched  # structured fields stay exact regardless of strategy


def test_llm_strategy_empty_pred_resolved_without_judging(monkeypatch):
    # gold has prose, pred is empty -> resolved deterministically (no judge call needed).
    called = {"n": 0}
    def fake_generate(*a, **k):
        called["n"] += 1
        return AssistantMessage(content='{"results":[]}')
    monkeypatch.setattr(_tms, "generate", fake_generate)
    # baseline = pred's NOTIF_900 row, so only the gold's changed subject is considered; gold has
    # prose, pred is empty -> the divergence lands in advisory without reaching the batched judge.
    base = _db_with_notification(subject="", message="")
    gold = _db_with_notification(subject="Outage resolved", message="")
    advisory: list[str] = []
    matched, _ = compare_dbs(gold, base, baseline_db=base,
                             cfg=TextMatchConfig(strategy="llm", llm="stub"),
                             advisory_out=advisory)
    assert matched and advisory and called["n"] == 0


def test_exact_strategy_paraphrase_is_advisory():
    gold = _db_with_notification()
    pred = _db_with_notification(subject="Update: work on INC0000003 has resumed now")
    advisory: list[str] = []
    matched, _ = compare_dbs(gold, pred, cfg=TextMatchConfig(strategy="exact"),
                             advisory_out=advisory)
    assert matched and advisory


def test_freetext_unchanged_by_gold_is_ignored():
    # gold leaves INC_003.worknotes at its seed value; the agent overwrote it with an unrelated
    # note. Since the task never set worknotes, the agent's value must NOT be penalised — with a
    # baseline it isn't even reported; without one it's advisory at worst.
    base = get_environment().tools.db
    gold = base.model_copy(deep=True)                 # unchanged vs baseline
    pred = base.model_copy(deep=True)
    pred.incident["INC_003"].worknotes = "agent added a transition note about the vendor part"
    advisory: list[str] = []
    assert compare_dbs(gold, pred, baseline_db=base, advisory_out=advisory)[0]
    assert not advisory                                      # baseline: not even advisory
    assert compare_dbs(gold, pred)[0]                        # no baseline: advisory, still match


# ── deterministic normalization (canonical surface forms) ────────────────────────────────


def test_normalization_accepts_canonical_variants():
    """Same referent, different surface form — normalized equal."""
    from enterprise_worlds.domains.itsm.data_model import Location

    def db_with_location(state, country="USA"):
        db = get_environment().tools.db.model_copy(deep=True)
        db.location["LOC_900"] = Location(
            location_id="LOC_900", org_id="ORG_001", name="Austin Hub", city="Austin",
            state=state, country=country, active=True,
        )
        return db

    assert compare_dbs(db_with_location("TX"), db_with_location("Texas"))[0]
    # Genuinely different values still fail.
    assert not compare_dbs(db_with_location("TX"), db_with_location("CA"))[0]
    # Country long-form.
    assert compare_dbs(db_with_location("TX", "United States"), db_with_location("TX", "USA"))[0]


def test_normalization_timestamp_separator_and_null_empty():
    gold = _db_with_notification(created_on="2024-06-01T00:00:00")
    pred = _db_with_notification(created_on="2024-06-01 00:00:00")
    assert compare_dbs(gold, pred)[0]
    # None == "" for optional strings.
    gold2, pred2 = _db_with_notification(subject=None), _db_with_notification(subject="")
    assert compare_dbs(gold2, pred2)[0]


def test_db_match_is_deterministic(monkeypatch):
    """No LLM sits in the gating path — identical inputs give identical verdicts even when the
    (advisory) judge flip-flops."""
    flip = {"v": True}
    def flaky_generate(model=None, messages=None, **kwargs):
        flip["v"] = not flip["v"]
        pairs = json.loads((messages[-1].content or "").split("pairs:\n", 1)[1])
        return AssistantMessage(content=json.dumps(
            {"results": [{"index": p["index"], "equivalent": flip["v"]} for p in pairs]}))
    monkeypatch.setattr(_tms, "generate", flaky_generate)
    gold = _db_with_notification(subject="Outage resolved after failover")
    pred = _db_with_notification(subject="The outage is fixed now")
    cfg = TextMatchConfig(strategy="llm", llm="stub")
    verdicts = [compare_dbs(gold, pred, cfg=cfg) for _ in range(4)]
    assert len({(m, tuple(mm)) for m, mm in verdicts}) == 1
