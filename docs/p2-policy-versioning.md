# P2 策略持久化、版本递增与旧审批失效

日期：2026-10-14。范围：策略持久化 / 版本化 / 失效，以及**并发配额预留的原子性修复**。
仅使用 Mock Agent，不调用真实产品接口。

## 1. 策略持久化与版本化

新增 `PolicyService.update_policy(root_task_id, new_policy, updated_by=...)`：

- 策略保存在**根任务行**（`TaskRow.policy` + `TaskRow.policy_version`），因此作用于
  **整棵根任务树**；Worker 通过根行读取，不依赖外部传参。
- 每次更新写入一条 `policy_versions` 记录并把旧的置为 `active=False`，
  **版本号递增**（`v1 -> v2 -> v3`），保留审计轨迹。
- 同时写入 `policy_updated` 事件，记录 `updated_by` / `version` / 被撤销的审批数。
- API 端点 `POST /api/tasks/{id}/policy` 现在**真正持久化**（此前只构造对象就返回，
  是空实现）；仅 `user` 角色可调用。

## 2. 旧审批失效（两道防线）

**第一道：撤销。** 更新策略时，把该树中所有**未消费**的审批
（`pending` / `approved`）置为 `revoked`。已 `consumed` 的审批与**正在运行**的调用
**不受影响**——不会自动取消正在跑的调用。

**第二道：消费时校验版本。** `consume` 在步骤 1b 比较
`invocation.snapshot.policy_version` 与根任务当前 `policy_version`；不一致即把调用标记
`invalid` 并抛 `policy_changed`。即使某条审批因任何原因没被撤销，也无法在旧策略下启动。

此外，子任务创建与调用快照现在继承根任务**当前**版本，避免策略更新后新建的调用被误判失效。

## 3. 降低上限不重置累计、不取消运行中调用

- `launched_total` 是**累计**计数，只在预留时 `+1`，**永不回退**；`release` 只减少
  `concurrent`。因此降低上限不会把已用量清零。
- `update_policy` 只改策略与撤销未消费审批，不触碰 `starting` / `running` 的调用。

## 4. 非法参数回滚

`Policy.__post_init__` 现在校验全部字段：非整数、负数、`max_agent_invocations < 1`、
`run_time_limit_seconds < 1`、以及 `concurrent > total` 均抛 `ValueError`。

API 在开事务**之前**先校验（返回 422 `invalid_policy`）；服务层若收到非法策略，
`ValueError` 向上传播并使事务回滚。两条路径都保证**不写入任何东西**：不递增版本、
不写 `policy_versions`、不撤销审批。

## 5. 并发配额预留的原子性修复（本次发现并修复的真实缺陷）

**问题**：原 `consume` 的配额检查是"先读后写"。4 条**不同**调用的测试暴露出
4 个并发事务都读到 `launched=0 / concurrent=0`，于是**全部通过**，
`max_subagents_total=1` 被突破（实测 `['ok','ok','ok','ok']`）。
审批的 CAS 只保护**同一条**审批，不能保护"不同调用争抢同一名额"。

**修复**：新增 `tree_quota` 表（每棵树一行：
`launched_total` / `concurrent` / `total_invocations`），配额预留改为
**单条条件更新（CAS）**，把上限写进 `WHERE`：

```sql
UPDATE tree_quota
   SET total_invocations = total_invocations + 1,
       launched_total    = launched_total + 1,      -- 仅子 Agent
       concurrent        = concurrent + 1           -- 仅子 Agent
 WHERE root_task_id = :tree
   AND total_invocations < :max_agent_invocations
   AND launched_total    < :max_subagents_total     -- 仅子 Agent
   AND concurrent        < :max_subagents_concurrent -- 仅子 Agent
```

`rowcount == 1` 才算拿到名额；落败者得到明确的配额错误码（`max_subagents_total` /
`max_subagents_concurrent` / `max_agent_invocations`），且不预留任何东西。
`release` 用 `concurrent = MAX(concurrent - 1, 0)` 原子释放并发名额，
**不动** `launched_total`。

## 6. 验证结果

命令：`python -m pytest -v`（仓库根目录，使用 `.venv`）

```
tests\http\test_auth.py ...........              11
tests\http\test_policy_api.py ...                  3
tests\persistence\test_shared_db.py ......         6
tests\policy\test_approval.py .......              7
tests\policy\test_consumable_approval.py .......   7
tests\policy\test_policy_versioning.py ........... 14
tests\policy\test_quota.py ......                  6
tests\policy\test_stop_and_authz.py ....           4
tests\policy\test_worker_atomicity.py ...          3
61 passed in 6.33s   exit code 0
```

**并发用例重复 10 轮**（4 项 × 10 = 40 次执行）全部 `exit=0`，确认修复非偶然通过。

本次新增的验收点：

| 验收点 | 用例 |
|---|---|
| 策略持久化 + 版本递增 + 旧版本保留 | `test_policy_persists_and_version_increments` |
| 更新写入事件并记录操作者 | `test_policy_update_emits_event_and_records_actor` |
| 重启后策略保留且**仍被执行** | `test_policy_restart_is_preserved` |
| 策略作用于**整棵树**（含后代） | `test_policy_applies_to_whole_tree_after_update` |
| 撤销未消费审批，保留已消费与运行中 | `test_policy_update_voids_unconsumed_but_keeps_consumed_and_running` |
| 降低上限不重置累计、不取消运行中 | `test_lowering_limit_keeps_cumulative_and_running` |
| 非法参数不写入任何东西 | `test_illegal_policy_writes_nothing`（4 组参数） |
| 策略变更后旧审批不可执行（第一道防线） | `test_consume_is_refused_after_policy_change` |
| 审批被强行复活仍不可执行（第二道防线） | `test_consume_refuses_when_approval_survived_but_version_moved` |
| **最后一个并发名额竞争** | `test_concurrent_race_for_last_concurrent_slot`（4 线程抢 `concurrent=1`，恰好 1 成功） |
| 策略更新与消费竞争 | `test_policy_update_racing_consume_never_uses_stale_policy` |
| HTTP 正向：持久化 + 撤销；非法 422；Agent 403 | `tests/http/test_policy_api.py`（3 项） |

## 7. 仍未验证（不得当作已通过）

- **进程被强杀后的收敛**：`starting` 状态下崩溃的 `unknown` 收敛与状态重查策略仍未实现。
- **取消正在运行的调用**：停止后不新调度已验证；适配器 `cancel` 已接通
  `Worker.cancel_tree`（2026-10-14 起：只有**确认**停止才结算 `canceled` 并释放名额，
  未确认则置 `unknown` 并保留名额，见 `docs/p2-stop-and-recovery.md` §2.1）。
  **真实适配器的取消时延与语义仍未验证**（Codex 目前返回 `unsupported`）。
- **并发规模**：验证到 4 线程 / 同机 SQLite；更高并发、跨机、以及 `busy_timeout`
  超阈值（>30s）时的失败语义未测。
- **策略更新的权限边界**：仅验证 HTTP 层角色控制；未验证越权修改**他人**项目策略
  的多用户场景（P2 只有单用户 `local-user`）。
- 真实适配器接入（P3）。

以上需单独实现与验证后才能宣称 P2 完整验收。
