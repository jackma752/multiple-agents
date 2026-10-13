# Agent Hub

把 Codex、Claude Code 等 Agent 接入一个统一聊天入口的私人控制台。核心约束：**每次 Agent 调用先经用户同意，子 Agent 数量由用户限制**。

定义、方案与验收标准见 `E:\plug\AGENT_HUB_PLAN.md`。

## 当前状态：P2 阶段性基线（Mock 内核）

阶段：**P2 审批与配额内核（Mock）阶段性基线**。实现的是方案 §10 的"先做能演示审批与限制的 Mock 版本"，仅使用 Mock Agent，不调用任何真实产品接口。

**已验证的行为**（以 `tests/` 中的自动测试为准）：

- 数据模型：`tasks`（根任务树，含 `root_task_id`/`depth`）、`invocations`（含不可变输入快照 `payload_hash`）、`approvals`、`quota_reservations`、`policy_versions`、`events`。
- 调用状态机：`draft → awaiting_approval → queued → starting → running → succeeded/failed/canceled/timed_out/unknown/rejected/invalid`。
- **审批是执行门**：只有 `approved` 审批可以进入消费路径（`pending → approved → consumed`）。`_find_consumable_approval` 只查询 `approved`；`consume` 在预留配额 / 更新状态 / 启动事件之前再次检查状态，非批准状态返回 `not_approved` 或 `no_approval`，不会造成消费、配额占用或适配器启动。
- Worker 事务式消费：`consume` 校验审批有效性与停机状态、原子消费审批并预留并发名额后，才调用适配器；适配器启动次数可作为"未批准 = 未调用"的断言依据。
- 三道配额在**整个根任务树**作用：`max_subagents_total`（累计，失败不返还）、`max_subagents_concurrent`（并发，结束即释放）、`max_depth`。`max_subagents_total=0` 禁止一切子 Agent。
- 快照绑定：审批绑定 `payload_hash`，输入 / 权限 / Agent / 策略变化后旧批准失效。
- Mock 适配器：只记录"被启动次数"，用于断言未批准时启动次数为 0。

**当前 24 个策略测试全部通过**（`test_approval.py` 7、`test_quota.py` 6、`test_stop_and_authz.py` 4、`test_consumable_approval.py` 7），见 `docs/p2-approval-gate.md` 的回归说明。

> **这不等于完整 P2 安全验收。** 以下仍需独立验证：HTTP 审批端点的用户身份鉴权、两个 Worker 并发消费同一审批的最多一次启动、崩溃/重启恢复、以及策略版本变化对既有审批的失效。参见 `docs/p2-approval-gate.md` §"本修复不能作为完整 P2 安全验收"。

## 仓库约定

见 `AGENTS.md`。

## 目录结构

```text
agent-hub/
  README.md
  AGENTS.md
  .gitignore
  .gitattributes
  docs/          能力矩阵、架构、审计报告、P2 审批门说明
  apps/          api（FastAPI）/ worker（调度与 Mock 适配器）
  packages/      contracts（数据模型与状态机）/ adapters（Mock 适配器）
  tests/         policy（审批与配额回归测试）；adapters / e2e（待建）
```

## 本地开发（Mock 环境）

无需真实 Agent 账号即可启动与测试：

```powershell
# 1. 建虚拟环境并安装锁定依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 依赖安装方式，见 fetch_wheels.py / bootstrap_deps.py 及 wheelhouse（本机离线下载）

# 2. 运行策略测试
python -m pytest tests/policy -v
```

真实配置放在 `.env`（已 gitignore），不提交。适配器能力矩阵见 `docs/adapter-capabilities.md`。
