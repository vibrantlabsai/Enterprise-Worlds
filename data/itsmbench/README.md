# ITSMBench

The first benchmark world in Enterprise-Worlds: an executable IT Service Management tenant —
persistent state, a written policy, a typed action space, and a simulated user — where reward is
read from the **final database state**, not the transcript.

## Contents

| Path | What it is |
| --- | --- |
| `tasks.json` | The benchmark set — **53 tasks**, 31 MSP / 22 single-tenant. All carry NL assertions; 24 carry an `initial_state_delta`. |
| `msp_db.json` | Provider/client seed world (20 client organizations). Cross-tenant tasks run here. |
| `single_tenant_db.json` | Single-company seed world. |
| `policy.md` | The written operating contract handed to the agent — required fields, authority boundaries, escalation rules, what must be asked before acting. |
| `fk_spec.json` | Foreign-key spec; single source of truth for the integrity check and FK-closed org slicing. |

## Seeds are per task

The suite is **mixed-seed**: each task names its own world in `seed_db`, and the runner honours it
per task (`EOPS_ITSM_DB` only supplies the default when a task doesn't name one). A task replayed
against the wrong seed will fail to resolve its own records.

## Trajectories

Recorded runs are **not yet checked in**. They are laid out as
`trajectories/<model>/<task_id>/trial_{0..3}.json`, keyed by the published task ids.

| Model | Trials | Tasks |
| --- | --- | --- |
| `claude_opus_4_8_medium_adaptive` | 212 | 53 |
| `glm_5p2` | 212 | 53 |
| `gpt_5_6_luna_azure_chat_32k` | 212 | 53 |
| `muse_spark_1_1_meta` | 212 | 53 |
| `nemotron_3_ultra_nvfp4_fireworks` | 212 | 53 |
| `qwen3_7_plus_fireworks` | 212 | 53 |

All six models cover the full set at 4 trials each. Qwen3.7-plus is pooled from two runs — 40 tasks
from the `amazon73` run and the remaining 13 from a later `sarvam_midrange19` run — which is
recorded in `trajectories/manifest.json` along with each run's provenance.

Note that these runs were scored on DB match alone (`nl_check` is null throughout), so their rewards
are the state half of the reward only, with the NL assertions unevaluated.

## Task IDs

IDs are stable and tenancy-tagged: `itsmbench_msp_001`–`itsmbench_msp_031` and
`itsmbench_single_001`–`itsmbench_single_022`. Treat them as the public identity of a task — they
are what the leaderboard and any downstream analysis should join on.

## Provenance

Seed data and action space are adapted from
[EnterpriseOps-Gym](https://github.com/ServiceNow/EnterpriseOps-Gym) (Apache-2.0). The tasks,
multi-turn user simulation, and recorded trajectories are original to this project.
