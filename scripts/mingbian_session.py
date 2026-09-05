#!/usr/bin/env python3
"""明辨 3.2：本地、可追溯的会话状态工具（仅标准库；不执行业务动作）。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterator

VERSION = "3.2.0"
SCHEMA_VERSION = 3
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
KINDS = {"decision", "fact", "authorization"}
STATUSES = {"open", "locked", "invalidated", "blocked", "defaulted", "assumed", "deferred"}
SATISFIED = {"locked", "defaulted", "assumed"}
STAGES = {"direction", "pilot", "execution"}
DIMENSIONS = ("ben", "lu", "jie", "xu", "fa", "cheng")
TEXT_FIELDS = ("id", "kind", "title", "question", "recommendation", "rationale")
EDITABLE = set(TEXT_FIELDS) - {"id"} | {"parent_id", "depends_on", "priority", "handler", "lookup_note"}


class UserError(Exception):
    """无副作用或不会被静默忽略的用户可读错误。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UserError(f"{field} 必须是非空字符串")
    return value.strip()


def integer(value: Any, field: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise UserError(f"{field} 必须是有效整数" + (f"且不小于 {minimum}" if minimum is not None else ""))
    return value


def strings(value: Any, field: str, required: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise UserError(f"{field} 必须是字符串数组")
    result = [text(item, field) for item in value]
    if required and not result:
        raise UserError(f"{field} 不能为空")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UserError(f"JSON 出现重复字段：{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise UserError(f"JSON 不支持非有限数值：{value}")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UserError(f"无法读取 JSON：{path}（{exc}）") from exc


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise UserError(f"无法读取来源文件：{path}（{exc}）") from exc
    return digest.hexdigest()


def ensure_dir(path: Path) -> None:
    if path.is_symlink():
        raise UserError(f"拒绝将符号链接作为会话目录：{path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise UserError(f"不是会话目录：{path}")


def atomic_write(path: Path, payload: dict[str, Any], *, new: bool = False) -> None:
    ensure_dir(path.parent)
    if path.is_symlink():
        raise UserError(f"拒绝写入符号链接：{path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if new:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise UserError(f"同名会话已存在，不会覆盖：{path}") from exc
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def session_path(store: Path, name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise UserError("会话名须以字母或数字开头，仅含字母、数字、点、下划线、连字符，最长 80")
    path = store / f"{name}.json"
    if path.is_symlink():
        raise UserError(f"拒绝读取或写入符号链接会话：{path}")
    return path


@contextmanager
def session_lock(path: Path) -> Iterator[None]:
    ensure_dir(path.parent)
    lock_path = path.with_suffix(".lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise UserError(f"会话正被写入或有遗留锁：{lock_path}；确认没有写入进程后再人工移除，不自动抢锁") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": now_iso()}, handle)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def prerequisites(branch: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(branch["depends_on"] + ([branch["parent_id"]] if branch["parent_id"] else [])))


def normalize_branch(raw: Any, timestamp: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UserError("分支必须是 JSON 对象")
    if set(raw) - (EDITABLE | {"id"}):
        raise UserError("输入分支包含未知或状态字段；初始图只声明问题，状态请通过 apply 设置")
    branch = {field: text(raw.get(field), field) for field in TEXT_FIELDS}
    if branch["kind"] not in KINDS:
        raise UserError("kind 只能是 decision、fact 或 authorization")
    parent = raw.get("parent_id")
    branch.update(parent_id=text(parent, "parent_id") if parent is not None else None,
                  depends_on=strings(raw.get("depends_on", []), "depends_on"),
                  priority=integer(raw.get("priority", 100), "priority"),
                  handler=raw.get("handler", "agent" if branch["kind"] == "fact" else "user"),
                  lookup_note=raw.get("lookup_note", ""))
    if not isinstance(branch["handler"], str) or branch["handler"] not in {"agent", "user"}:
        raise UserError("handler 必须是 agent 或 user")
    if not isinstance(branch["lookup_note"], str):
        raise UserError("lookup_note 必须是字符串")
    if branch["kind"] != "fact" and branch["handler"] != "user":
        raise UserError("决策与授权必须由用户或正确责任人承担")
    if branch["kind"] == "fact" and branch["handler"] == "user":
        text(branch["lookup_note"], "事实补充必须说明为何现有材料和工具无法查得：lookup_note")
    stamp = timestamp or now_iso()
    branch.update(status="open", answer=None, evidence=[], reason=None, resolution=None,
                  created_at=stamp, updated_at=stamp, history=[])
    return branch


def validate_branches(branches: Any) -> list[dict[str, Any]]:
    if not isinstance(branches, list):
        raise UserError("branches 必须是数组；允许空数组表示零问题路径")
    by_id: dict[str, dict[str, Any]] = {}
    for branch in branches:
        if not isinstance(branch, dict):
            raise UserError("分支必须是对象")
        identity = text(branch.get("id"), "id")
        if identity in by_id:
            raise UserError("分支 id 必须唯一")
        by_id[identity] = branch
        raw = {key: branch[key] for key in EDITABLE | {"id"} if key in branch}
        normalized = normalize_branch(raw)
        for key in EDITABLE | {"id"}:
            if key not in branch or branch[key] != normalized[key]:
                raise UserError(f"持久化分支字段缺失或未标准化：{key}")
        if not isinstance(branch.get("status"), str) or branch.get("status") not in STATUSES:
            raise UserError(f"分支 {identity} 状态无效")
        if not isinstance(branch.get("history"), list):
            raise UserError("分支 history 必须是数组")
        strings(branch.get("evidence"), "evidence")
        if len(branch["depends_on"]) != len(set(branch["depends_on"])):
            raise UserError("depends_on 不能重复")
        if branch["status"] in SATISFIED:
            text(branch.get("answer"), "answer")
            resolution = branch.get("resolution")
            if not isinstance(resolution, dict):
                raise UserError("已处理分支必须保留 resolution 来源分类")
            if branch["status"] == "locked":
                basis = resolution.get("basis")
                if branch["kind"] == "fact":
                    if not isinstance(basis, str) or basis not in {"verified", "user_reported"}:
                        raise UserError("事实必须区分 verified 与 user_reported")
                    strings(branch["evidence"], "事实 evidence", required=True)
                elif branch["kind"] == "decision" and basis != "user_decision":
                    raise UserError("正式决策必须标明 user_decision")
                elif branch["kind"] == "authorization":
                    if basis != "authorization":
                        raise UserError("授权来源无效")
                    text(resolution.get("scope"), "authorization.scope")
                text(resolution.get("source"), "resolution.source")
            elif branch["status"] == "defaulted":
                if branch["kind"] != "decision" or resolution.get("basis") != "default":
                    raise UserError("只有决策可采用透明默认")
                for key in ("reason", "boundary", "reversal"):
                    text(resolution.get(key), key)
                if resolution.get("low_risk") is not True or resolution.get("reversible") is not True:
                    raise UserError("默认必须低风险且可逆")
            else:
                if branch["kind"] != "fact" or resolution.get("basis") != "assumption":
                    raise UserError("假设必须保留为未验证事实")
                for key in ("owner", "trigger", "method", "pass_condition", "fail_action", "boundary"):
                    text(resolution.get(key), key)
        if branch["status"] in {"blocked", "invalidated", "deferred"}:
            text(branch.get("reason"), "reason")
        if branch["status"] == "deferred":
            resolution = branch.get("resolution")
            if not isinstance(resolution, dict) or resolution.get("basis") != "deferred":
                raise UserError("后议须保留 resolution")
            for key in ("owner", "trigger", "basis_for_decision", "interim_action"):
                text(resolution.get(key), key)
    indegree = {key: 0 for key in by_id}
    children: dict[str, list[str]] = {key: [] for key in by_id}
    for identity, branch in by_id.items():
        for dependency in prerequisites(branch):
            if dependency not in by_id:
                raise UserError(f"分支 {identity} 引用了不存在的依赖 {dependency}")
            children[dependency].append(identity)
            indegree[identity] += 1
            if branch["status"] in SATISFIED and by_id[dependency]["status"] not in SATISFIED:
                raise UserError(f"分支 {identity} 的前置分支尚未满足：{dependency}")
    queue = [key for key, count in indegree.items() if count == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(by_id):
        raise UserError("依赖图存在环")
    return branches


def validate_session(session: Any) -> dict[str, Any]:
    if not isinstance(session, dict) or session.get("schema_version") != SCHEMA_VERSION:
        raise UserError("会话协议版本无效；schema 2 请先显式 migrate，不能静默写入")
    name = text(session.get("name"), "name")
    if not NAME_RE.fullmatch(name):
        raise UserError("会话 name 无效")
    integer(session.get("revision"), "revision", 1)
    if not isinstance(session.get("mode"), str) or session.get("mode") not in {"sequential", "frontier"}:
        raise UserError("mode 无效")
    if not 1 <= integer(session.get("max_frontier"), "max_frontier") <= 3:
        raise UserError("max_frontier 必须在 1 到 3 之间")
    if not isinstance(session.get("target_stage"), str) or session.get("target_stage") not in STAGES:
        raise UserError("target_stage 无效")
    if not isinstance(session.get("status"), str) or session.get("status") not in {"active", "awaiting_confirmation", "closed"}:
        raise UserError("会话 status 无效")
    if not isinstance(session.get("history"), list):
        raise UserError("history 必须是数组")
    source = session.get("plan_source")
    if not isinstance(source, dict) or not isinstance(source.get("type"), str) or source.get("type") not in {"file", "conversation"}:
        raise UserError("plan_source 无效")
    if source["type"] == "file":
        text(source.get("path"), "plan_source.path")
        if not re.fullmatch(r"[0-9a-f]{64}", text(source.get("sha256"), "plan_source.sha256")):
            raise UserError("来源摘要必须为 SHA-256")
    else:
        text(source.get("label"), "plan_source.label")
    validate_branches(session.get("branches"))
    review = session.get("review")
    if review is not None:
        if not isinstance(review, dict):
            raise UserError("review 必须是对象或 null")
        integer(review.get("valid_for_revision"), "review.valid_for_revision", 1)
        content = text(review.get("record_text"), "review.record_text")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != review.get("record_sha256"):
            raise UserError("明辨录内容与摘要不一致")
        validate_checks(review.get("checks"), session["target_stage"])
    if session["status"] == "closed":
        if not review or review["valid_for_revision"] != session["revision"]:
            raise UserError("closed 会话缺少当前版本的交付复核")
        if any(b["status"] in {"open", "blocked"} for b in session["branches"]):
            raise UserError("closed 会话仍有未解决分支")
        text(session.get("confirmation"), "confirmation")
    return session


def validate_checks(raw: Any, stage: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("stage") != stage:
        raise UserError("复核 stage 必须与本次 target_stage 一致")
    checks = raw.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(DIMENSIONS):
        raise UserError("复核须覆盖 ben、lu、jie、xu、fa、cheng 六层")
    for dimension in DIMENSIONS:
        item = checks[dimension]
        if not isinstance(item, dict) or not isinstance(item.get("status"), str) or item.get("status") not in {"pass", "not_applicable"}:
            raise UserError(f"复核 {dimension} 未通过或缺少明确状态")
        text(item.get("evidence"), f"{dimension}.evidence")
        if item["status"] == "not_applicable" and (stage != "direction" or dimension not in {"xu", "fa"}):
            raise UserError(f"当前阶段的 {dimension} 不可跳过")
    for key in ("action", "owner", "done_when"):
        text(raw.get("next_action", {}).get(key) if isinstance(raw.get("next_action"), dict) else None,
             f"next_action.{key}")
    return raw


def source_status(session: dict[str, Any]) -> dict[str, Any]:
    source = session["plan_source"]
    if source["type"] != "file":
        return {"state": "conversation", "note": "无文件摘要；恢复时仍须核对实际历史与新增信息"}
    try:
        actual = digest_file(Path(source["path"]))
    except UserError as exc:
        return {"state": "unavailable", "error": str(exc)}
    return {"state": "unchanged" if actual == source["sha256"] else "changed", "current_sha256": actual}


def classify(session: dict[str, Any]) -> dict[str, Any]:
    validate_session(session)
    branches = sorted(session["branches"], key=lambda b: (b["priority"], b["id"]))
    by_id = {b["id"]: b for b in branches}
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in (
        "facts_to_resolve", "ask_now", "waiting", "orphaned", "blocked", "locked_decisions",
        "verified_facts", "user_reported_facts", "authorizations", "defaults", "assumptions",
        "deferred", "invalidated", "legacy_context")}
    frontier = []
    for branch in branches:
        item = copy.deepcopy(branch)
        item.pop("history", None)
        status = branch["status"]
        if branch.get("legacy_context"):
            buckets["legacy_context"].append({"id": branch["id"], **branch["legacy_context"]})
        if status == "locked":
            basis = branch["resolution"]["basis"]
            bucket = {"verified": "verified_facts", "user_reported": "user_reported_facts",
                      "user_decision": "locked_decisions", "authorization": "authorizations"}[basis]
            buckets[bucket].append(item)
        elif status in {"defaulted", "assumed", "deferred", "invalidated", "blocked"}:
            buckets[{"defaulted": "defaults", "assumed": "assumptions"}.get(status, status)].append(item)
        else:
            ancestors = list(prerequisites(branch))
            seen: set[str] = set()
            while ancestors:
                identity = ancestors.pop()
                if identity in seen:
                    continue
                seen.add(identity)
                ancestors.extend(prerequisites(by_id[identity]))
            if any(by_id[identity]["status"] == "invalidated" for identity in seen):
                buckets["orphaned"].append(item)
            elif any(by_id[identity]["status"] not in SATISFIED for identity in prerequisites(branch)):
                buckets["waiting"].append(item)
            elif branch["kind"] == "fact" and branch["handler"] == "agent":
                buckets["facts_to_resolve"].append(item)
            else:
                frontier.append(item)
    limit = 1 if session["mode"] == "sequential" else session["max_frontier"]
    buckets["ask_now"] = frontier[:limit]
    unresolved = sum(b["status"] in {"open", "blocked"} for b in branches)
    graph_resolved = unresolved == 0
    source = source_status(session)
    review_current = bool(session.get("review") and session["review"]["valid_for_revision"] == session["revision"])
    can_confirm = graph_resolved and review_current and source["state"] not in {"changed", "unavailable"}
    state = "closed" if session["status"] == "closed" else ("awaiting_confirmation" if can_confirm else "active")
    return {"ok": True, "schema_version": SCHEMA_VERSION, "session": session["name"],
            "revision": session["revision"], "state": state, "target_stage": session["target_stage"],
            "mode": session["mode"], "max_frontier": session["max_frontier"],
            "plan_source": copy.deepcopy(session["plan_source"]), "source_check": source,
            "counts": {"total": len(branches), "locked": sum(b["status"] == "locked" for b in branches),
                       "invalidated": len(buckets["invalidated"]), "unresolved": unresolved},
            **buckets, "additional_frontier_decisions": frontier[limit:],
            "graph_resolved": graph_resolved, "review_current": review_current,
            "can_confirm": can_confirm and state != "closed", "confirmed_at": session.get("confirmed_at"),
            "external_execution_authorized": False,
            "note": "状态工具不判定业务就绪，也不授予任何外部操作权限；authorizations 仅为待核对记录"}


def branch_event(branch: dict[str, Any], op: str, reason: str) -> None:
    previous = {key: copy.deepcopy(value) for key, value in branch.items() if key != "history"}
    branch["history"].append({"at": now_iso(), "op": op, "reason": reason, "previous": previous})
    branch["updated_at"] = now_iso()


def reopen_branch(branch: dict[str, Any], reason: str) -> None:
    branch_event(branch, "reopen", reason)
    branch.update(status="open", answer=None, evidence=[], resolution=None, reason=None)


def cascade_reopen(session: dict[str, Any], root: str, reason: str) -> None:
    affected = {root}
    while True:
        new = {b["id"] for b in session["branches"] if any(dep in affected for dep in prerequisites(b))}
        if new <= affected:
            break
        affected |= new
    for branch in session["branches"]:
        if branch["id"] in affected - {root} and branch["status"] != "invalidated":
            reopen_branch(branch, f"上游 {root} 变化，必须重验：{reason}")


def touch(session: dict[str, Any], event: dict[str, Any]) -> None:
    session["revision"] += 1
    session["updated_at"] = now_iso()
    session["status"] = "active"
    session["review"] = None
    session["confirmed_at"] = None
    session["confirmation"] = None
    session["history"].append({"at": now_iso(), **event})


def apply_operations(session: dict[str, Any], changes: Any) -> dict[str, Any]:
    validate_session(session)
    if session["status"] == "closed":
        raise UserError("已关闭会话请先 reopen-session，并说明原因")
    if not isinstance(changes, dict) or not isinstance(changes.get("operations"), list) or not changes["operations"]:
        raise UserError("变更须包含非空 operations 数组")
    result = copy.deepcopy(session)
    summaries = []
    for operation in changes["operations"]:
        if not isinstance(operation, dict):
            raise UserError("operation 必须是对象")
        op = text(operation.get("op"), "op")
        if op == "add":
            branch = normalize_branch(operation.get("branch"))
            if any(b["id"] == branch["id"] for b in result["branches"]):
                raise UserError("分支 id 已存在")
            result["branches"].append(branch)
        else:
            identity = text(operation.get("id"), "id")
            branch = next((b for b in result["branches"] if b["id"] == identity), None)
            if branch is None:
                raise UserError(f"分支不存在：{identity}")
            if op in {"lock", "default", "assume"}:
                if branch["status"] not in {"open", "blocked"}:
                    raise UserError("已处理分支须先 reopen，不能静默覆盖结论")
                by_id = {b["id"]: b for b in result["branches"]}
                if any(dep not in by_id or by_id[dep]["status"] not in SATISFIED for dep in prerequisites(branch)):
                    raise UserError("前置分支尚未满足")
                answer = text(operation.get("answer"), "answer")
                if op == "lock":
                    resolution = {"basis": operation.get("basis"), "source": text(operation.get("source"), "source")}
                    if branch["kind"] == "authorization":
                        resolution["scope"] = text(operation.get("scope"), "scope")
                    evidence = strings(operation.get("evidence", []), "evidence", branch["kind"] == "fact")
                elif op == "default":
                    resolution = {key: operation.get(key) for key in ("reason", "low_risk", "reversible", "boundary", "reversal")}
                    resolution["basis"] = "default"
                    evidence = []
                else:
                    resolution = {key: operation.get(key) for key in ("owner", "trigger", "method", "pass_condition", "fail_action", "boundary")}
                    resolution["basis"] = "assumption"
                    resolution["acceptance"] = operation.get("acceptance", "未记录用户风险接受；仅可在既有边界内验证")
                    evidence = []
                branch_event(branch, op, operation.get("reason", op))
                branch.update(status={"lock": "locked", "default": "defaulted", "assume": "assumed"}[op],
                              answer=answer, evidence=evidence, reason=None, resolution=resolution)
                branch.pop("legacy_context", None)
            elif op in {"reopen", "invalidate", "block", "defer", "revise"}:
                reason = text(operation.get("reason"), "reason")
                if op == "reopen":
                    if branch["status"] == "open":
                        raise UserError("分支已是 open")
                    reopen_branch(branch, reason)
                    cascade_reopen(result, identity, reason)
                elif op == "revise":
                    if branch["status"] not in {"open", "blocked"}:
                        raise UserError("修改已处理分支前必须 reopen")
                    fields = operation.get("fields")
                    if not isinstance(fields, dict) or not fields or set(fields) - EDITABLE:
                        raise UserError("revise.fields 包含未知字段或为空")
                    branch_event(branch, op, reason)
                    candidate = {key: copy.deepcopy(branch[key]) for key in EDITABLE | {"id"} if key in branch}
                    candidate.update(fields)
                    normalized = normalize_branch(candidate)
                    branch.update({key: normalized[key] for key in EDITABLE | {"id"} if key in normalized})
                    cascade_reopen(result, identity, reason)
                else:
                    if op == "invalidate" and branch["status"] == "invalidated":
                        raise UserError("分支已经失效")
                    if op in {"block", "defer"} and branch["status"] not in {"open", "blocked"}:
                        raise UserError("block/defer 只能用于尚未解决的分支")
                    branch_event(branch, op, reason)
                    branch.update(status={"invalidate": "invalidated", "block": "blocked", "defer": "deferred"}[op], reason=reason)
                    if op == "defer":
                        branch["resolution"] = {key: operation.get(key) for key in ("owner", "trigger", "basis_for_decision", "interim_action")}
                        branch["resolution"]["basis"] = "deferred"
                    cascade_reopen(result, identity, reason)
            else:
                raise UserError(f"不支持的操作：{op}")
        summaries.append({"op": op, "id": branch["id"]})
    touch(result, {"type": "apply", "operations": summaries})
    validate_session(result)
    return result


def plan_source(plan: str | None, label: str = "当前对话") -> dict[str, str]:
    if plan:
        path = Path(plan).expanduser().resolve()
        return {"type": "file", "path": str(path), "sha256": digest_file(path)}
    return {"type": "conversation", "label": text(label, "source_label")}


def new_session(name: str, branches: list[dict[str, Any]], *, stage: str = "execution",
                mode: str = "sequential", maximum: int = 3, source: dict[str, str] | None = None) -> dict[str, Any]:
    timestamp = now_iso()
    session = {"schema_version": SCHEMA_VERSION, "name": name, "revision": 1,
               "target_stage": stage, "mode": mode, "max_frontier": maximum, "status": "active",
               "plan_source": source or plan_source(None), "created_at": timestamp, "updated_at": timestamp,
               "confirmed_at": None, "confirmation": None, "review": None, "branches": branches,
               "history": [{"at": timestamp, "type": "init"}]}
    return validate_session(session)


def load_tree(path: Path) -> list[dict[str, Any]]:
    raw = read_json(path)
    branches = raw.get("branches") if isinstance(raw, dict) else raw
    if not isinstance(branches, list):
        raise UserError("辨题图必须是数组，或包含 branches 数组")
    return validate_branches([normalize_branch(item) for item in branches])


def check_revision(session: dict[str, Any], expected: Any) -> None:
    integer(expected, "expected_revision（先 status 获取）", 1)
    actual = integer(session.get("revision"), "会话 revision", 1)
    if actual != expected:
        raise UserError(f"版本冲突：预期 {expected}，实际 {session['revision']}；重新读取、判断后再写，不能自动重试旧决定")


def migrate_legacy(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        raise UserError("仅支持显式迁移 schema 2")
    if not isinstance(raw.get("branches"), list):
        raise UserError("旧会话 branches 无效")
    branches = []
    for old in raw["branches"]:
        if not isinstance(old, dict) or not isinstance(old.get("status"), str) or old.get("status") not in {"open", "locked", "invalidated", "blocked"}:
            raise UserError("旧分支状态无效")
        new = normalize_branch({key: old[key] for key in TEXT_FIELDS + ("parent_id", "depends_on", "priority") if key in old})
        if old["status"] in {"invalidated", "blocked"}:
            new.update(status=old["status"], reason=text(old.get("reason"), "旧 reason"))
        elif old["status"] == "locked":
            new["legacy_context"] = {"previous_answer": text(old.get("answer"), "旧 answer"),
                                     "previous_evidence": strings(old.get("evidence", []), "旧 evidence"),
                                     "note": "原结论保留，须核对来源分类；不要机械重新询问用户"}
        new["history"] = copy.deepcopy(old.get("history", []))
        new["history"].append({"at": now_iso(), "op": "migrate", "previous": copy.deepcopy(old)})
        branches.append(new)
    session = new_session(text(raw.get("name"), "旧 name"), branches,
                          mode=raw.get("mode", "sequential"), maximum=raw.get("max_frontier", 3),
                          source=raw.get("plan_source"))
    session["revision"] = integer(raw.get("revision"), "旧 revision", 1) + 1
    session["created_at"] = raw.get("created_at", session["created_at"])
    if not isinstance(raw.get("history"), list):
        raise UserError("旧 history 无效")
    session["history"] = copy.deepcopy(raw["history"]) + [{"at": now_iso(), "type": "migrate", "from_schema": 2,
                                                             "previous_status": raw.get("status")}]
    return validate_session(session)


def export_markdown(session: dict[str, Any]) -> str:
    state = classify(session)
    if session.get("review") and state["review_current"]:
        body = session["review"]["record_text"]
    else:
        body = "# 明辨录｜阶段底稿\n\n尚未完成本阶段整体复核，不代表方案已就绪。\n"
    body += f"\n\n---\n\n## 会话快照\n\n会话：{session['name']}；版本：{session['revision']}；目标阶段：{session['target_stage']}。\n"
    if state["source_check"]["state"] in {"changed", "unavailable"}:
        body += "\n**来源已变化或无法回读，既有结论需重新核对；不得继续沿用就绪结论。**\n"
    labels = {"locked_decisions": "已定决策", "verified_facts": "已核实事实", "user_reported_facts": "用户陈述（未独立核实）",
              "defaults": "透明默认（非用户决策）", "assumptions": "假设与验证", "deferred": "后议与触发",
              "authorizations": "授权记录（须核对具体动作，非通用许可）", "blocked": "阻断", "invalidated": "失效",
              "facts_to_resolve": "待查事实", "ask_now": "当前待响应", "additional_frontier_decisions": "其余已解锁事项",
              "waiting": "待前置", "orphaned": "前置失效待重审", "legacy_context": "迁移保留的旧结论"}
    for key, label in labels.items():
        if not state[key]:
            continue
        body += f"\n### {label}\n"
        for item in state[key]:
            body += f"\n- {item['id']}｜{item.get('title', '')}：{item.get('answer') or item.get('previous_answer') or item.get('reason') or item.get('question', '')}\n"
            if item.get("resolution"):
                body += "  依据与条件：" + json.dumps(item["resolution"], ensure_ascii=False) + "\n"
            if item.get("reason") and item.get("answer"):
                body += "  当前原因：" + item["reason"] + "\n"
            if item.get("evidence"):
                body += "  证据：" + "；".join(item["evidence"]) + "\n"
    return body.rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-dir", help="默认 MINGBIAN_SESSIONS_DIR 或 ~/.mingbian_sessions")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-tree")
    validate.add_argument("--tree", required=True)
    init = commands.add_parser("init")
    init.add_argument("--session", required=True)
    init.add_argument("--tree", required=True)
    init.add_argument("--plan")
    init.add_argument("--source-label", default="当前对话")
    init.add_argument("--stage", choices=sorted(STAGES), default="execution")
    init.add_argument("--mode", choices=("sequential", "frontier"), default="sequential")
    init.add_argument("--max-frontier", type=int, default=3)
    for command in ("status", "apply", "review", "confirm", "set-mode", "rebase", "reopen-session", "migrate", "export"):
        sub = commands.add_parser(command)
        sub.add_argument("--session", required=True)
        if command not in {"status", "export"}:
            sub.add_argument("--expected-revision", type=int, required=command != "apply")
        if command == "apply":
            sub.add_argument("--changes", required=True)
        elif command == "review":
            sub.add_argument("--record", required=True)
            sub.add_argument("--checks", required=True)
        elif command == "confirm":
            sub.add_argument("--confirmation", required=True, help="实际用户确认的来源或原话；不允许编造")
        elif command == "set-mode":
            sub.add_argument("--mode", choices=("sequential", "frontier"), required=True)
            sub.add_argument("--max-frontier", type=int)
        elif command in {"rebase", "reopen-session"}:
            sub.add_argument("--reason", required=True)
            if command == "rebase":
                sub.add_argument("--plan")
                sub.add_argument("--stage", choices=sorted(STAGES))
        elif command == "export":
            sub.add_argument("--output", required=True, help="新 Markdown 文件；拒绝覆盖")
    commands.add_parser("list")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    store = Path(args.store_dir or os.environ.get("MINGBIAN_SESSIONS_DIR", "~/.mingbian_sessions")).expanduser().absolute()
    if args.command == "validate-tree":
        return {"ok": True, "schema_version": SCHEMA_VERSION, "branch_count": len(load_tree(Path(args.tree)))}
    if args.command == "list":
        if not store.exists():
            return {"ok": True, "sessions": []}
        ensure_dir(store)
        items = []
        for path in sorted(store.glob("*.json")):
            try:
                data = read_json(session_path(store, path.stem))
                if not isinstance(data, dict):
                    raise UserError("会话不是对象")
                items.append({"name": path.stem, "schema_version": data.get("schema_version"), "revision": data.get("revision"),
                              "state": data.get("status", "unknown")})
            except UserError as exc:
                items.append({"name": path.stem, "error": str(exc)})
        return {"ok": True, "sessions": items}
    path = session_path(store, args.session)
    if args.command == "init":
        session = new_session(args.session, load_tree(Path(args.tree)), stage=args.stage, mode=args.mode,
                              maximum=args.max_frontier, source=plan_source(args.plan, args.source_label))
        with session_lock(path):
            atomic_write(path, session, new=True)
        return classify(session)
    if args.command in {"status", "export"}:
        session = read_json(path)
        if isinstance(session, dict) and session.get("schema_version") == 2:
            if args.command == "export":
                raise UserError("schema 2 请先 migrate，再导出")
            return {"ok": True, "state": "migration_required", "schema_version": 2,
                    "session": args.session, "revision": session.get("revision"),
                    "can_confirm": False, "note": "原文件未修改；先显式 migrate，原结论会保留"}
        state = classify(session)
        if args.command == "export":
            output = Path(args.output).expanduser().absolute()
            if output == path or output.is_symlink():
                raise UserError("不能将明辨录写入会话文件或符号链接")
            content = export_markdown(session)
            # exclusive create: export is an explicitly requested write, never an overwrite.
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            return {"ok": True, "output": str(output), "revision": session["revision"]}
        return state
    with session_lock(path):
        session = read_json(path)
        changes = read_json(Path(args.changes)) if args.command == "apply" else None
        expected = args.expected_revision
        if args.command == "apply" and isinstance(changes, dict):
            supplied = changes.get("expected_revision")
            if expected is not None and supplied is not None and expected != supplied:
                raise UserError("命令与变更文件的 expected_revision 不一致")
            expected = expected if expected is not None else supplied
        if not isinstance(session, dict):
            raise UserError("会话不是对象")
        check_revision(session, expected)
        if args.command == "migrate":
            updated = migrate_legacy(session)
            backup = path.with_suffix(".schema2.bak")
            # Byte-for-byte, exclusive backup before any replacement.
            with backup.open("xb") as handle:
                if os.name == "posix":
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(path.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            atomic_write(path, updated)
            return {**classify(updated), "backup": str(backup)}
        validate_session(session)
        if session["status"] == "closed" and args.command != "reopen-session":
            raise UserError("已关闭会话请先 reopen-session")
        source = source_status(session)
        if source["state"] in {"changed", "unavailable"} and args.command not in {"rebase", "reopen-session"}:
            raise UserError("来源已变化或无法回读；先核对材料，再 rebase，不得沿用旧结论写入")
        if args.command == "apply":
            session = apply_operations(session, changes)
        elif args.command == "review":
            if not classify(session)["graph_resolved"]:
                raise UserError("仍有未解决分支，不能登记就绪复核")
            checks = validate_checks(read_json(Path(args.checks)), session["target_stage"])
            content = text(Path(args.record).read_text(encoding="utf-8"), "record")
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            touch(session, {"type": "review", "record_text": content, "record_sha256": sha, "checks": checks})
            session["review"] = {"valid_for_revision": session["revision"], "record_text": content,
                                 "record_sha256": sha, "checks": checks}
            session["status"] = "awaiting_confirmation"
        elif args.command == "confirm":
            if not classify(session)["can_confirm"]:
                raise UserError("不能确认完成：需无关键未解事项、当前版本的明辨录与六层复核")
            session["revision"] += 1
            session["review"]["valid_for_revision"] = session["revision"]
            session["confirmed_at"] = session["updated_at"] = now_iso()
            session["status"] = "closed"
            session["confirmation"] = text(args.confirmation, "confirmation")
            session["history"].append({"at": now_iso(), "type": "confirmed", "source": session["confirmation"]})
        elif args.command == "set-mode":
            maximum = args.max_frontier if args.max_frontier is not None else session["max_frontier"]
            session.update(mode=args.mode, max_frontier=maximum)
            touch(session, {"type": "set_mode", "mode": args.mode, "max_frontier": maximum})
        elif args.command == "reopen-session":
            if session["status"] != "closed":
                raise UserError("只有 closed 会话需要 reopen-session")
            touch(session, {"type": "reopen_session", "reason": text(args.reason, "reason")})
        elif args.command == "rebase":
            reason = text(args.reason, "reason")
            old_source = copy.deepcopy(session["plan_source"])
            if args.plan:
                session["plan_source"] = plan_source(args.plan)
            elif session["plan_source"]["type"] == "file":
                session["plan_source"] = plan_source(session["plan_source"]["path"])
            if args.stage:
                session["target_stage"] = args.stage
            for branch in session["branches"]:
                if branch["status"] != "invalidated":
                    reopen_branch(branch, f"重新核对方案基线：{reason}")
            touch(session, {"type": "rebase", "reason": reason, "previous_source": old_source,
                            "source": copy.deepcopy(session["plan_source"]), "stage": session["target_stage"]})
        validate_session(session)
        atomic_write(path, session)
        return classify(session)


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (UserError, OSError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
