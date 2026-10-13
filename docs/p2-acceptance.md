# P2 完整验收对照（AGENT_HUB_PLAN §6 / §10）

日期：2026-10-14。范围：对照计划逐条核对 P2 的验收条款与"必须通过的安全阀用例"。
全部为 Mock Agent，未接入任何真实产品。

验证命令（仓库根目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

结果：**99 passed in 10.69s，exit code 0**。

---

## A. 计划 §6 阶段表对 P2 的定义

> P2：审批和限额核心 | 3–5 天 | 用 Mock Agent 验证批准、拒绝、过期、计数、并发和停止；未批准绝不调用适配器

| 要求 | 状态 | 证据 |
|---|---|---|
| 验证**批准** | 通过 | `test_approved_invocation_starts_adapter_once`、`e2e/test_demo_flow.py` |
| 验证**拒绝** | 通过 | `test_rejected_approval_never_reaches_adapter`、`test_demo_flow_rejection_and_expiry_never_start` |
| 验证**过期** | 通过 | `test_expired_approval_never_reaches_adapter`、同上 |
| 验证**计数** | 通过 | `test_total_limit_blocks_new_subagent`、`test_lowering_limit_keeps_cumulative_and_running` |
| 验证**并发** | 通过 | `test_concurrent_limit_blocks_when_at_cap`、`test_concurrent_race_for_last_concurrent_slot`、`test_concurrent_distinct_invocations_do_not_exceed_total_cap` |
| 验证**停止** | 通过 | `test_stop_cancel.py`（8 项）、`e2e/test_demo_flow.py` 第 7 步 |
| **未批准绝不调用适配器** | 通过 | 所有用例均以 Mock 适配器 `start_count` 断言；未批准路径实测为 0 |

**结论：计划 §6 对 P2 的定义全部通过。**

---

## B. 计划 §6「必须通过的安全阀用例」逐条核对

该列表是整个项目的验收线，部分条目按阶段表属 P3–P5。以下逐条给出状态与证据。

| # | 用例 | 状态 | 证据 / 说明 |
|---|---|---|---|
| 1 | 无审批、审批过期、审批被撤销：适配器启动次数为零 | **通过** | `test_unapproved_invocation_never_reaches_adapter`、`test_expired_approval_never_reaches_adapter`、`test_revoked_approval_never_reaches_adapter`、`test_pending_approval_is_not_consumable`、`test_pending_consume_does_not_change_approval_or_reserve_resources` |
| 2 | 一个审批被重复提交或由两个 Worker 同时处理：最多一次正常启动 | **通过** | `test_double_approval_consumed_once`、`test_concurrent_consume_starts_at_most_once`（文件库 + 独立连接 + barrier，且重复 10 轮） |
| 3 | 批准后输入、权限、Agent 或策略变化：旧批准无效 | **通过** | 输入/权限/Agent/资源/深度：`test_snapshot_binding.py`（12 项，含无重算哈希与重算哈希两条路径）；策略：`test_consume_is_refused_after_policy_change`、`test_consume_refuses_when_approval_survived_but_version_moved` |
| 4 | 总子实例 3、并发 2：全树不超过 3，同时不超过 2；失败不恢复累计总量 | **通过** | `test_total_limit_blocks_new_subagent`、`test_concurrent_limit_blocks_when_at_cap`、`test_releasing_concurrent_slot_allows_next`、`test_release_does_not_return_cumulative_total`、`test_lowering_limit_keeps_cumulative_and_running` |
| 5 | 总子实例 0：用户手动分派、自动分派和孙 Agent 分派均被拒绝 | **通过** | `test_total_zero_blocks_all_subagents`（手动）、`test_total_zero_blocks_auto_delegation_too`（自动）、`test_total_zero_blocks_grandchild_dispatch`（孙） |
| 6 | 深度 1：子 Agent 不能再生成子 Agent | **通过** | `test_depth_limit_blocks_grandchild`（`max_depth=1` 时创建 depth=2 被拒） |
| 7 | Agent 尝试批准自己、增加上限或通过 CLI 绕过中转站：被权限边界阻止 | **部分通过** | 批准自己：`test_non_user_cannot_approve`（agent/worker/无 token/伪造 token 四种，启动数 0）。增加上限：`test_non_user_cannot_change_policy`、`test_agent_cannot_update_policy_via_api`。**CLI 绕过：P2 无 CLI，无法验证**（属 P3 真实适配器范围） |
| 8 | 关闭自动协作后无新自动分派；全部停止后无新启动 | **部分通过** | 全部停止：`test_stopped_tree_never_starts_new_invocation`、`test_stop_request_is_persisted_and_blocks_new_starts`、`e2e` 第 7 步。**自动协作开关：未实现**（计划阶段表归 P4） |
| 9 | 进程崩溃、远程超时、重复事件：不盲目重跑，不错误宣称远程停止 | **部分通过** | 崩溃：`test_crash_before_start_becomes_unknown_without_restart`、`test_crash_after_start_confirms_running_and_holds_slot`、`test_recovery_is_idempotent_and_never_restarts`（`restarted` 恒空）。重复事件：审批 CAS + 释放幂等（`test_release_is_idempotent_on_repeat_calls`、`test_duplicate_stop_is_idempotent`）。**远程超时：`timed_out` 状态已定义但无超时enforcement**，未实现 |
| 10 | SSE 断线恢复后状态和事件顺序一致；日志和仓库不含真实密钥 | **部分通过** | 仓库无密钥：`tests/test_hygiene.py`（扫描全部源文件 + 确认 token 取自环境变量 + 生成物已 ignore）。**SSE 未实现**：`GET /api/tasks/{id}/events` 是普通列表，无 SSE、无断线续传序号 |

