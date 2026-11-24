# Multi-Agent PR / Diff Gatekeeper

A modular LangGraph state machine that inspects Git commits or diff ranges, runs parallel security and logic reviews using deterministic checks plus optional structured LLM outputs, verifies findings in a bounded critic loop, and pauses at a human-in-the-loop gate before emitting a final review report.

## Architecture Overview

1. **`load_diff` (`load_diff_node`)**: Runs `git show` or `git diff` safely via `subprocess.run` argument lists and extracts `changed_files` from `+++ b/` headers.
2. **Parallel Fan-Out (`security_auditor` & `logic_reviewer`)**:
   - **Deterministic pre-pass**: Scans newly added (`+`) diff lines (excluding `+++` headers) for hardcoded secrets/tokens, unsafe `eval`/`exec`/`os.system`/`subprocess(..., shell=True)` calls, bare `except:`, and `TODO`/`FIXME`/`XXX` markers.
   - **Structured LLM pass**: Calls `client.with_structured_output(FindingList)` when an LLM client or supported API key (`GROQ_API_KEY` or `OPENAI_API_KEY`) is available, truncating large diffs to a bounded character budget and normalizing the finding `category`. When no key or client is provided, it skips network calls cleanly.
3. **`critic` (`critic_node` & `route_after_critic`)**: Fan-in node that filters findings against `changed_files`, increments `revision_count` up to `max_revisions`, sets `critic_notes`, and assigns a preliminary `verdict` (`APPROVE`, `NEEDS_HUMAN_REVIEW`, or `REQUEST_CHANGES`).
4. **`human_gate` (`human_gate_node`)**: Auto-approves clean `APPROVE` runs; otherwise calls `interrupt(...)` with a checkpointer-backed `thread_id` so an operator can resume with `Command(resume=...)` (`approve`, `block`, or `reject`).
5. **`finalize` (`finalize_review_node`)**: Formats the final Markdown review report (`final_review`) with the verdict, critic notes, and verified findings.

## Project Layout

```text
multi-agent-pr-diff-gatekeeper/
├── pyproject.toml
├── .env                  # Local environment variables (gitignored)
├── README.md
├── src/
│   └── pr_gatekeeper/
│       ├── __init__.py
│       ├── state.py      # Pydantic GatekeeperState, Finding, and Verdict models
│       ├── nodes.py      # Diff loader, deterministic rules, and structured LLM auditors
│       ├── graph.py      # StateGraph wiring, critic loop, human interrupt gate, and finalizer
│       └── cli.py        # CLI entrypoint with thread_id checkpointing and resume loop
└── test/
    └── test_gatekeeper.py
```

## Setup

Create and activate your virtual environment, then install dependencies and the package in editable mode:

```powershell
python -m venv multi_agent_venv
.\multi_agent_venv\Scripts\Activate.ps1
pip install -e .
pip install pytest
```

### Environment Variables (`.env`)

Create a `.env` file in the project root (and keep it out of Git):

```dotenv
GROQ_API_KEY=your_groq_api_key_here
# Or use OPENAI_API_KEY instead:
# OPENAI_API_KEY=your_openai_api_key_here
PR_GATEKEEPER_MODEL=llama-3.3-70b-versatile
```

## Running Tests

Run the offline test suite (uses temporary Git repositories and a fake LLM client, requiring no live API calls):

```powershell
python -m pytest -v
```

## Running the CLI

Run the gatekeeper against a commit or commit range:

```powershell
python -m pr_gatekeeper.cli --repo . --ref HEAD
```

Example with a diff range and custom `thread_id`:

```powershell
python -m pr_gatekeeper.cli --repo . --ref HEAD~1..HEAD --thread-id pr-check-01
```

If `human_gate` triggers, the CLI prints the interrupt details and prompts for `approve` or `block`, resuming the exact `thread_id` checkpoint via `Command(resume=choice)`.