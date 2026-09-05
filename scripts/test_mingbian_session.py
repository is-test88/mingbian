#!/usr/bin/env python3
"""可重复的结构/状态回归；不冒充大模型行为评测。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import mingbian_session as m

SCRIPT = Path(m.__file__)


def branch(identity="D01", kind="decision", **extra):
    result = {"id": identity, "kind": kind, "title": f"标题 {identity}", "question": f"问题 {identity}？",
              "recommendation": f"建议 {identity}", "rationale": "会改变本阶段的行动与验收", "priority": 100}
    result.update(extra)
    return result


def lock(identity="D01", kind="decision", **extra):
    op = {"op": "lock", "id": identity, "answer": "采用当前方案",
          "basis": "verified" if kind == "fact" else "user_decision", "source": "测试中明确给定的来源"}
    if kind == "fact":
        op["evidence"] = ["测试固定数据：feature=true"]
    op.update(extra)
    return op


def default(identity="D01", **extra):
    op = {"op": "default", "id": identity, "answer": "先按 5 条分页", "reason": "当前内容少",
          "low_risk": True, "reversible": True, "boundary": "仅原型，不影响真实数据", "reversal": "更改展示配置即可"}
    op.update(extra)
    return op


def assume(identity="F01", **extra):
    op = {"op": "assume", "id": identity, "answer": "现有样本足以验证原型",
          "owner": "建议由测试负责人验证，尚未指派", "trigger": "试验开始前",
          "method": "检查脱敏样本的覆盖范围", "pass_condition": "所有关键路径均有样本",
          "fail_action": "补齐样本前不开始试验", "boundary": "仅离线原型，不写生产"}
    op.update(extra)
    return op


def defer(identity="D01", **extra):
    op = {"op": "defer", "id": identity, "reason": "不影响方向判断",
          "owner": "产品负责人（待指派）", "trigger": "进入试点前",
          "basis_for_decision": "验证结果与实际资源", "interim_action": "不进入相关执行步骤"}
    op.update(extra)
    return op


def checks(stage="execution"):
    return {"stage": stage, "checks": {d: {"status": "pass", "evidence": f"测试明辨录的 {d} 部分已有明确内容"}
                                       for d in m.DIMENSIONS},
            "next_action": {"action": "做离线验证", "owner": "测试负责人（测试设定已确认）", "done_when": "产出验证结果"}}


class PureStateTests(unittest.TestCase):
    def session(self, *branches, **kwargs):
        return m.new_session("test", [m.normalize_branch(b) for b in branches], **kwargs)

    def apply(self, session, *ops):
        return m.apply_operations(session, {"operations": list(ops)})

    def test_zero_question_is_supported_but_not_auto_confirmed(self):
        state = m.classify(self.session())
        self.assertTrue(state["graph_resolved"])
        self.assertFalse(state["can_confirm"])
        self.assertEqual(state["ask_now"], [])

    def test_initial_fact_does_not_block_independent_decision(self):
        state = m.classify(self.session(branch("F01", "fact", priority=1), branch()))
        self.assertEqual(state["facts_to_resolve"][0]["id"], "F01")
        self.assertEqual(state["ask_now"][0]["id"], "D01")

    def test_dependency_frontier(self):
        session = self.session(branch(), branch("F01", "fact", parent_id="D01"), branch("D02", depends_on=["F01"]))
        session = self.apply(session, lock())
        self.assertEqual(m.classify(session)["facts_to_resolve"][0]["id"], "F01")
        with self.assertRaises(m.UserError):
            self.apply(session, lock("D02"))
        session = self.apply(session, lock("F01", "fact"))
        self.assertEqual(m.classify(session)["ask_now"][0]["id"], "D02")

    def test_fact_requires_evidence(self):
        with self.assertRaises(m.UserError):
            self.apply(self.session(branch("F01", "fact")), lock("F01", "fact", evidence=[]))

    def test_decision_requires_explicit_basis(self):
        with self.assertRaises(m.UserError):
            self.apply(self.session(branch()), lock(basis="preference"))

    def test_decision_requires_source(self):
        with self.assertRaises(m.UserError):
            self.apply(self.session(branch()), lock(source=" "))

    def test_reported_fact_not_verified(self):
        session = self.apply(self.session(branch("F01", "fact")), lock("F01", "fact", basis="user_reported"))
        state = m.classify(session)
        self.assertEqual(state["verified_facts"], [])
        self.assertEqual(len(state["user_reported_facts"]), 1)

    def test_default_is_not_locked_decision(self):
        state = m.classify(self.apply(self.session(branch()), default()))
        self.assertEqual(state["locked_decisions"], [])
        self.assertEqual(len(state["defaults"]), 1)
        self.assertTrue(state["graph_resolved"])

    def test_default_requires_low_risk_and_reversal(self):
        for change in ({"low_risk": False}, {"reversible": False}, {"reversal": ""}):
            with self.subTest(change=change), self.assertRaises(m.UserError):
                self.apply(self.session(branch()), default(**change))

    def test_fact_cannot_be_defaulted(self):
        with self.assertRaises(m.UserError):
            self.apply(self.session(branch("F01", "fact")), default("F01"))

    def test_assumption_never_becomes_verified_fact(self):
        state = m.classify(self.apply(self.session(branch("F01", "fact")), assume()))
        self.assertEqual(state["verified_facts"], [])
        self.assertEqual(len(state["assumptions"]), 1)
        self.assertIn("未记录", state["assumptions"][0]["resolution"]["acceptance"])

    def test_assumption_requires_validation_and_failure_route(self):
        for field in ("owner", "trigger", "method", "pass_condition", "fail_action", "boundary"):
            with self.subTest(field=field), self.assertRaises(m.UserError):
                self.apply(self.session(branch("F01", "fact")), assume(**{field: ""}))

    def test_assumption_can_unlock_guarded_dependent(self):
        state = m.classify(self.apply(self.session(branch("F01", "fact"), branch(depends_on=["F01"])), assume()))
        self.assertEqual(state["ask_now"][0]["id"], "D01")

    def test_deferred_leaf_is_explicit(self):
        state = m.classify(self.apply(self.session(branch()), defer()))
        self.assertTrue(state["graph_resolved"])
        self.assertEqual(len(state["deferred"]), 1)
        self.assertFalse(state["can_confirm"])

    def test_defer_does_not_satisfy_hard_dependency(self):
        session = self.apply(self.session(branch(), branch("D02", depends_on=["D01"])), defer())
        self.assertFalse(m.classify(session)["graph_resolved"])
        self.assertEqual(m.classify(session)["waiting"][0]["id"], "D02")
        with self.assertRaises(m.UserError):
            self.apply(session, lock("D02"))

    def test_defer_requires_trigger_and_interim_action(self):
        for field in ("trigger", "owner", "basis_for_decision", "interim_action"):
            with self.subTest(field=field), self.assertRaises(m.UserError):
                self.apply(self.session(branch()), defer(**{field: ""}))

    def test_reopen_cascades_even_when_relocked_in_same_batch(self):
        session = self.session(branch(), branch("D02", depends_on=["D01"]), branch("D03", depends_on=["D02"]))
        session = self.apply(session, lock(), lock("D02"), lock("D03"))
        session = self.apply(session, {"op": "reopen", "id": "D01", "reason": "首期范围变了"}, lock(answer="改为方案 B"))
        statuses = {b["id"]: b["status"] for b in session["branches"]}
        self.assertEqual(statuses, {"D01": "locked", "D02": "open", "D03": "open"})
        self.assertEqual(session["branches"][1]["history"][-1]["previous"]["answer"], "采用当前方案")

    def test_reopen_leaves_independent_decision_locked(self):
        session = self.apply(self.session(branch(), branch("D02")), lock(), lock("D02"))
        session = self.apply(session, {"op": "reopen", "id": "D01", "reason": "目标纠正"})
        self.assertEqual(session["branches"][1]["status"], "locked")

    def test_invalidation_orphans_descendants_transitively(self):
        session = self.session(branch(), branch("D02", parent_id="D01"), branch("D03", depends_on=["D02"]))
        state = m.classify(self.apply(session, {"op": "invalidate", "id": "D01", "reason": "移出范围"}))
        self.assertEqual({b["id"] for b in state["orphaned"]}, {"D02", "D03"})

    def test_revision_rejects_locked_branch_edits(self):
        session = self.apply(self.session(branch()), lock())
        with self.assertRaises(m.UserError):
            self.apply(session, {"op": "revise", "id": "D01", "reason": "修改", "fields": {"question": "新问题"}})

    def test_revise_cannot_change_identity(self):
        with self.assertRaises(m.UserError):
            self.apply(self.session(branch()), {"op": "revise", "id": "D01", "reason": "修改", "fields": {"id": "D02"}})

    def test_cycle_rejected(self):
        with self.assertRaisesRegex(m.UserError, "存在环"):
            self.session(branch(depends_on=["D02"]), branch("D02", depends_on=["D01"]))

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(m.UserError):
            self.session(branch(depends_on=["D99"]))

    def test_duplicate_ids_and_dependencies_rejected(self):
        with self.assertRaises(m.UserError):
            self.session(branch(), branch())
        with self.assertRaises(m.UserError):
            self.session(branch(), branch("D02", depends_on=["D01", "D01"]))

    def test_self_dependency_rejected(self):
        with self.assertRaises(m.UserError):
            self.session(branch(depends_on=["D01"]))

    def test_boolean_priority_rejected(self):
        with self.assertRaises(m.UserError):
            self.session(branch(priority=True))

    def test_initial_state_not_silently_discarded(self):
        with self.assertRaises(m.UserError):
            self.session(branch(status="locked", answer="A"))

    def test_context_question_uses_same_single_question_budget(self):
        session = self.session(branch("F01", "fact", handler="user", lookup_note="已查现有材料，无硬截止日期", priority=1), branch())
        state = m.classify(session)
        self.assertEqual([b["id"] for b in state["ask_now"]], ["F01"])
        self.assertEqual(state["facts_to_resolve"], [])
        self.assertEqual(len(state["additional_frontier_decisions"]), 1)

    def test_context_question_requires_lookup_note(self):
        with self.assertRaises(m.UserError):
            self.session(branch("F01", "fact", handler="user"))

    def test_authorization_record_never_grants_execution(self):
        session = self.session(branch("A01", "authorization"))
        session = self.apply(session, lock("A01", basis="authorization", scope="仅发送给指定测试收件人"))
        state = m.classify(session)
        self.assertEqual(len(state["authorizations"]), 1)
        self.assertFalse(state["external_execution_authorized"])

    def test_authorization_requires_exact_scope(self):
        with self.assertRaises(m.UserError):
            self.apply(self.session(branch("A01", "authorization")), lock("A01", basis="authorization"))

    def test_frontier_respects_limit_and_dependencies(self):
        state = m.classify(self.session(branch("D01", priority=1), branch("D02", priority=2),
                                       branch("D03", priority=3), branch("D04", depends_on=["D01"]), mode="frontier", maximum=2))
        self.assertEqual([b["id"] for b in state["ask_now"]], ["D01", "D02"])
        self.assertEqual([b["id"] for b in state["waiting"]], ["D04"])

    def test_invalid_batch_is_atomic_in_memory(self):
        session = self.session(branch())
        snapshot = copy.deepcopy(session)
        with self.assertRaises(m.UserError):
            self.apply(session, lock(), {"op": "unknown", "id": "D01"})
        self.assertEqual(snapshot, session)

    def test_direction_review_may_skip_implementation_with_reason(self):
        review = checks("direction")
        for d in ("xu", "fa"):
            review["checks"][d] = {"status": "not_applicable", "evidence": "仅比较方向，不进入任何执行"}
        self.assertEqual(m.validate_checks(review, "direction"), review)

    def test_execution_review_cannot_skip_implementation(self):
        review = checks()
        review["checks"]["fa"]["status"] = "not_applicable"
        with self.assertRaises(m.UserError):
            m.validate_checks(review, "execution")

    def test_review_requires_evidence_and_next_action(self):
        review = checks()
        review["checks"]["ben"]["evidence"] = ""
        with self.assertRaises(m.UserError):
            m.validate_checks(review, "execution")
        review = checks()
        del review["next_action"]
        with self.assertRaises(m.UserError):
            m.validate_checks(review, "execution")


    def test_malformed_types_return_user_errors(self):
        for field, value in (("status", []), ("mode", []), ("target_stage", {})):
            session = self.session(branch())
            session[field] = value
            with self.subTest(field=field), self.assertRaises(m.UserError):
                m.validate_session(session)
        for field, value in (("handler", []), ("lookup_note", []), ("status", {})):
            session = self.session(branch())
            session["branches"][0][field] = value
            with self.subTest(field=field), self.assertRaises(m.UserError):
                m.validate_session(session)

    def test_persisted_branch_cannot_omit_dependency_fields(self):
        session = self.session(branch())
        del session["branches"][0]["depends_on"]
        with self.assertRaises(m.UserError):
            m.validate_session(session)

class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / "sessions"
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def write(self, payload, suffix="json"):
        self.counter += 1
        path = self.root / f"input-{self.counter}.{suffix}"
        path.write_text(json.dumps(payload, ensure_ascii=False) if suffix == "json" else payload, encoding="utf-8")
        return path

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "--store-dir", str(self.store), *map(str, args)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr or result.stdout)
        return json.loads(result.stdout if success else result.stderr)

    def init(self, branches=None, *extra):
        path = self.write({"branches": [branch()] if branches is None else branches})
        return self.cli("init", "--session", "test", "--tree", path, *extra)

    def apply(self, ops, revision=1, success=True):
        payload = {"operations": ops}
        if revision is not None:
            payload["expected_revision"] = revision
        return self.cli("apply", "--session", "test", "--changes", self.write(payload), success=success)

    def review(self, revision, stage="execution", success=True):
        return self.cli("review", "--session", "test", "--expected-revision", revision,
                        "--record", self.write("# 明辨录\n\n这是测试固定交付正文，不是实际业务就绪证明。", "md"),
                        "--checks", self.write(checks(stage)), success=success)

    def test_complete_review_confirm_reopen_lifecycle(self):
        self.init()
        self.apply([lock()])
        self.assertTrue(self.review(2)["can_confirm"])
        result = self.cli("confirm", "--session", "test", "--expected-revision", 3,
                          "--confirmation", "测试模拟：用户明确确认此版内容")
        self.assertEqual(result["state"], "closed")
        self.assertFalse(result["can_confirm"])
        self.assertFalse(result["external_execution_authorized"])
        result = self.cli("reopen-session", "--session", "test", "--expected-revision", 4, "--reason", "新增条件")
        self.assertEqual(result["state"], "active")
        self.assertEqual(len(result["locked_decisions"]), 1)
        self.assertFalse(result["review_current"])

    def test_graph_clear_does_not_allow_confirmation_without_record(self):
        self.init([])
        result = self.cli("confirm", "--session", "test", "--expected-revision", 1,
                          "--confirmation", "测试中的确认文本", success=False)
        self.assertIn("复核", result["error"])
        self.assertTrue(self.review(1)["can_confirm"])

    def test_review_rejects_open_branch(self):
        self.init()
        result = self.review(1, success=False)
        self.assertIn("未解决", result["error"])

    def test_apply_invalidates_existing_review(self):
        self.init([])
        self.review(1)
        result = self.apply([{"op": "add", "branch": branch()}], 2)
        self.assertFalse(result["review_current"])
        self.assertFalse(result["can_confirm"])

    def test_stale_revision_rejected_without_modifying_file(self):
        self.init()
        self.apply([lock()])
        path = self.store / "test.json"
        before = path.read_bytes()
        result = self.apply([{"op": "add", "branch": branch("D02")}], 1, success=False)
        self.assertIn("版本冲突", result["error"])
        self.assertEqual(before, path.read_bytes())

    def test_missing_expected_revision_rejected(self):
        self.init()
        result = self.apply([lock()], None, success=False)
        self.assertIn("expected_revision", result["error"])

    def test_revision_cli_and_file_must_match(self):
        self.init()
        changes = self.write({"expected_revision": 1, "operations": [lock()]})
        result = self.cli("apply", "--session", "test", "--changes", changes, "--expected-revision", 2, success=False)
        self.assertIn("不一致", result["error"])

    def test_invalid_batch_does_not_change_disk(self):
        self.init()
        path = self.store / "test.json"
        before = path.read_bytes()
        self.apply([lock(), {"op": "unknown", "id": "D01"}], success=False)
        self.assertEqual(before, path.read_bytes())

    def test_duplicate_init_cannot_overwrite(self):
        self.init()
        before = (self.store / "test.json").read_bytes()
        self.cli("init", "--session", "test", "--tree", self.write({"branches": []}), success=False)
        self.assertEqual(before, (self.store / "test.json").read_bytes())

    def test_source_drift_detected_and_rebase_reopens(self):
        plan = self.write("原始方案 A", "md")
        self.init(None, "--plan", plan)
        self.apply([lock()])
        plan.write_text("变更为方案 B", encoding="utf-8")
        self.assertEqual(self.cli("status", "--session", "test")["source_check"]["state"], "changed")
        self.apply([{"op": "add", "branch": branch("D02")}], 2, success=False)
        result = self.cli("rebase", "--session", "test", "--expected-revision", 2, "--reason", "用户修订方案")
        self.assertEqual(result["source_check"]["state"], "unchanged")
        self.assertEqual(result["locked_decisions"], [])
        self.assertEqual(result["ask_now"][0]["id"], "D01")

    def test_unavailable_source_blocks_confirmation(self):
        plan = self.write("完整方案", "md")
        self.init([], "--plan", plan)
        self.review(1)
        plan.unlink()
        state = self.cli("status", "--session", "test")
        self.assertEqual(state["source_check"]["state"], "unavailable")
        self.assertFalse(state["can_confirm"])
        self.cli("confirm", "--session", "test", "--expected-revision", 2, "--confirmation", "测试确认", success=False)

    def test_lock_contention_is_not_overridden(self):
        self.init()
        lock_path = self.store / "test.lock"
        lock_path.write_text("held", encoding="utf-8")
        result = self.apply([lock()], success=False)
        self.assertIn("锁", result["error"])
        self.assertEqual(lock_path.read_text(), "held")
        self.assertEqual(self.cli("status", "--session", "test")["revision"], 1)

    def test_concurrent_writers_cannot_overwrite_each_other(self):
        self.init([branch(), branch("D02")])
        paths = [self.write({"expected_revision": 1, "operations": [lock(identity)]}) for identity in ("D01", "D02")]
        processes = [subprocess.Popen([sys.executable, str(SCRIPT), "--store-dir", str(self.store), "apply", "--session", "test",
                                       "--changes", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for path in paths]
        results = [process.communicate(timeout=15) for process in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 2], results)
        state = self.cli("status", "--session", "test")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(len(state["locked_decisions"]), 1)
        self.assertEqual(state["counts"]["unresolved"], 1)

    def test_lock_cleaned_up_after_failed_write(self):
        self.init()
        self.apply([{"op": "unknown", "id": "D01"}], success=False)
        self.assertFalse((self.store / "test.lock").exists())
        self.apply([lock()])

    def test_export_distinguishes_defaults_and_does_not_overwrite(self):
        self.init()
        self.apply([default()])
        output = self.root / "record.md"
        self.cli("export", "--session", "test", "--output", output)
        body = output.read_text(encoding="utf-8")
        self.assertIn("透明默认（非用户决策）", body)
        self.assertIn("尚未完成", body)
        self.cli("export", "--session", "test", "--output", output, success=False)
        self.assertEqual(body, output.read_text(encoding="utf-8"))

    def test_export_includes_remaining_frontier(self):
        self.init([branch(), branch("D02"), branch("D03")])
        output = self.root / "record.md"
        self.cli("export", "--session", "test", "--output", output)
        body = output.read_text(encoding="utf-8")
        self.assertIn("D02", body)
        self.assertIn("D03", body)

    def test_export_of_stale_record_warns(self):
        plan = self.write("方案 A", "md")
        self.init([], "--plan", plan)
        self.review(1)
        plan.write_text("方案 B", encoding="utf-8")
        output = self.root / "record.md"
        self.cli("export", "--session", "test", "--output", output)
        self.assertIn("来源已变化", output.read_text(encoding="utf-8"))

    def test_record_integrity_is_checked(self):
        self.init([])
        self.review(1)
        path = self.store / "test.json"
        session = json.loads(path.read_text())
        session["review"]["record_text"] += "篡改"
        path.write_text(json.dumps(session), encoding="utf-8")
        result = self.cli("status", "--session", "test", success=False)
        self.assertIn("摘要不一致", result["error"])

    def test_schema2_explicit_migration_and_exact_backup(self):
        self.init()
        self.apply([lock()])
        path = self.store / "test.json"
        legacy = json.loads(path.read_text())
        legacy["schema_version"] = 2
        legacy.pop("review")
        path.write_text(json.dumps(legacy, ensure_ascii=False, indent=4), encoding="utf-8")
        before = path.read_bytes()
        state = self.cli("status", "--session", "test")
        self.assertEqual(state["state"], "migration_required")
        self.assertEqual(before, path.read_bytes())
        state = self.cli("migrate", "--session", "test", "--expected-revision", 2)
        self.assertEqual((self.store / "test.schema2.bak").read_bytes(), before)
        self.assertEqual(state["revision"], 3)
        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(state["legacy_context"][0]["previous_answer"], "采用当前方案")
        self.assertEqual(state["locked_decisions"], [])
        self.assertFalse(state["can_confirm"])

    def test_migrate_refuses_to_overwrite_existing_backup(self):
        self.init()
        path = self.store / "test.json"
        legacy = json.loads(path.read_text())
        legacy["schema_version"] = 2
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        backup = self.store / "test.schema2.bak"
        backup.write_bytes(b"existing backup")
        self.cli("migrate", "--session", "test", "--expected-revision", 1, success=False)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(backup.read_bytes(), b"existing backup")

    def test_closed_session_cannot_be_modified(self):
        self.init([])
        self.review(1)
        self.cli("confirm", "--session", "test", "--expected-revision", 2, "--confirmation", "测试确认")
        self.apply([{"op": "add", "branch": branch()}], 3, success=False)

    def test_path_traversal_rejected(self):
        self.cli("status", "--session", "../outside", success=False)

    @unittest.skipUnless(os.name == "posix", "POSIX 权限测试")
    def test_private_file_and_directory_modes(self):
        self.init()
        self.assertEqual((self.store / "test.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(hasattr(os, "symlink"), "符号链接测试")
    def test_symlink_session_rejected(self):
        self.store.mkdir()
        target = self.write({"secret": "not a session"})
        (self.store / "test.json").symlink_to(target)
        before = target.read_bytes()
        self.cli("status", "--session", "test", success=False)
        self.assertEqual(before, target.read_bytes())

    def test_duplicate_json_keys_and_nonfinite_values_rejected(self):
        for content in ('{"branches": [], "branches": []}', '{"branches": [], "bad": NaN}'):
            path = self.write(content, "txt")
            self.cli("validate-tree", "--tree", path, success=False)

    def test_corrupted_json_is_reported(self):
        self.store.mkdir()
        (self.store / "test.json").write_text("not json", encoding="utf-8")
        result = self.cli("status", "--session", "test", success=False)
        self.assertIn("无法读取 JSON", result["error"])

    def test_set_mode_switches_frontier_without_overasking(self):
        self.init([branch(), branch("D02"), branch("D03")])
        result = self.cli("set-mode", "--session", "test", "--expected-revision", 1, "--mode", "frontier", "--max-frontier", 2)
        self.assertEqual(len(result["ask_now"]), 2)
        result = self.cli("set-mode", "--session", "test", "--expected-revision", 2, "--mode", "sequential")
        self.assertEqual(len(result["ask_now"]), 1)

    def test_invalid_mode_limit_leaves_file_unchanged(self):
        self.init()
        before = (self.store / "test.json").read_bytes()
        self.cli("set-mode", "--session", "test", "--expected-revision", 1, "--mode", "frontier", "--max-frontier", 4, success=False)
        self.assertEqual(before, (self.store / "test.json").read_bytes())

    def test_list_handles_invalid_file_without_hiding_valid_session(self):
        self.init()
        (self.store / "bad.json").write_text("broken", encoding="utf-8")
        result = self.cli("list")
        self.assertEqual(len(result["sessions"]), 2)
        self.assertTrue(any("error" in item for item in result["sessions"]))


if __name__ == "__main__":
    unittest.main()
