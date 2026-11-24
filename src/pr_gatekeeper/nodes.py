from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from pr_gatekeeper.state import Finding, GatekeeperState

MAX_DIFF_CHARS = 12_000
MAX_LINE_HINT_CHARS = 80

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(api_key|secret|password|token)\s*=\s*['\"][^'\"]{6,}['\"]"),
    re.compile(r"(?i)sk-[a-zA-Z0-9]{10,}"),
)

DANGEROUS_CALL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"subprocess\..*shell\s*=\s*True"),
    re.compile(r"os\.system\s*\("),
)

BARE_EXCEPT_PATTERN = re.compile(r"\bexcept\s*:")
TODO_MARKER_PATTERN = re.compile(r"\b(?:TODO|FIXME|XXX)\b")

SECURITY_SYSTEM_PROMPT = (
    "You are a senior application security reviewer inspecting a Git diff. "
    "Identify concrete security vulnerabilities introduced in added (+) lines: "
    "hardcoded secrets, injection flaws (SQL, command, path traversal, SSRF), "
    "unsafe deserialization, or broken auth/validation. "
    "Only cite file paths present in the changed files list and keep findings concise."
)

LOGIC_SYSTEM_PROMPT = (
    "You are a senior software engineer reviewing a Git diff for logic bugs and regressions. "
    "Identify concrete correctness issues in added (+) lines: missing None/key/bounds checks, "
    "swallowed exceptions, off-by-one or state bugs, or broken assumptions. "
    "Only cite file paths present in the changed files list and keep findings concise."
)


class FindingList(BaseModel):
    """Top-level wrapper required for reliable structured LLM output parsing."""

    findings: list[Finding] = Field(default_factory=list)


def _extract_changed_files(diff_text: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            rel_path = line[6:].strip()
            if rel_path and rel_path != "/dev/null" and rel_path not in seen:
                seen.add(rel_path)
                files.append(rel_path)
    return files


def _iter_added_lines(diff_text: str):
    """Yield (file_path, added_code_line) for newly added lines in a unified diff."""
    current_file = "<unknown>"
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            yield current_file, line[1:]


def _build_default_llm() -> Any | None:
    """Create a chat model only when an API key is configured in the environment."""
    if os.getenv("GROQ_API_KEY"):
        from langchain_groq import ChatGroq

        model_name = os.getenv("PR_GATEKEEPER_MODEL", "llama-3.3-70b-versatile")
        return ChatGroq(model=model_name, temperature=0)

    if os.getenv("OPENAI_API_KEY"):
        from langchain_openai import ChatOpenAI

        model_name = os.getenv("PR_GATEKEEPER_MODEL", "gpt-4o-mini")
        return ChatOpenAI(model=model_name, temperature=0)

    return None


def _run_llm_review(
    *,
    diff_text: str,
    changed_files: list[str],
    category: Literal["security", "logic"],
    system_prompt: str,
    llm: Any | None = None,
) -> list[Finding]:
    """Run structured LLM review when a client or supported API key is available."""
    if not diff_text.strip():
        return []

    client = llm if llm is not None else _build_default_llm()
    if client is None:
        return []

    structured_llm = client.with_structured_output(FindingList)
    bounded_diff = diff_text[:MAX_DIFF_CHARS]
    truncation_note = (
        "\n\n[Diff truncated to character budget for review]"
        if len(diff_text) > MAX_DIFF_CHARS
        else ""
    )
    user_prompt = (
        f"Changed files: {', '.join(changed_files) or 'none'}\n\n"
        f"Git diff:\n{bounded_diff}{truncation_note}"
    )

    response = structured_llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )

    raw_items = (
        response.findings
        if isinstance(response, FindingList)
        else FindingList.model_validate(response).findings
    )

    return [
        Finding(
            category=category,
            severity=item.severity,
            file_path=item.file_path,
            line_hint=item.line_hint[:MAX_LINE_HINT_CHARS],
            summary=item.summary,
            remediation=item.remediation,
        )
        for item in raw_items
    ]


def load_diff_node(state: GatekeeperState) -> dict[str, Any]:
    repo = Path(state.repo_path).resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"Not a valid git repository: {repo}")

    if ".." in state.target_ref:
        cmd = ["git", "-C", str(repo), "diff", state.target_ref]
    else:
        cmd = ["git", "-C", str(repo), "show", "--format=", state.target_ref]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Git diff failed: {proc.stderr.strip()}")

    diff_text = proc.stdout
    return {
        "diff_text": diff_text,
        "changed_files": _extract_changed_files(diff_text),
    }


def security_auditor_node(
    state: GatekeeperState,
    llm: Any | None = None,
) -> dict[str, Any]:
    findings: list[Finding] = []

    for file_path, added_code in _iter_added_lines(state.diff_text):
        stripped = added_code.strip()
        if any(pat.search(added_code) for pat in SECRET_PATTERNS):
            findings.append(
                Finding(
                    category="security",
                    severity="high",
                    file_path=file_path,
                    line_hint=stripped[:MAX_LINE_HINT_CHARS],
                    summary="Potential hardcoded secret or credential detected in diff.",
                    remediation="Move secrets to environment variables or a secret manager.",
                )
            )
            continue

        if any(pat.search(added_code) for pat in DANGEROUS_CALL_PATTERNS):
            findings.append(
                Finding(
                    category="security",
                    severity="high",
                    file_path=file_path,
                    line_hint=stripped[:MAX_LINE_HINT_CHARS],
                    summary="Potentially unsafe dynamic execution or shell=True invocation.",
                    remediation="Use safe argument lists and avoid dynamic eval/exec on untrusted input.",
                )
            )

    findings.extend(
        _run_llm_review(
            diff_text=state.diff_text,
            changed_files=state.changed_files,
            category="security",
            system_prompt=SECURITY_SYSTEM_PROMPT,
            llm=llm,
        )
    )

    return {"findings": findings}


def logic_reviewer_node(
    state: GatekeeperState,
    llm: Any | None = None,
) -> dict[str, Any]:
    findings: list[Finding] = []

    for file_path, added_code in _iter_added_lines(state.diff_text):
        stripped = added_code.strip()
        if BARE_EXCEPT_PATTERN.search(stripped):
            findings.append(
                Finding(
                    category="logic",
                    severity="medium",
                    file_path=file_path,
                    line_hint=stripped[:MAX_LINE_HINT_CHARS],
                    summary="Bare 'except:' swallows all exceptions including system/keyboard signals.",
                    remediation="Catch specific exception types and log context.",
                )
            )
        elif TODO_MARKER_PATTERN.search(stripped):
            findings.append(
                Finding(
                    category="logic",
                    severity="low",
                    file_path=file_path,
                    line_hint=stripped[:MAX_LINE_HINT_CHARS],
                    summary="Unresolved TODO/FIXME marker introduced in changed code.",
                    remediation="Complete the logic or track it in an issue before merge.",
                )
            )

    findings.extend(
        _run_llm_review(
            diff_text=state.diff_text,
            changed_files=state.changed_files,
            category="logic",
            system_prompt=LOGIC_SYSTEM_PROMPT,
            llm=llm,
        )
    )

    return {"findings": findings}