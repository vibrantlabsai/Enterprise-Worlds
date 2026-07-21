## Enterprise-Worlds

Enterprise-Worlds is a suite of **RL environments for enterprise work**. The environments are
executable replicas of enterprise ecosystems: real seed databases with all their mess and history,
the systems and tools agents act through, and the policies that govern them. The tasks are real
workflows running inside those worlds — natively **multi-turn**, where the conversation with the
user is part of the work itself, with **verifiable rewards**, so the same environments that
benchmark an agent can train it.

The first world is **ITSM**: an agent **converses with a simulated user**, operates on an
in-memory ITSM database through ~95 typed Python tools, and is scored by **golden-trajectory /
state verification combined with natural-language assertions**.

Each domain specifies:

- A **policy** the agent must follow (`data/<domain>/policy.md`)
- A set of **tools** the agent can use (in-memory, pydantic-typed)
- A set of **tasks** with evaluation criteria


## What this is

The ITSM environment — the seed databases (the simulated enterprise world: incidents, problems,
changes, SLAs, knowledge, configuration items, services, users/groups/roles, organizations,
notifications) and the ~95 typed tools (the action space) — is ported from
**[EnterpriseOps-Gym](https://github.com/ServiceNow/EnterpriseOps-Gym)** (Apache-2.0), reimplemented
as in-memory Python tool calls (no Docker / SQL server at runtime).

Original to this repo:

- **Multi-turn conversational tasks** — a persona-driven user simulator the agent must converse with
  (progressive disclosure of `known_info`, never invents ids); a turn-based agent ↔ user ↔ environment
  loop, rather than a one-shot instruction.
- **Verification** — state-based DB comparison (a gold-action replay produces a target DB, compared by
  record/field) **combined with** natural-language assertions, in the style of
  [τ-bench](https://arxiv.org/abs/2406.12045); free-text DB fields are graded by a **semantic judge**
  (not exact match) so reworded-but-correct prose passes. Reward is the product of the checks a task
  defines.
- **Multi-tenancy** — org-scoped tasks (`org_ids`) with FK-closed DB slicing for **single-tenant** and
  **provider/client (MSP)** cross-org scenarios.

## Architecture

A run drives a turn-based loop — **user simulator ↔ agent ↔ environment (tools + DB)** — then
scores the final database state. The agent and user simulator are LLMs (via litellm); the
environment and evaluator are pure in-memory Python (no Docker, no SQL server at runtime).

```mermaid
flowchart TD
    POL["policy.md"] --> AGENT
    SEED[("seed DB (msp_db.json)<br/>+ task delta")] --> ENV

    subgraph LOOP["Conversation loop — Orchestrator (agent greets first)"]
        direction LR
        USER["User Simulator<br/>persona + task description"]
        AGENT["LLM Agent<br/>policy + tool schemas"]
        USER <-->|messages| AGENT
    end

    AGENT -->|tool calls| ENV["Environment<br/>ToolKit · 95 typed tools<br/>in-memory ItsmDB"]
    ENV -->|tool results| AGENT
    AGENT -.->|litellm| PROV(("LLM provider"))
    USER -.->|litellm| PROV

    ENV ==>|final DB state| EVAL["Evaluator"]
    EVAL --> DBH["gold-action DB-hash"]
    EVAL --> NL["NL-assertion judge"]
    DBH --> REW(["reward"])
    NL --> REW
```

## Setup

Requires Python 3.11+ and (recommended) [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
```

Put provider credentials in a `.env` at the repo root (gitignored) to run live evals:

```bash
OPENAI_API_KEY=sk-...
# any litellm-supported provider works (gpt-4o, anthropic/claude-..., bedrock/..., etc.)
```

Models are resolved by [litellm](https://docs.litellm.ai/), so any provider string works.

## The `eworlds` CLI

```bash
set -a && . ./.env && set +a              # load credentials

eworlds run    --domain itsm                 # run the eval over the domain's tasks
eworlds tasks  --domain itsm                 # list the tasks (persona, goal, criteria)
eworlds domain itsm                          # print the domain policy
```

`eworlds run` ties together a litellm tool-calling agent, the user simulator, the environment, and
the evaluator. Useful flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--domain`, `-d` | `itsm` | Domain to evaluate |
| `--agent-llm` | `gpt-4o` | LLM for the agent (must support tool calling) |
| `--user-llm` | `gpt-4o-mini` | LLM for the user simulator |
| `--judge-llm` | `gpt-4o-mini` | LLM for the NL-assertion judge |
| `--task-ids` | all | Run only these task ids |
| `--num-tasks` | all | Run at most N tasks |
| `--max-steps` | `12` | Max conversation steps per task |
| `--k` | `1` | Trials (rollouts) per task; `>1` reports `pass^k` |
| `--seed` | — | Base seed; trial `i` uses `seed+i` (best-effort per provider) |
| `--save-to` | — | Write all results (trajectories + rewards) to one JSON file |
| `--log-dir` | — | Write a structured run dir: `summary.json` + per task/trial trajectories |
| `--verbose`, `-v` | off | Print each task's conversation |

```bash
eworlds run --domain itsm --num-tasks 1 --verbose --save-to results.json
```

**Scoring**: reward = gold-action full-DB-hash match × NL assertions. A task passes (reward 1.0)
only if the agent's end-state matches the gold replay of the task's `actions` **and** every NL
assertion is met. Either criterion may be omitted by a task, in which case it doesn't gate.

**Multiple rollouts**: pass `--k N` to run each task `N` times; the summary then reports the
`pass^k` reliability curve (`C(c,j)/C(n,j)`, averaged over tasks) alongside `avg_reward`. Add
`--log-dir DIR` to persist a run: `DIR/summary.json` (metadata + metrics) plus
`DIR/<task_id>/trial_<i>.json` for every run.

### Running with a specific provider

Set credentials in `.env` (see `.env.example`), `set -a && . ./.env && set +a`, then pick models
with the `--*-llm` flags. The defaults are OpenAI (`gpt-4o`/`gpt-4o-mini`), so for any other
provider you must pass the flags explicitly.

```bash
# OpenAI (default models — no flags needed)
#   .env: OPENAI_API_KEY=sk-...
eworlds run --domain itsm --num-tasks 1 -v

# Anthropic — standard API key
#   .env: ANTHROPIC_API_KEY=sk-ant-api03-...
eworlds run --domain itsm --num-tasks 1 -v \
    --agent-llm anthropic/claude-sonnet-4-5 \
    --user-llm  anthropic/claude-haiku-4-5 \
    --judge-llm anthropic/claude-haiku-4-5

```


### Programmatic API

```python
from enterprise_worlds.domains.itsm.environment import get_tasks
from enterprise_worlds.run import run_task

task = get_tasks()[0]
result = run_task("itsm", task, agent_llm="gpt-4o")
print(result.reward)
```

`examples/run_eval.py` runs one task end to end (override models via `AGENT_MODEL` / `USER_MODEL`
/ `JUDGE_MODEL`).

## How a task is defined

Tasks are loaded by `get_tasks()` from two optional, merged sources: `data/itsm/tasks.json`
(a JSON list) and a `data/itsm/tasks/` directory (each `*.json` holds one task or a list of
tasks). Duplicate ids across the sources are rejected. Each task specifies:

- a **scenario** — a persona (`name`, `personality`, and a free-form **`known_info`** dict) plus
  a natural-language task description, all fed to the user simulator. `known_info` holds facts the
  user knows and reveals when asked (their `user_id`, email, an incident number, …) so the
  simulator never invents ids. Its `user_id` also becomes the **authenticated caller** for the
  tools (org scoping + default attribution: requested_by on changes, opened_by on problems,
  notification sender) — exposed as the derived `Task.acting_user_id`,
- an optional **initial_state_delta** applied over the seed DB (`set` / `create` / `delete`),
- **evaluation_criteria**: two signals —
  - **actions** — a correct gold tool-call sequence, replayed to compute a target DB state for
    hash matching.
  - **nl_assertions** — natural-language outcomes graded by an LLM judge.

## Evaluation

The reward is the product of the two criteria a task defines; all defined checks gate — a state-based
DB check and a natural-language layer (conversation-level assertions + semantic judging of free-text
DB fields).

- **DB-match** (`evaluator/evaluator_env.py`): replays the task's gold `actions` on a fresh
  seed+delta env and compares its DB hash to the run's final DB. Tools generate IDs/timestamps
  deterministically so gold replay is reproducible. Structured fields must match exactly; free-text
  DB fields are compared by a semantic judge (`evaluator/text_match_strategy.py`) so reworded-but-
  equivalent prose still passes.
- **NL assertions** (`evaluator/evaluator_nl.py`): an LLM judge grades each assertion against the
  conversation.

## Acknowledgements

Enterprise-Worlds is built on the ITSM environment from:

> **EnterpriseOps-Gym: Environments and Evaluations for Stateful Agentic Planning and Tool Use in Enterprise Settings**
> Shiva Krishna Reddy Malay et al. (ServiceNow) — https://github.com/ServiceNow/EnterpriseOps-Gym · [arXiv:2603.13594](https://arxiv.org/abs/2603.13594)

The ITSM domain schema, the seed databases (the simulated enterprise world), and the typed
tool surface (the action space) originate from the EnterpriseOps-Gym project, licensed under
Apache-2.0. Enterprise-Worlds reimplements that environment as in-memory Python tool calls (no
Docker / SQL server at runtime) and adds, original to this work: a **multi-turn conversational
user-simulator loop**, an **org-scoped multi-tenant (single-tenant + provider/client MSP) task
model**, and a **verifier that combines state-based DB comparison with natural-language
assertions** in the style of [τ-bench](https://arxiv.org/abs/2406.12045) (Yao et al.).

### Related work

[EnterpriseBench / CoreCraft](https://surgehq.ai/blog/enterprisebench-corecraft) (Surge AI) also
builds RL environments for enterprise agents, simulating a single fictional company — a
customer-support organization — with long-horizon tasks graded by expert-authored rubrics.
Enterprise-Worlds takes a different path to the same destination: **executable replicas of the
enterprise systems of record themselves** (real schemas, seed data, and policies), tasks that are
**natively multi-turn** — the conversation with the user is part of the work — and rewards
**verified against database state** rather than rubrics. Our longer-term aim is to grow the unit
of evaluation from the task to the job: from agents that assist with work to agents accountable
for it.

## Citation

If you use Enterprise-Worlds, please cite this repository:

```bibtex
@misc{enterpriseworlds2026,
      title={{Enterprise-Worlds: RL Environments for Enterprise Work}},
      author={Shahul Elavakkattil and Ankit Sridhar and Andrew Bastin and Jithin James and Kumar Anirudha and Arjun Devarajan},
      year={2026},
      publisher={Vibrant Labs},
      url={https://github.com/vibrantlabsai/Enterprise-Worlds},
}
```

This work builds on EnterpriseOps-Gym; please also cite the original:

```bibtex
@misc{malay2026enterpriseopsgymenvironmentsevaluationsstateful,
      title={EnterpriseOps-Gym: Environments and Evaluations for Stateful Agentic Planning and Tool Use in Enterprise Settings},
      author={Shiva Krishna Reddy Malay and Shravan Nayak and Jishnu Sethumadhavan Nair and Sagar Davasam and Aman Tiwari and Sathwik Tejaswi Madhusudhan and Sridhar Krishna Nemala and Srinivas Sunkara and Sai Rajeswar},
      year={2026},
      eprint={2603.13594},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2603.13594},
}
```

## License

Licensed under **Apache-2.0**; see [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).

