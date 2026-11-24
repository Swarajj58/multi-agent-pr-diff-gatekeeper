```text 

Lesson 1: Why State Design Comes First

In LangGraph, State is the single source of truth flowing through every node. Two rules trip up beginners in production graphs:   

- Parallel write collision: When two nodes run in parallel (like security_auditor and logic_reviewer) and both return a list of findings, LangGraph needs an append reducer (Annotated[list[...], operator.add]) so their results combine instead of overwriting each other.   

- Explicit schema over loose dicts: Using TypedDict + Literal types makes allowed verdicts ("APPROVE", "REQUEST_CHANGES", "NEEDS_HUMAN_REVIEW") explicit and catches bad state shapes early.
```

```
1. pr-gatekeeper/
├── pyproject.toml
├── src/
│   └── pr_gatekeeper/
│       ├── __init__.py
│       ├── state.py
│       ├── nodes.py
│       ├── graph.py
│       └── cli.py
└── tests/
    └── test_gatekeeper.py
```


```text


Lesson 2: Diff Loading & Parallel Specialist Nodes (nodes.py)
Now that state.py is ready, let's build Lesson 2 in src/pr_gatekeeper/nodes.py:
1. load_diff_node: Safely runs git diff / git show on repo_path for target_ref (using a list of args—never shell=True—and extracting changed_files).
2. security_auditor_node: Inspects the diff for security risks (hardcoded secrets/tokens, eval/exec, os.system/shell=True, raw SQL string formatting, SSRF/path patterns) and returns {"findings": [...]}.
3. logic_reviewer_node: Inspects added/changed lines (+ lines) for logic/regression risks (missing None/key guards, unhandled edge cases, bare except blocks, missing tests/validation) and returns {"findings": [...]}.
Starting with deterministic checks in Lesson 2 gives you a working, testable graph right away before we wire in LLM calls and the critic loop in Lesson 3.

```

```

Teacher Check for Lesson 2
Notice how both security_auditor_node and logic_reviewer_node only inspect lines starting with + (excluding +++). Why do we filter to + lines instead of scanning the entire diff text?

Answer: 

me : becasue in git ++ means modification in code

AI : 

Close—you have the right intuition about changes in a diff, with one important distinction in unified diff syntax:

Single + (at the start of a line): Marks a newly added or modified line introduced by this change (the file header itself starts with +++ b/..., which is why we exclude +++).

Single - (at the start of a line): Marks a removed line that used to exist before this change.

No leading + or -: Context lines that were already there and untouched.

Why Filtering to + Matters in a PR Gatekeeper
If you scanned - lines too, your gatekeeper would flag code the author deleted (such as removing a hardcoded secret or replacing a bare except:), creating false positives on fixes. Checking + lines focuses the review on code actually being introduced.

(Also worth noting: if someone runs a diff range like git diff main..feature, a line that was replaced shows up as - old_line and + new_line—so scanning + catches the new code cleanly.)

```


```

Lesson 3: Wiring the StateGraph & Adding the Critic Loop (graph.py)
Now that state.py and nodes.py are in place, let's connect them in src/pr_gatekeeper/graph.py and introduce the Critic / Verifier loop (critic_node + conditional edge).


How the Flow Works Here

1 START → load_diff

2 load_diff → fan-out to both security_auditor and logic_reviewer in parallel.

3 Both auditors → critic (fan-in).

4 critic node:

Checks every finding against changed_files and diff_text so hallucinated/non-existent file paths are filtered out (verified_findings).

Increments revision_count (state.revision_count + 1).

Sets critic_notes and determines a preliminary verdict:

    Any high severity finding → "REQUEST_CHANGES"

    Any medium severity finding → "NEEDS_HUMAN_REVIEW"

    Only low or no findings → "APPROVE"

5 Conditional edge after critic:

If revision_count < max_revisions and no verified_findings exist yet while diff_text is non-empty, you can loop back to re-audit (or pass through once verified). In our deterministic baseline, let's include a clean should_recheck router that demonstrates a bounded loop (revision_count < max_revisions) and then routes to finalize_review (END).



Teacher Check for Lesson 3

Look closely at route_after_critic and build_gatekeeper_graph():
If route_after_critic returns "recheck_audit" ("security_auditor"), what happens to state.findings when security_auditor runs a second time because of the operator.add reducer on findings?

Create graph.py, think through that operator.add behavior on a loop, and we'll tackle Lesson 4: Human-in-the-Loop (interrupt + Command(resume=...)), CLI, and pytest.


Quick wrap on that Lesson 3 question: because findings uses operator.add, re-running an auditor node appends to state.findings rather than replacing it. In our route_after_critic check (len(state.findings) == 0), nothing is duplicated when zero findings existed—but in a general retry loop where findings do exist, looping back without clearing or deduplicating would stack duplicate findings across passes. That is a classic LangGraph loop subtlety to keep in mind.


```

