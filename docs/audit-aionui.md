# AionUi / AionCore 接入审计报告（P0）

审计日期：2026-10-13
审计对象：`iOfficeAI/AionUi`（TypeScript/Electron）、`iOfficeAI/AionCore`（Rust/Axum/SQLite）
方法：**只读源码审计**。未安装、未编译、未运行任何被测程序。
材料：AionUi `main`（33486 stars）、AionCore `main`（2026-10-13 最后推送），下载快照见 `docs/audit/aionui/`。

> 纪律说明：子 Agent 的结论只作线索。本报告每条关键断言都由主 Agent 打开源码复核，逐条记录在
> `docs/audit/VERIFICATION-LOG.md`。仅"文档写了"的内容一律标注为文档，不算实现。

## 1. 结论

**AionUi 不能作为"每次委派必须批准 + 全树总量限制"的现成基座。** 三个硬条件全部判定为 `未实现`。

| # | 硬条件 | 判定 | 最强证据 |
|---|---|---|---|
| 1 | 未同意能否阻止委派 | **未实现** | `aionui-team/src/session.rs:1846` `spawn_agent` 全程无审批分支；权限模型只有角色（`mcp/tools.rs:29-35`）；"等用户批准"只写在工具描述 `aionui-api-types/src/team_tools.rs:9,19` 与提示词里 |
| 2 | 数量限制能否覆盖整棵任务树 | **未实现** | 全 `crates` grep 数量类命名 **0 命中**；`aionui-team` 内 `parent_id\|root_id\|depth` **0 命中**——树不是可度量对象，没有地方能写树级配额 |
| 3 | 原生子 Agent 能否禁用或拦截 | **未实现** | 原生委派（claude `Task`/`Workflow`、codex `spawnAgent`）只被翻译成事件、不计数不拦截；团队成员被**强制**为 full-auto（`service/spawn_support.rs:68-79` → `aionui-common/src/enums.rs:127-136`） |

## 2. 逐条证据

### 2.1 硬条件 1：委派无用户审批

- `AionCore\crates\aionui-team\src\session.rs:1846` `TeamSession::spawn_agent`：
  Lead 角色检查(`:1850-1853`) → 名字去重(`:1856-1869`) → `assistant_id` 必填(`:1871-1876`) →
  落库 `persist_spawned_agent`(`:1900-1910`) → 登记运行时(`:1912`) → 加入调度器(`:1916`) →
  写 mailbox(`:1921-1944`) → `enqueue_system_work`(`:1946`) → 后台拉起 CLI 进程(`:1963-1973`)。
  **没有任何审批校验分支。** `:1847-1849` 注释自述此处是所有入口的复查点。
- `mcp/tools.rs:29-35` `authorize_tool`：唯一判定 `permission == LeadOnly && caller_role != Lead`。
- **团队 MCP 权限请求被硬编码自动批准**：`aionui-ai-agent/src/manager/acp/permission_router.rs:15`
  `AUTO_APPROVE_MCP_SERVERS = &[TEAM_MCP_SERVER_NAME]`，`:73` 日志 `"ACP team MCP permission auto-approved"`，
  `:75-76` 直接 `Selected{..}` + `continue`。按 **server 名**匹配、不区分工具。
- **权限面本身很宽**：`aionui-api-types/src/team_tools.rs` 实测 `TeamSendMessage`(`:351`)、
  `TeamTaskCreate`(`:392`)、`TeamTaskUpdate`(`:411`) 均为 `AnyTeamAgent`，仅 `TeamInterruptAgent`(`:373`) 为 `LeadOnly`。
  → 任意 teammate 都能建任务、改 owner、给任意 slot 发消息。
- **身份自报**：`session.rs:215` 每 team 只生成一个 token；`:316-324` 发给各 agent 的配置 token 相同、仅 `slot_id` 不同；
  `mcp/server.rs:286` 只校验 token 相等，`:294-299` 的 `slot_id` 直接取客户端传参。
  → 持共享 token 者可声明 Lead 的 slot_id 过门。**代码层面成立，利用难度未验证。**