**汇总：10 条中 6 条完全通过，4 条部分通过**（其中 2 条属后续阶段：自动协作开关=P4、SSE=P3/P5；
2 条属 P2 遗留：远程超时 enforcement、CLI 绕过无法在 Mock 验证）。

---

## C. 计划 §10 首个演示

> 用户聊天 → 手动分派两个 Mock 子 Agent → 未同意不启动 → 同意后执行 → 达到上限拒绝新增 → 停止按钮生效

**通过** — `tests/e2e/test_demo_flow.py::test_p2_demo_flow` 完整走通该链路（走真实 HTTP 接口 + 文件型库）：

1. 用户聊天：`POST /api/conversations/c1/messages` → acked
2. 主 Agent 调用 → 用户批准 → drain → 启动数 **1**
3. 手动分派两个子 Agent：`POST /api/tasks/{id}/delegations` ×2 → 均为 `awaiting_approval`
4. **未同意**：drain → 启动数仍为 **1**（未批准不启动）
5. **同意后执行**：批准两者 → drain → 启动数 **3**
6. **达到上限拒绝新增**：第 3 个子 Agent 批准后 drain → 启动数仍为 **3**（`launched_total==2`、`concurrent==2`）
7. **停止按钮生效**：`POST /api/tasks/{id}/stop` → 取消 **3** 个运行中调用、`concurrent` 归 **0**、`launched_total` 保持 **2**（累计不返还）；停止后再批准新调用并 drain → 启动数仍为 **3**

---

## D. 计划 §5 拟定接口核对

| 计划接口 | 实现 | 备注 |
|---|---|---|
| `GET /api/agents` | ✅ | 返回能力列表（Mock） |
| `POST /api/agents` | ✅ | 登记（Mock，未持久化） |
| `POST /api/conversations` | ⚠️ 未实现 | 仅有 `POST /api/conversations/{id}/messages` |
| `POST /api/conversations/{id}/messages` | ✅ | 回执；未产生调用草稿（P3 接） |
| `POST /api/tasks/{id}/delegations` | ✅ | 提议子任务，不执行 |
| `POST /api/invocations` | ✅ | 创建待审批调用 |
| `POST /api/approvals/{id}/approve` | ✅ | **仅 user 角色** |
| `POST /api/approvals/{id}/reject` | ✅ | 仅 user |
| `POST /api/approvals/{id}/revoke` | ✅ | 仅 user |
| `PATCH /api/tasks/{id}/policy` | ✅（用 POST） | 仅 user；增加策略版本；**方法应为 PATCH** |
| `GET /api/tasks/{id}/events` | ⚠️ | 普通列表，**非 SSE** |
| `POST /api/tasks/{id}/pause` | ❌ 未实现 | 阻止后续调度 |
| `POST /api/tasks/{id}/stop` | ✅ | 持久化 + 取消全树 |

额外实现（计划外但必要）：`POST /api/worker/drain`、`POST /api/worker/recover`。

---

## E. 调用状态机核对

计划 §5：`draft → awaiting_approval → queued → starting → running → succeeded/failed/canceled/timed_out/unknown`，拒绝直接进 `rejected`。

`packages/contracts/agenthub/contracts.py::InvocationStatus` 定义了全部状态。
**实测走过的路径**：`draft → starting → running → canceled / succeeded`，
以及 `draft → invalid`（快照/策略失效）、`starting → unknown`（恢复无法确认）。

**未实际走通**：`awaiting_approval`、`queued`、`timed_out`。这三个是已定义但
未被任何流程设置的"空状态"——调用创建后停留在 `draft`，`consume` 直接置 `starting`。
属 P2 的模型与流程不完全一致，需在 P3 收敛（或明确删除未用状态）。

---

## F. P2 结论

- **P2 自身的定义（§6 阶段表）全部达成**：批准、拒绝、过期、计数、并发、停止均已用 Mock 验证，
  且"未批准绝不调用适配器"有 99 个测试中的大量 `start_count == 0` 断言支撑。
- **§10 首个演示链路完整通过。**
- **不宣称整个项目安全阀全部通过**：§6 列表中 4 条部分通过，其中
  CLI 绕过（P3）、自动协作开关（P4）、SSE（P3/P5）属后续阶段；
  **远程超时 enforcement** 与**状态机中 `awaiting_approval`/`queued`/`timed_out` 三个空状态**
  是 P2 内部遗留，应在进入 P3 前决定"补齐或删除"。
- **未使用任何真实 Agent 接口**，符合本阶段约束。

## G. 建议进入 P3 前处理的两项

1. **状态机收敛**：要么让创建调用后进入 `awaiting_approval`、排队后进 `queued`，
   要么删除这三个未用状态，避免文档与实现不一致。
2. **`unknown` 的人工裁决入口**：恢复把无法确认的调用置为 `unknown` 并**保留名额**，
   但没有"人工确认后释放/重试"的接口，长期运行会永久占用并发名额。
