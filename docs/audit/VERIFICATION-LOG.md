# 验证日志（主 Agent 独立复核）

规范：子 Agent 的结论只是线索，不是事实。凡是要写进最终结论的关键断言，主 Agent 必须自己打开 `文件:行` 复核。
本文件只记录**主 Agent 亲自读过并确认**的内容。

工具：`read` / `grep`（只读）；便携 git 位于 `E:\ds_agent\workplace\tools\MinGit\cmd\git.exe`。
源码根：`E:\ds_agent\workplace\vendor\AionCore\AionCore-main`（下称 `AionCore`）、
`E:\ds_agent\workplace\vendor\AionUi\AionUi-main`（下称 `AionUi`）。

---

## 2026-10-13 H1「未同意能否阻止委派」复核

结论：**H1 的主要断言全部复核通过**，硬条件 1 判定为**未实现**（委派动作本身没有用户审批环节）。

### 1. 委派路径只查角色、无审批 ✅ 复核通过

- `AionCore\crates\aionui-team\src\session.rs:1846` `pub async fn spawn_agent(...)`：
  函数体依次为 ① Lead 角色检查（`:1850-1853`）→ ② 名字规范化与去重（`:1856-1869`）→
  ③ `assistant_id` 必填（`:1871-1876`）→ ④ `persist_spawned_agent`（`:1900-1910`）→
  ⑤ `reserve_dynamic_member_attach`（`:1912`）→ ⑥ `scheduler.add_agent`（`:1916`）→
  ⑦ 写 mailbox 欢迎消息（`:1921-1944`）→ ⑧ `enqueue_system_work`（`:1946`）→
  ⑨ `spawn_attach_agent_process_bg` 后台拉起 CLI 进程（`:1963-1973`）。
  **全程没有任何"用户审批/审批单校验"分支**。`:1847-1849` 的注释自述该方法是所有入口的复查点。
- `AionCore\crates\aionui-team\src\mcp\server.rs:1018-1020` 附近（`exec_spawn_agent`）在 MCP 分发层再做一次 Lead 检查。
- `AionCore\crates\aionui-team\src\mcp\tools.rs:29-35` `authorize_tool`：唯一判定是
  `spec.permission == TeamToolPermission::LeadOnly && caller_role != Lead` → 报错。**权限模型只有"角色"，没有"用户审批"。**

### 2. 审批要求只存在于文本 ✅ 复核通过

- `AionCore\crates\aionui-api-types\src\team_tools.rs:6-24` `TEAM_SPAWN_AGENT_DESCRIPTION`：
  `:9` "The user explicitly approved the proposed teammate lineup in a previous message"、
  `:19` "Do NOT call this tool in that same turn; wait for explicit approval in a later user message"。
  ——这是写给 Lead 模型看的**工具描述文本**，不是代码校验。
- `AionCore\crates\aionui-team\src\mcp\tools.rs:186-201` 的单元测试把"description 必须包含
  `explicitly approved`"锁死，说明项目自己也把这条当作**描述文本契约**来维护。

### 3. 工具权限映射（AnyTeamAgent 面很宽） ✅ 复核通过

`AionCore\crates\aionui-api-types\src\team_tools.rs` `tool_specs()` 实测：

| 工具 | 行 | 权限 |
|---|---|---|
| `TeamMembers` | `:319-320` | AnyTeamAgent |
| `TeamReadMessages` | `:332-333` | AnyTeamAgent |
| `TeamSendMessage` | `:350-351` | AnyTeamAgent |
| `TeamInterruptAgent` | `:372-373` | **LeadOnly** |
| `TeamTaskCreate` | `:391-392` | AnyTeamAgent |
| `TeamTaskUpdate` | `:410-411` | AnyTeamAgent |

即：**任意 teammate 都能建任务、改任务归属（`owner`）、给任意 slot 发消息**——派活不受角色限制。

### 4. team MCP 权限请求被硬编码自动批准 ✅ 复核通过

- `AionCore\crates\aionui-ai-agent\src\manager\acp\permission_router.rs:15`
  `const AUTO_APPROVE_MCP_SERVERS: &[&str] = &[TEAM_MCP_SERVER_NAME];`
- `:73` 日志字面 `"ACP team MCP permission auto-approved"`；`:75-76` 直接
  `send(PermissionDecision::Selected { option_id }); continue;`
- `:182` 另一处 `AUTO_APPROVE_MCP_SERVERS.contains(&server_name)` 判定。
- 匹配依据是 **MCP server 名**，不区分具体工具 → team 全部工具（含 `team_spawn_agent`）都自动放行。

### 5. MCP 身份自报（共享 token） ✅ 复核通过

- `AionCore\crates\aionui-team\src\session.rs:215` `let auth_token = aionui_common::generate_id();`
  ——**每个 TeamSession 只生成一个 token**。
