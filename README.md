# Agent Hub

把 Codex、Claude Code 等 Agent 接入一个统一聊天入口的私人控制台。核心约束：**每次 Agent 调用先经用户同意，子 Agent 数量由用户限制**。

定义、方案与验收标准见 `E:\plug\AGENT_HUB_PLAN.md`。

## 当前状态：P3（严格模式 + 可选用户监督模式）

**2026-10-20 产品调整**：按用户要求增加“用户监督模式”、手动结束本次自有本地进程，以及默认关闭的 Hub 子任务协作扩展。每次调用仍需审批；原生子 Agent、工具操作与远端停止不保证受控。严格模式保留原有阻止规则。见 [边界、接口与启动步骤](docs/manual-control-boundaries.md)。本轮仅离线验证，没有追加真实模型调用。

**2026-10-17 历史基线**：app-server 真实 S1（零模型握手）与另行批准的一次 S2（回复 OK、消息增量、终态、真实用量）均已通过。[S1 记录](docs/appserver-s1-2026-10-17.md)、[S2 记录](docs/appserver-s2-2026-10-17.md)。S3+ 的真实行为未验证。

S2 单回合探针与离线正反对照已就绪，全套 489 项测试通过。[范围与判定](docs/appserver-s2-probe.md)。真实探测授权不等于开放控制台任意调用。

**2026-10-15 更新**：真实记录确认，当前配置在传入 `--disable multi_agent` 后仍派生原生子 Agent，
且未登记到 Hub 审批/配额。已实现后端执行准入：当前真实 Codex 在消费审批、预留配额、启动进程前被拒绝。
这一阻止规则继续适用于严格模式；后续新增用户监督模式的明确例外见上方修订。Mock 和固定离线桩可继续验证。
见 [准入契约与验证](docs/p3-strict-adapter-admission.md)、[原生派生观测](docs/p3-native-subagent-incident.md)。
以下真实成功记录是此前联调证据，不代表当前符合严格协作要求。历史子任务现状仍未知。

阶段：**P2 审批与配额内核已验收**，**P3 已接入第一个真实适配器（Codex）**并完成一次
经用户逐次批准的真实调用。控制台 `/ui` 可切换 Mock 与真实 Codex 适配器。

### 已验证（有命令与实际输出）

| 能力 | 证据 |
|---|---|
| **未批准不启动** | 网页手工验收 + `tools/demo/codex_verify.py`：未批准时进程启动数 **0**；`test_unapproved_codex_never_launches_a_process` 等 |
| **批准后执行（恰好一次）** | 同上：批准后启动数 **1**；`test_approved_codex_launches_exactly_one_process` |
| **最终回复** | 网页验收回复逐字 `OK`；真实联调记录见 `docs/p3-codex-real-run-2026-10-14.md` |
| **thread_id** | 真实联调 `01a13a77-316d-7b01-a59e-5323a6c0728f`；内核与适配器记录一致（`test_kernel_and_adapter_records_agree_on_the_execution_id`） |
| **真实用量** | `input_tokens=15094, cached_input_tokens=12544, output_tokens=5`（`turn.completed.usage`） |
| **成功结算、名额归零** | 内核 `succeeded`、`open_reservations 0→0`、`concurrent 0`；`test_completed_codex_call_does_not_leak_its_slot` |
| **退出码 / 事件证据** | 退出码 `0`、4 个 JSONL 事件；控制台结果面板逐项显示 |
| **审批、配额、停止、恢复、unknown 裁决** | `docs/p2-acceptance.md`（Mock 内核，全部自动测试覆盖） |

### 未完成（属后续阶段，本轮未做）

* **网页 SSE / 断线恢复**：事件面板是轮询读取，**不是 SSE**。
* **会话恢复**：已拿到 `thread_id`，但**按 thread_id 恢复会话既未验证也未接入**。
* **第二个真实 Agent**：只接了 Codex；CC / WorkBuddy / DSH / Ink Stone 尚未接入。
* 自动协作开关与任务分解（P4）、worktree 与差异产物（P5）。

### 未验证（**不得当作已支持**）

