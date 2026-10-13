# 硬条件 1 验证报告：未同意能否阻止委派

审计对象（只读，未编译、未运行、未联网）：

- AionCore 后端根：`E:\ds_agent\workplace\vendor\AionCore\AionCore-main`（下文路径均相对此根）
- AionUi 桌面端根：`E:\ds_agent\workplace\vendor\AionUi\AionUi-main`（下文路径均相对此根）

---

## 结论

**未同意不能阻止委派 —— 状态：未实现。**

在 AionCore 后端，`team_spawn_agent`（创建 teammate）与 `team_task_create` / `team_task_update` / `team_send_message`（把任务派给另一个 Agent 并唤醒它）这三类"委派动作"的整条执行路径上，**不存在任何"必须由用户批准"的代码环节**：唯一的执行层门禁是 **调用者角色必须是 Lead**（`crates/aionui-team/src/session.rs:1850-1853`、`crates/aionui-team/src/mcp/server.rs:1018-1020`），这是**Agent 之间的角色校验，不是用户审批**。用户是否同意只出现在 **MCP 工具描述文本**（`crates/aionui-api-types/src/team_tools.rs:8-19`）与 **角色提示词**（`crates/aionui-team-prompts/src/role_prompt.rs:52-55`）里 —— 按审计规范属于 **"仅文档或提示词约定"**，不构成实现。更严重的是：ACP 层对 team MCP 工具的权限请求被**显式自动批准**（`crates/aionui-ai-agent/src/manager/acp/permission_router.rs:15,66-77`），即使 Agent 的 CLI 主动发起权限询问，也不会到达用户。

---

## 委派入口总表

