# 明辨簿｜schema 3 状态协议

版本：Skill 3.2.0；状态协议：3。

## 1. 用途与边界

这是可选的本地辅助工具，管理最小辨题图、证据分类、依赖、版本与《明辨录》快照，不运行任何业务动作，不是多人在线协作服务，也不是内容真实性或用户身份的认证系统。

普通单次对话无需创建会话。需要跨会话恢复、持久化或可追溯记录，且当前环境有文件权限与 Python 时再使用。无工具时输出可复制检查点，不声称已经保存。

Python 3.10+，只使用标准库。默认目录 `~/.mingbian_sessions`；可用 `MINGBIAN_SESSIONS_DIR` 或全局 `--store-dir` 指定。全局参数放在子命令前。

## 2. 基本命令

以下命令从 Skill 根目录运行。`examples/` 中是合成示例，不是真实业务结论。

```bash
python3 scripts/mingbian_session.py validate-tree --tree examples/tree.json
python3 scripts/mingbian_session.py init --session demo --tree examples/tree.json --plan examples/sample-plan.md --stage pilot
python3 scripts/mingbian_session.py status --session demo
python3 scripts/mingbian_session.py apply --session demo --changes examples/changes.json
python3 scripts/mingbian_session.py review --session demo --expected-revision 2 --record examples/mingbian-record.md --checks examples/review-checks.json
python3 scripts/mingbian_session.py export --session demo --output /path/to/new-record.md
```

示例变更假定会话刚初始化、revision=1；不要对真实会话反复套用示例文件。`export` 的父目录须已存在，输出必须是新文件，不覆盖已有文件。

仅当用户确实明确确认本版内容，且需要关闭归档时才执行：

```bash
python3 scripts/mingbian_session.py confirm --session demo --expected-revision 3 --confirmation "实际确认的原话或来源"
```

**这段占位文字不能照抄成真实用户确认。** 用户只要讨论结果、不要求确认关口时，交付后可以保留在 awaiting_confirmation，不为关闭文件而额外问一轮。工具的归档状态不控制普通讨论是否可以结束。

## 3. 输入图

输入为 `{"branches": [...]}` 或数组，允许空数组。只描述当前未解决的关键问题，不在初始图里夹带 `status`、`answer` 等状态字段。

```json
{
  "branches": [{
    "id": "D01",
    "kind": "decision",
    "title": "首期范围",
    "question": "首期离线验证，还是进入真实流程试点？",
    "recommendation": "在现有授权边界内优先离线验证。",
    "rationale": "会改变权限要求、风险与验收。",
    "parent_id": null,
    "depends_on": [],
    "priority": 10
  }]
}
```

`kind` 支持 `decision`、`fact`、`authorization`。`parent_id` 和 `depends_on` 都是硬前置，不是章节分类；priority 越小越优先，不自动代表问题质量。

事实默认由代理查实。确实只能补问用户时，设置 `handler: "user"` 并提供非空 `lookup_note`，说明现有材料与工具为何无法取得。决策与授权必须交给用户或正确责任人。

所有需要用户响应的分支共享当前问题额度：顺序模式最多一个；frontier 最多三个。图只能识别已声明的硬依赖，语义上的关联仍须代理判断，不能因脚本给出三个候选就全部问出。

## 4. 变更来源与原子性

所有修改都需要当前 `expected_revision`。`apply` 可放在变更文件中，也可通过命令参数提供；两处都有时必须一致。

```json
{
  "expected_revision": 1,
  "operations": [{
    "op": "lock",
    "id": "D01",
    "answer": "首期采用离线验证",
    "basis": "user_decision",
    "source": "本轮用户明确选择离线验证的原话或定位"
  }]
}
```

支持下列操作：

