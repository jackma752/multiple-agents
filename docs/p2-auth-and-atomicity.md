# P2 审批权限与原子执行门

日期：2026-10-14。范围：HTTP 审批鉴权 + Worker 事务边界 + 审批消费原子性。
仅使用 Mock Agent，不调用真实产品接口。

## 1. 用户审批权限（HTTP 层）

方案 §4.1 要求"只有登录的用户通过审批端点才能批准；Agent、Worker、插件和模型输出
没有审批权限"。实现见 `apps/api/agenthub_rt/auth.py`：

- 请求须携带 `Authorization: Bearer <token>`（或 `X-Agent-Hub-Token`）。
  无 token → **401 `unauthenticated`**，不存在匿名放行。
- token 映射为 `Principal(identity, role)`，角色为 `user / agent / worker / service`。
- 只有 `user` 角色通过 `require_approver`；`agent` / `worker` 身份**认证通过但被
  **403 `not_permitted`** 拒绝**，因此 Agent 即使拿到服务 token 也不能批准自己。
- 受保护端点：`/approvals/{id}/approve|reject|revoke`、`/tasks/{id}/policy`、
  `/tasks/{id}/stop`。
- 用户 token 从环境变量 `AGENT_HUB_USER_TOKEN` 读取，**不写进代码**（AGENTS.md 规则 4）。

## 2. Worker 事务边界（先提交，后启动）

修复前：`adapter.start()` 在 `with session` 内、`commit()` 之前调用——若提交失败，
适配器已经启动，违反 §4.1"原子地消费审批并预留名额，**之后**才启动适配器"。

修复后 `Worker._process_one` 分三段，见 `apps/worker/agenthub_kernel/worker.py`：

1. **Phase 1**：事务内 `consume()`（校验 + 原子消费 + 预留名额），退出上下文时
   **提交**。任何失败都抛错，不进入 Phase 2。
2. **Phase 2**：提交成功后才 `adapter.start()`。
3. **Phase 3**：新事务记录结果（running / failed）。

## 3. 审批消费的原子性（CAS）

`PolicyService.consume` 用**条件更新**消费审批，而非读后写：

```sql
UPDATE approvals SET status='consumed', consumed_at=:t
 WHERE id=:id AND status='approved'
```

`rowcount != 1` 即判定并发落败，抛 `already_consumed`，且在**预留配额、写事件、
启动适配器之前**拒绝。这使"同一审批并发消费最多启动一次"在真实并发下成立。

## 4. 数据库与并发验证方式

`Database` 现在区分两种 SQLite：

- 内存库（`:memory:`）：用 `StaticPool` 共享单连接，便于顺序测试；**不能**表达
  多连接并发。
- 文件库：每 session 独立连接，启用 `journal_mode=WAL` 与 `busy_timeout=30000`，
  使两个 Worker 能真实交错而不直接报 "database is locked"。

因此并发用例（`tests/policy/test_worker_atomicity.py`）使用**文件型 SQLite**，
不再依赖内存库的顺序执行。

## 5. 验证结果

命令：`python -m pytest -v`（仓库根目录，使用 `.venv`）

```
tests\http\test_auth.py ...........              11
tests\policy\test_approval.py .......             7
tests\policy\test_consumable_approval.py .......  7
tests\policy\test_quota.py ......                 6
tests\policy\test_stop_and_authz.py ....          4
tests\policy\test_worker_atomicity.py ...         3
38 passed in 3.67s   exit code 0
```

其中本次新增/覆盖的验收点：

| 验收点 | 用例 |
|---|---|
| 未登录 / Agent / Worker / 伪造 token 批准 → 拒绝且启动次数 0 | `test_auth.py::test_non_user_cannot_approve`（4 参数） |
| 登录用户可批准 | `test_auth.py::test_user_can_approve` |
| 非用户不得改配额、不得停止任务树 | `test_auth.py::test_non_user_cannot_change_policy` / `_stop_tree` |
| 消费事务提交失败 → 不启动适配器 | `test_worker_atomicity.py::test_adapter_starts_only_after_consume_is_committed` |
| 两线程抢同一审批 → 恰好一次成功、仅一条名额预留 | `test_concurrent_consume_starts_at_most_once` |
| 结束释放并发名额但不返还累计总量 | `test_release_does_not_return_cumulative_total` |

## 6. 仍未验证（不得当作已通过）

- **重启/故障恢复**：进程崩溃后不重复调用、`unknown` 状态收敛。
- **策略版本变化**：更新策略后旧审批是否立即失效（`_maybe_invalidate_if_task_changed`
  目前仍是空实现；策略更新端点尚未持久化新策略）。
- **API 与 Worker 共用同一数据库文件**：目前 API 默认内存库、Worker 默认文件库。
- **取消正在运行的调用**的端到端行为。
- 真实适配器的鉴权与配额（本阶段全部为 Mock）。

以上属 P2 后续项，需单独实现与验证后才能宣称 P2 完整验收。