| # | 入口名 | 文件:行 | 调用者身份要求 | 派发前是否检查用户审批 | 证据片段 |
|---|---|---|---|---|---|
| 1 | `POST /api/teams/{id}/agents`（创建 teammate） | `crates/aionui-team/src/routes.rs:188`（注册）、`routes.rs:415-424`（handler） | 用户 JWT（`Extension<CurrentUser>`）+ CSRF；路由挂 `auth_middleware`（`crates/aionui-app/src/router/routes.rs:339`） | ❌ 无。**但调用者就是用户本人**，属用户自身动作；仅校验团队归属（`crates/aionui-team/src/service.rs:972` `load_owned_team_row`） | `routes.rs:422` `let agent = state.service.add_agent(&user.id, &id, req).await?;` |
| 2 | `create_team`（含初始 teammate 编排） | `crates/aionui-team/src/routes.rs:181,233-241` → `service.rs:762` → `provisioning.rs:235` `provision_initial_agents` | 用户 JWT | ❌ 无（用户自身动作） | `routes.rs:239` `state.service.create_team(&user.id, req).await?` |
| 3 | **MCP `team_spawn_agent`**（Agent 自己创建 teammate，重点） | 描述：`crates/aionui-api-types/src/team_tools.rs:6-24`；分发：`crates/aionui-team/src/mcp/server.rs:531`；实现：`mcp/server.rs:1007-1072` | Agent 的 MCP 凭证（stdio bridge → TCP + 共享 `auth_token`，`mcp/server.rs:286`）；**外加 Lead 角色**（`mcp/server.rs:1018-1020`） | ❌ **无。** 只有角色检查与参数规范化，然后直接 `spawn_agent_in_session` | `mcp/server.rs:1018-1020`<br>`if caller_role != TeammateRole::Lead { return Err(ToolCallError::from_message("Only Lead can spawn agents")); }`<br>`mcp/server.rs:1060-1061` `service.spawn_agent_in_session(team_id, caller_slot_id, req).await` |
| 4 | `TeamSession::spawn_agent`（真正的落库+拉起进程） | `crates/aionui-team/src/session.rs:1846-1975` | 内部函数；仅再查一次 Lead 角色 | ❌ **无。** Step 3-7 顺序为：解析 assistant → `persist_spawned_agent` → `scheduler.add_agent` → 写 mailbox → `enqueue_system_work` → **`spawn_attach_agent_process_bg` 拉起 CLI 进程**，全程无审批分支 | `session.rs:1850-1853` `if caller.role != TeammateRole::Lead { return Err(TeamError::LeaderOnly("spawn_agent".into())); }`<br>`session.rs:1963-1969` `spawn_attach_agent_process_bg(...)` |
| 5 | `TeamSessionService::spawn_agent_in_session` | `crates/aionui-team/src/service/spawn_support.rs:208-219` | 内部，无鉴权 | ❌ 无 | `spawn_support.rs:218` `entry.session.spawn_agent(caller_slot_id, req).await` |
| 6 | **MCP `team_task_create`**（把任务派给 owner 并唤醒，= 委派一次运行） | 描述/权限：`crates/aionui-api-types/src/team_tools.rs:390-408`（`AnyTeamAgent`）；实现：`crates/aionui-team/src/mcp/server.rs:1164-1189` | MCP 凭证即可，**任何 teammate 都能调用**（非 LeadOnly） | ❌ **无。** 直接写 task board，并 `maybe_notify_task_owner` 唤醒 owner 使其开始一轮运行 | `mcp/server.rs:1174-1182` `scheduler.create_task(...)`（无门禁）<br>`mcp/server.rs:1186` `maybe_notify_task_owner(..., "create").await;` |
| 7 | **MCP `team_task_update`**（改派 owner / 解阻塞唤醒下游） | 描述/权限：`team_tools.rs:409-425`；实现：`mcp/server.rs:1191-1232` | MCP 凭证即可（`AnyTeamAgent`） | ❌ **无。** 改派与"完成→解锁下游"都会唤醒新的 owner | `mcp/server.rs:1216-1218` `if reassigned { maybe_notify_task_owner(..., "reassign").await; }`<br>`mcp/server.rs:1222-1229` 下游 `"unblock"` 唤醒 |
| 8 | **MCP `team_send_message`**（给指定 teammate 下发指令并唤醒） | 描述/权限：`team_tools.rs:350-351`（`AnyTeamAgent`）；实现：`mcp/server.rs:773-870` | MCP 凭证即可（`AnyTeamAgent`） | ❌ 无（消息内容与目标校验除外） | `mcp/server.rs:835-841` 解析目标后直接走发信+入队路径 |
| 9 | 任务看板写入（底层） | `crates/aionui-team/src/task_board.rs:35-101` | 无用户身份概念，默认 `"system_default_user"` | ❌ 无 | `task_board.rs:36` `Self::new_for_user(repo, "system_default_user")`<br>`task_board.rs:86` `self.repo.create_task(&self.user_id, &row).await?` |
| 10 | Mailbox 写入（底层，唤醒载体） | `crates/aionui-team/src/mailbox.rs:42-98` | 无 | ❌ 无 | `mailbox.rs:82` `self.repo.write_message(&self.user_id, &row).await?` |
| 11 | 工作调度入队 | `crates/aionui-team/src/work_coordinator/coordinator.rs:128`（`acquire_enqueue`）、`:203`（`commit_enqueue`） | 内部 | ❌ 无。全文件无 approval/confirm 逻辑 | `coordinator.rs:1010` 附近的 `enqueue_system_work`（`session.rs:1010-1024`）→ `enqueue_existing_work` |
| 12 | Team Run 绑定（每次运行归属一个 run） | `crates/aionui-team/src/team_run/manager.rs:61`（`bind_user_enqueue`）、`:107`（`bind_system_enqueue`） | 内部 | ❌ 无。仅区分"是否算用户介入" | `manager.rs:61-99` 全程无审批字段 |
| 13 | Agent 生命周期/task 动作 | `crates/aionui-team/src/scheduler/actions.rs:6-16`（`create_task`）、`:45-83`（`request_shutdown_agent`） | 内部；shutdown 仅 Lead-only | ❌ 无用户审批 | `actions.rs:59-61` `if from_role != TeammateRole::Lead { return Err(...) }` —— 依然是角色校验 |
| 14 | Provisioning 层落库与拉起 | `crates/aionui-team/src/provisioning.rs:379`（`add_agent`）、`:450`（`persist_spawned_agent`）、`:484`（`attach_agent_process`）、`:699`（`provision_new_agent`） | 内部，`user_id` 仅作数据归属键 | ❌ 无 | `provisioning.rs` 全文件 grep `approval/approve/consent` **零命中** |
| 15 | CLI stdio 桥（`aionui-app mcp-team-stdio`） | `crates/aionui-app/src/commands/cmd_team_stdio.rs:39`、`:335-347` | 持有 `TEAM_MCP_TOKEN`/`TEAM_MCP_PORT` 的本地进程 | ❌ 无本地审批，仅**转发**到后端 TCP MCP | `cmd_team_stdio.rs:473-489` `forward_to_tcp(...)`；`:336` 描述文本含"explicitly approved"，但只是工具描述字符串 |
| 16 | AionUi 桌面端 UI"添加成员" | `packages/desktop/src/common/adapter/ipcBridge.ts:2270-2276`；唯一调用点 `packages/desktop/src/renderer/pages/team/hooks/useTeamSession.ts:132` | 用户点击（renderer → IPC → 后端 HTTP + JWT） | ❌ 无额外审批弹窗（用户点击本身即授权） | `useTeamSession.ts:132` `await ipcBridge.team.addAgent.invoke({ team_id: team.id, assistant });` —— 无 confirm 前置 |