| 操作 | 含义与必需条件 |
|---|---|
| `add` | 新增分支；完整内容放在 `branch` 中 |
| `lock` | 记录已核实事实、用户陈述、正式决策或具体授权；须提供 answer、basis、source；事实还需 evidence |
| `default` | 仅用于 decision；须提供 answer、reason、low_risk=true、reversible=true、boundary、reversal |
| `assume` | 仅用于 fact；须提供 answer、owner、trigger、method、pass_condition、fail_action、boundary；可补充 acceptance |
| `defer` | 须有 reason、owner、trigger、basis_for_decision、interim_action；不能满足下游硬前置 |
| `block` | 当前条件不足；须有 reason，并在理由中明确解锁条件与影响范围 |
| `invalidate` | 已不适用；须有 reason，保留旧值与变更历史 |
| `reopen` | 重开已处理事项；须有 reason，自动重开受影响下游 |
| `revise` | 修改开放或阻断分支的可编辑字段；须有 fields 与 reason；不能改 id，已处理项先 reopen |

`lock.basis` 取值：事实为 `verified` 或 `user_reported`；决策为 `user_decision`；授权为 `authorization`，授权另须非空 `scope`。`evidence` 为非空字符串组成的数组，可定位到文件、会话原话或工具回读，不得填编造的来源。

用户的倾向不执行 `lock`。假设不写成 verified；默认不写成 user_decision。建议负责人可以记录，但注明待确认，不能用脚本字段伪造任命。

默认的低风险、可逆性以及事实证据的真实性，仍是代理必须核对的语义条件；脚本只能验证声明齐全，不能独立认证这些声明。

### 默认、假设与后议示例

```json
{
  "op": "default", "id": "D02", "answer": "原型先分页展示",
  "reason": "仅涉及可调整的展示方式", "low_risk": true, "reversible": true,
  "boundary": "不改变业务规则或生产数据", "reversal": "调整原型展示配置"
}
```

```json
{
  "op": "assume", "id": "F01", "answer": "现有样例可覆盖关键路径",
  "owner": "建议由测试负责人验证，尚未指派", "trigger": "离线验证前",
  "method": "对照正常与异常路径核对样例", "pass_condition": "关键路径均有可复验样例",
  "fail_action": "补齐后再验证", "boundary": "仅离线，不使用真实个人敏感信息"
}
```

```json
{
  "op": "defer", "id": "D03", "reason": "不影响本次方向选择",
  "owner": "项目负责人（待确认）", "trigger": "决定进入试点时",
  "basis_for_decision": "验证结果与实际资源", "interim_action": "不启动相关实施"
}
```

一批操作任一失败，整批不写入。锁定前检查硬前置；默认和有边界假设可以支撑相应下游，后议、阻断或失效不行。上游重开、失效或修订后，受影响下游自动重审；即使同批重新锁定上游，也不保留旧下游为已定。

## 5. 怎样读 status

`locked_decisions`、`verified_facts`、`user_reported_facts`、`defaults`、`assumptions`、`deferred`、`authorizations` 分开返回。

`facts_to_resolve` 是当前代理可查事实；`ask_now` 是本轮待响应事项；`additional_frontier_decisions` 是其余已解锁事项（为兼容名称保留该字段，可能包含事实补充或授权）；`waiting` 等待前置；`orphaned` 的祖先已失效；`blocked` 是明确阻断。

三个状态必须区分：

- `graph_resolved`：已登记分支没有开放或阻断项；不代表未登记的关键内容也完整。
- `review_current`：当前版本已关联明辨录快照与六层复核；不等于自动证明复核结论正确。
- `can_confirm`：图处理完、复核是当前版本、来源可回读且未漂移，允许按实际用户确认关闭归档。

`external_execution_authorized` 恒为 false：脚本不会授予任何外部操作权限。`authorizations` 只是记录，执行时仍需核对动作、对象、范围、有效条件与工具规则。

## 6. 交付复核与导出

`review` 读取 Markdown 明辨录和 JSON 复核。文本去掉首尾空白后，作为快照和 SHA-256 摘要存入会话。摘要用于一致性检查，不是防篡改签名或不可抵赖审计。