### 2.2 硬条件 2：零配额、无树模型

- 4 个成员创建入口（`provisioning.rs:235 / :379 / :450 / :699`）**均无计数比较**；
  加成员即 `team.agents.push()` + 整列 JSON 覆写（`:419-420`、`:479-480`、`:914-932`）。
- 无并发上限（`event_loop.rs:72-94` 每 slot 一个 tokio task，无 semaphore）、无深度上限、无每用户团队数上限。
- `TeamAgent`（`types.rs:88-116`）无 `parent_id/root_id/depth`；`teams` 表只有 `agents` JSON 列。
- 「3×3 不会变 9」只是副产品：`team_spawn_agent` 是 LeadOnly 且新成员角色硬编码 `Teammate`（`provisioning.rs:469`），
  所以树恒为"1 lead + N 平铺"。**宽度 N 无界**，且唯一限流器主动排除团队会话
  （`aionui-session-message/src/service.rs:167-170` `SenderIsTeam` 403、`:225` `TargetIsTeam` 403）。

### 2.3 硬条件 3：原生委派只观测、且团队模式自废权限门

- 原生委派仅被翻译为事件：claude `backend/claude_conn.rs:1791-1797`、codex `backend/codex_conn.rs:6287-6329`，
  仅供 UI roster；reducer 契约明确 subagents 不影响 `can_send`（`aionui-session/src/state.rs:298-345`）。
- **团队成员的 session mode 被强制为 full-auto**：`aionui-team/src/service/spawn_support.rs:68-79`
  最终 `full_auto_mode_id(Some(backend))`；`aionui-common/src/enums.rs:127-136`：
  claude/codebuddy → `bypassPermissions`、codex → `agent-full-access`、兜底 → `yolo`。
  → 团队模式**比单聊更不安全**：审批门在启动阶段就被关掉，连"被自动放行"这一步都不会发生。
- **agy 的审批钩子被主动删除**：`aionui-ai-agent/src/factory/antigravity.rs:66-79`
  记录 `"full-auto mode — tools execute without per-call approval"`，随后 `remove_hooks_json(...)` 删钩子、
  `revoke(...)` 吊销令牌。
- 全仓**无任何关闭原生子 Agent 的开关**；`--disallowed-tools` 只剩历史注释（`claude_conn.rs:242`）。
- `TeamToolPermission::LeadOnly` 是**真代码强制**，但**只管 13 个 `team_*` 工具**，与 CLI 原生工具无关；
  governance 规则与"别用平台内置工具"**全部仅为提示词**（`aionui-team-prompts/src/governance.rs:1-24`、
  `team_tool_usage.rs:18-21`）。

### 2.4 额外发现（不在三个硬条件内，但影响选型）

| 发现 | 证据 | 影响 |
|---|---|---|
| 审批端点无凭证边界 | `AionUi\packages\desktop\src\common\adapter\httpBridge.ts:202-204` CSRF 恒返回 `''`，注释自述后端不校验；`AionCore\crates\aionui-app\src\router\routes.rs:428-435` 本地模式不挂 `csrf_middleware` | 本机任意进程（含 Agent）可代用户批准 → 审批会退化成 UI 装饰 |
| IPC 整桥透传 | `preload/main.ts:18-32` `emit(name,data)` 无事件白名单；`:56-60` 暴露 `__backendPort` | 渲染进程侧无最小权限 |
| 无全局免确认开关 | `yoloMode` 全仓 14 处命中全为 i18n 资源，代码零引用 | 免确认靠助手级默认值与团队 `session_mode` 传播固化，无法一处关停 |
| 团队共享单一 workspace | `aionui-team/src/workspace.rs:50-77` | 无 per-agent 目录、无 worktree，多 Agent 改动互相踩 |
| 无 OS 级隔离 | `aionui-process/src/containment.rs:1-8` 自述 "Lifecycle-only — orthogonal to any security sandbox" | 只回收进程树，不是沙箱 |
| Agent 可绕过中转站 | Agent 有 shell 且 spawn 不做环境净化（`aionui-runtime/src/spawn.rs:97-130`） | 可 fork 原始 CLI，完全绕开 AionCore 的审批与计数 |
| 审批无超时 | `permission_router.rs:124-146` | 与 AionUi PRD 自述一致；团队模式无超时 |