**表格结论**：16 个入口中，涉及 **Agent 自主委派**的是 #3/#4/#6/#7/#8，全部**只检查角色（Lead 或 AnyTeamAgent），不检查用户审批**。

---

## 审批对象辨析

### 结论：委派/创建 teammate **完全不走** ACP 权限审批；而且团队 MCP 工具的权限请求被**显式自动批准**。

**(a) ACP 权限审批的对象确实是"敏感工具操作"，不含委派动作。**

- `docs/prds/conversations/acp/permissions.md:12` 用户故事原文："我希望 AI 在执行敏感操作（如**写入文件、运行命令**）前征求我的许可"。
- 该文档通篇（F-PERM-01 ~ F-PERM-06）只描述"编辑文件/运行命令"这类操作（`permissions.md:20`），**未出现任何"创建 Agent / 委派任务需审批"的条目**；`permissions.md:33` 只提到"团队模式下：权限请求无超时限制（等待团队管理者审批）"——注意这里的"团队管理者"指 Lead Agent 的注意力，不是新增一类委派审批。
- 后端审批内存也只以"动作+命令类型"为键：`crates/aionui-ai-agent/src/shared_kernel/approval.rs:7` `pub fn approval_key(action: Option<&str>, command_type: Option<&str>) -> String`（如 `"exec:curl"`，见同文件 `:21`）。没有以 `spawn_agent` / 委派为键的任何审批记录。
- 对外接口同样是工具动作审批：`crates/aionui-conversation/src/routes.rs:143` `GET /api/conversations/{id}/approvals/check` → `service.rs:3533-3553` `check_approval(... ) -> ApprovalCheckResponse { approved }`。

**(b) 决定性证据：team MCP 工具的权限请求被硬编码自动批准，绕过用户。**

`crates/aionui-ai-agent/src/manager/acp/permission_router.rs:15`

```rust
const AUTO_APPROVE_MCP_SERVERS: &[&str] = &[TEAM_MCP_SERVER_NAME];
```

`:66-77`

```rust
// Auto-approve team MCP tools without user interaction.
if let Some(option_id) = auto_approve_option_id(&perm_req.request) {
    info!(... "ACP team MCP permission auto-approved");
    let _ = perm_req.response_tx.send(PermissionDecision::Selected { option_id });
    continue;          // ← 直接应答，绝不生成 Permission 事件给前端
}
```

- `TEAM_MCP_SERVER_NAME = "aionui-team"`（`crates/aionui-api-types/src/team_mcp.rs:17`）。
- 匹配范围只看 MCP server 名（`permission_router.rs:180-186`、`:202-235`），**不区分具体工具**：`team_spawn_agent` 与 `team_task_create` 与 `team_read_messages` 一视同仁，全部落在 `AUTO_APPROVE_MCP_SERVERS` 内。
- 选中项优先 `AllowAlways`，退化到 `AllowOnce`（`permission_router.rs:188-200`）。
- 有配套单元测试锁定该行为，例如 `permission_router.rs:534` `start_auto_approves_team_mcp_with_existing_option_id()`、`:334` `auto_approve_matches_claude_team_mcp_title_prefix()`、`:345` `auto_approve_matches_codex_raw_input_server_name()`。