复核 JSON 包含 target_stage 对应的 stage、六层 checks 与 next_action。`examples/review-checks.json` 是完整可运行示例。

六层键为 ben、lu、jie、xu、fa、cheng；各项须有 status 与非空 evidence。status 可为 pass 或 not_applicable；仅 direction 阶段的 xu、fa 可以注明不适用，其余不可跳过。`next_action` 须有 action、owner、done_when。这里的证据应具体说明明辨录何处支持该判断，不以“已检查”替代实质内容。

一次后续修改会使旧复核失效；需要重新核对与登记，不沿用旧“通过”。脚本检查字段、版本与摘要，不会读懂业务逻辑，也不能替代陌生执行者检验或真实行为评测。

`export` 输出当前明辨录与分类状态快照；没有当前复核时，明确标成“阶段底稿”。它只生成新文件，不覆盖用户已有方案，也不会自动提交、上传或发送。

## 7. 来源变化、会话重开与恢复

绑定文件时保存 SHA-256。每次 status 回读检查；文件改变或不可取得时阻止继续修改和确认。先读实际差异，再执行：

```bash
python3 scripts/mingbian_session.py rebase --session demo --expected-revision 4 --reason "具体说明本次材料或约束变化"
```

可用 `--plan /path/to/new-plan.md` 绑定新文件；可用 `--stage direction|pilot|execution` 更新已明确改变的阶段。工具采取保守重审：保留历史，重开除已失效项之外的分支，清除旧复核。代理需重新判别仍然有效的旧结论，不把它们机械变成重复问题。

对话来源没有文件哈希，只能提示代理回读实际历史；状态脚本不访问模型记忆、邮箱或远程仓库，也不会自动验证证据链接的新旧。

已关闭会话出现新信息，不必丢弃历史重新 init：

```bash
python3 scripts/mingbian_session.py reopen-session --session demo --expected-revision 4 --reason "出现了新的明确约束"
```

这只重开会话并清除旧复核，不撤销未受影响的决策；之后通过 reopen/revise 更新受影响项。恢复优先 status，禁止同名 init 覆盖。

## 8. 从 schema 2 迁移

```bash
python3 scripts/mingbian_session.py status --session old-session
python3 scripts/mingbian_session.py migrate --session old-session --expected-revision 8
```

revision 以实际 status 为准。先逐字节备份为 `old-session.schema2.bak`，已存在备份则拒绝覆盖。旧文件在显式迁移前不改动；迁移失败也不静默丢弃旧会话。

旧版已锁定项保留到 `legacy_context` 和历史，待核对来源分类后重新登记；不会无依据地把旧回答判断成已核实事实、用户批准或执行授权。恢复时使用保留的原答案和实际来源，不要求用户把整个讨论重来。

旧协议不记录本版交付复核，迁移后不保留“已完成”作为当前就绪结论。需要回退时停止写入，备份新状态，在确认路径与进程安全后人工恢复旧文件；不要在运行中的会话上直接覆盖。

## 9. 并发、权限与安全边界

本地同一目录下，通过每会话排他锁与 expected_revision 防止合作写入者相互覆盖。发现遗留锁时不自动抢锁；确认没有写入进程后，由维护者人工移除。版本冲突必须重读与重判，不自动重放旧决定。

新建目录在 POSIX 上为 0700，会话、备份与导出文件为 0600；已有目录权限不自动修改。Windows ACL、网络文件系统、多机器同步、加密与权限认证不在本工具的保证范围内，需由运行环境管理。

拒绝路径穿越式会话名与符号链接会话；临时文件同目录写入，刷盘后原子替换。没有远程调用和环境凭据读取；除配置目录外，不采集用户账号信息。

会话中只放必要、脱敏的证据定位与结论，不保存令牌、密码、身份证、病历或完整敏感业务原文。是否允许持久化由所在环境与用户授权决定。