```text

Lesson 4: Human-in-the-Loop (interrupt), CLI & pytest
In production PR gating, high-severity findings (REQUEST_CHANGES) or uncertain cases (NEEDS_HUMAN_REVIEW) often require a human engineer to confirm whether to block, override, or acknowledge before finalizing.

LangGraph handles this with two core pieces:

Checkpointer (MemorySaver / SQLite): Persists graph state per thread_id so execution can pause and resume cleanly.

interrupt() + Command(resume=...): Inside a node, calling interrupt({...}) pauses the graph and returns an __interrupt__ payload to the caller. Resuming with Command(resume=decision) feeds the human's answer right back into the paused node.

1. Add Human Approval Gate in src/pr_gatekeeper/graph.py
Update build_gatekeeper_graph(checkpointer=None) and add a human_gate_node between critic and finalize (or when verdict != "APPROVE"):



Created : cli.py A clean CLI runner using a thread_id so interrupted runs can resume seamlessly:

```

```
Created test folder and test_gatekeeper.py



Your Checkpoint for Lesson 4
Add human_gate_node to graph.py, create cli.py and tests/test_gatekeeper.py, and run pytest -v.

Notice how test_gatekeeper_blocks_secret_and_resumes_on_approve uses the same thread_id across app.invoke(state, config=config) and app.invoke(Command(resume="approve"), config=config). Why would using a new random thread_id on the second app.invoke(Command(resume=...)) call fail to resume the paused graph?


Answer: Before moving on, let's quickly answer that thread_id checkpoint question: if you pass a new thread_id on Command(resume=...), LangGraph looks up an empty/nonexistent checkpoint for that thread and has no paused interrupt() state to resume. Keeping the same thread_id ties the resume command to the exact saved snapshot.


```

```text

Next Step: Verify Lesson 4 & Add LLM-Backed Specialist Review
Once you have human_gate_node, cli.py, and tests/test_gatekeeper.py in place and pytest -v passes, the next step is upgrading security_auditor_node and logic_reviewer_node from regex-only heuristics to structured LLM outputs (with_structured_output) while keeping deterministic checks as a fast pre-filter.



Install pip install pytest

and install -e .

Installing with -e . also ensures pr_gatekeeper is importable from anywhere without manual PYTHONPATH tweaks.
```

```text 

Lesson 5: Adding Structured LLM Review to nodes.py
In src/pr_gatekeeper/nodes.py, add a FindingList schema and an optional LLM helper that uses with_structured_output(FindingList).


Modifed Nodes.py


Where the LLM Is Integrated in nodes.py
Schema Wrapper (FindingList): Wraps list[Finding] in a Pydantic BaseModel so with_structured_output(FindingList) produces a valid top-level JSON object schema across providers.

Client Factory (_build_default_llm): Lazily instantiates ChatGroq (GROQ_API_KEY) or ChatOpenAI (OPENAI_API_KEY) only when a key is present—returning None otherwise so offline pytest runs stay fast and network-free.

Structured Review Helper (_run_llm_review): Truncates diff_text to a bounded character budget (12_000 chars) before invoking client.with_structured_output(FindingList) and normalizes category to match the calling specialist node.

Specialist Nodes (security_auditor_node & logic_reviewer_node): Run fast regex/rule checks first (deduplicated per line/pattern), then append _run_llm_review(...) findings—and both accept an optional llm override for clean unit testing.


One Quick Check Before Moving On
Notice how _run_llm_review handles both isinstance(response, FindingList) and FindingList.model_validate(response): why is that fallback useful when testing or swapping LangChain chat models?

```


