# P2 完整验收对照（AGENT_HUB_PLAN §6 / §10）

日期：2026-10-14（含 P2 收尾轮更新）。范围：对照计划逐条核对 P2 的验收条款与
"必须通过的安全阀用例"。全部为 Mock Agent，**未接入任何真实产品**。

验证命令（仓库根目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

结果：**127 passed，exit code 0**。并发/恢复/E2E 敏感用例另做 **8 轮重复**
（9 项 × 8 = 72 次执行）全部 `exit=0`。

---

## A. 计划 §6 阶段表对 P2 的定义

> P2：审批和限额核心 | 3–5 天 | 用 Mock Agent 验证批准、拒绝、过期、计数、并发和停止；未批准绝不调用适配器

| 要求 | 状态 | 证据 |
|---|---|---|
| 验证**批准** | 通过 | `test_approved_invocation_starts_adapter_once`、`e2e/test_demo_flow.py` |
| 验证**拒绝** | 通过 | `test_rejected_approval_never_reaches_adapter`、`test_rejected_state_is_reachable` |
| 验证**过期** | 通过 | `test_expired_approval_never_reaches_adapter`、`test_demo_flow_rejection_and_expiry_never_start` |
| 验证**计数** | 通过 | `test_total_limit_blocks_new_subagent`、`test_lowering_limit_keeps_cumulative_and_running` |
| 验证**并发** | 通过 | `test_concurrent_limit_blocks_when_at_cap`、`test_concurrent_race_for_last_concurrent_slot`、`test_concurrent_distinct_invocations_do_not_exceed_total_cap` |
| 验证**停止** | 通过 | `test_stop_cancel.py`（8 项）、`e2e` 第 7 步 |
| **未批准绝不调用适配器** | 通过 | 所有用例以 Mock 适配器 `start_count` 断言；未批准路径实测为 0 |

**结论：计划 §6 对 P2 的定义全部通过。**

---

## B. 计划 §6「必须通过的安全阀用例」逐条核对

该列表是整个项目的验收线；按阶段表，部分条目属 P3–P5。逐条给出状态与证据。

| # | 用例 | 状态 | 证据 / 说明 |
|---|---|---|---|
| 1 | 无审批、审批过期、审批被撤销：适配器启动次数为零 | **通过** | `test_unapproved_invocation_never_reaches_adapter`、`test_expired_approval_never_reaches_adapter`、`test_revoked_approval_never_reaches_adapter`、`test_pending_approval_is_not_consumable`、`test_pending_consume_does_not_change_approval_or_reserve_resources` |
| 2 | 一个审批被重复提交或由两个 Worker 同时处理：最多一次正常启动 | **通过** | `test_double_approval_consumed_once`、`test_concurrent_consume_starts_at_most_once`（文件库 + 独立连接 + barrier，重复 8 轮） |
| 3 | 批准后输入、权限、Agent 或策略变化：旧批准无效 | **通过** | 输入/权限/Agent/资源/深度：`test_snapshot_binding.py`（12 项，含"未重算哈希"与"已重算哈希"两条路径）；策略：`test_consume_is_refused_after_policy_change`、`test_consume_refuses_when_approval_survived_but_version_moved` |
| 4 | 总子实例 3、并发 2：全树不超过 3，同时不超过 2；失败不恢复累计总量 | **通过** | `test_total_limit_blocks_new_subagent`、`test_concurrent_limit_blocks_when_at_cap`、`test_releasing_concurrent_slot_allows_next`、`test_release_does_not_return_cumulative_total`、`test_lowering_limit_keeps_cumulative_and_running` |
| 5 | 总子实例 0：用户手动分派、自动分派和孙 Agent 分派均被拒绝 | **通过** | `test_total_zero_blocks_all_subagents`（手动）、`test_total_zero_blocks_auto_delegation_too`（自动）、`test_total_zero_blocks_grandchild_dispatch`（孙） |
| 6 | 深度 1：子 Agent 不能再生成子 Agent | **通过** | `test_depth_limit_blocks_grandchild` |
| 7 | Agent 尝试批准自己、增加上限或通过 CLI 绕过中转站：被权限边界阻止 | **部分通过**（CLI 项属 P3） | 批准自己：`test_non_user_cannot_approve`（agent/worker/无 token/伪造 token，启动数 0）。增加上限：`test_non_user_cannot_change_policy`、`test_agent_cannot_update_policy_via_api`、`test_patch_policy_requires_user`。**CLI 绕过：P2 无 CLI，无法验证** |
| 8 | 关闭自动协作后无新自动分派；全部停止后无新启动 | **部分通过**（自动协作属 P4） | 全部停止：`test_stopped_tree_never_starts_new_invocation`、`test_stop_request_is_persisted_and_blocks_new_starts`、`e2e` 第 7 步。**本题另有"暂停"能力**（`test_pause_blocks_new_scheduling_without_cancelling`、`test_pause_blocks_new_start_and_resume_unblocks`），但计划所列的**自动协作开关**仍未实现（P4） |
| 9 | 进程崩溃、远程超时、重复事件：不盲目重跑，不错误宣称远程停止 | **部分通过**（真实远程超时属 P3） | 崩溃：`test_crash_before_start_becomes_unknown_without_restart`、`test_crash_after_start_confirms_running_and_holds_slot`、`test_recovery_is_idempotent_and_never_restarts`。重复事件：`test_release_is_idempotent_on_repeat_calls`、`test_duplicate_stop_is_idempotent`。**本地超时已实现**（`test_overdue_invocation_is_timed_out_and_releases_slot`、`test_timeout_reports_unconfirmed_remote_stop`：无法取消时如实报告 `remote_stop_confirmed=False`）。**真实适配器的远程超时行为未验证** |
| 10 | SSE 断线恢复后状态和事件顺序一致；日志和仓库不含真实密钥 | **部分通过**（SSE 属 P3/P5） | 仓库无密钥：`tests/test_hygiene.py`（扫描全部源文件 + token 取自环境变量 + 生成物已 ignore）。**SSE 未实现**：`GET /api/tasks/{id}/events` 是普通列表，无 SSE、无断线续传序号 |