- `AionCore\crates\aionui-team\src\session.rs:316-324` `mcp_stdio_config(slot_id)`：返回给每个 agent 的配置里
  `token: self.mcp_server.auth_token().to_owned()`（同一个），**只有 `slot_id` 不同**。
- `AionCore\crates\aionui-team\src\mcp\server.rs:286` 只做 `if token != auth_token` 相等校验；
  `:294-299` `slot_id` **直接取客户端传参**，缺省 `"unknown"`，不做与 token 的绑定校验。
- 推论：角色由自报 `slot_id` 推导，故持有共享 token 的 teammate 若填 Lead 的 slot_id 即可通过 Lead 门禁。
  **代码层面成立；实际利用难度（取用共享 token 的必要条件）主 Agent 未验证，标注为未验证。**

---

## 2026-10-13 H2「数量限制能否覆盖整棵任务树」复核

结论：**H2 的主要断言全部复核通过**，硬条件 2 判定为**未实现**（后端零配额，且树级配额的载体在数据模型里不存在）。

### 1. 全仓不存在任何 Agent 数量类限制 ✅ 复核通过

对 `AionCore\crates` 全目录（`*.rs`）grep：
`max_members|member_limit|max_agents|max_teammates|team_size|max_subagents|agent_quota|spawn_limit|total_agents|agent_budget|max_depth`
→ **0 命中**。

### 2. 树级配额的载体不存在 ✅ 复核通过

对 `crates\aionui-team` 整 crate（`*.rs`）grep `parent_id|root_id|\bdepth\b|nested|recursion`
→ **0 命中**。即：没有 parent/root/depth 概念，"整棵任务树"在本代码里**不是一个可度量的对象**，
因此不是"配额没写"，而是"没有地方可写"。

### 3. 统一落点无计数校验 ✅ 复核通过

- `crates\aionui-team\src\provisioning.rs:699` `async fn provision_new_agent(&self, input: NewAgentProvisioning, ...)`：
  函数体为 `create_team_conversation_for_agent(...)`（`:704-718`）后直接 `Ok(TeamAgent { .. })`（`:719` 起）。
  **没有任何计数比较、上限判断或"已达上限"分支**。

### 4. 唯一的限流器主动排除团队会话 ✅ 复核通过

`crates\aionui-session-message\src\service.rs`：
- `:167-170` 与 `:313` 以 `SenderIsTeam` 报错（`error.rs:17` 定义，`:56` 映射为 **403**）；
- `:225` 以 `TargetIsTeam` 报错（`error.rs:14`）。

即 **agent ↔ agent 的 team 通道完全不受该限流器约束**——不是"有闸门但阈值宽"，而是被显式排除在外。

### 5. 补充确认（H1 与 H2 的交汇点）

- Lead 的提示词约束位于 `crates\aionui-team-prompts\src\role_prompt.rs:52,54`
  （同一件事：只提示，不强制）。
- `session.rs:1834-1845` 的文档注释自述 spawn 校验链为三条（caller 是 Lead / 名字不重 / assistant_id 存在），
  **其中没有"用户已同意"**——与 H1 的结论一致。

## 2026-10-13 H4「桌面端审批/权限/团队 UI」复核

结论：**H4 关于凭证边界与免确认无全局开关的断言复核通过**。委派审批未实现（仅提示词）与 H1 结论一致。

### 1. 审批答复没有凭证边界 ✅ 复核通过（这是本次最严重的发现）

- `AionUi\packages\desktop\src\common\adapter\httpBridge.ts:202-204`
  `export function resolveCoreCsrfToken(): string { return ''; }`
  ——注释 `:187-198` 自述：WebUI 的 CSRF 层随旧 webserver 一起移除（M6），
  "Until then this is a stub that reports no token available"，
  且 "the backend — which enforces no CSRF check here — accepts the request unchanged"。
- `:206-218` `sendHttpRequest` 即普通 `fetch(url, {method, headers, body})`，无鉴权头注入。
- 后端侧印证：`AionCore\crates\aionui-app\src\router\routes.rs:428-435`
  `if services.identity_mode.is_local() { router } else { router.layer(csrf_middleware) }`
  ——**本地模式下 CSRF 中间件根本不挂载**。

推论：本机任何进程（含被接入的 Agent）只要能连上后端端口，就能以用户身份 POST 一个"批准"。
这与方案 §4.1 要求的"只有登录的用户通过审批端点才能批准"**直接冲突**。

### 2. 渲染进程侧没有最小权限 ✅ 复核通过

`AionUi\packages\desktop\src\preload\main.ts`：
- `:18-32` `electronAPI.emit(name, data)` 是**整桥透传**：`ipcRenderer.invoke(ADAPTER_BRIDGE_EVENT_KEY, JSON.stringify({name, data}))`，
  `name` 由调用方任意给定，**没有事件白名单**。
