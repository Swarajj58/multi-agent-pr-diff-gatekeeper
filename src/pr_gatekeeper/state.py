from __future__ import annotations

import operator
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Verdict = Literal["APPROVE", "REQUEST_CHANGES", "NEEDS_HUMAN_REVIEW"]


class Finding(BaseModel):
    category: Literal["security", "logic"]
    severity: Literal["low", "medium", "high"]
    file_path: str
    line_hint: str
    summary: str
    remediation: str


class GatekeeperState(BaseModel):
    # Input & context
    repo_path: str
    target_ref: str
    diff_text: str = ""
    changed_files: list[str] = Field(default_factory=list)

    # Parallel fan-out/in fields (operator.add merges lists across parallel branches)
    findings: Annotated[list[Finding], operator.add] = Field(default_factory=list)

    # Critic & loop control
    critic_notes: str = ""
    verified_findings: list[Finding] = Field(default_factory=list)
    revision_count: int = 0
    max_revisions: int = 2

    # Final decision & human approval
    verdict: Verdict = "NEEDS_HUMAN_REVIEW"
    human_decision: str = ""
    final_review: str = ""