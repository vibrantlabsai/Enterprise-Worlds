"""How this gym describes itself to the platform — the ``gym.describe`` result.

Ported from the platform's ``task-review/src/gyms/itsm/descriptor.ts``, which becomes a fallback
cache once the platform reads this from the gym itself. The shape is ``GymDescriptor`` in
``gym-contract/src/descriptor.ts``.

The descriptor is authored here because it goes stale the moment the gym changes how it runs, and
nobody outside the gym would notice. ``policy`` carries the *contents* of ``policy.md`` — a reviewer
judging a task against the rules needs the rules, not a pointer to them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from enterprise_worlds.platform.wire import PROTOCOL_VERSION

GYM_ID = "itsm"
DISPLAY_NAME = "EnterpriseOps ITSM"

# Declared ONLY for what `eworlds serve` actually implements — an over-claim becomes a failure at
# the call site; an under-claim is a graceful skip. Add each capability string AS the corresponding
# method is implemented, never ahead of it. (Known strings: materializeState, queryState, verify,
# rollout, cancelRollout.)
CAPABILITIES: list[str] = []

# Models this gym can be evaluated against. `sarvam` is the default: it is the band model the miner
# itself uses for pass@k, so an edited revision's evidence is comparable with the mining evidence it
# replaces.
ROLLOUT_TARGETS = [
    {"alias": "opus", "model": "anthropic:claude-opus-4-7"},
    {"alias": "sonnet", "model": "anthropic:claude-sonnet-4-6"},
    {"alias": "sarvam", "model": "sarvam:sarvam-105b", "isDefault": True},
]

# What a reviewing agent needs to know about this gym before it can judge a task — how an episode
# runs, which fields are graded strictly, the traps that make a plausible-looking task wrong.
# Ported verbatim from descriptor.ts.
REVIEW_PRIMER = "\n".join([
    "How the gym runs this task:",
    "A task is one EnterpriseOps ITSM Gym2 scenario. An LLM agent talks to a simulated user and operates the",
    "in-memory ITSM database through typed tools, following the ITSM policy, and is scored on",
    "the final database state. The agent greets first, then it's a multi-turn conversation with the user",
    "simulator. The fields configure that run:",
    "- `scenario.persona`: who the agent talks to — `identity` (the authenticated caller: `user_id` plus",
    "  `first_name`/`last_name`/`email`/`role`), `personality`, `role_description`, and `known_info`. `identity.user_id`",
    "  is the authenticated caller (org scoping + default attribution on tools). `known_info` is a free-form dict of",
    "  progressive-disclosure facts the user reveals only when asked (an incident number, a device serial, etc.). The",
    "  user never invents ids — if a fact isn't in `identity`/`known_info`, the agent must discover it via tools.",
    "- `scenario.task_description`: the goal the user wants accomplished, in natural language.",
    "- `scenario.simulator_guidance`: OPTIONAL sim-only guidance — how the user simulator should respond when the",
    "  agent PROPOSES/ASKS a follow-up (the policy's PROPOSE/ASK cascades), e.g. 'if the agent proposes notifying",
    "  the caller, confirm and give the email if asked'. It configures the USER, is NEVER shown to the agent under",
    "  test, and should stay narrow (only conditional PROPOSE/ASK responses, no persona restatement); empty/absent",
    "  when the task has no such cascade. It may name facts from `known_info` (sim-side, not a leak to the agent).",
    "- `initial_state_delta`: per-task edits applied over the ITSM seed `db.json` BEFORE the run, keyed",
    "  `collection -> record_id -> {set|create|delete}`. This is how a task sets up its starting state.",
    "- `evaluation_criteria.actions`: the gold tool-call sequence (`{name, arguments}`). It is replayed on a",
    "  fresh ITSM seed+delta to compute a target DB; the agent passes the structural DB check only if its final",
    "  database matches that target. This replaces SQL verifiers with DB matching.",
    "- How prose is graded (NOT fully exempt): the DB-match is field by field. Structural fields — ids, enums,",
    "  links, statuses, numbers, timestamps-as-data, AND `short_description` and `title` — must match EXACTLY. A",
    "  specific set of prose columns is instead graded by a LENIENT LLM semantic-equivalence judge (only when the",
    "  gold changed the field from baseline; an empty field where the gold has content fails): `notification.subject`/`message`,",
    "  `incident.description`/`worknotes`/`resolution_notes`/`close_notes`, `problem.problem_statement`/`worknotes`/",
    "  `workaround`/`fix_notes`, `change.description`/`implementation_plan`/`testing_plan`/`close_notes`,",
    "  `knowledge.body`, and `description` on `service`/`service_offering`/`user_group`. The judge accepts the agent's",
    "  text when it conveys the same MATERIAL FACTS as the gold — ignoring wording, phrasing, formatting, length, and",
    "  added detail — and fails only when the agent CONTRADICTS a material fact or OMITS one essential to the field's",
    "  purpose. So an agent may freely paraphrase a semantically-judged field but must still convey its substance;",
    "  `short_description`/`title` are exact and must be reproduced.",
    "- `evaluation_criteria.nl_assertions`: natural-language outcomes graded by an LLM judge (they pin specific prose",
    "  substance the per-field equivalence judge doesn't target — cross-cutting outcomes, the right person informed).",
    "  Reward = (DB-hash match) × (all nl_assertions met); either criterion may be omitted, and an omitted one",
    "  doesn't gate.",
    "- Rollout evidence (`__seo`): READ-ONLY mining/rollout evidence the gym attaches AFTER running the task",
    "  (NOT shown to the solver). It is NOT in the task JSON above — it is summarized in the `Rollout evidence`",
    "  block below the task JSON, which carries the evaluation header (`target_model`, `k_runs`, `pass_rate`) and",
    "  one breadcrumb per trial (`run_idx`, `passed`, `db_match`, `structural_match`, `nl` met/total, `tool_calls`)",
    "  plus that trial's file path. Open a per-run file (Read or jq) for its full `transcript[]` (the agent ↔",
    "  user-sim ↔ tools conversation — PRIMARY evidence for conversational tasks; cite specific turns) and `db_diff`",
    "  (`structural_match` + per-collection `field_diffs` = `{record: {field: {gold, pred, free_text}}}`;",
    "  `structural_match:true` with only `free_text` diffs = tolerated prose, not a failure). Open the oracle file",
    "  for the gold replay: `final_db_state` (the TARGET DB a correct run must reach) and `node_executions` (each",
    "  gold action's tool/args/result, incl. generated ids). Older tasks have no rollout evidence — judge from the",
    "  static fields above.",
    "- Policy: the ITSM policy is NOT a task field — it lives in `policy.md` and applies to every task. Call",
    "  the `get_policy` tool to read it. The agent must follow it, and the gold actions / nl_assertions often",
    "  rely on side effects the policy mandates (notify a stakeholder, link a CI, set a status) even when",
    "  `task_description` never restates them.",
    "Gym gotchas: IDs and timestamps are generated deterministically, so the gold actions assume an exact",
    "creation order (an action that expects a new `change_id='CHG_026'` assumes the seed already holds 25);",
    "the env clock is the task's `current_time` (a task field; default `2024-06-01T00:00:00`) — created_on/updated_on,",
    "and a NEW incident SLA's `start_time`, are auto-stamped from it (the gold drops `start_time`), so those ARE",
    "derivable and reproducible, NOT underivable traps; a genuinely agent-underivable value in gold args (a timestamp",
    "the tool stores verbatim rather than auto-stamping, or a predicted id) is still unsolvable; exact-string matching on structural fields incl.",
    "`short_description`/`title` (the semantically-judged prose fields above need only convey the same material facts); enum-vocabulary mismatch",
    "(`incident.priority='moderate'` vs `change.priority='medium'`); currency/unit suffixes (numeric columns store",
    "bare `8000`, not '8000 GBP'); dual representations / polymorphic FKs (a relationship in both an FK column and a join table).",
    "Seed database (ground truth): the `seed-inspector` tools expose the EXACT state this task starts from —",
    "the ITSM `db.json` with this task's `initial_state_delta` applied and then narrowed to the task's",
    "`org_ids`, which is exactly the env the agent runs against. The scope is a SET: one org is a",
    "single-tenant task, two (`{provider, client}`) is an MSP one. The narrowing closes over foreign keys,",
    "so a record referenced from in-scope data is present even when it belongs to another org. Judge",
    "against what IS in the seed — a row you cannot find is out of scope for this task, not missing.",
    "It is loaded into SQLite (one table per",
    "collection). `describe_seed_db` lists tables/columns/row-counts; `query_seed_db(sql)` runs a read-only",
    "SELECT. Prefer them over guessing. A value the gold actions/nl_assertions rely on that ALREADY exists in",
    "the seed is one the agent should discover with tools — leaking it in `task_description`/`known_info` is bad;",
    "a value absent from the seed is user-supplied input, not a leak. Check predicted new ids against actual row",
    "counts, and use `get_policy` to see which side effects the verifier can legitimately expect.",
])

# How a reviewer should see a task: `source` is a dotted path into the task JSON (or several paths
# rendered as one grouped section). A client that does not recognise a `render` kind falls back to
# `json`, so new kinds may be added without breaking an older client.
TASK_VIEW = {
    "sections": [
        {
            "id": "task",
            "heading": "Task",
            "source": "scenario.task_description",
            "render": "prose",
        },
        {
            "id": "sim",
            "heading": "Simulator guidance",
            "subheading": "Sim-only — how the simulated user responds to the agent",
            "source": "scenario.simulator_guidance",
            "render": "prose",
            "optional": True,
        },
        {
            "id": "persona",
            "heading": "Persona",
            "source": "scenario.persona",
            "render": "entity_card",
        },
        {
            "id": "scope",
            "heading": "Environment scope",
            "source": ["org_ids", "tenancy", "seed_db", "current_time"],
            "render": "kv",
        },
        {
            "id": "setup",
            "heading": "Initial state delta",
            "source": "initial_state_delta",
            "render": "json",
            "optional": True,
        },
        {
            "id": "gold",
            "heading": "Verifier — gold actions",
            "source": "evaluation_criteria.actions",
            "render": "tool_call_list",
            "emptyNote": "No gold actions — the database check does not gate this task.",
        },
        {
            "id": "assertions",
            "heading": "Verifier — NL assertions",
            "source": "evaluation_criteria.nl_assertions",
            "render": "string_list",
        },
    ],
}


def build_descriptor(gym_commit_sha: Optional[str] = None) -> Dict[str, Any]:
    """The complete ``GymDescriptor``, with ``policy`` read from the gym's own ``policy.md``."""
    from enterprise_worlds.domains.itsm.environment import ITSM_POLICY_PATH

    descriptor: Dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "gymId": GYM_ID,
        "displayName": DISPLAY_NAME,
        "capabilities": list(CAPABILITIES),
        "policy": ITSM_POLICY_PATH.read_text(encoding="utf-8"),
        "reviewPrimer": REVIEW_PRIMER,
        "rolloutTargets": [dict(t) for t in ROLLOUT_TARGETS],
        "taskView": TASK_VIEW,
    }
    if gym_commit_sha:
        descriptor["gymCommitSha"] = gym_commit_sha
    return descriptor