**净效果**：即使用户的 CLI 后端（claude/codex 等 ACP 后端）为 `team_spawn_agent` 这类 MCP 工具发起了 `session/request_permission`，后端也会**在协议层立即代替用户选"允许"**，前端权限卡片根本不会出现。这是"工具审批通道"对委派动作**主动关闭**，而不是"委派走同一套审批"。

**(c) 免确认模式只是叠加风险，不是本条的关键。** `docs/prds/conversations/acp/permissions.md:70-104` 的 YOLO 模式（`crates/aionui-ai-agent/src/manager/aionrs/agent.rs:191` `auto_approve: config_extra.session_mode.as_deref() == Some("yolo")`）会让**所有**带可选项的请求自动通过；但它作用于工具审批，与 (b) 是两条独立通道。

---

## 自我扩权与绕过路径

### 4.1 Agent 能否自行创建 teammate / 给自己加人 / 直接写 task board —— **能，且无需伪装**

- **Lead 直通**：`team_spawn_agent` 对 Lead 完全开放（`crates/aionui-team/src/mcp/server.rs:1018-1020` 通过后 `:1060-1061` 直接 `spawn_agent_in_session`）。Lead 可以在**任何无用户消息介入的时机**（例如自己的一轮工具调用中）连续创建 teammate；`session.rs:1846-1969` 无审批、无"本轮不得创建"的执行层约束（只有提示词说 `Do NOT call this tool in that same turn`，`role_prompt.rs:52` —— 仅提示词）。
- **Teammate 也能委派**：`team_task_create` / `team_task_update` / `team_send_message` 的权限是 `AnyTeamAgent`（`crates/aionui-api-types/src/team_tools.rs:392,411,351`），因此**任何 teammate 都能给别人派活并唤醒对方**（`mcp/server.rs:1174-1186`、`:1216-1229`），无需任何审批。这是"合法的横向自我扩权"：委派能力不限于 Lead。
- **无"Agent 自己批准自己"的审批对象可被利用**，因为不存在审批对象；等价效果是"无需批准"。唯一形似审批的机制是 **`shutdown_approved`**，但它是**agent→agent 的对等握手**，不是用户审批：`mcp/server.rs:784-806` 中，teammate 自己发字符串 `"shutdown_approved"` 就会被拦截、记 ack、并在 500ms 后**删除自己**（`:793-803` 调 `svc.remove_agent`）。即"被关停的 Agent 自己批准了关停"。

### 4.2 身份伪装路径（真实缺口，非推测）

MCP 层的调用者身份完全由**客户端自报**，构成 Lead 伪装路径：

1. **一个团队共用一个 token**：`crates/aionui-team/src/session.rs:215` `let auth_token = aionui_common::generate_id();` 每个 team session 只生成一个 token；`session.rs:316-324` `mcp_stdio_config(slot_id)` 给每个 agent 的配置里**只有 `slot_id` 不同，`token` 是同一个** `self.mcp_server.auth_token()`。
2. **`slot_id` 不经校验**：`crates/aionui-team/src/mcp/server.rs:294-299` 直接从 initialize 参数读 `slot_id`，只做了 `unwrap_or("unknown")`，**没有**与 token 绑定、也没有任何二次校验；`:286` 只校验 token 相等。
3. **角色由此推导**：`mcp/server.rs:429-432` `scheduler.get_agent(caller_slot_id).await.map(|agent| agent.role).unwrap_or(TeammateRole::Teammate)`。
4. **Lead 的 slot_id 对每个 teammate 都是公开的**：
   - 角色提示词里直接注入：`crates/aionui-team-prompts/src/role_prompt.rs:244` `Leader: {{LEADER_NAME}} (slot_id: {{LEADER_SLOT_ID}})`，`:316` 用 `params.leader.slot_id` 替换（即 teammate 的提示词里就写着 Lead 的 slot_id）。
   - `team_members` 工具输出全量 `slot_id`+`role`：`mcp/server.rs:932-942`（`agent_json`）、`:1269-1277`（`exec_members`），权限为 `AnyTeamAgent`（`team_tools.rs:319-320`）。

