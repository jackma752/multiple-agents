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
- **配额预留是原子的**：每棵树一行计数器（`tree_quota`），预留用单条条件更新把上限写进 `WHERE`，多条**不同**调用争抢最后一个名额时也只有一条成功（"先读后写"的检查曾使 4 条全部通过，已修复）。
- **策略持久化与版本化**：策略存在根任务行，作用于整棵树；每次更新版本递增并保留旧版本与事件。更新会撤销**未消费**的审批，但不重置累计数量、不取消运行中的调用；非法参数不写入任何东西。
- **停止与取消**：停止请求先**持久化**（带操作者）、再接通适配器 `cancel`；取消或重复停止只释放一次名额（按预留记录 CAS）。适配器不支持取消时返回 `unsupported`。
- **暂停与恢复**：`POST /api/tasks/{id}/pause` 阻止后续调度但**不取消**运行中的调用；`/resume` 解除。
- **执行超时**：超过树策略 `run_time_limit_seconds` 的调用——取消**已确认**才置 `timed_out` 并释放名额；**未确认**（不支持取消 / 外部仍报 running / 证据丢失）则置 `unknown` 并**保留名额**，不谎称远程已停。
- **崩溃恢复**：重启后核对 `starting`/`running`，向外部侧按 `invocation_id` 查询；确认在跑的保留名额，确认终态的结算并释放一次，**无法确认的置 `unknown`、不自动重启、不提前释放名额**。
- **unknown 用户裁决**：`POST /api/invocations/{id}/reconcile`（仅 user）——`settle` 认定结果（人工确认）并幂等释放名额；`retry` **必须先确认旧执行已结束**（否则 409 `not_stopped`，不改旧状态、不释放名额、不新建调用），确认后释放一次并**新建调用 + 新待批审批**（重试必须重新审批，不自动启动）。
- **统一原则**：并发名额只有在能确认对应执行已结束时才释放；无法确认时一律保留占位。
- **API 与 Worker 共用同一数据库文件**：路径锚定仓库根（`config.py`，不依赖工作目录），文件型 SQLite 启用 WAL 与 busy timeout。
- 三道配额在**整个根任务树**作用：`max_subagents_total`（累计，失败不返还）、`max_subagents_concurrent`（并发，结束即释放）、`max_depth`。`max_subagents_total=0` 禁止一切子 Agent。
- 快照绑定：审批绑定 `payload_hash`，输入 / 权限 / Agent / 策略变化后旧批准失效。
- Mock 适配器：只记录"被启动次数"，另有可持久化、按 `invocation_id` 查询的执行记录（用于恢复对账）。
- **P3 控制台**：`/ui` 提供统一聊天、审批卡片、任务树、配额显示与停止/暂停按钮；界面**自动管理** task_id / invocation_id / approval_id，用户无需复制或填写任何 id（零构建单页，见 `docs/p3-ui.md`）。

**当前 158 个测试全部通过**。完整验收矩阵与逐条证据见 **`docs/p2-acceptance.md`**；
控制台说明见 **`docs/p3-ui.md`**；本地试用步骤见 **`docs/trial-mock.md`**；
进程与启动方式见 **`docs/tooling-diagnostics.md`**；各轮实现说明见
`docs/p2-approval-gate.md`、`docs/p2-auth-and-atomicity.md`、`docs/p2-shared-persistence.md`、
`docs/p2-policy-versioning.md`、`docs/p2-stop-and-recovery.md`。

> **这不等于整个项目安全阀全部通过。** 计划 §6 十条中 6 条完全通过、4 条部分通过，
> 后者的阶段归属与未验证状态：**CLI 拦截**（P3，待真实 Agent 接入时验证）、
> **自动协作开关**（P4）、**真实适配器远程超时**（P3）、**SSE 断线恢复**（P3/P5）。
> 详见 `docs/p2-acceptance.md` §B/§G。

## 仓库约定

见 `AGENTS.md`。

## 本地试用（Mock，无需真实 Agent 账号）

```powershell
.\.venv\Scripts\python.exe tools\demo\serve.py      # 启动 Mock API
# 控制台（统一聊天 / 审批卡片 / 任务树 / 配额 / 停止）： http://127.0.0.1:8080/ui
# 交互式 API 页：                                      http://127.0.0.1:8080/docs
.\.venv\Scripts\python.exe tools\demo\demo_flow.py  # 一键跑完整演示流程
.\.venv\Scripts\python.exe tools\demo\stop.py       # 按 PID 精确停止（勿按名强杀）
```

详细步骤（启动命令、凭证设置、建任务、批准/拒绝/配额/停止/超时/恢复操作、
数据位置与重置、已知限制）见 **`docs/trial-mock.md`**；
控制台说明见 **`docs/p3-ui.md`**。

> **暂未接入任何真实 Agent。** 试用只验证审批与配额这套安全阀本身，
> Mock 适配器仅记录"被启动次数"与执行记录，不调用真实产品、不发外部网络请求。

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