取消（Codex 侧返回 `unsupported`）、工具操作的**启动前拦截**、
原生子 Agent 阻断已在当前配置观测到**失败**（见上方记录）；
**只读沙箱是否真拒绝写入**（2026-10-14 已做 **1 次真实探测：结果 = 无法判断**——
文件未创建，但 JSONL 里没有任何工具/文件类 item、驱动通道无相关拒绝证据（即**探针未取得实际尝试及拒绝证据**；官方文档列出 exec JSON 工具事件，本机版本差异需核实），
助手自述 `REFUSED` 不算证据；见 `docs/p3-probe-readonly-write-2026-10-14.md`）；app-server 已验证 S1/S2，工具审批、中断与恢复仍待验证。
四项验证方案、判定条件与清理步骤见 `docs/p3-permission-probes.md`。

历史 P3 收尾为 304 个测试通过；接续开发的实际验证见 **`docs/HANDOFF_CURRENT.md`**。P3 验收与逐条证据见 **`docs/p3-acceptance.md`**；
真实调用记录见 `docs/p3-codex-real-run-2026-10-14.md`；P2 验收矩阵见 **`docs/p2-acceptance.md`**；
控制台说明见 **`docs/p3-ui.md`**，试用步骤见 **`docs/p3-console-trial-steps.md`**；
Codex 能力与未决项见 `docs/agent-codex-capabilities.md`；
进程与启动方式见 **`docs/tooling-diagnostics.md`**；各轮实现说明见
`docs/p2-approval-gate.md`、`docs/p2-auth-and-atomicity.md`、`docs/p2-shared-persistence.md`、
`docs/p2-policy-versioning.md`、`docs/p2-stop-and-recovery.md`、`docs/p3-codex-adapter.md`。

> **这不等于整个项目安全阀全部通过。** 计划 §6 十条中 6 条完全通过、4 条部分通过，
> 后者的阶段归属与未验证状态：**CLI 拦截**（P3，验证方案已备好、**尚未执行**）、
> **自动协作开关**（P4）、**真实适配器的远程超时与取消**（P3，未验证）、**SSE 断线恢复**（P3/P5）。
> 详见 `docs/p2-acceptance.md` §B/§G 与 `docs/p3-acceptance.md`。

<details>
<summary>P2 内核已实现并逐条验证的行为（点击展开）</summary>