**推论**（依据上述 4 点代码事实）：teammate 一旦能拿到**所在团队共用的 token**（它的 MCP bridge 进程环境里就带着 `TEAM_MCP_TOKEN`，见 `crates/aionui-api-types/src/team_mcp.rs:35-39` 与 `crates/aionui-app/src/commands/cmd_team_stdio.rs:89-99`），就可以直连该团队的 TCP 端口，在 `initialize` 里把 `slot_id` 填成 Lead 的 slot_id，从而 `authorize_tool`（`crates/aionui-team/src/mcp/tools.rs:29-37`）与 `exec_spawn_agent` 的 Lead 门禁全部通过 —— **把"Lead-only"降级为"知道 Lead slot_id 即可"**。

> 证据定级说明：第 1-4 点均为**代码事实**（已逐一读到）。由此推出的"teammate 可伪装 Lead"是**基于这 4 点的推论**；我**未验证**在该部署形态下 teammate 能否实际取得该 token 字符串（Windows 下读取父/子进程环境变量的难易、以及 CLI 沙箱是否限制网络/进程探测）。因此该路径标注为**"代码允许、利用难度未验证"**。

### 4.3 能否直接 spawn CLI 进程 / 直接写 SQLite

- **`spawn_attach_agent_process_bg`**（`crates/aionui-team/src/session.rs:2076`）是**纯内部 Rust 函数**，仅被 `session.rs:1963`（spawn_agent）与 `service.rs:984`（用户 add_agent）调用；**未通过任何 MCP 工具或 HTTP 路由暴露给 Agent**。所以 Agent **不能**通过团队协作接口直接拉起 teammate 进程 —— 它只能通过 #3/#6/#7/#8 这些工具。
- **但 Agent 可以通过自己的 CLI 工具集起进程**：team 工具清单（`team_tools.rs` 13 个工具，`crates/aionui-team/src/mcp/tools.rs:164` 断言 `len() == 13`）**不含任何 shell/exec 工具**；能起进程的是各 CLI 产品自带的工具与内置 `aionrs` 运行时。本项目对 `aionrs` 的工具审批依赖外部 crate `aion-protocol`（`Cargo.toml:71` `aion-protocol = { git = "https://github.com/iOfficeAI/aionrs.git", tag = "v0.2.11" }`），**该 crate 源码不在本 checkout 内**（`crates/aionui-ai-agent/src/manager/aionrs/agent.rs:16` `use aion_protocol::{ToolApprovalManager, ToolApprovalResult};`）→ **未验证**。
- **直接写 SQLite**：数据库是普通 SQLite 文件（`aionui-app/src/commands/cmd_user.rs:205` `cli.data_dir.join("aionui-backend.db")`），**全库未做 SQLCipher 整库加密**（`crates/aionui-db/src` grep `sqlcipher|pragma key` 零命中；加密仅作用于个别敏感列，如 `crates/aionui-db/src/models/provider.rs:16` `api_key_encrypted`）。团队表 `teams` / `mailbox` / `team_tasks` 明文（`crates/aionui-db/migrations/001_initial_schema.sql:477-491` 起）。CLI 进程与后端同 OS 用户，因此**理论上**具备绕过 HTTP/MCP 直接改表的文件权限。**但我未找到也不主张存在这样一个工具化的绕过路径**；这属于"进程同权限即可为"的环境性敞口，应用层没有任何针对它的防护代码，也**没有任何后端强制**。

---

## 缺口清单

按"能否被后端强制"分类。**后端强制 = 有代码分支会在缺审批时拒绝执行**。

### A. 后端强制（已有的执行层门禁 —— 但都不是"用户审批"）

| 项 | 位置 | 强制内容 |
|---|---|---|
| Lead 角色门禁 | `crates/aionui-team/src/mcp/server.rs:1018-1020`；`crates/aionui-team/src/session.rs:1850-1853` | teammate 不能调 `team_spawn_agent`（**可被 4.2 的 slot_id 伪装绕过**） |
| Lead-only 工具表 | `crates/aionui-team/src/mcp/tools.rs:29-37`（`authorize_tool`）；`crates/aionui-api-types/src/team_tools.rs:504-506,521-522,538-539,554-555` | `team_spawn_agent`/`rename`/`clear_context`/`shutdown` 仅 Lead |
| 重名 / 空名 / assistant 存在性 | `crates/aionui-team/src/session.rs:1856-1876` | 拒绝重复/无效创建（**非审批**） |
| 不能移除/关停 Lead | `crates/aionui-team/src/service.rs:1035-1037`；`scheduler/actions.rs:68-70` | 保护 Lead |
| HTTP 路由需 JWT + CSRF | 中间件 `crates/aionui-app/src/router/routes.rs:339`；`crates/aionui-team/src/routes.rs:233-662` 全部 handler 均取 `Extension<CurrentUser>` | **用户**发起的委派需登录（= 用户授权，**非审批**） |
| MCP 连接需 token | `crates/aionui-team/src/mcp/server.rs:286-292` | 仅证明"属于本团队"，**不区分 team 内哪个 agent** |

