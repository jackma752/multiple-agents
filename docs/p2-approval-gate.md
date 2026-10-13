# P2 审批消费入口修复

日期：2026-10-14。范围：Mock 内核的审批状态校验。

## 契约

只有 `approved` 可以进入消费路径，合法授权链为
`pending -> approved -> consumed`。`pending` 表示尚未同意；
`rejected / expired / revoked / consumed` 均不可消费。
`CONSUMABLE_APPROVAL_STATUSES` 与此规则保持一致。

`PolicyService._find_consumable_approval` 只查询 `approved`。
`consume` 在任何配额预留、状态更新或启动事件之前再次检查状态；
若没有合格审批，返回 `no_approval`，若查询错误地返回非批准状态，
返回 `not_approved`。错误不应造成消费、配额占用或启动。

这是消费入口的状态检查修复，未改变适配器接口和能力声明。
对 pending/approved 的过期扫描仍保留：扫描待定审批不代表允许消费它。

## 回归验证

- 原有 `test_pending_approval_is_not_consumable` 在修复前实际失败：
  `consume` 返回成功，`consumed is False` 断言失败。
- 新测试验证直接消费 pending 后审批、调用、配额和事件保持不变，
  Worker 的 Mock 适配器启动次数为 0。
- 强制查询返回五种非批准状态时，消费入口独立拒绝且不预留配额。
- 同一调用存在更早 pending 与更晚 approved 记录时，只消费 approved，
  保留 pending，已批准调用启动一次。

验证命令：在仓库目录执行 `python -m pytest tests/policy -v`。
在独立修复 worktree 中实际收集并通过 24 个策略测试（2026-10-14）：
`test_approval.py` 7 个、`test_quota.py` 6 个、
`test_stop_and_authz.py` 4 个、`test_consumable_approval.py` 7 个。
同步回主目录后，以同一命令复验：`24 passed in 2.11s`。

本修复不能作为完整 P2 安全验收：真实并发消费、崩溃恢复以及
HTTP 审批端点的用户身份鉴权需要独立验证。
