"""Sample eval run via the programmatic API.

Equivalent to ``eworlds run --domain itsm --num-tasks 1 --verbose`` but shows how to
drive a run from Python. Run with credentials loaded:

    set -a && source .env && set +a
    python examples/run_eval.py
"""

import os

from enterprise_worlds.domains.itsm.environment import get_tasks
from enterprise_worlds.run import run_task

AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4o")
USER_MODEL = os.environ.get("USER_MODEL", "gpt-4o-mini")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")


def main() -> None:
    task = get_tasks()[0]
    print(f"=== Task: {task.id} ===")
    print(f"Persona : {task.scenario.persona.name} — {task.scenario.persona.personality}")
    print(f"Goal    : {task.scenario.task_description}\n")

    result = run_task(
        "itsm",
        task,
        agent_llm=AGENT_MODEL,
        user_llm=USER_MODEL,
        judge_llm=JUDGE_MODEL,
    )

    print("=== Conversation ===")
    for msg in result.trajectory:
        role = getattr(msg, "role", "?")
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                print(f"[{role} tool_call] {tc.name}({tc.arguments})")
        elif getattr(msg, "content", None):
            print(f"[{role}] {msg.content}")

    ri = result.reward_info
    print("\n=== Evaluation ===")
    if ri.db_check is not None:
        print(f"DB match : {ri.db_check.db_match}  (reward {ri.db_check.reward})")
    if ri.nl_check is not None:
        for c in ri.nl_check.checks:
            print(f"NL [{'PASS' if c.met else 'FAIL'}] {c.nl_assertion}")
    print(f"\nTOTAL REWARD: {result.reward}")


if __name__ == "__main__":
    main()