### B. 仅前端限制

| 项 | 位置 | 说明 |
|---|---|---|
| 桌面端只在用户点按处调用 addAgent | `packages/desktop/src/renderer/pages/team/hooks/useTeamSession.ts:132` | 前端没有"Agent 请求建 teammate"的确认组件；**无任何确认弹窗代码**。前端限制的只是"UI 上没有自动入口"，不是"阻止"。 |
| 前端不渲染 team MCP 的权限卡 | 由后端 `permission_router.rs:66-77` 决定，前端无从渲染 | 属 A 类后果，非前端限制 |

### C. 仅提示词 / 文档约定（不构成实现）

| 项 | 位置 | 原文要点 |
|---|---|---|
| 工具描述要求"用户已明确批准" | `crates/aionui-api-types/src/team_tools.rs:8-19` | "Use this only when ... The user explicitly approved the proposed teammate lineup" / "wait for explicit approval in a later user message" |
| CLI stdio 工具描述同样文本 | `crates/aionui-app/src/commands/cmd_team_stdio.rs:336` | 同一段字符串（**后端未解析、未校验**） |
| 测试把描述文本当契约锁定 | `crates/aionui-team/src/mcp/tools.rs:186-201` | `desc.contains("explicitly approved")`，注释 `"description must retain the explicit-approval precondition clause"` —— **明确证明这只是描述文本** |
| Lead 角色提示词 11/12 条 | `crates/aionui-team-prompts/src/role_prompt.rs:54-55` | "Wait for explicit confirmation before using team_spawn_agent"（**提示词，非实现**） |
| 规划流程 10 条 | `crates/aionui-team-prompts/src/role_prompt.rs:52`、`:105`、`:108` | "Do NOT call team_spawn_agent in that same turn"（**提示词**） |
| teammate 侧 shutdown 握手提示 | `crates/aionui-team-prompts/src/role_prompt.rs:288,94` | 由 Agent 自己回 `shutdown_approved` |

### D. 无（不存在）

| 项 | 证据 |
|---|---|
| 委派审批单 / 审批表 | `crates/aionui-db/migrations/` 全部 `CREATE TABLE` 清单中**无 `approvals` / `delegations` 表**；唯一含 `'pending','approved','rejected','expired'` 的是 `assistant_pairing_codes`（`001_initial_schema.sql:477-486`，远程渠道配对，与委派无关） |
| 委派专用的审批内存 / 状态机 | `crates/aionui-team/src` 全目录 grep `(?i)approval\|approved\|consent\|pending_approval\|user_decision\|is_approved` 仅命中两类：`shutdown_approved` 哨兵（agent 互认）与 `mcp/tools.rs:198` 的**测试断言字符串** |
| 委派前的阻塞/等待态 | `work_coordinator` 的 `RequiresAction` 只统计**工具审批**计数：`crates/aionui-session/src/state.rs:61` `waiting_on_approval`、`reducer.rs:333-360` —— 与委派无关 |
| "每次委派必须批准"开关 | 未找到任何配置项 |

---

## 最小改动点

目标：以 AionUi 为二次开发基座，实现"**调用单 + 审批校验**"，使"没有用户批准就无法委派"成为**后端强制**而非提示词约定。建议分三层插桩，**必须全部落在 AionCore 的 Rust 侧**，改前端一律无效。

### 第 1 层（必需，唯一不可绕过的收口点）：`TeamSession::spawn_agent`