**汇总：10 条中 6 条完全通过，4 条部分通过**，其中
CLI 拦截（P3）、自动协作开关（P4）、真实远程超时（P3）、SSE（P3/P5）为**阶段归属明确的未验证项**。

---

## C. 计划 §10 首个演示

> 用户聊天 → 手动分派两个 Mock 子 Agent → 未同意不启动 → 同意后执行 → 达到上限拒绝新增 → 停止按钮生效

**通过** — `tests/e2e/test_demo_flow.py::test_p2_demo_flow` 走真实 HTTP 接口 + 文件型库：

1. 用户聊天 → acked
2. 主 Agent 调用 → 用户批准 → drain → 启动数 **1**
3. 手动分派两个子 Agent → 均 `awaiting_approval`
4. **未同意**：drain → 启动数仍为 **1**
5. **同意后执行**：批准两者 → drain → 启动数 **3**
6. **达到上限拒绝新增**：第 3 个子 Agent 批准后 drain → 仍为 **3**（`launched_total==2`、`concurrent==2`）
7. **停止按钮生效**：取消 **3** 个运行中调用、`concurrent→0`、`launched_total` 保持 **2**；停止后再批准新调用并 drain → 仍为 **3**

---

## D. 计划 §5 拟定接口核对

| 计划接口 | 实现 | 备注 |
|---|---|---|
| `GET /api/agents` | ✅ | 返回能力列表（Mock） |
| `POST /api/agents` | ✅ | 登记（Mock，未持久化） |
| `POST /api/conversations` | ✅ | **本轮补齐** |
| `POST /api/conversations/{id}/messages` | ✅ | 回执；调用草稿属 P3 |
| `POST /api/tasks/{id}/delegations` | ✅ | 提议子任务，不执行 |
| `POST /api/invocations` | ✅ | 创建待审批调用 |
| `POST /api/approvals/{id}/approve` | ✅ | 仅 user |
| `POST /api/approvals/{id}/reject` | ✅ | 仅 user |
| `POST /api/approvals/{id}/revoke` | ✅ | 仅 user |
| `PATCH /api/tasks/{id}/policy` | ✅ | **本轮补齐 PATCH**（POST 保留为兼容别名），仅 user |
| `GET /api/tasks/{id}/events` | ⚠️ | 普通列表，**非 SSE**（SSE 属 P3/P5） |
| `POST /api/tasks/{id}/pause` | ✅ | **本轮补齐**；阻止后续调度，不取消运行中调用 |
| `POST /api/tasks/{id}/stop` | ✅ | 持久化 + 取消全树 |

