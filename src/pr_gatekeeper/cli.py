from __future__ import annotations

import argparse
import uuid

from langgraph.types import Command

from pr_gatekeeper.graph import build_gatekeeper_graph
from pr_gatekeeper.state import GatekeeperState

from pathlib import Path
import os

def _load_env() -> None:
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

def main() -> None:
    parser = argparse.ArgumentParser(description="Run PR / Diff Gatekeeper graph.")
    parser.add_argument("--repo", default=".", help="Path to Git repository")
    parser.add_argument("--ref", default="HEAD", help="Commit hash or range (e.g. HEAD~1..HEAD)")
    parser.add_argument("--thread-id", default=None, help="Checkpoint thread ID")
    args = parser.parse_args()

    thread_id = args.thread_id or f"pr-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    app = build_gatekeeper_graph()

    initial_state = GatekeeperState(repo_path=args.repo, target_ref=args.ref)
    result = app.invoke(initial_state, config=config)

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print("\n=== HUMAN GATE TRIGGERED ===")
        print(f"Target:   {payload.get('target_ref')}")
        print(f"Verdict:  {payload.get('verdict')}")
        print(f"Notes:    {payload.get('critic_notes')}")
        print(f"Findings: {payload.get('findings_count')}")
        choice = input("Enter decision (approve / block): ").strip()
        result = app.invoke(Command(resume=choice), config=config)

    print("\n" + result["final_review"])


if __name__ == "__main__":
    main()