- `:56-60` `contextBridge.exposeInMainWorld('__backendPort', backendPort)` —— 后端端口直接暴露给渲染进程。

### 3. 没有应用级免确认总开关 ✅ 复核通过

对 `AionUi-main` 全仓 grep `yoloMode`：命中 **14 处，全部是 i18n 资源与类型声明**
（`locales\*/settings.json:136`、`i18n-keys.d.ts:2750`），**没有任何 .ts/.tsx 代码引用**
→ 不存在全局免确认开关；实际生效路径是助手级默认权限与团队级 `session_mode` 传播（见 H4 报告正文）。

---

## 2026-10-13 H3「原生子 Agent 能否禁用或拦截」复核

结论：**H3 的主要断言全部复核通过**，硬条件 3 判定为**未实现**——既不能在启动前关闭，也不能纳入同一审批与计数链路。

### 1. 团队成员被强制写成 full-auto ✅ 复核通过（本次最严重的发现）

- `crates\aionui-team\src\service\spawn_support.rs:68-79` `session_mode_for_backend(...)`：
  函数体最终 `return agent_type.full_auto_mode_id(Some(backend)).to_owned();`（`:78`）
  ——团队的 session mode **就是 full-auto**，不是可选项。
- `crates\aionui-common\src\enums.rs:127-136` `full_auto_mode_id`：
  `claude | codebuddy => "bypassPermissions"`（`:130`）、`codex => "agent-full-access"`（`:131`）、
  兜底 `_ => "yolo"`（`:135`）。

即：**接入团队模式后，工具权限门在启动阶段就被关掉了**，与单聊相比是安全性下降。

### 2. 审批钩子被主动删除 ✅ 复核通过

- `crates\aionui-ai-agent\src\factory\antigravity.rs:66-79`：
  `full_auto` 为真时记录 `"antigravity: full-auto mode — tools execute without per-call approval"`（`:71`），
  然后 `crate::antigravity_hook::remove_hooks_json(...)`（`:76`）删除钩子配置，
  `deps.antigravity_hook_tokens.revoke(&ctx.conversation_id)`（`:77`）**吊销令牌**，返回空 hook。
  ——不是"没配钩子"，而是**特意清理掉**已有的钩子，理由是"否则 agy 会一直回调等一个没人给的批准"。

### 3. 没有 OS 级隔离 ✅ 复核通过

- `crates\aionui-process\src\containment.rs:1-8` 自述：
  "Tears down a whole subprocess subtree ... **Lifecycle-only — orthogonal to any security sandbox**"，
  且 "Job Object / cgroup tiers are intentionally not built"。
  ——`Containment` 只是**进程树回收**，不构成安全沙箱。

### 4. 与 H1 的交汇

`AUTO_APPROVE_MCP_SERVERS`（`permission_router.rs:15`）只对 team MCP 自动放行，
但团队成员的 CLI 原生工具因为在 full-auto 模式下**根本不发权限请求**，所以连"被自动放行"这一步都不会发生。
两道机制叠加的结果是：**团队模式下既没有工具审批，也没有委派审批。**

---

## 三个硬条件的最终判定（主 Agent 复核后）

| 硬条件 | 判定 | 强证据 |
|---|---|---|
| 1 未同意能否阻止委派 | **未实现** | `session.rs:1846` 无审批分支；审批只在 `team_tools.rs:9,19` 文本里 |
| 2 数量限制覆盖整棵树 | **未实现** | 全 `crates` 数量类命名 0 命中；`parent_id/root_id/depth` 0 命中 |
| 3 原生子 Agent 禁用或拦截 | **未实现** | `spawn_support.rs:68-79` 强制 full-auto；`antigravity.rs:66-79` 删钩子；原生委派仅观测不计数 |

结论：AionUi **不能**作为"每次委派必须批准 + 全树总量限制"的现成基座；
它的审批卡片只覆盖单聊的敏感工具操作，且本地模式下审批端点没有凭证边界（`httpBridge.ts:202-204`、`routes.rs:428-435`）。

## 主 Agent 已独立确认的"不存在"类断言（防止误报"已实现"）

- `aionui-team\src\member_runtime.rs` 内 grep `Semaphore|semaphore|permit|limit|max_|MAX_` → **仅 1 处命中**（`:183 fn reserve_attach`），
  即成员运行时**没有并发信号量或数量上限**，`reserve_attach` 是生命周期登记而非配额。
- `aionui-team\src` 全目录 grep `max_members|member_capacity|capacity` → `capacity` 命中全部是
  `Vec::with_capacity(..)`，**没有成员数量上限常量**。
