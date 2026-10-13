# Agent Hub

把 Codex、Claude Code 等 Agent 接入一个统一聊天入口的私人控制台。核心约束：**每次 Agent 调用先经用户同意，子 Agent 数量由用户限制**。

定义、方案与验收标准见 `E:\plug\AGENT_HUB_PLAN.md`。

## 当前状态：P2 阶段性基线（Mock 内核）

阶段：**P2 审批与配额内核（Mock）阶段性基线**。实现的是方案 §10 的"先做能演示审批与限制的 Mock 版本"，仅使用 Mock Agent，不调用任何真实产品接口。

**已验证的行为**（以 `tests/` 中的自动测试为准）：

- 数据模型：`tasks`（根任务树，含 `root_task_id`/`depth`）、`invocations`（含不可变输入快照 `payload_hash`）、`approvals`、`quota_reservations`、`policy_versions`、`events`。
- 调用状态机：`draft → awaiting_approval → queued → starting → running → succeeded/failed/canceled/timed_out/unknown/rejected/invalid`。
- **审批是执行门**：只有 `approved` 审批可以进入消费路径（`pending → approved → consumed`）。`_find_consumable_approval` 只查询 `approved`；`consume` 在预留配额 / 更新状态 / 启动事件之前再次检查状态，非批准状态返回 `not_approved` 或 `no_approval`，不会造成消费、配额占用或适配器启动。
- **审批权限在后端强制**：HTTP 审批端点须携带 Bearer token，只有 `user` 角色可批准 / 拒绝 / 撤销、改配额、停止任务树；无 token 返回 401，`agent` / `worker` 身份返回 403。因此 Agent 不能批准自己。
- **Worker 事务边界**：Worker 先在事务中消费审批并预留名额，**提交成功后**才启动适配器；提交失败则不会启动。
- **并发消费最多一次**：审批用条件更新（`WHERE status='approved'`）消费，两个 Worker 抢同一审批时只有一个成功，且只产生一条名额预留。
- **API 与 Worker 共用同一数据库文件**：路径锚定仓库根（`config.py`，不依赖工作目录），文件型 SQLite 启用 WAL 与 busy timeout；记录可跨进程读取、重启后保留，已消费的审批不会重复启动。
- 三道配额在**整个根任务树**作用：`max_subagents_total`（累计，失败不返还）、`max_subagents_concurrent`（并发，结束即释放）、`max_depth`。`max_subagents_total=0` 禁止一切子 Agent。
- 快照绑定：审批绑定 `payload_hash`，输入 / 权限 / Agent / 策略变化后旧批准失效。
- Mock 适配器：只记录"被启动次数"，用于断言未批准时启动次数为 0。

**当前 44 个测试全部通过**：`http/test_auth.py` 11、`persistence/test_shared_db.py` 6、`test_approval.py` 7、`test_consumable_approval.py` 7、`test_quota.py` 6、`test_stop_and_authz.py` 4、`test_worker_atomicity.py` 3。并发用例使用**文件型 SQLite + 独立连接**（内存库共享单连接，无法表达并发）。细节见 `docs/p2-approval-gate.md`、`docs/p2-auth-and-atomicity.md`、`docs/p2-shared-persistence.md`。

> **这不等于完整 P2 安全验收。** 以下仍需独立验证：进程被强杀后的 `unknown` 收敛与重查、策略版本变化使既有审批失效（`update_policy` 尚未持久化策略）、取消正在运行的调用。参见 `docs/p2-shared-persistence.md` §5。

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
