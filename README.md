# Enterprise-Worlds

**[Live leaderboard →](https://enterpriseworlds.vibrantlabs.com/)** — six models on ITSMBench, scored `pass@k` and `pass^k` over four independent trials.

Most agent benchmarks stop at the answer. Real organizations don't — work only counts once it has
been carried out in a system of record, by someone allowed to do it, with a trail that survives an
audit. That operational layer is where agent work actually lands, and it is largely unmeasured.

Enterprise-Worlds is a suite of executable enterprise environments for measuring it. Each world
ships the persistent state an agent must act on, the typed tools that change it, the written policy
that constrains it, and a simulated colleague who only volunteers what they are asked for. Nothing
is scored from the transcript; every task is graded on the state the agent leaves behind.

| World | Domain | Tasks | Status |
| --- | --- | --- | --- |
| [**ITSMBench**](./data/itsmbench) | IT Service Management | 53 | Released |

## ITSMBench

IT Service Management is the first world because it sits on a company's control surface — identity,
access, incidents, change, approvals, ownership. A one-line request ("deactivate this account")
routinely expands into a policy-governed cascade: find the right records, work out which
consequences are mandatory and which need the operator's say-so, and leave the rest alone.

An ITSMBench environment is a live tenant:

- **Two worlds** — a single-tenant company, and a provider world serving 20 client organizations
  where tasks cross tenant boundaries and access scope becomes part of the difficulty. Around
  twenty interconnected tables: incidents, SLA clocks, problems, change requests, a CMDB, a service
  catalog, knowledge articles, notifications, users, groups, roles, and permissions.
- **A policy** the agent is handed and held to, covering required fields, authority limits,
  escalation, and what must be asked before acting.
- **93 typed tools** for lookup, mutation, and aggregation. They enforce enum gates, required
  fields, and referential integrity like the real system would, and generate IDs and timestamps
  deterministically so runs stay comparable.
- **A simulated operator** with a persona and private `known_info`, disclosed progressively — facts
  surface only when the agent asks the right question.
- **State-based reward.** The final database is compared against the state a gold action sequence
  produces. Natural-language assertions cover what the database can't express.

Together those pieces test three things a static tool-use benchmark tends to miss: pulling out what
the user never thought to mention, reaching the right outcome by a sanctioned route, and finding
the right records in messy live state where a wrong match quietly corrupts everything downstream.

## Setup

Requires Python 3.11+ and (recommended) [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
```

Put provider credentials in a `.env` at the repo root (gitignored). Models resolve through
[litellm](https://docs.litellm.ai/), so any provider string works.

```bash
OPENAI_API_KEY=sk-...
```

## Running an eval

```bash
set -a && . ./.env && set +a          # load credentials

eworlds run    --domain itsm             # run the eval over the task set
eworlds tasks  --domain itsm             # list tasks (persona, goal, criteria)
eworlds domain itsm                      # print the policy
```

| Flag | Default | Description |
| --- | --- | --- |
| `--agent-llm` | `gpt-4o` | Agent model (must support tool calling) |
| `--user-llm` | `gpt-4o-mini` | User-simulator model |
| `--judge-llm` | `gpt-4o-mini` | NL-assertion judge |
| `--num-tasks` / `--task-ids` | all | Restrict the task set |
| `--k` | `1` | Trials per task; `>1` reports `pass^k` |
| `--log-dir` | — | Write `summary.json` + per-trial trajectories |

```bash
eworlds run --domain itsm --k 4 --log-dir runs/opus \
    --agent-llm anthropic/claude-opus-4-5 \
    --user-llm  anthropic/claude-haiku-4-5 \
    --judge-llm anthropic/claude-haiku-4-5
```

Or programmatically:

```python
from enterprise_worlds.domains.itsm.environment import get_tasks
from enterprise_worlds.run import run_task

task = get_tasks()[0]
print(run_task("itsm", task, agent_llm="gpt-4o").reward)
```

## How a task is scored

Reward is the product of the criteria a task defines — every one it defines must pass:

- **DB match** — the task's gold `actions` are replayed on a fresh seed to produce the expected
  final state, which is compared to the run's. Structured fields must match exactly; free-text
  fields are graded by a semantic judge, so reworded-but-correct prose still passes.
- **NL assertions** — an LLM judge grades each assertion against the conversation.

A task carries its own seed world (`seed_db`), tenancy scope (`org_ids`), and clock
(`current_time`), so single-tenant and cross-org tasks run side by side in one suite and every
timestamp a run stamps is reproducible.

## Layout

```
data/itsmbench/     the world: seed databases, policy, and the 53-task benchmark set
src/enterprise_worlds/  environment, tools, user simulator, evaluator, CLI
tests/              offline validation — every gold trajectory replays to reward 1.0
```

## Acknowledgements

The ITSM seed databases and typed tool surface are adapted from
**[EnterpriseOps-Gym](https://github.com/ServiceNow/EnterpriseOps-Gym)** (Apache-2.0),
reimplemented here as in-memory Python tool calls — no Docker or SQL server at runtime.

> **EnterpriseOps-Gym: Environments and Evaluations for Stateful Agentic Planning and Tool Use in Enterprise Settings**
> Shiva Krishna Reddy Malay et al. (ServiceNow) — [arXiv:2603.13594](https://arxiv.org/abs/2603.13594)

Original to this work: the multi-turn conversational user-simulator loop, the org-scoped
multi-tenant task model, and a verifier combining state-based DB comparison with natural-language
assertions in the style of [τ-bench](https://arxiv.org/abs/2406.12045).

## Citation

```bibtex
@misc{enterpriseworlds2026,
      title={{Enterprise-Worlds: Executable Enterprise Environments for Measuring Operational Agents}},
      author={Shahul Elavakkattil and Ankit Sridhar and Andrew Bastin and Jithin James and Kumar Anirudha and Arjun Devarajan},
      year={2026},
      publisher={Vibrant Labs},
      url={https://github.com/vibrantlabsai/Enterprise-Worlds},
}
```

## License

Apache-2.0 — see [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE).
