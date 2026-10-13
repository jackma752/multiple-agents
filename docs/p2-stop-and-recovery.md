# P2 停止/取消与崩溃恢复

日期：2026-10-14。范围：停止请求持久化并接通 cancel、配额释放幂等、以及崩溃恢复对账。
仅使用 Mock Agent，不调用真实产品接口。

## 1. Mock 适配器：可持久化、按 invocation_id 查询的执行记录

`packages/adapters/agenthub_adapters/mock.py` 新增 `MockExecutionStore`：
一张独立 SQLite 表 `mock_executions(invocation_id PK, adapter_invocation_id, status,
detail, started_at, updated_at)`，状态为 `running / finished / failed / cancelled`。

它刻意**不复用 kernel 的 SQLAlchemy 元数据**（自己的表、自己的连接），以诚实模拟
"外部系统自己的状态"这一边界：kernel 崩溃后，唯一能判断"这次调用到底启动了没有"
的方式就是**问外部那一侧**。

`MockAdapter` 新增 `lookup_by_invocation(invocation_id)`、`cancel_invocation(...)`，
并在 `start()` 返回**之前**先落库，因此"启动后立刻崩溃"也不会丢失证据。

## 2. 停止请求：先持久化，再接通 cancel

`PolicyService.request_stop(root_task_id, requested_by)`：把停止**持久化**为
`tree_stop` 事件（带 `requested_by`，重复停止会标记 `repeat`），并返回当前
`starting`/`running` 的调用清单。

`Worker.cancel_tree(...)`：**先**持久化停止请求，**再**对每条在飞调用调用
`adapter.cancel_invocation`，然后把调用置 `canceled` 并释放其名额。

这个顺序是关键：即使取消阶段失败或进程在中途死掉，**已落库的停止请求**仍保证
该树不会再被调度新调用。适配器不支持取消时返回 `unsupported`，不伪造成功。

API `POST /api/tasks/{id}/stop` 现走 `cancel_tree`（此前只持久化、不取消），
返回 `already_stopped` 与 `cancelled` 计数；仅 `user` 角色可调用。

## 3. 配额释放幂等（按预留记录）

`release()` 改为按**预留记录**做条件更新（CAS）：

```sql
UPDATE quota_reservations SET released_at=:t WHERE id=:id AND released_at IS NULL
```

只有 `rowcount==1`（即**本次调用真正认领了**这条预留）才对
`tree_quota.concurrent` 减 1，且 `concurrent = MAX(concurrent-1, 0)`。
因此重复调用 `release()`、重复停止、或"取消与正常完成竞争"，都只释放**一次**名额；
`launched_total`（累计）始终不动。

## 4. 崩溃恢复：核对 starting/running，不确定即 unknown

`Worker.recover()` 遍历所有 `starting` / `running` 的调用，向外部侧（适配器）
按 `invocation_id` 查询，然后：

| 外部侧结果 | kernel 处理 | 名额 |
|---|---|---|
| `running`（确认在跑） | 置为 `running`，补记 `adapter_invocation_id` | **保留** |
| `finished` / `failed` / `cancelled` | 结算为对应终态 | 释放**一次** |
| **无记录 / 无法识别** | 置为 `unknown`（记录原因） | **不释放** |

三条硬性约束（方案 §4.1「执行状态未知...不能盲目重试」）：

1. **绝不自动重启**任何调用（`summary["restarted"]` 恒为空）。
2. **不确定就不提前释放名额**——无法证明外部调用已停止时释放名额，会让新调用
   占用一个可能仍在运行的名额。因此 `unknown` 保持占位。
3. 恢复**幂等**：重复执行不会重复结算或重复释放。

API `POST /api/worker/recover` 暴露该流程，要求 `user` 角色（恢复会结算调用并释放
名额，不应由 Agent/Worker 身份触发）。

## 5. 本次修复的真实缺陷

新增的"消费时校验策略版本"暴露了一个**HTTP 层潜在缺陷**：
`POST /api/invocations` 构造快照时**没有传 `policy_version`**（默认 0），
而根任务为 v1，导致**所有经 HTTP 创建的调用在 consume 时都被判失效**。
已修复为携带 `task.policy_version`。

## 6. 验证结果

命令：`python -m pytest -v`（仓库根目录，使用 `.venv`）

```
tests\http\test_auth.py ...........              11
tests\http\test_policy_api.py ...                  3
tests\http\test_stop_recover_api.py ...            3   ← 新增
tests\persistence\test_shared_db.py ......         6
tests\policy\test_approval.py .......              7
tests\policy\test_consumable_approval.py .......   7
tests\policy\test_policy_versioning.py ........... 14
tests\policy\test_quota.py ......                  6
tests\policy\test_recovery.py ........             8   ← 新增
tests\policy\test_stop_and_authz.py ....           4
tests\policy\test_stop_cancel.py ........          8   ← 新增
tests\policy\test_worker_atomicity.py ...          3
80 passed in 7.78s   exit code 0
```

并发/恢复敏感用例**重复 10 轮**（7 项 × 10 = 70 次执行）全部 `exit=0`。

新增验收点：

| 验收点 | 用例 |
|---|---|
| 停止请求持久化并阻止新调度 | `test_stop_request_is_persisted_and_blocks_new_starts` |
| 停止请求记录操作者与运行中清单 | `test_stop_request_reports_running_and_persists_actor` |
| cancel 接通并释放名额（累计不返还） | `test_cancel_tree_stops_in_flight_call_and_releases_slot` |
| **重复停止幂等** | `test_duplicate_stop_is_idempotent` |
| 重复 release 只释放一次 | `test_release_is_idempotent_on_repeat_calls` |
| **取消与完成竞争只释放一次** | `test_cancel_racing_completion_releases_once` |
| cancel 不覆盖已完成调用 | `test_cancel_does_not_stomp_a_finished_call` |
| 不支持取消的适配器如实返回 | `test_adapter_without_cancel_reports_unsupported` |
| **崩溃窗口 A**：启动前崩溃 → unknown、不重启、不释放 | `test_crash_before_start_becomes_unknown_without_restart` |
| **崩溃窗口 B**：启动后崩溃 → 确认 running、保留名额、不重启 | `test_crash_after_start_confirms_running_and_holds_slot` |
| **崩溃窗口 C**：启动后外部已终态 → 结算并释放一次 | `test_crash_after_start_settles_confirmed_terminal`（3 参数） |
| 无法识别的外部状态 → unknown | `test_unrecognised_external_state_becomes_unknown` |
| 恢复幂等且从不重启 | `test_recovery_is_idempotent_and_never_restarts` |
| 恢复不触碰已结算调用 | `test_recovery_does_not_touch_settled_invocations` |
| HTTP 停止/取消与恢复鉴权 | `tests/http/test_stop_recover_api.py`（3 项） |

## 7. 仍未验证（不得当作已通过）

- **真实进程强杀**：测试用"新 Worker + 新适配器 + 同库文件"模拟重启，
  未用 `kill -9` 在写入中途打断事务、也未验证 WAL 未 checkpoint 时的行为。
- **`unknown` 的人工收敛路径**：未知调用保持占位后，**没有**提供"人工确认后释放/
  重试"的接口；这是有意的保守设计，但需要 P3 补一个人工裁决入口，否则名额会
  永久占用。
- **取消的时延**：`cancel_invocation` 是同步调用；未验证真实适配器的取消是否阻塞、
  是否需要超时与重试。
- **多树并发恢复**：恢复按全库扫描，未验证大量 `starting/running` 时的性能。
- 真实适配器接入（P3）。

以上需单独实现与验证后才能宣称 P2 完整验收。