## 3. 与方案验收用例的对照（`AGENT_HUB_PLAN.md` §6）

| 安全阀用例 | 现状 |
|---|---|
| 无审批/过期/撤销 → 适配器启动次数为 0 | ❌ 无审批概念，无从强制 |
| 同一审批并发消费最多启动一次 | ❌ 无审批对象 |
| 总子实例 3、并发 2 全树生效 | ❌ 零配额、无树模型 |
| 深度 1：子 Agent 不能再生成子 Agent | ⚠️ 偶然成立（spawn 是 LeadOnly），非配额机制 |
| Agent 尝试批准自己 / 自行提高上限 / 绕过中转站 | ❌ 身份自报 + 共享 token + shell 可 fork CLI |
| 关停后无新启动 | ⚠️ 需实测（本次未运行） |

## 4. 可复用与不可复用

**可直接复用**：统一聊天与多 Agent 会话界面、ACP 接入与后端适配层、任务板与邮箱、WebSocket 事件、
`team_*` 工具的角色授权骨架、Git worktree 之外的会话管理。

**必须重做或大改**：委派审批（无）、全树配额（无载体）、原生子 Agent 拦截（无开关）、
审批凭证边界（本地免鉴权）、Agent 隔离（共享 workspace、无沙箱）。

## 5. 两条路线与建议

**路线 A：以 AionUi 为基座改 AionCore 核心。** 改动点已定位：
① `mcp/server.rs::handle_initialize`（`:269-316`）改为一 slot 一 token，否则其余全可绕；
② `session.rs::spawn_agent`（`:1846` 起，`:1853` 之后）插调用单校验；
③ `provisioning.rs::provision_new_agent`（`:699`，四入口唯一汇合点）做 total + depth 门；
④ `work_coordinator/coordinator.rs::next` 的 claim 点（`:578` 附近）做 concurrent 门；
⑤ `mcp/server.rs::maybe_notify_task_owner`（"派活即运行"咽喉）；
⑥ 审批必须走**独立通道**，绝不能挂到 `AUTO_APPROVE_MCP_SERVERS`。
注意 `spawn_agent` 走 `persist_spawned_agent`，**不共享** `service.rs:965-970` 的 `add_agent_locks`，
只在 `add_agent` 内加配额会被 spawn 路径绕过。

**路线 B：按方案 §10 先自建 Mock 版审批与配额内核，把 AionUi 当 UI/接入参考。**

**建议走 B。** 理由：三个硬条件里有两个是"数据模型层面缺失"（无审批对象、无树结构），
不是"开关没开"；补齐它们等于在 AionCore 的核心生命周期里插一层新语义，
同时还要修本地模式的信任模型（否则审批可被本机任意进程代签）。
自建内核能一对一满足 §6 那张用例表，且不承担上游改动被合并/回归的风险。
若最终仍选 A，建议**先做 ①②③ 三项并写自动化测试**，其余功能暂缓。

## 6. 未验证清单（不得当作结论）

- 各 ACP 桥（`claude-agent-acp` / `codex-acp`）是否为 `Task` 发 `session/request_permission`——本仓无桥源码，按"不得猜测 CLI 行为"标注未验证。
- `aionrs` 的工具审批依赖外部 crate `aion-protocol v0.2.11`，源码不在本机；`PermissionAuthorizer` 仅存在于注释。
- 共享 token 冒充 Lead 的实际利用难度（需真实连接测试）。
- 两条成员创建路径（`add_agent` 与 `spawn_agent`）并发是否丢成员。
- 关停/取消的端到端行为（本次未运行任何进程）。
