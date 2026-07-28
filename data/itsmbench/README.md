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

Recorded runs are **not yet checked in**. Three models cover all 53 tasks at 4 trials each and are
ready to land here as `trajectories/<model>/<task_id>/trial_{0..3}.json`. Qwen3.7-plus is excluded:
it ran on the earlier `amazon73` set and covered only 40 of these 53 tasks.

## Task IDs

IDs currently carry their mining-run provenance verbatim
(`envscaler_msp_band_sarvam_50_20260702-230603_shahul/…/iter_029`), and the `trajectories/`
directory layout mirrors them exactly. These are **provisional** — a rename to stable public IDs
has to rewrite tasks, trajectory paths, and error-analysis references together.

## Provenance

Seed data and action space are adapted from
[EnterpriseOps-Gym](https://github.com/ServiceNow/EnterpriseOps-Gym) (Apache-2.0). The tasks,
multi-turn user simulation, and recorded trajectories are original to this project.
