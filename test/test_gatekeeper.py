from __future__ import annotations

import subprocess
from pathlib import Path

from langgraph.types import Command

from pr_gatekeeper.graph import build_gatekeeper_graph
from pr_gatekeeper.state import GatekeeperState
from pr_gatekeeper.nodes import FindingList, logic_reviewer_node, security_auditor_node
from pr_gatekeeper.state import Finding


class _FakeStructuredLLM:
    def __init__(self, payload: FindingList) -> None:
        self._payload = payload

    def invoke(self, _messages):
        return self._payload


class _FakeChatModel:
    def __init__(self, payload: FindingList) -> None:
        self._payload = payload

    def with_structured_output(self, _schema):
        return _FakeStructuredLLM(self._payload)


def test_specialist_nodes_merge_deterministic_and_llm_findings() -> None:
    state = GatekeeperState(
        repo_path=".",
        target_ref="HEAD",
        changed_files=["billing.py"],
        diff_text=(
            "--- a/billing.py\n"
            "+++ b/billing.py\n"
            "@@ -1,2 +1,3 @@\n"
            "+def apply_discount(order):\n"
            "+    return order['discount_tier']\n"
        ),
    )

    security_llm = _FakeChatModel(FindingList(findings=[]))
    logic_llm = _FakeChatModel(
        FindingList(
            findings=[
                Finding(
                    category="logic",
                    severity="medium",
                    file_path="billing.py",
                    line_hint="return order['discount_tier']",
                    summary="Direct dict key access may raise KeyError if discount_tier is absent.",
                    remediation="Use order.get('discount_tier', 'standard') or validate the key.",
                )
            ]
        )
    )

    sec_out = security_auditor_node(state, llm=security_llm)
    logic_out = logic_reviewer_node(state, llm=logic_llm)

    assert sec_out["findings"] == []
    assert len(logic_out["findings"]) == 1
    assert logic_out["findings"][0].category == "logic"
    assert logic_out["findings"][0].file_path == "billing.py"
    assert logic_out["findings"][0].severity == "medium"

def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Dev User"], cwd=repo, check=True, capture_output=True)


def test_gatekeeper_blocks_secret_and_resumes_on_approve(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    app_file = tmp_path / "config.py"
    app_file.write_text("DEBUG = False\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=tmp_path, check=True, capture_output=True)

    app_file.write_text("DEBUG = False\napi_key = 'sk-test-secret-123456'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: add config"], cwd=tmp_path, check=True, capture_output=True)

    app = build_gatekeeper_graph()
    config = {"configurable": {"thread_id": "test-thread-1"}}
    state = GatekeeperState(repo_path=str(tmp_path), target_ref="HEAD")

    paused = app.invoke(state, config=config)
    assert "__interrupt__" in paused
    assert paused["verdict"] == "REQUEST_CHANGES"

    resumed = app.invoke(Command(resume="approve"), config=config)
    assert "__interrupt__" not in resumed
    assert resumed["verdict"] == "APPROVE"
    assert resumed["human_decision"] == "approve"
    assert "config.py" in resumed["final_review"]