- **文件/函数**：`crates/aionui-team/src/session.rs:1846` `pub async fn spawn_agent(&self, caller_slot_id: &str, req: SpawnAgentRequest) -> Result<TeamAgent, TeamError>`
- **插入位置**：在现有 `Step 1`（`:1850-1853` 角色检查）之后、`Step 4`（`:1900` `persist_spawned_agent`）之前，插入委派单校验。
- **为什么必须在这里**：`session.rs:1847-1849` 的注释明确写着"this method is exposed on TeamSession so every entry point (including future direct service callers) re-checks" —— 它是**所有**创建路径（MCP `exec_spawn_agent` → `spawn_agent_in_session`（`spawn_support.rs:208-218`）、以及未来任何直接调用）的唯一汇合点。在此拒绝，则 `team_spawn_agent` 与任何新增入口同时被封堵。
- **所需配套**：新增 `TeamError::DelegationNotApproved { request_id }`（`crates/aionui-team/src/error.rs:20` 附近，参照 `LeaderOnly` 的写法）并在 `crates/aionui-team/src/routes.rs:43-177` 的 `From<TeamError> for ApiError` 中映射为一个新的 `ApiError::coded(... "TEAM_DELEGATION_NOT_APPROVED" ...)`，以便前端可识别、MCP 侧经 `classify_public_error` 透出。

### 第 2 层（必需，堵住"派活即启动运行"）：任务分派与唤醒

仅卡 `spawn_agent` 不够 —— teammate 可以用 `team_task_create` / `team_task_update` / `team_send_message` 达到"让另一个 Agent 跑起来"的同等效果。收口点：

- **`crates/aionui-team/src/mcp/server.rs:1120` `maybe_notify_task_owner(...)`** —— 这是"任务变为可执行 → 唤醒 owner 启动一轮运行"的**唯一**咽喉（被 `:1186` create、`:1217` reassign、`:1227` unblock 三处调用）。在 `should_wake_task_owner`（`:1092-1109`）的纯门禁中增加一个"该 (from_slot, owner, task) 是否已有获批调用单"的条件，即可让"未批准的派活"只写看板、不触发运行。
- **`crates/aionui-team/src/scheduler/actions.rs:6` `create_task` / `:18` `update_task`** —— 若要求"未批准连看板都不能写"，在此拒绝（此层更严格但语义更清晰：委派单未批则任务不可下达）。
- **`crates/aionui-team/src/mcp/server.rs:773` `exec_send_message`**（底层为 `crates/aionui-team/src/service.rs:3158` `send_agent_message_from_agent` 与 `crates/aionui-team/src/session.rs` 中对应的 `send_agent_message_from_agent`）—— 对 `to != "*"` 且目标为 teammate 的定向消息同样要求调用单，否则保留广播/只读语义。

### 第 3 层（必需，否则第 1/2 层形同虚设）：堵住 Lead 伪装

第 1、2 层的校验都以"caller 是 Lead"为前提，而该前提目前可由客户端自报突破。**必须绑定 slot 身份**：

- **首选**：`crates/aionui-team/src/mcp/server.rs:269` `fn handle_initialize(request, auth_token) -> InitResult` —— 改为**按 `slot_id` 生成/校验 token**（一 slot 一 token），并在 `initialize` 时校验 `(token, slot_id)` 配对；配套改 `crates/aionui-team/src/session.rs:215`（每 session 单 token → 每 slot 生成）与 `session.rs:316-324` `mcp_stdio_config`（签发该 slot 专属 token）。
- **次选（改动更小）**：在 `handle_initialize` 收到 `slot_id` 后，用一个由服务端生成的 `pending_bindings: slot_id -> one-time secret` 映射校验客户端附加的 secret，避免改动 `TeamMcpStdioConfig` 类型（`crates/aionui-api-types/src/team_mcp.rs:24-31`）。
- 若不做第 3 层，则第 1、2 层可被"teammate 冒充 Lead"绕过，**审批链不成立**。

### 第 4 层（建议，用于"免确认模式"兜底）：与现有审批基建合并

- 委派单可直接复用 `crates/aionui-conversation/src/service.rs:3533` `check_approval` 的**记忆语义**（"始终允许"按 `approval_key(action, command_type)` 缓存，`crates/aionui-ai-agent/src/shared_kernel/approval.rs:7`），但**务必新增独立的 action 命名空间**（如 `delegate:spawn`），否则会与工具审批串味。
- **注意**：**不要**把委派单挂到 `crates/aionui-ai-agent/src/manager/acp/permission_router.rs:66-77` 的 `AUTO_APPROVE_MCP_SERVERS` 通道上 —— 该通道会 `continue` 直接放行、永不生成前端卡片，等于自毁审批。委派审批必须走**独立的、非 MCP-allowlist** 的通道。
- 另需在 `crates/aionui-ai-agent/src/manager/aionrs/agent.rs:191` 的 `yolo` 判定处明确"yolo 模式是否豁免委派审批"，并**默认不豁免**。