计划外但必要的补充端点：`POST /api/tasks/{id}/resume`、
`POST /api/invocations/{id}/reconcile`（用户裁决 unknown）、
`POST /api/worker/drain`、`POST /api/worker/recover`、`POST /api/worker/reap`（执行超时）。

---

## E. 调用状态机核对

计划 §5 状态链：`draft → awaiting_approval → queued → starting → running →
succeeded/failed/canceled/timed_out/unknown`，拒绝直接进 `rejected`。

**本轮已全部落地，且每个状态都有测试证明可达**（`tests/policy/test_state_machine.py`）：

| 状态 | 何时进入 | 测试 |
|---|---|---|
| `draft` | 创建调用 / 审批被撤销 | `test_chain_draft_awaiting_queued_then_running`、`test_revoked_approval_returns_invocation_to_draft` |
| `awaiting_approval` | 存在待批审批 | 同上、`test_retry_requires_a_new_approval_and_does_not_auto_start` |
| `queued` | 用户批准 | 同上 |
| `starting` | 消费门原子消费 + 预留名额 | `test_starting_is_reachable_at_the_execution_gate` |
| `running` | 适配器接受 | `test_chain_draft_awaiting_queued_then_running` |
| `rejected` | 用户拒绝 | `test_rejected_state_is_reachable` |
| `timed_out` | 超过 `run_time_limit_seconds` | `test_overdue_invocation_is_timed_out_and_releases_slot` |
| `unknown` | 恢复无法确认 | `test_crash_before_start_becomes_unknown_without_restart` |
| `invalid` | 快照/策略失效 | `test_consume_refuses_when_approval_survived_but_version_moved` |

**消费门新增前置校验**：调用必须处于 `queued`（或 `awaiting_approval`）才可消费，
否则 `not_queued`（`test_consume_requires_queued_state`）。

---

## F. unknown 的用户裁决入口（本轮新增）

`unknown` 的调用**保留名额**（不提前释放）。此前没有任何人工出口，会永久占用并发名额。
现提供**用户专用**的裁决入口 `POST /api/invocations/{id}/reconcile`（仅 `user` 角色）：

| 动作 | 行为 | 测试 |
|---|---|---|
| `settle` + `outcome` | 用户认定真实结果，调用转终态并**幂等**释放名额；再次调用被拒（`not_unknown`），不会二次释放 | `test_settle_unknown_releases_slot_once`、`test_settle_rejects_bad_outcome` |
| `retry` | 旧调用结算为 `failed`（被取代）并释放名额；**新建**调用 + **新的待批审批**，快照携带**当前策略版本**；**不自动启动** | `test_retry_requires_a_new_approval_and_does_not_auto_start`、`test_retry_snapshot_carries_current_policy_version` |

API 层：`test_reconcile_settle_via_api`、`test_reconcile_retry_requires_new_approval_via_api`、
`test_reconcile_requires_user_and_valid_action`（Agent→403、非法动作→422）、
`test_reconcile_refuses_non_unknown`（非 unknown→409）。

---

## G. P2 结论

- **P2 自身的定义（§6 阶段表）全部达成**：批准、拒绝、过期、计数、并发、停止均以 Mock 验证，
  "未批准绝不调用适配器"有大量 `start_count == 0` 断言支撑。
- **§10 首个演示链路完整通过。**
- **P2 内部遗留已清零**：状态机三状态落地、`unknown` 人工裁决入口补齐、接口契约对齐。
- **不宣称整个项目安全阀全部通过。** 4 条部分通过的条目均有明确阶段归属与未验证标注：

| 条目 | 阶段 | 未验证原因 |
|---|---|---|
| CLI 拦截 Agent 绕过中转站 | **P3** | P2 无 CLI 进程可拦截 |
| 自动协作开关关闭后无新自动分派 | **P4** | 该功能属 P4 受控自动协作 |
| 真实适配器的远程超时行为 | **P3** | 本地超时已实现；外部系统超时语义需真实适配器验证 |
| SSE 断线恢复与事件顺序一致 | **P3/P5** | 事件为普通列表，SSE 属后续阶段 |

- **未使用任何真实 Agent 接口**，符合本阶段约束，可以进入 P3。
