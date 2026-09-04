#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("mingbian_session.py")


def branch(
    branch_id: str,
    kind: str = "decision",
    *,
    parent_id: str | None = None,
    depends_on: list[str] | None = None,
    priority: int = 100,
) -> dict[str, object]:
    return {
        "id": branch_id,
        "kind": kind,
        "title": f"标题 {branch_id}",
        "question": f"问题 {branch_id}？",
        "recommendation": f"建议 {branch_id}",
        "rationale": f"理由 {branch_id}",
        "parent_id": parent_id,
        "depends_on": depends_on or [],
        "priority": priority,
    }


class MingbianSessionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / "sessions"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_json(self, name: str, payload: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, *args: str, expect: int = 0) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--store-dir", str(self.store), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, expect, completed.stderr or completed.stdout)
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        return json.loads(stream)

    def init(self, name: str, branches: list[dict[str, object]], *extra: str) -> dict[str, object]:
        tree = self.write_json(f"{name}-tree.json", {"branches": branches})
        return self.run_cli("init", "--session", name, "--tree", str(tree), *extra)

    def apply(self, name: str, operations: list[dict[str, object]], expect: int = 0) -> dict[str, object]:
        changes = self.write_json("changes.json", {"operations": operations})
        return self.run_cli("apply", "--session", name, "--changes", str(changes), expect=expect)

    def test_dependency_frontier_and_confirmation_gate(self) -> None:
        state = self.init(
            "workflow",
            [
                branch("D01", priority=1),
                branch("F01", "fact", parent_id="D01", priority=2),
                branch("D02", depends_on=["F01"], priority=3),
            ],
        )
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D01"])
        self.assertEqual([item["id"] for item in state["waiting"]], ["F01", "D02"])

        state = self.apply("workflow", [{"op": "lock", "id": "D01", "answer": "选择 A"}])
        self.assertEqual([item["id"] for item in state["facts_to_resolve"]], ["F01"])
        early = self.apply("workflow", [{"op": "lock", "id": "D02", "answer": "选择 B"}], expect=2)
        self.assertIn("前置分支尚未锁定", early["error"])

        no_evidence = self.apply(
            "workflow", [{"op": "lock", "id": "F01", "answer": "系统已支持"}], expect=2
        )
        self.assertIn("evidence", no_evidence["error"])
        state = self.apply(
            "workflow",
            [{"op": "lock", "id": "F01", "answer": "系统已支持", "evidence": ["配置回读"]}],
        )
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D02"])

        blocked = self.run_cli("confirm", "--session", "workflow", expect=2)
        self.assertIn("不能确认完成", blocked["error"])
        state = self.apply("workflow", [{"op": "lock", "id": "D02", "answer": "使用结果指标"}])
        self.assertTrue(state["can_confirm"])
        closed = self.run_cli("confirm", "--session", "workflow")
        self.assertEqual(closed["state"], "closed")
        self.assertFalse(closed["can_confirm"])
        self.assertEqual(
            [{"id": item["id"], "answer": item["answer"]} for item in closed["locked_decisions"]],
            [
                {"id": "D01", "answer": "选择 A"},
                {"id": "D02", "answer": "使用结果指标"},
            ],
        )
        self.assertEqual(closed["verified_facts"][0]["evidence"], ["配置回读"])

    def test_apply_is_atomic_and_duplicate_init_is_rejected(self) -> None:
        self.init("atomic", [branch("D01")])
        failed = self.apply(
            "atomic",
            [
                {"op": "lock", "id": "D01", "answer": "A"},
                {"op": "unknown", "id": "D01"},
            ],
            expect=2,
        )
        self.assertIn("不支持的操作", failed["error"])
        state = self.run_cli("status", "--session", "atomic")
        self.assertEqual(state["revision"], 1)
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D01"])
        duplicate_tree = self.write_json("duplicate-tree.json", {"branches": [branch("D99")]})
        duplicate = self.run_cli(
            "init", "--session", "atomic", "--tree", str(duplicate_tree), expect=2
        )
        self.assertFalse(duplicate["ok"])

    def test_invalidated_prerequisite_creates_orphan(self) -> None:
        self.init("orphan", [branch("D01"), branch("D02", parent_id="D01")])
        state = self.apply("orphan", [{"op": "invalidate", "id": "D01", "reason": "不在范围内"}])
        self.assertEqual([item["id"] for item in state["orphaned"]], ["D02"])
        self.assertFalse(state["can_confirm"])
        state = self.apply("orphan", [{"op": "invalidate", "id": "D02", "reason": "父分支已失效"}])
        self.assertTrue(state["can_confirm"])

    def test_frontier_mode_and_mode_switch(self) -> None:
        state = self.init(
            "frontier",
            [branch("D01", priority=1), branch("D02", priority=2), branch("D03", priority=3)],
            "--mode",
            "frontier",
            "--max-frontier",
            "2",
        )
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D01", "D02"])
        self.assertEqual([item["id"] for item in state["additional_frontier_decisions"]], ["D03"])
        state = self.run_cli("set-mode", "--session", "frontier", "--mode", "sequential")
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D01"])

    def test_unresolved_fact_does_not_block_independent_decision_questions(self) -> None:
        state = self.init(
            "fact-first",
            [branch("F01", "fact", priority=1), branch("D01", priority=2)],
        )
        self.assertEqual([item["id"] for item in state["facts_to_resolve"]], ["F01"])
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D01"])
        self.assertEqual(state["additional_frontier_decisions"], [])
        state = self.apply(
            "fact-first",
            [{"op": "lock", "id": "F01", "answer": "已核实", "evidence": ["当前配置回读"]}],
        )
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D01"])

    def test_cycle_and_unknown_branch_are_rejected(self) -> None:
        tree = self.write_json(
            "cycle.json",
            {"branches": [branch("D01", depends_on=["D02"]), branch("D02", depends_on=["D01"])]},
        )
        result = self.run_cli("validate-tree", "--tree", str(tree), expect=2)
        self.assertIn("存在环", result["error"])
        self.init("unknown", [branch("D01")])
        result = self.apply("unknown", [{"op": "lock", "id": "D99", "answer": "A"}], expect=2)
        self.assertIn("分支不存在", result["error"])

    def test_status_exposes_resolved_context_for_resume(self) -> None:
        state = self.init(
            "resume",
            [
                branch("D01", priority=1),
                branch("F01", "fact", depends_on=["D01"], priority=2),
                branch("D02", depends_on=["F01"], priority=3),
                branch("D03", priority=4),
            ],
        )
        self.assertEqual(state["plan_source"], {"type": "conversation", "label": "当前对话"})

        state = self.apply("resume", [{"op": "lock", "id": "D01", "answer": "先做 A"}])
        self.assertEqual(
            [{"id": item["id"], "answer": item["answer"]} for item in state["locked_decisions"]],
            [{"id": "D01", "answer": "先做 A"}],
        )

        state = self.apply(
            "resume",
            [
                {
                    "op": "lock",
                    "id": "F01",
                    "answer": "当前系统支持 A",
                    "evidence": ["配置回读：feature_a=true"],
                },
                {"op": "invalidate", "id": "D03", "reason": "移出当前范围"},
            ],
        )
        self.assertEqual(state["verified_facts"][0]["id"], "F01")
        self.assertEqual(state["verified_facts"][0]["evidence"], ["配置回读：feature_a=true"])
        self.assertEqual(
            [{"id": item["id"], "reason": item["reason"]} for item in state["invalidated"]],
            [{"id": "D03", "reason": "移出当前范围"}],
        )
        self.assertEqual([item["id"] for item in state["ask_now"]], ["D02"])


if __name__ == "__main__":
    unittest.main()