- 数据模型：`tasks`（根任务树，含 `root_task_id`/`depth`）、`invocations`（含不可变输入快照 `payload_hash`）、`approvals`、`quota_reservations`、`policy_versions`、`events`。
- 调用状态机：`draft → awaiting_approval → queued → starting → running → succeeded/failed/canceled/timed_out/unknown/rejected/invalid`。
- **审批是执行门**：只有 `approved` 审批可以进入消费路径（`pending → approved → consumed`）。`_find_consumable_approval` 只查询 `approved`；`consume` 在预留配额 / 更新状态 / 启动事件之前再次检查状态，非批准状态返回 `not_approved` 或 `no_approval`，不会造成消费、配额占用或适配器启动。
- **审批权限在后端强制**：HTTP 审批端点须携带 Bearer token，只有 `user` 角色可批准 / 拒绝 / 撤销、改配额、停止任务树；无 token 返回 401，`agent` / `worker` 身份返回 403。因此 Agent 不能批准自己。
- **Worker 事务边界**：Worker 先在事务中消费审批并预留名额，**提交成功后**才启动适配器；提交失败则不会启动。
- **并发消费最多一次**：审批用条件更新（`WHERE status='approved'`）消费，两个 Worker 抢同一审批时只有一个成功，且只产生一条名额预留。
- **配额预留是原子的**：每棵树一行计数器（`tree_quota`），预留用单条条件更新把上限写进 `WHERE`，多条**不同**调用争抢最后一个名额时也只有一条成功（"先读后写"的检查曾使 4 条全部通过，已修复）。
- **策略持久化与版本化**：策略存在根任务行，作用于整棵树；每次更新版本递增并保留旧版本与事件。更新会撤销**未消费**的审批，但不重置累计数量、不取消运行中的调用；非法参数不写入任何东西。
- **停止与取消**：停止请求先**持久化**（带操作者）、再接通适配器 `cancel`；只有**确认**停止才结算 `canceled` 并释放名额，未确认则置 `unknown` 并**保留名额**；重复停止只释放一次名额（按预留记录 CAS）。
- **暂停与恢复**：`POST /api/tasks/{id}/pause` 阻止后续调度但**不取消**运行中的调用；`/resume` 解除。
- **执行超时**：超过树策略 `run_time_limit_seconds` 的调用——取消**已确认**才置 `timed_out` 并释放名额；**未确认**（不支持取消 / 外部仍报 running / 证据丢失）则置 `unknown` 并**保留名额**，不谎称远程已停。
- **崩溃恢复**：重启后核对 `starting`/`running`，向外部侧按 `invocation_id` 查询；确认在跑的保留名额，确认终态的结算并释放一次，**无法确认的置 `unknown`、不自动重启、不提前释放名额**。
- **unknown 用户裁决**：`POST /api/invocations/{id}/reconcile`（仅 user）——`settle` 认定结果（人工确认）并幂等释放名额；`retry` **必须先确认旧执行已结束**（否则 409 `not_stopped`，不改旧状态、不释放名额、不新建调用），确认后释放一次并**新建调用 + 新待批审批**（重试必须重新审批，不自动启动）。
- **统一原则**：并发名额只有在能确认对应执行已结束时才释放；无法确认时一律保留占位。
- **API 与 Worker 共用同一数据库文件**：路径锚定仓库根（`config.py`，不依赖工作目录），文件型 SQLite 启用 WAL 与 busy timeout。
- 三道配额在**整个根任务树**作用：`max_subagents_total`（累计，失败不返还）、`max_subagents_concurrent`（并发，结束即释放）、`max_depth`。`max_subagents_total=0` 禁止 Hub 分派子调用，不能据此阻止产品原生派生。
- 快照绑定：审批绑定 `payload_hash`，输入 / 权限 / Agent / 策略变化后旧批准失效。
- Mock 适配器：只记录"被启动次数"，另有可持久化、按 `invocation_id` 查询的执行记录（用于恢复对账）。
- **P3 控制台**：`/ui` 提供统一聊天、审批卡片、任务树、配额显示与停止/暂停按钮；界面**自动管理** task_id / invocation_id / approval_id，用户无需复制或填写任何 id（零构建单页，见 `docs/p3-ui.md`）。
- **P3 真实适配器**：Codex 适配器复用同一套门禁（`gate_token`）与配额，`agent_id` 必须匹配、可执行文件按 sha256+size 固定、只读沙箱、禁用多 Agent 参数；启动期失败按"能否证明进程不存在"分流为终态失败或 `unknown`（见 `docs/p3-codex-adapter.md`）。

</details>

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
控制台说明见 **`docs/p3-ui.md`**，逐步试用（含真实模式）见 **`docs/p3-console-trial-steps.md`**。

> Mock 试用只验证审批与配额这套安全阀本身：Mock 适配器仅记录"被启动次数"与执行记录，
> 不调用真实产品、不发外部网络请求。
> `serve.py --adapter codex` 目前仅供查看能力与记录，**真实启动被后端阻止**；
> 凭据位置与登录步骤见 **`docs/p3-codex-credentials.md`**（凭证放在仓库之外，由你本人登录）。

## 目录结构

```text
agent-hub/
  README.md
  AGENTS.md
  .gitignore
  .gitattributes
  docs/          能力矩阵、验收报告、权限验证方案、审计报告
  apps/          api（FastAPI + 控制台）/ worker（调度、门禁与适配器选择）
  packages/      contracts（数据模型与状态机）/ adapters（Mock 与 Codex 适配器）
  tests/         policy / adapters / http / persistence / e2e
  tools/         demo（控制台、桩进程、验证脚本）/ probes（权限验证工具）
```

## 本地开发（Mock 环境）

无需真实 Agent 账号即可启动与测试：

```powershell
# 1. 建虚拟环境并安装锁定依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 依赖安装方式，见 fetch_wheels.py / bootstrap_deps.py 及 wheelhouse（本机离线下载）

# 2. 运行全部测试
python -m pytest

# 3. 离线跑通完整栈（桩进程，不联网）
python tools/demo/offline_integration.py
python tools/demo/codex_verify.py --mode stub
```

真实配置放在 `.env`（已 gitignore），不提交。适配器能力矩阵见 `docs/adapter-capabilities.md`；
Codex 的实测能力与未决项见 `docs/agent-codex-capabilities.md`。
