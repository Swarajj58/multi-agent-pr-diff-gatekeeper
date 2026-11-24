from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from pr_gatekeeper.nodes import load_diff_node, logic_reviewer_node, security_auditor_node
from pr_gatekeeper.state import Finding, GatekeeperState, Verdict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt


def critic_node(state: GatekeeperState) -> dict:
    changed_set = set(state.changed_files)
    verified: list[Finding] = []
    dropped = 0

    for finding in state.findings:
        if finding.file_path in changed_set:
            verified.append(finding)
        else:
            dropped += 1

    has_high = any(f.severity == "high" for f in verified)
    has_medium = any(f.severity == "medium" for f in verified)

    if has_high:
        verdict: Verdict = "REQUEST_CHANGES"
    elif has_medium:
        verdict = "NEEDS_HUMAN_REVIEW"
    else:
        verdict = "APPROVE"

    next_revision = state.revision_count + 1
    notes = (
        f"Pass {next_revision}: verified {len(verified)} finding(s), "
        f"dropped {dropped} unverified file reference(s). Verdict={verdict}."
    )

    return {
        "verified_findings": verified,
        "revision_count": next_revision,
        "critic_notes": notes,
        "verdict": verdict,
    }


def route_after_critic(state: GatekeeperState) -> str:
    # Bounded retry guard: never exceed max_revisions
    if state.revision_count < state.max_revisions and len(state.findings) == 0 and state.diff_text.strip():
        return "recheck_audit"
    return "finalize"


def human_gate_node(state: GatekeeperState) -> dict:
    # Auto-approve clean low/no-issue changes without blocking
    if state.verdict == "APPROVE":
        return {"human_decision": "auto_approved"}

    decision = interrupt(
        {
            "target_ref": state.target_ref,
            "verdict": state.verdict,
            "critic_notes": state.critic_notes,
            "findings_count": len(state.verified_findings),
            "prompt": "Approve override, confirm REQUEST_CHANGES, or reject? (approve / block / reject)",
        }
    )

    normalized = str(decision).strip().lower()
    updated_verdict: Verdict = state.verdict
    if normalized == "approve":
        updated_verdict = "APPROVE"
    elif normalized in {"block", "reject"}:
        updated_verdict = "REQUEST_CHANGES"

    return {
        "human_decision": normalized,
        "verdict": updated_verdict,
    }



def finalize_review_node(state: GatekeeperState) -> dict:
    lines = [
        f"# PR Gatekeeper Report (`{state.target_ref}`)",
        f"**Verdict:** `{state.verdict}`",
        f"**Critic Notes:** {state.critic_notes}",
        "",
        "## Verified Findings",
    ]

    if not state.verified_findings:
        lines.append("- No issues found.")
    else:
        for f in state.verified_findings:
            lines.append(
                f"- **[{f.severity.upper()}] ({f.category})** `{f.file_path}` — {f.summary} "
                f"*Fix:* {f.remediation}"
            )

    return {"final_review": "\n".join(lines)}


def build_gatekeeper_graph(checkpointer=None):
    builder = StateGraph(GatekeeperState)

    builder.add_node("load_diff", load_diff_node)
    builder.add_node("security_auditor", security_auditor_node)
    builder.add_node("logic_reviewer", logic_reviewer_node)
    builder.add_node("critic", critic_node)
    builder.add_node("human_gate", human_gate_node)
    builder.add_node("finalize", finalize_review_node)

    builder.add_edge(START, "load_diff")
    builder.add_edge("load_diff", "security_auditor")
    builder.add_edge("load_diff", "logic_reviewer")

    builder.add_edge("security_auditor", "critic")
    builder.add_edge("logic_reviewer", "critic")

    builder.add_conditional_edges(
        "critic",
        route_after_critic,
        {
            "recheck_audit": "security_auditor",
            "finalize": "human_gate",
        },
    )
    builder.add_edge("human_gate", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())