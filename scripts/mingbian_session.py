#!/usr/bin/env python3
"""明辨 v3.1：校验最小辨题图，原子保存会话，并计算当前前沿。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 2
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
KINDS = {"decision", "fact"}
STATUSES = {"open", "locked", "invalidated", "blocked"}
TEXT_FIELDS = ("id", "kind", "title", "question", "recommendation", "rationale")
REVISABLE_FIELDS = {
    "kind",
    "title",
    "question",
    "recommendation",
    "rationale",
    "parent_id",
    "depends_on",
    "priority",
}


class UserError(Exception):
    """可向调用者直接展示的输入或状态错误。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nonempty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UserError(f"{field} 必须是非空字符串")
    return value.strip()


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise UserError(f"文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise UserError(f"JSON 无效：{path}（{exc}）") from exc


def ensure_store_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise UserError(f"会话目录不是文件夹：{path}")
        return
    path.mkdir(parents=True, mode=0o700)


def write_temp_json(path: Path, payload: dict[str, Any]) -> str:
    ensure_store_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return temp_name
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_name = write_temp_json(path, payload)
    try:
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_json_new(path: Path, payload: dict[str, Any]) -> None:
    """原子创建新文件；目标已存在时绝不覆盖。"""
    temp_name = write_temp_json(path, payload)
    try:
        os.link(temp_name, path)
        os.chmod(path, 0o600)
    except FileExistsError as exc:
        raise UserError(f"同名会话已存在，不会覆盖：{path}") from exc
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def resolve_store_dir(raw: str | None) -> Path:
    """解析明辨簿目录。"""
    if raw:
        return Path(raw).expanduser().resolve()

    configured = os.environ.get("MINGBIAN_SESSIONS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    return Path("~/.mingbian_sessions").expanduser().resolve()


def session_path(store_dir: Path, name: str) -> Path:
    if not SESSION_RE.fullmatch(name):
        raise UserError("会话名只能包含字母、数字、点、下划线和连字符，长度不超过 80")
    return store_dir / f"{name}.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise UserError(f"方案文件不存在：{path}") from exc
    return digest.hexdigest()


def normalize_branch(raw: Any, created_at: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UserError("每个分支必须是 JSON 对象")
    branch = {field: nonempty_text(raw.get(field), field) for field in TEXT_FIELDS}
    if branch["kind"] not in KINDS:
        raise UserError(f"分支 {branch['id']} 的 kind 只能是 decision 或 fact")

    parent_id = raw.get("parent_id")
    if parent_id is not None:
        parent_id = nonempty_text(parent_id, f"{branch['id']}.parent_id")
    depends_on = raw.get("depends_on", [])
    if not isinstance(depends_on, list) or any(not isinstance(item, str) or not item.strip() for item in depends_on):
        raise UserError(f"分支 {branch['id']} 的 depends_on 必须是字符串数组")
    depends_on = [item.strip() for item in depends_on]
    if len(depends_on) != len(set(depends_on)):
        raise UserError(f"分支 {branch['id']} 的 depends_on 不能重复")

    priority = raw.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise UserError(f"分支 {branch['id']} 的 priority 必须是整数")

    branch.update(
        {
            "parent_id": parent_id,
            "depends_on": depends_on,
            "priority": priority,
            "status": "open",
            "answer": None,
            "evidence": [],
            "reason": None,
            "created_at": created_at,
            "updated_at": created_at,
            "history": [],
        }
    )
    return branch


def prerequisites(branch: dict[str, Any]) -> list[str]:
    result = list(branch["depends_on"])
    if branch["parent_id"] is not None and branch["parent_id"] not in result:
        result.append(branch["parent_id"])
    return result


def validate_branches(branches: Any) -> list[dict[str, Any]]:
    if not isinstance(branches, list) or not branches:
        raise UserError("branches 必须是非空数组")
    if any(not isinstance(branch, dict) for branch in branches):
        raise UserError("每个分支必须是 JSON 对象")

    ids = [branch.get("id") for branch in branches]
    if any(not isinstance(item, str) or not item for item in ids):
        raise UserError("每个分支都必须有非空 id")
    if len(ids) != len(set(ids)):
        raise UserError("分支 id 必须唯一")

    by_id = {branch["id"]: branch for branch in branches}
    for branch in branches:
        if branch.get("kind") not in KINDS:
            raise UserError(f"分支 {branch['id']} 的 kind 无效")
        for field in ("title", "question", "recommendation", "rationale"):
            nonempty_text(branch.get(field), f"{branch['id']}.{field}")
        if branch.get("status") not in STATUSES:
            raise UserError(f"分支 {branch['id']} 的 status 无效")
        parent_id = branch.get("parent_id")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.strip()):
            raise UserError(f"分支 {branch['id']} 的 parent_id 必须是非空字符串或 null")
        depends_on = branch.get("depends_on")
        if not isinstance(depends_on, list) or any(
            not isinstance(item, str) or not item.strip() for item in depends_on
        ):
            raise UserError(f"分支 {branch['id']} 的 depends_on 必须是字符串数组")
        if len(depends_on) != len(set(depends_on)):
            raise UserError(f"分支 {branch['id']} 的 depends_on 不能重复")
        priority = branch.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise UserError(f"分支 {branch['id']} 的 priority 必须是整数")
        if not isinstance(branch.get("history"), list):
            raise UserError(f"分支 {branch['id']} 的 history 必须是数组")
        for dependency in prerequisites(branch):
            if dependency == branch["id"]:
                raise UserError(f"分支 {branch['id']} 不能依赖自身")
            if dependency not in by_id:
                raise UserError(f"分支 {branch['id']} 引用了不存在的依赖 {dependency}")

        status = branch["status"]
        if status == "locked":
            nonempty_text(branch.get("answer"), f"{branch['id']}.answer")
            if branch["kind"] == "fact":
                evidence = branch.get("evidence")
                if not isinstance(evidence, list) or not evidence or any(
                    not isinstance(item, str) or not item.strip() for item in evidence
                ):
                    raise UserError(f"事实分支 {branch['id']} 锁定时必须提供非空 evidence")
        elif status in {"invalidated", "blocked"}:
            nonempty_text(branch.get("reason"), f"{branch['id']}.reason")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(branch_id: str) -> None:
        if branch_id in visiting:
            raise UserError(f"依赖图存在环：{branch_id}")
        if branch_id in visited:
            return
        visiting.add(branch_id)
        for dependency in prerequisites(by_id[branch_id]):
            visit(dependency)
        visiting.remove(branch_id)
        visited.add(branch_id)

    for branch_id in ids:
        visit(branch_id)

    for branch in branches:
        if branch["status"] == "locked":
            unresolved = [dep for dep in prerequisites(branch) if by_id[dep]["status"] != "locked"]
            if unresolved:
                raise UserError(
                    f"分支 {branch['id']} 已锁定，但前置分支尚未锁定：{', '.join(unresolved)}"
                )
    return branches


def validate_session(session: Any) -> dict[str, Any]:
    if not isinstance(session, dict) or session.get("schema_version") != SCHEMA_VERSION:
        raise UserError(f"会话协议版本无效；当前需要 schema_version={SCHEMA_VERSION}")
    nonempty_text(session.get("name"), "name")
    if session.get("mode") not in {"sequential", "frontier"}:
        raise UserError("会话 mode 无效")
    maximum = session.get("max_frontier")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 3:
        raise UserError("会话 max_frontier 必须在 1 到 3 之间")
    if session.get("status") not in {"active", "awaiting_confirmation", "closed"}:
        raise UserError("会话 status 无效")
    revision = session.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise UserError("会话 revision 必须是正整数")
    if not isinstance(session.get("history"), list):
        raise UserError("会话 history 必须是数组")
    validate_branches(session.get("branches"))
    return session


def load_tree(path: Path) -> list[dict[str, Any]]:
    raw = read_json(path)
    raw_branches = raw.get("branches") if isinstance(raw, dict) else raw
    if not isinstance(raw_branches, list) or not raw_branches:
        raise UserError("决策树必须包含非空 branches 数组")
    timestamp = now_iso()
    return validate_branches([normalize_branch(item, timestamp) for item in raw_branches])


def load_session(path: Path) -> dict[str, Any]:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise UserError(f"会话文件必须是 JSON 对象：{path}")
    return raw


def classify(session: dict[str, Any]) -> dict[str, Any]:
    validate_session(session)
    branches = session["branches"]
    by_id = {branch["id"]: branch for branch in branches}
    decisions: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    locked_decisions: list[dict[str, Any]] = []
    verified_facts: list[dict[str, Any]] = []
    invalidated: list[dict[str, Any]] = []

    ordered = sorted(branches, key=lambda branch: (branch["priority"], branch["created_at"], branch["id"]))
    for branch in ordered:
        if branch["status"] == "locked":
            (verified_facts if branch["kind"] == "fact" else locked_decisions).append(branch)
            continue
        if branch["status"] == "invalidated":
            invalidated.append(branch)
            continue
        if branch["status"] == "blocked":
            blocked.append(branch)
            continue
        if branch["status"] != "open":
            continue
        dependency_statuses = [by_id[item]["status"] for item in prerequisites(branch)]
        if "invalidated" in dependency_statuses:
            orphaned.append(branch)
        elif all(status == "locked" for status in dependency_statuses):
            (facts if branch["kind"] == "fact" else decisions).append(branch)
        else:
            waiting.append(branch)

    unresolved_count = sum(1 for branch in branches if branch["status"] in {"open", "blocked"})
    limit = 1 if session["mode"] == "sequential" else session["max_frontier"]
    can_confirm = unresolved_count == 0
    state = "closed" if session["status"] == "closed" else ("awaiting_confirmation" if can_confirm else "active")

    def compact(branch: dict[str, Any]) -> dict[str, Any]:
        return {
            key: branch.get(key)
            for key in (
                "id",
                "kind",
                "title",
                "question",
                "recommendation",
                "rationale",
                "parent_id",
                "depends_on",
                "priority",
                "status",
                "reason",
            )
        }

    def compact_resolved(branch: dict[str, Any]) -> dict[str, Any]:
        result = {
            "id": branch["id"],
            "kind": branch["kind"],
            "title": branch["title"],
            "question": branch["question"],
            "answer": branch.get("answer"),
            "updated_at": branch.get("updated_at"),
        }
        if branch["kind"] == "fact":
            result["evidence"] = branch.get("evidence", [])
        return result

    def compact_invalidated(branch: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": branch["id"],
            "kind": branch["kind"],
            "title": branch["title"],
            "reason": branch.get("reason"),
            "updated_at": branch.get("updated_at"),
        }

    # 事实只阻塞显式依赖它的决策。无关事实和独立决策可以同时进入当前前沿，
    # 避免一个待核实事实冻结整场访谈。
    ask_now = decisions[:limit]
    deferred_decisions = decisions[limit:]
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "session": session["name"],
        "revision": session["revision"],
        "state": state,
        "mode": session["mode"],
        "max_frontier": session["max_frontier"],
        "plan_source": copy.deepcopy(session.get("plan_source")),
        "counts": {
            "total": len(branches),
            "locked": sum(branch["status"] == "locked" for branch in branches),
            "invalidated": sum(branch["status"] == "invalidated" for branch in branches),
            "unresolved": unresolved_count,
        },
        "facts_to_resolve": [compact(branch) for branch in facts],
        "ask_now": [compact(branch) for branch in ask_now],
        "additional_frontier_decisions": [compact(branch) for branch in deferred_decisions],
        "waiting": [compact(branch) for branch in waiting],
        "orphaned": [compact(branch) for branch in orphaned],
        "blocked": [compact(branch) for branch in blocked],
        "locked_decisions": [compact_resolved(branch) for branch in locked_decisions],
        "verified_facts": [compact_resolved(branch) for branch in verified_facts],
        "invalidated": [compact_invalidated(branch) for branch in invalidated],
        "can_confirm": can_confirm and session["status"] != "closed",
        "confirmed_at": session.get("confirmed_at"),
    }



def branch_by_id(session: dict[str, Any], branch_id: str) -> dict[str, Any]:
    for branch in session["branches"]:
        if branch["id"] == branch_id:
            return branch
    raise UserError(f"分支不存在：{branch_id}")


def require_reason(operation: dict[str, Any]) -> str:
    return nonempty_text(operation.get("reason"), "reason")


def record_branch_history(branch: dict[str, Any], op: str, timestamp: str, reason: str | None = None) -> None:
    event: dict[str, Any] = {"at": timestamp, "op": op, "from_status": branch["status"]}
    if reason:
        event["reason"] = reason
    if op == "reopen":
        event["previous_answer"] = branch.get("answer")
        event["previous_evidence"] = branch.get("evidence", [])
        event["previous_reason"] = branch.get("reason")
    branch.setdefault("history", []).append(event)


def apply_operations(session: dict[str, Any], raw_changes: Any) -> dict[str, Any]:
    if not isinstance(raw_changes, dict) or not isinstance(raw_changes.get("operations"), list):
        raise UserError("变更文件必须包含 operations 数组")
    operations = raw_changes["operations"]
    if not operations:
        raise UserError("operations 不能为空")
    if session.get("status") == "closed":
        raise UserError("已确认完成的会话不能继续修改；请创建新会话")

    result = copy.deepcopy(session)
    timestamp = now_iso()
    event_summaries: list[dict[str, str]] = []

    for operation in operations:
        if not isinstance(operation, dict):
            raise UserError("每个 operation 必须是 JSON 对象")
        op = nonempty_text(operation.get("op"), "op")
        if op == "add":
            branch = normalize_branch(operation.get("branch"), timestamp)
            if any(existing["id"] == branch["id"] for existing in result["branches"]):
                raise UserError(f"分支 id 已存在：{branch['id']}")
            result["branches"].append(branch)
            event_summaries.append({"op": op, "id": branch["id"]})
            continue

        branch_id = nonempty_text(operation.get("id"), "id")
        branch = branch_by_id(result, branch_id)

        if op == "lock":
            if branch["status"] not in {"open", "blocked"}:
                raise UserError(f"分支 {branch_id} 当前状态不能 lock：{branch['status']}")
            by_id = {item["id"]: item for item in result["branches"]}
            unresolved = [dep for dep in prerequisites(branch) if by_id[dep]["status"] != "locked"]
            if unresolved:
                raise UserError(f"分支 {branch_id} 的前置分支尚未锁定：{', '.join(unresolved)}")
            answer = nonempty_text(operation.get("answer"), "answer")
            evidence = operation.get("evidence", [])
            if not isinstance(evidence, list) or any(not isinstance(item, str) or not item.strip() for item in evidence):
                raise UserError("evidence 必须是非空字符串组成的数组")
            if branch["kind"] == "fact" and not evidence:
                raise UserError(f"事实分支 {branch_id} 锁定时必须提供 evidence")
            record_branch_history(branch, op, timestamp)
            branch.update(status="locked", answer=answer, evidence=[item.strip() for item in evidence], reason=None)
        elif op == "invalidate":
            reason = require_reason(operation)
            if branch["status"] == "invalidated":
                raise UserError(f"分支 {branch_id} 已经失效")
            record_branch_history(branch, op, timestamp, reason)
            branch.update(status="invalidated", reason=reason)
        elif op == "block":
            reason = require_reason(operation)
            if branch["status"] != "open":
                raise UserError(f"只有 open 分支可以 block：{branch_id}")
            record_branch_history(branch, op, timestamp, reason)
            branch.update(status="blocked", reason=reason)
        elif op == "reopen":
            reason = require_reason(operation)
            if branch["status"] == "open":
                raise UserError(f"分支 {branch_id} 已经是 open")
            record_branch_history(branch, op, timestamp, reason)
            branch.update(status="open", answer=None, evidence=[], reason=None)
        elif op == "revise":
            reason = require_reason(operation)
            if branch["status"] not in {"open", "blocked"}:
                raise UserError(f"修改已锁定或失效分支前必须先 reopen：{branch_id}")
            fields = operation.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise UserError("revise 必须提供非空 fields 对象")
            unknown = set(fields) - REVISABLE_FIELDS
            if unknown:
                raise UserError(f"revise 包含不支持的字段：{', '.join(sorted(unknown))}")
            record_branch_history(branch, op, timestamp, reason)
            for key, value in fields.items():
                if key in {"kind", "title", "question", "recommendation", "rationale"}:
                    value = nonempty_text(value, key)
                elif key == "parent_id" and value is not None:
                    value = nonempty_text(value, key)
                elif key == "depends_on":
                    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                        raise UserError("depends_on 必须是字符串数组")
                    value = [item.strip() for item in value]
                elif key == "priority" and (not isinstance(value, int) or isinstance(value, bool)):
                    raise UserError("priority 必须是整数")
                branch[key] = value
            if branch["kind"] not in KINDS:
                raise UserError("kind 只能是 decision 或 fact")
        else:
            raise UserError(f"不支持的操作：{op}")

        branch["updated_at"] = timestamp
        event_summaries.append({"op": op, "id": branch_id})

    validate_branches(result["branches"])
    result["revision"] += 1
    result["updated_at"] = timestamp
    result["history"].append({"at": timestamp, "type": "apply", "operations": event_summaries})
    result["status"] = "awaiting_confirmation" if classify(result)["counts"]["unresolved"] == 0 else "active"
    return result


def command_validate_tree(args: argparse.Namespace) -> dict[str, Any]:
    branches = load_tree(Path(args.tree).expanduser().resolve())
    return {"ok": True, "schema_version": SCHEMA_VERSION, "branch_count": len(branches)}


def command_init(args: argparse.Namespace, store_dir: Path) -> dict[str, Any]:
    path = session_path(store_dir, args.session)
    if args.max_frontier < 1 or args.max_frontier > 3:
        raise UserError("max-frontier 必须在 1 到 3 之间")
    branches = load_tree(Path(args.tree).expanduser().resolve())
    timestamp = now_iso()
    plan_source: dict[str, Any]
    if args.plan:
        plan_path = Path(args.plan).expanduser().resolve()
        plan_source = {"type": "file", "path": str(plan_path), "sha256": file_sha256(plan_path)}
    else:
        plan_source = {"type": "conversation", "label": args.source_label}
    session = {
        "schema_version": SCHEMA_VERSION,
        "name": args.session,
        "mode": args.mode,
        "max_frontier": args.max_frontier,
        "status": "active",
        "revision": 1,
        "plan_source": plan_source,
        "created_at": timestamp,
        "updated_at": timestamp,
        "confirmed_at": None,
        "branches": branches,
        "history": [{"at": timestamp, "type": "init", "branch_count": len(branches)}],
    }
    session["status"] = "awaiting_confirmation" if classify(session)["counts"]["unresolved"] == 0 else "active"
    write_json_new(path, session)
    return classify(session)


def command_status(args: argparse.Namespace, store_dir: Path) -> dict[str, Any]:
    path = session_path(store_dir, args.session)
    session = load_session(path)
    return classify(session)


def command_apply(args: argparse.Namespace, store_dir: Path) -> dict[str, Any]:
    path = session_path(store_dir, args.session)
    session = load_session(path)
    validate_session(session)
    updated = apply_operations(session, read_json(Path(args.changes).expanduser().resolve()))
    write_json_atomic(path, updated)
    return classify(updated)


def command_confirm(args: argparse.Namespace, store_dir: Path) -> dict[str, Any]:
    path = session_path(store_dir, args.session)
    session = load_session(path)
    current = classify(session)
    if session["status"] == "closed":
        raise UserError("会话已经确认完成")
    if not current["can_confirm"]:
        raise UserError(f"仍有 {current['counts']['unresolved']} 个未解决分支，不能确认完成")
    timestamp = now_iso()
    session["status"] = "closed"
    session["confirmed_at"] = timestamp
    session["updated_at"] = timestamp
    session["revision"] += 1
    session["history"].append({"at": timestamp, "type": "confirmed"})
    write_json_atomic(path, session)
    return classify(session)


def command_set_mode(args: argparse.Namespace, store_dir: Path) -> dict[str, Any]:
    path = session_path(store_dir, args.session)
    session = load_session(path)
    validate_session(session)
    if session["status"] == "closed":
        raise UserError("已完成会话不能切换模式")
    maximum = args.max_frontier if args.max_frontier is not None else session["max_frontier"]
    if maximum < 1 or maximum > 3:
        raise UserError("max-frontier 必须在 1 到 3 之间")
    timestamp = now_iso()
    session["mode"] = args.mode
    session["max_frontier"] = maximum
    session["revision"] += 1
    session["updated_at"] = timestamp
    session["history"].append(
        {"at": timestamp, "type": "set_mode", "mode": args.mode, "max_frontier": maximum}
    )
    write_json_atomic(path, session)
    return classify(session)


def command_list(store_dir: Path) -> dict[str, Any]:
    if not store_dir.exists():
        return {"ok": True, "sessions": []}
    sessions: list[dict[str, Any]] = []
    for path in sorted(store_dir.glob("*.json")):
        try:
            session = load_session(path)
            sessions.append(
                {
                    "name": session.get("name", path.stem),
                    "schema_version": session.get("schema_version", 1),
                    "state": session.get("status", "unknown"),
                    "updated_at": session.get("updated_at"),
                }
            )
        except UserError as exc:
            sessions.append({"name": path.stem, "error": str(exc)})
    return {"ok": True, "sessions": sessions}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-dir", help="明辨簿目录；默认 ~/.mingbian_sessions")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-tree", help="只校验辨题图，不写会话")
    validate.add_argument("--tree", required=True)

    init = subparsers.add_parser("init", help="新建明辨会话；拒绝覆盖同名文件")
    init.add_argument("--session", required=True)
    init.add_argument("--tree", required=True)
    init.add_argument("--plan")
    init.add_argument("--source-label", default="当前对话")
    init.add_argument("--mode", choices=("sequential", "frontier"), default="sequential")
    init.add_argument("--max-frontier", type=int, default=3)

    status = subparsers.add_parser("status", help="读取会话并计算当前前沿")
    status.add_argument("--session", required=True)

    apply = subparsers.add_parser("apply", help="原子应用结构化变更")
    apply.add_argument("--session", required=True)
    apply.add_argument("--changes", required=True)

    confirm = subparsers.add_parser("confirm", help="在辨题图无未解决分支时确认《明辨录》")
    confirm.add_argument("--session", required=True)

    mode = subparsers.add_parser("set-mode", help="切换逐辨或 frontier 前沿模式")
    mode.add_argument("--session", required=True)
    mode.add_argument("--mode", choices=("sequential", "frontier"), required=True)
    mode.add_argument("--max-frontier", type=int)

    subparsers.add_parser("list", help="列出会话摘要")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        store_dir = resolve_store_dir(args.store_dir)
        if args.command == "validate-tree":
            result = command_validate_tree(args)
        elif args.command == "init":
            result = command_init(args, store_dir)
        elif args.command == "status":
            result = command_status(args, store_dir)
        elif args.command == "apply":
            result = command_apply(args, store_dir)
        elif args.command == "confirm":
            result = command_confirm(args, store_dir)
        elif args.command == "set-mode":
            result = command_set_mode(args, store_dir)
        else:
            result = command_list(store_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except UserError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