### 改动优先级

1. 第 3 层（身份绑定）—— 不修则 1、2 层可绕。
2. 第 1 层（`TeamSession::spawn_agent`）—— 覆盖"创建 teammate"。
3. 第 2 层（`maybe_notify_task_owner`）—— 覆盖"派活即运行"。
4. 第 4 层（记忆/豁免策略）—— 体验与一致性。

---

## 未找到 / 未验证清单

**未找到（grep 已覆盖，明确不存在）：**

1. **未找到** 任何"委派审批单"持久化对象：`crates/aionui-db/migrations/` 无 `approvals`/`delegations` 表；`CREATE TABLE` 全量清单已核对。
2. **未找到** `crates/aionui-team/src` 内任何 `approval`/`approved`/`consent`/`pending_approval`/`user_decision`/`is_approved` 语义的实现代码（仅 `shutdown_approved` 哨兵与描述文本测试断言）。
3. **未找到** `team_spawn_agent` / `team_task_create` / `team_task_update` / `team_send_message` 执行路径上的任何用户审批分支。
4. **未找到** 前端（`AionUi-main/packages`）中任何针对"Agent 请求创建 teammate / 委派"的确认 UI；`ipcBridge.team.addAgent` 唯一调用点是用户点按的 UI hook。
5. **未找到** `PermissionAuthorizer` 的实现：该标识符**只在注释中出现**（`crates/aionui-session/src/event.rs:241`、`crates/aionui-session/src/backend/acp_conn.rs:1560`），在 `crates/` 下无任何 Rust 实现；`acp_conn.rs:1556-1560` 注释所称的"注入 PermissionAuthorizer（team-MCP allowlist 自动批准）"**无法在本 checkout 中证实**。实际生效的自动批准实现在 `crates/aionui-ai-agent/src/manager/acp/permission_router.rs:15,66-77`（有实现、有测试）。
6. **未找到** `exec_spawn_agent` 之外第二条创建 teammate 的 MCP 工具；工具清单固定 13 个（`crates/aionui-team/src/mcp/tools.rs:164`）。

**未验证（不做推测，需进一步取证）：**

1. **`aionrs`（内置运行时）工具级审批内部逻辑未验证**：依赖外部 git crate `aion-protocol v0.2.11`（`Cargo.toml:68-73`），源码不在本 checkout；`crates/aionui-ai-agent/src/manager/aionrs/agent.rs:16,565-566` 仅调用 `ToolApprovalManager` / `is_auto_approved`，其判定规则（是否对团队委派工具豁免）无法从本仓库读取。
2. **teammate 实际取得共用 `TEAM_MCP_TOKEN` 的可行性未验证**：token 经 env（`crates/aionui-api-types/src/team_mcp.rs:35-39`、`crates/aionui-app/src/commands/cmd_team_stdio.rs:89-99`）注入 stdio bridge 子进程；在不同的 CLI 沙箱/OS（Windows/Linux）下能否读取该字符串未验证。故 4.2 的 Lead 伪装标注为"代码允许、利用难度未验证"。
3. **`acp_conn.rs` 与 `permission_router.rs` 两条 ACP 路径中哪条为团队模式实际生效路径未验证**：两套 ACP 实现并存（`crates/aionui-session/src/backend/acp_conn.rs` 与 `crates/aionui-ai-agent/src/manager/acp/`）。`spawn_attach_agent_process_bg`（`crates/aionui-team/src/session.rs:2076`）经 `task_manager`（`aionui-ai-agent`）挂载，故推断 `permission_router.rs` 生效，但**未做运行时验证**。
4. **未验证** 该 CLI/桌面组合是否有本仓库之外的网关层对 `/api/teams/*/agents` 做二次审批（如企业策略/代理）。
5. **未验证** `crates/aionui-ai-agent/src/agent_task.rs:117,387` 的 `check_approval` 是否在任何团队工具路径被调用 —— 本仓库内 grep 只找到其定义与测试，**未找到团队路径上的调用点**。
