# 硬条件 3 验证：原生子 Agent 能否禁用或拦截

审计对象：`vendor\AionCore\AionCore-main`（Rust/Axum/SQLite 后端）、`vendor\AionUi\AionUi-main`（Electron/TS 桌面端）。
只读审计，未编译、未运行。所有结论均给出 `文件路径:行号`；找不到的一律写「未找到」，不做推测。

---

## 结论

**未能禁用，也未能拦截 —— 状态：`未实现`。**

AionCore 对 CLI 产品自带的子 Agent / 后台委派**只有观测能力，没有任何门禁或计数**：它把 claude 的 `Task`/`Workflow`、codex 的 `collabAgentToolCall(spawnAgent)` 翻译成 `SessionEvent::SubagentUpdate` 事件，仅用于 UI 的 roster 显示与「转圈」判定（`crates\aionui-session\src\backend\claude_conn.rs:1791-1797`、`crates\aionui-session\src\backend\codex_conn.rs:6287-6329`、`crates\aionui-session\src\state.rs:38-45`）。

全仓 grep 未找到任何「关闭原生委派」的开关：没有 `--disallowed-tools`/`--allowedTools`/`disable_tools`/`disallowedTools` 被真实传入（唯一出现是历史注释 `crates\aionui-session\src\backend\claude_conn.rs:242`，说明该 flag 已被**移除**），没有任何 `CLAUDE_CODE_*` / `CODEX_*` 类「禁止子 Agent」环境变量，`agent_metadata` 的 args/env 也没有相关项。

更进一步，**团队模式实际上把权限门全部打开了**：团队成员的 `session_mode` 被强制写成各后端的 full-auto 值（claude→`bypassPermissions`、codex→`agent-full-access`、gemini/qwen→`yolo`；`crates\aionui-common\src\enums.rs:127-136` 与 `crates\aionui-team\src\service\spawn_support.rs:68-79`），agy 的审批钩子还会被**主动删除**（`crates\aionui-ai-agent\src\factory\antigravity.rs:68-79`）。所以在团队模式下，原生委派既不会被审批、也不会被计数，**且比单聊更不可能被拦**。

唯一「代码强制」的授权元数据是 `TeamToolPermission::LeadOnly`，但它只管 `team_*` MCP 工具（`crates\aionui-team\src\mcp\server.rs:523`），**与 CLI 原生工具完全无关**。

---

## 各后端启动参数表

### 启动入口总览（两条并行生产路径）

- **ACP 桥路径**（claude / codex / gemini / qwen / codebuddy 等默认走这条）：命令与参数来自数据库 catalog，不是代码常量。`crates\aionui-db\migrations\001_initial_schema.sql:186-230`（种子行），后续被 `008_builtin_acp_agents_use_npx.sql:32`、`025_sync_and_add_acp_registry_agents.sql:5-23` 改写；启动时只做策略叠加 `crates\aionui-ai-agent\src\factory\acp_launch_policy.rs:16-102`。
- **直连 CLI 路径**（claude / codex 另有此路径）：`crates\aionui-ai-agent\src\session_agent.rs:1882-1886` 按 backend label 选 `ClaudeConnection` / `CodexConnection`。

| 后端 | 启动入口 文件:行 | 关键参数/环境变量 | 权限模式 | 能否禁止原生委派 |
|---|---|---|---|---|
| **Claude Code（ACP 桥，生产默认）** | `crates\aionui-db\migrations\001_initial_schema.sql:192-198`（`bun x --bun @agentclientprotocol/claude-agent-acp@0.29.2`）；模式落地 `crates\aionui-ai-agent\src\factory\acp_launch_policy.rs:16-24` + `crates\aionui-ai-agent\src\manager\acp\mode_normalize.rs:6-41` | 无 `--permission-mode`；环境变量 `AIONUI_CONVERSATION_ID` / `AIONUI_HELPER_BIN` / `AIONUI_BASE_URL` / `AIONUI_RUNTIME_TOKEN`（常量定义 `crates\aionui-ai-agent\src\types.rs:99-100`，注入 `acp_launch_policy.rs:26-33`）；claude 额外注入 cc-switch 的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`（`acp_launch_policy.rs:35-53`） | 走 ACP 带内 `session/set_mode`（写入点 `crates\aionui-session\src\backend\acp_conn.rs:2331-2334`）；团队强制 `bypassPermissions` | **未找到** |
| **Claude Code（直连 CLI）** | `crates\aionui-session\src\backend\claude_conn.rs:154-269`（`build_claude_init_args`）；argv 拼装 `crates\aionui-session\src\adapter\claude.rs:1080-1150` | `--append-system-prompt <preset>`、`--mcp-config <json>` + `--strict-mcp-config`、`--permission-mode <mode>`、`--allow-dangerously-skip-permissions`、`--permission-prompt-tool stdio`（`adapter\claude.rs:1104-1105`）、`--session-id`/`--resume` | `--permission-mode`；缺省 fail-closed 为 `default`（`claude_conn.rs:174-214`）；白名单校验 `claude_conn.rs:196-214`；团队强制 `bypassPermissions` | **未找到**（`--disallowed-tools` 仅存在于历史注释 `claude_conn.rs:239-244`，已不再传入） |
| **Codex CLI（直连 app-server）** | `crates\aionui-session\src\backend\codex_conn.rs:65-72`（`codex_app_server_args`）、`:121-139`（spawn） | `codex app-server`、`-c shell_environment_policy.inherit=all`、`-c shell_environment_policy.include_only=[]`（常量 `codex_conn.rs:47-60`） | `thread/start{approvalPolicy, sandbox}`（`codex_conn.rs:551-562`），默认 `on-request` + `workspace-write`；`agent-full-access` → `danger-full-access`（`codex_conn.rs:2357-2360`） | **未找到**；codex 原生 `spawnAgent` 只被翻译成事件（`codex_conn.rs:6287-6329`） |
| **Codex CLI（ACP 桥）** | `crates\aionui-db\migrations\001_initial_schema.sql:200-206`（`bun x --bun @zed-industries/codex-acp@0.9.5`，测试里见 `crates\aionui-db\tests\cron_assistant_first_migration.rs:342`） | 追加 `-c shell_environment_policy.inherit=all`、`-c shell_environment_policy.include_only=[]`、`-c sandbox_mode="<m>"`，`danger-full-access` 时再加 `-c windows.sandbox="unelevated"`（`crates\aionui-ai-agent\src\factory\acp_launch_policy.rs:72-102`，常量 `:7`） | 由 `codex_sandbox_mode_for_requested_mode` 决定（`acp_launch_policy.rs:97-102`）；团队强制 `agent-full-access` → `danger-full-access` | **未找到** |
| **Gemini CLI** | `crates\aionui-db\migrations\001_initial_schema.sql:208-214`（`gemini --experimental-acp`）→ 改为 `["--acp"]`：`025_sync_and_add_acp_registry_agents.sql:5-6` | 仅 `--acp` | ACP `session/set_mode`；团队 fallback `yolo`（`enums.rs:135`） | **未找到** |
| **Qwen** | `crates\aionui-db\migrations\001_initial_schema.sql:216-222`（`qwen --acp`）→ `025:7-8`（`["--acp","--experimental-skills"]`，`yolo_id=NULL`） | `--acp --experimental-skills` | `yolo_id=NULL` → fallback `full_auto_mode_id(Some("qwen"))` = `yolo`（`spawn_support.rs:73-78`、`enums.rs:135`） | **未找到** |
| **CodeBuddy** | `crates\aionui-db\migrations\001_initial_schema.sql:224-230`（`bun x --bun @tencent-ai/codebuddy-code@2.73.0 --acp`）→ `008:32` → `025:9-11`（`npx -y --package @tencent-ai/codebuddy-code codebuddy --acp`，版本由 npx lock 钉住 `crates\aionui-runtime\src\registry_npx_lock.rs:113-124`） | `npx -y --package … codebuddy --acp` | 团队 fallback `bypassPermissions`（`enums.rs:130`） | **未找到** |
| **iFlow** | **未找到** | — | — | — |
| **Antigravity（agy）** | `crates\aionui-session\src\backend\antigravity\argv.rs:78-138` | `-p <prompt> --output-format stream-json --dangerously-skip-permissions --print-timeout 1h`，加 `--conversation <id>` 或 `--new-project`，加 `--add-dir <workspace>`，可选 `--model` / `--mode accept-edits\|plan`，末尾追加 `extra_args` | 宿主侧钩子决定；`yolo` 哨兵**不进命令行**，改为「不安装审批钩子」（`antigravity\argv.rs:6,119-128`） | **未找到**（agy 无 full-auto 模式，见 `enums.rs:137-145`） |
| **aionrs（内置，无外部进程）** | `crates\aionui-ai-agent\src\manager\aionrs\agent.rs:191`（`auto_approve: config_extra.session_mode.as_deref() == Some("yolo")`） | 无 CLI 参数 | `auto_approve` 布尔 | **未找到** |

### 关键参数拼装代码片段

Claude 直连（含 fail-closed 注释与两个权限 flag）：

```rust
// crates\aionui-session\src\backend\claude_conn.rs:196-237
let mode = config.mode.as_deref()
    .map(str::trim).filter(|s| !s.is_empty())
    .filter(|m| { let ok = crate::adapter::is_valid_claude_permission_mode(m); … ok })
    .unwrap_or("default");
args.push("--permission-mode".to_string());
args.push(mode.to_string());
…
args.push("--allow-dangerously-skip-permissions".to_string());
```

Codex ACP 沙箱由模式推导：

```rust
// crates\aionui-ai-agent\src\factory\acp_launch_policy.rs:97-102
fn codex_sandbox_mode_for_requested_mode(mode: Option<&str>) -> &'static str {
    match mode.map(str::trim) {
        Some("agent-full-access" | "full-access" | "yoloNoSandbox") => "danger-full-access",
        _ => "workspace-write",
    }
}
```

团队成员的权限模式被强制写死：

```rust
// crates\aionui-team\src\service\spawn_support.rs:68-79
pub(crate) fn session_mode_for_backend(backend: &str, agent_type: AgentType, acp_metadata: Option<&AgentMetadataRow>) -> String {
    if let Some(row) = acp_metadata
        && let Some(yolo_id) = row.yolo_id.as_deref().map(str::trim).filter(|value| !value.is_empty())
    { return yolo_id.to_owned(); }
    agent_type.full_auto_mode_id(Some(backend)).to_owned()
}
```

```rust
// crates\aionui-team\src\provisioning.rs:632-640（写进 conversation extra）
let session_mode = session_mode_for_backend(&agent.backend, agent_type, cli_metadata.as_ref());
let mut patch = if preserve_session_mode {
    serde_json::json!({ "team_mcp_stdio_config": mcp_stdio_cfg })
} else {
    serde_json::json!({ "team_mcp_stdio_config": mcp_stdio_cfg, "session_mode": session_mode })
};
```

```rust
// crates\aionui-common\src\enums.rs:127-136
pub fn full_auto_mode_id(&self, backend: Option<&str>) -> &'static str {
    match self {
        AgentType::Acp => match backend {
            Some("claude") | Some("codebuddy") => "bypassPermissions",
            Some("codex") => "agent-full-access",
            Some("hermes") => "default",
            Some("opencode") => "build",
            Some("cursor") => "agent",
            _ => "yolo",
        },
```

设计意图也被文档固化（团队模式卡权限确认是「bug」）：

> `crates\aionui-team\docs\phase2\bugfix.md:44-56` —「Problem 3: 成员 MCP 工具调用卡在权限确认 … 根因：Team 成员 conversation extra 里没有 `session_mode`。Claude Code 默认 default mode，MCP 调用触发权限确认。修复：在 team 成员创建时强制写入 `"session_mode": "bypassPermissions"`」

---

## 工具授权机制辨析

### 1. `aionui-team-prompts` 的 governance —— **仅提示词，非强制**

`crates\aionui-team-prompts\src\governance.rs:1-24` 的 `TEAM_GOVERNANCE_PROMPT` 是一段 `&'static str` 提示词，经 `with_team_governance()` 拼到角色提示词前缀。全文没有任何调用点做校验；它只声明「Lead-only tools cannot be used by teammates」（`governance.rs:19`）。**这是文字约定，不是白名单。**

同类：`crates\aionui-team-prompts\src\team_tool_usage.rs:18-21` 明确写着要求模型别用平台内置工具：

> `You MUST use the team_* MCP tools for ALL team coordination. Your platform may provide similarly named built-in tools. Do NOT use those.`

以及 `crates\aionui-team\src\prompts\mod.rs:261-282` 断言提示词里含「Wait for explicit confirmation before using team_spawn_agent」等句子 —— 这些**全部是提示词，非强制**，模型完全可以忽略。

对照 `crates\aionui-team\docs\team-prompts.md:94`：「**严禁用平台内置工具**（SendMessage/TaskCreate/Agent），必须用 `team_*` MCP 工具」—— 文档措辞是「严禁」，但落到代码只是一行提示词文本。**必须标注为「仅提示词」**。

### 2. `TeamToolPermission` —— **代码强制，但只管 `team_*` 工具**

元数据是真的被代码消费的：

- 描述符级权限：`crates\aionui-api-types\src\team_tools.rs:40-47`（`TeamToolPermission::LeadOnly`）、`:373,505,522,539,555`（`team_spawn_agent` / `team_rename_agent` / `team_clear_agent_context` / `team_shutdown_agent` / `team_interrupt_agent` 标记为 LeadOnly）。
- 角色过滤（列表层）：`team_tools.rs:277-283` `team_tool_descriptors_for_role` 对非 Lead 剔除 LeadOnly；MCP `tools/list` 走它（`crates\aionui-team\src\mcp\server.rs:332,350`），测试 `crates\aionui-team\tests\mcp_server_integration.rs:322,344`。
- **调用层强制（真正的门）**：`crates\aionui-team\src\mcp\server.rs:523`

```rust
pub(crate) async fn dispatch_tool(…, caller_role: TeammateRole) -> Result<String, ToolCallError> {
    super::tools::authorize_tool(caller_role, tool_name).map_err(ToolCallError::from_message)?;
```

`authorize_tool` 定义在 `crates\aionui-team\src\mcp\tools.rs:29-37`，`team_spawn_agent` 另有二次硬门 `mcp\server.rs:1018-1020`（`if caller_role != TeammateRole::Lead { return Err("Only Lead can spawn agents") }`）与 `crates\aionui-team\src\session.rs:1846-1852`（`TeamError::LeaderOnly`）。
CLI 与 HTTP 回落路径同样过这道门：`crates\aionui-app\src\router\runtime_team_tools.rs:99-108` → `crates\aionui-team\src\service.rs:2346-2354` → `crates\aionui-team\src\tool_executor.rs:40-54` → `mcp\server.rs:dispatch_tool`。

**注意**：`aionui-team-prompts` 自己导出的 `authorize_team_tool`（`crates\aionui-team-prompts\src\tools.rs:37-45`）**在全仓没有任何生产调用点**——只在它自己的单测 `:66` 里被调用。生产强制走的是 `aionui-team\src\mcp\tools.rs` 里的同名逻辑。

### 3. `capability.rs` / `runtime_tools.rs` / `tool_executor.rs` / `visibility.rs` —— **无原生工具过滤**

- `crates\aionui-team\src\capability.rs:1-18` 只判断「这个后端能不能进团队模式 / 支不支持 Team MCP」，与工具白名单无关。
- `crates\aionui-team\src\runtime_tools.rs:1-53` 只做 conversation→agent 归属校验与错误载荷映射，无工具名断言。
- `crates\aionui-team\src\tool_executor.rs:36-38` `list_tools` 用 `team_tool_descriptors_for_role` 过滤（同上一节），无其它过滤。
- `crates\aionui-team\src\visibility.rs:1-65` 是消息可见性策略（是否写 mailbox、是否插气泡、是否剥离 `[SYSTEM NOTE]`），**与工具无关**。

### 4. 结论

存在一套**代码强制的角色白名单**，但它的作用域严格限定为 13 个 `team_*` 工具（`crates\aionui-team-prompts\src\tools.rs:76-93` 的完整清单）。**CLI 自带工具（含 `Task`、`Workflow`、codex `spawnAgent`、bash、write…）不在任何白名单内，也没有任何代码路径尝试枚举或过滤它们。**

---

## ACP 拦截覆盖范围

### 唯一的拦截点

ACP 反向 RPC 处理器是全部拦截能力的所在地：

```rust
// crates\aionui-session\src\backend\acp_conn.rs:1544-1621
async fn handle_reverse_rpc(method: &str, …) {
    match method {
        "session/request_permission" => { … emit(SessionEvent::Permission { … }) }
        _ => { write_reverse_error(stdin, &id, -32601, "method not handled by aionui-session").await; … }
    }
}
```

即：**Agent 主动发起的每一个 `session/request_permission` 都会浮到审批链路**（`:1556-1607`），其它反向方法一律 `-32601` 拒绝（`:1609-1610`）。注意这里**没有任何按工具名的过滤/拒绝逻辑** —— 请求一律放行到审批层。

审批层的自动放行只有一种，且范围极窄：

```rust
// crates\aionui-ai-agent\src\manager\acp\permission_router.rs:15
const AUTO_APPROVE_MCP_SERVERS: &[&str] = &[TEAM_MCP_SERVER_NAME];
// :66-77  auto_approve_option_id() 命中才免人工，否则 emit AcpPermission 等用户
// :180-200 auto_approve_option_id / select_allow_option_id
```

`TEAM_MCP_SERVER_NAME = "aionui-team"`（`crates\aionui-api-types\src\team_mcp.rs:17`）。除 `aionui-team` 这个 MCP server 之外，「人工决定一切」。

直连 claude 路径同理：`--permission-prompt-tool stdio` 让 claude 用 `control_request{can_use_tool}` 提问，adapter **对任何 `can_use_tool` 都 surfacing 成 `Permission`**，不过滤工具名（`crates\aionui-session\src\adapter\claude.rs:930-1020`，尤其 `:985-987` 与 `:1101-1105`）。codex 直连的 `*/requestApproval` 也一样（`crates\aionui-session\src\backend\codex_conn.rs:2581-2694`），其注释还明确写了 G3 自动放行是 **ACP-only**（`codex_conn.rs:2611-2612,2673-2674`）。

### 逐后端：内部委派能否进审批链路

| 后端 | 原生委派机制 | 是否进 AionCore 审批链路 | 证据 / 判定依据 |
|---|---|---|---|
| **Claude Code（ACP 桥）** | `Task` / `Workflow` 工具 | **取决于 `@agentclientprotocol/claude-agent-acp` 是否为 Task 发 `session/request_permission` —— 本仓未验证** | 本仓只证明「ACP 的每个 permission request 都会被 surfacing」（`acp_conn.rs:1556-1607`），**没有**证明 claude-agent-acp 会为 Task 提问。未找到该桥的源码或探针数据 → 标注**未验证** |
| **Claude Code（直连 CLI）** | `Task` / `Workflow` 工具 | **未验证**，但即使提问也只是「人工审批」而非「禁止」 | adapter 对任何 `can_use_tool` 都 surface（`adapter\claude.rs:985-987,1101-1105`）；而 Task 子 Agent 在 `claude_conn.rs:1791-1797` 只被 `sniff_task` **旁路观测**，无任何 gating/counter |
| **Codex（直连 app-server）** | `collabAgentToolCall` / `spawnAgent` | **拦截：否。无提问、无审批、无计数** | `crates\aionui-session\src\backend\codex_conn.rs:6287-6329` 把 collab 帧翻译为 `SubagentUpdate` 事件，测试断言只检查事件形状；`:2611-2612` 明示 codex 原生 approval 不走 G3 自动放行，也没有任何 subagent 相关 approval 分支 |
| **Codex（ACP 桥）** | 同上 | **未验证**（同 claude ACP：取决于桥是否提问） | 同上 |
| **Gemini / Qwen / CodeBuddy / 其它 ACP vendor** | 各家内部实现 | **未验证**；本仓无对应探针或桥源码 | `acp_conn.rs:1556-1607` 只能保证「问了就拦」，不能保证「会问」 |
| **Antigravity（agy）** | agy 自身能力 | **取决于 agy 是否把子任务作为工具调用** —— agy 的钩子 `matcher:"*"` 会拦所有工具调用（`crates\aionui-ai-agent\src\antigravity_hook.rs:101-120`），但团队模式下图钩子被删除（`factory\antigravity.rs:68-79`） | 见下节 |
| **aionrs（内置）** | 无外部 CLI | 不适用 | `crates\aionui-ai-agent\src\manager\aionrs\agent.rs:191` |

### 明确判定

- **能进审批链路**：任何**经由 ACP `session/request_permission` 或 claude `can_use_tool` 或 codex `*/requestApproval` 提出的**工具调用 —— 但 AionCore 无法决定 CLI 是否为「创建子 Agent」这件事提问。
- **无法拦截（明确结论）**：
  1. codex 的 `spawnAgent` / collab agent —— 本仓证据显示它只被翻译成 `SubagentUpdate` 事件（`codex_conn.rs:6287-6329`），**不过审批、不计数**。
  2. claude 的 `Task`/`Workflow` —— AionCore 对它们**只有 `sniff_task` 旁路观测**（`claude_conn.rs:1791-1797`），且 `subagents` 明确「NEVER affect can_send / requires-action」（契约见 `crates\aionui-session\src\state.rs:298-345`；对应断言 `crates\aionui-session\src\reducer.rs:1941-1948`），**不构成门禁或计数**。
  3. 团队模式下 agy —— 审批钩子被主动移除（`factory\antigravity.rs:76`），加上 `--dangerously-skip-permissions`（`antigravity\argv.rs:98`），**完全没有审批**。

### 旁证：子 Agent 完全不计入任何配额

`SubagentUpdate` 的唯一 reducer 读臂只做 roster upsert（`crates\aionui-session\src\reducer.rs:455-492`），其效果是 `has_activity`（转圈）判定（`reducer.rs:1826-1830`、`crates\aionui-session\src\state.rs:298-345`）。契约本身写明 subagents 不参与任何门禁，测试断言见 `crates\aionui-session\src\reducer.rs:1941-1948`。全仓**未找到**任何对 subagent 的数量/并发/深度计数或上限。

---

## 免确认模式传染性

### 权限模式是怎么传给子进程的

| 路径 | 传递方式 | 代码 |
|---|---|---|
| ACP 桥 | 不作为 CLI 参数；由 host 写 `session/set_mode` JSON-RPC | `crates\aionui-session\src\backend\acp_conn.rs:2321-2341`；mode 别名归一 `crates\aionui-ai-agent\src\manager\acp\mode_normalize.rs:6-41` |
| Claude 直连 | `--permission-mode <mode>` CLI 参数（spawn 期） | `crates\aionui-session\src\backend\claude_conn.rs:196-214` |
| Codex 直连 | `thread/start{approvalPolicy, sandbox}` JSON-RPC | `crates\aionui-session\src\backend\codex_conn.rs:551-562` |
| Codex ACP | `-c sandbox_mode="<m>"` CLI 参数 | `crates\aionui-ai-agent\src\factory\acp_launch_policy.rs:85-89` |
| Antigravity | 宿主侧：装/卸 `hooks.json`（`mode` 值本身不进命令行） | `crates\aionui-session\src\backend\antigravity\argv.rs:119-134`；`crates\aionui-ai-agent\src\factory\antigravity.rs:64-99` |

### 团队模式下强制某个模式 —— **是，且是 full-auto**

- 挂载点：团队成员创建时写 `session_mode`（`crates\aionui-team\src\provisioning.rs:632-640`、`:662-670`）；MCP 路径 `crates\aionui-team\src\provisioning.rs:870-881`（`build_team_extra` 里 `"session_mode": session_mode`）。
- 取值：`spawn_support.rs:68-79` → catalog 行的 `yolo_id`，否则 `full_auto_mode_id(backend)`。对 claude/codebuddy = `bypassPermissions`，codex = `agent-full-access`，gemini/qwen 及其它 = `yolo`（`crates\aionui-common\src\enums.rs:127-136`）。
- 团队级开关：`crates\aionui-team\src\service.rs:3106-3145`（`set_session_mode`）可整体改团队模式，并写种子给后续新成员（`:3144`）；默认没有任何「必须保持严格」的约束。
- agy 特例：`full_auto` 判定命中即**删除** `.agents/hooks.json` 并吊销 hook token（`crates\aionui-ai-agent\src\factory\antigravity.rs:66-79`），注释直说「tools execute without per-call approval」。
- 文档侧承认了这一设计：`crates\aionui-team\docs\phase2\bugfix.md:44-56`（把「成员卡在权限确认」当作 bug 修复，方案是强制 `bypassPermissions`）。

### Agent 能否自己开启 YOLO

- **通过 ACP 协议**：**不能**。`session/set_mode` 是 host→agent 的请求（`acp_conn.rs:2321-2341`），反向只识别 `session/request_permission`，其余一律 `-32601`（`acp_conn.rs:1609-1610`）。没有 agent→host 的「我要改成 bypass」通道。
- **通过团队 CLI**：**不能**。`$AIONUI_HELPER_BIN team …` 的子命令清单里没有改模式项（`crates\aionui-app\src\commands\cmd_team.rs:27-65`）。
- **通过改配置文件**：**可能**，但无提权意义。Agent 有 shell，可改 `~/.claude/settings.json` 等；但 claude 团队成员的 spawn argv/ACP mode 已由 AionCore 固定为 `bypassPermissions`（`claude_conn.rs:196-214` + `provisioning.rs:632-640`），改配置无法让它「更宽松」。同时 `crates\aionui-runtime\src\spawn.rs:96,113` 会剥掉 `NODE_OPTIONS`/`NODE_INSPECT`/`NODE_DEBUG`/`CLAUDECODE`。
- **通过环境变量注入**：用户可用 per-agent `env_override` 注入任意环境变量，唯一禁区是 `AIONUI_*` 前缀与 `HOME/PATH/USER/SHELL/TERM/CODEX_HOME`（`crates\aionui-ai-agent\src\registry.rs:1378-1387`）。**但全仓未找到任何可用于关闭原生委派的 CLI 环境变量**，因此这只是理论扩展点，不是现有能力。
- **提升自身权限为宿主决策**：**不能**。模式切换需要 `Command::SetMode`（host 侧命令），Agent 无法触发。

### 超时

未找到权限请求的超时/自动拒绝实现。`crates\aionui-team\src\mcp\server.rs`、`crates\aionui-ai-agent\src\manager\acp\permission_router.rs` 的 pending 表只由 `confirm()` 或 `cancel_all()` 清理（`permission_router.rs:124-146`）。全仓 `permission.*timeout` 类 grep 唯一命中的是 agy 的 `PERMISSION_ANSWER_DEADLINE`（`crates\aionui-session\src\backend\antigravity\conn.rs:1103`，且那里 agy 超时后是**放行执行**，见 `conn.rs:89` 注释）。与 PRD 一致：`vendor\AionUi\AionUi-main\docs\prds\conversations\acp\permissions.md:10,33,41`（团队模式无超时、30 分钟自动拒绝未实现）。

---

## 隔离能力

### 工作目录 —— **团队共享一个目录，无 per-agent workspace、无 worktree**

```rust
// crates\aionui-team\src\workspace.rs:50-53（新成员的 workspace 解析）
pub(crate) async fn resolve_for_new_agent(&self, row: &TeamRow, team: &Team) -> Result<String, TeamError> {
    if let Some(workspace) = usable_runtime_workspace(row.workspace.trim()) {
        return Ok(workspace);          // ← 直接用「团队级」row.workspace
    }
    if let Some(leader_workspace) = self.resolve_from_leader(team).await? { … }   // ← 或继承 leader 的
    let workspace = self.conversation_port.create_team_temp_workspace(&row.user_id, &row.id).await?;  // ← 团队级临时目录
```

```rust
// crates\aionui-conversation\src\service.rs:452-457
pub fn create_team_temp_workspace(&self, user_id: &str, team_id: &str) -> Result<String, ConversationError> {
    let ws_path = auto_workspace_parent(&self.workspace_root, user_id).join(format!("team-temp-{team_id}"));
```

```rust
// crates\aionui-team\src\service.rs:85-89 —— 同样的 workspace 字符串注给每一个成员
pub(crate) fn inherit_team_workspace(extra: &mut serde_json::Value, workspace: &str) {
    if !workspace.trim().is_empty() { extra["workspace"] = serde_json::Value::String(workspace.to_owned()); }
}
```

该 workspace 最终成为 CLI 的 `cwd`（claude 直连 `claude_conn.rs:1141-1146`，codex 直连 `codex_conn.rs:132`，agy `--add-dir` `antigravity\argv.rs:113-116`）。**所有成员共享同一 cwd，可互相读写对方的文件，无 worktree、无目录隔离。**

worktree 相关代码只用于**展示已存在的 git worktree**（`crates\aionui-project\src\scm\git_provider.rs:342-415`）与快照服务（`crates\aionui-file\src\snapshot_service\helpers.rs:92-124`），**没有任何「为每个 Agent 建 worktree」的实现**。

### OS 级隔离 —— **未找到，且模块自己声明不做安全沙箱**

```rust
// crates\aionui-process\src\containment.rs:1-8
//! Per-platform lifecycle fence (Containment). Tears down a whole subprocess
//! subtree (agent CLI + grandchildren like MCP servers), not just the direct
//! child. Lifecycle-only — orthogonal to any security sandbox.
//!
//! Single tier ships: [`ProcessGroupContainment`] (best-effort Unix process
//! group). Job Object / cgroup tiers are intentionally not built
```

只发一档 `ReapGuarantee::BestEffort`（`containment.rs:14-20,81-83`），且杀完后**可能仍有逃逸的孙进程存活**（`ContainmentKillOutcome::DegradedBestEffort`，`:26-30,74-78`）。这是**生命周期栅栏，不是安全边界**。

其它相关事实：
- spawn 走统一 Builder，继承父环境（`crates\aionui-runtime\src\spawn.rs:97-106`），未做 `env_clear`。
- 没有 seccomp / namespace / cgroup / Windows Job Object / AppContainer。
- codex 的 `windows.sandbox="unelevated"`（`acp_launch_policy.rs:7,88`）是 codex **自己**的沙箱降级开关，不是 AionCore 加的隔离层。

### Agent 能不能自己再起一个 CLI 绕过中转站 —— **能**

1. **直接 fork 原始 CLI**：所有 CLI 都能看到 `PATH` 与真实安装位置（`spawn.rs` 不做环境净化，仅剥 4 个 Node 变量）。一个团队成员完全可以 `bash -c "claude -p '...'"` 等，起一个**完全不被 AionCore 观测**的进程，绕开审批、计数、roster。**这是本硬条件最严重的缺口**，且**未找到任何缓解措施**（无 seccomp、无 PATH 白名单、无进程数限制）。
2. **`conversation create` 这条路已被堵住**：`$AIONUI_HELPER_BIN conversation create` 存在（`crates\aionui-app\src\commands\cmd_conversation.rs:39,46-62`，路由 `crates\aionui-conversation\src\runtime_routes.rs:38-42`），但团队会话身份的调用者被显式 403：

```rust
// crates\aionui-conversation\src\runtime_create.rs:188-193
// 2. Team callers get no ordinary-conversation surface.
if team_id_from_extra_str(&caller.extra).is_some() {
    return Err(ConversationCreateError::CallerIsTeam { id: caller_conversation_id.to_owned() });
}
```

3. **跨会话消息也被堵住**：`crates\aionui-session-message\src\service.rs:163,224,312` 三处 `team_id_from_extra_str(...).is_some()` 直接拒绝团队会话作为发送方或目标。
4. **团队工具本身是过门的**：CLI 与 MCP 两条路径都汇入 `dispatch_tool`（`runtime_team_tools.rs:99-108` → `service.rs:2346-2354` → `tool_executor.rs:43-52` → `mcp\server.rs:523`），角色校验一致，且 runtime token 绑定了 conversation（`runtime_team_tools.rs:126-` `runtime_headers`）。`team_spawn_agent` 对非 Lead 一律拒绝。
5. **`diagnose http get` 是只读逃生舱**（`crates\aionui-app\src\cli.rs:325`），不构成写通道。

因此绕过点集中在第 1 条：**Agent 只要有 shell，就能在 AionCore 视野外再起任意 CLI 进程。**

---

## 缺口清单

| # | 缺口 | 严重度 | 证据 |
|---|---|---|---|
| G1 | **无任何禁用原生委派的开关**：全仓找不到可关闭 claude `Task`/`Workflow`、codex `spawnAgent`、或其它 CLI 内部委派的参数/env/配置 | 高 | 无 `--disallowed-tools`/`--allowedTools`/`disallowedTools` 被传入（唯一命中为历史注释 `claude_conn.rs:242`）；`env_override` 禁区里也没有相关变量（`registry.rs:1378-1387`） |
| G2 | **原生子 Agent 不进审批链路**：claude `Task`/`Workflow` 与 codex `spawnAgent` 只被翻译成 `SubagentUpdate` 事件，仅供 UI roster / 转圈使用 | 高 | `claude_conn.rs:1791-1797`、`codex_conn.rs:6287-6329`、`reducer.rs:455-492`、`state.rs:38-45` |
| G3 | **原生子 Agent 不计数、不设上限**：未找到任何 subagent 数量/并发/深度/时长限制 | 高 | 全仓 grep 无命中；`crates\aionui-session\src\reducer.rs:1941-1948` 断言 subagents 不改 `can_send`/`requires_action` |
| G4 | **团队模式强制 full-auto**：claude `bypassPermissions` / codex `agent-full-access` / gemini·qwen `yolo` | 高 | `spawn_support.rs:68-79`、`enums.rs:127-136`、`provisioning.rs:632-640`、`docs\phase2\bugfix.md:44-56` |
| G5 | **团队模式 agy 直接卸掉审批钩子** | 高 | `factory\antigravity.rs:66-79`（`remove_hooks_json` + 注释「tools execute without per-call approval」） |
| G6 | **无超时/自动拒绝**：pending permission 只能被 `confirm()` 或 `cancel_all()` 清掉 | 中 | `permission_router.rs:124-146`；PRD 自述 `permissions.md:10,41` |
| G7 | **团队共享单一 workspace，无隔离**：无 per-agent 目录、无 worktree | 中 | `workspace.rs:50-77`、`service.rs:85-89`、`conversation\service.rs:452-457` |
| G8 | **无 OS 级隔离**：唯一 containment 是 best-effort 进程组，且模块自述「orthogonal to any security sandbox」 | 中 | `crates\aionui-process\src\containment.rs:1-8,74-78` |
| G9 | **Agent 可自行 spawn 任意 CLI 绕开一切**：无 PATH 白名单、无 seccomp、无进程限额 | 高 | `crates\aionui-runtime\src\spawn.rs:97-106`（继承父环境）；全仓未找到缓解措施 |
| G10 | **「禁止使用平台内置工具」只是提示词** | 中 | `governance.rs:1-24`、`team_tool_usage.rs:18-21`、`docs\team-prompts.md:94` —— 均无代码强制 |
| G11 | **`authorize_team_tool`（team-prompts 版）是死代码**：仅被自身单测调用 | 低 | `crates\aionui-team-prompts\src\tools.rs:37-45`（生产无调用点），生产实现在 `aionui-team\src\mcp\tools.rs:29` |
| G12 | **claude 直连会无条件解锁 bypass 通道**：`--allow-dangerously-skip-permissions` 无条件传入 | 低（有 fail-closed 初值） | `claude_conn.rs:216-237` |

---

## 最小改动点

按「改动量 ÷ 收益」排序；均为**尚未实现**的建议，非现状描述。

1. **给每个后端加「原生委派禁用」启动参数（补 G1）**
   在 `crates\aionui-session\src\backend\claude_conn.rs:154-269`（`build_claude_init_args`）与 `crates\aionui-ai-agent\src\factory\acp_launch_policy.rs:16-24` 增加一个受配置驱动的 flag 注入点：claude 直连传 `--disallowed-tools Task,Workflow`（该 flag 历史上确实用过，见 `claude_conn.rs:242` 注释）；ACP 桥则在 catalog 行的 `args` 里追加等价参数（`agent_metadata.args` 已支持追加，见 `crates\aionui-runtime\src\registry_npx_lock.rs:54-86` 的 pin 逻辑）。**前提**：需先向各 CLI 官方 `--help` 确认 flag 名与语义（本仓未验证）。

2. **把 subagent 生命周期纳入计数与门禁（补 G2/G3）**
   现在 `SubagentUpdate` 的 reducer 读臂是 `crates\aionui-session\src\reducer.rs:455-492`（纯 upsert）。可在该臂加「非终态 subagent 计数」并暴露给会话层；配套的强制点在 `crates\aionui-team\src\mcp\server.rs:523`（`dispatch_tool` 的唯一入口）——在那里对「已达上限的会话」拒绝新增 `team_*` 委派，并对 `SubagentUpdate` 超限发出终止请求。这是**唯一一处已经集中化的调用点**，改动面最小。

3. **团队模式不要强制 full-auto，改为按需（补 G4/G5）**
   `crates\aionui-team\src\service\spawn_support.rs:68-79` 是唯一决定点。把「catalog `yolo_id` → full_auto」改为「读团队级偏好（`crates\aionui-team\src\service.rs:3106-3145` 已有 `set_session_mode` 通道）」，并在 `crates\aionui-ai-agent\src\factory\antigravity.rs:66-79` 保留钩子（用「无人在场时自动拒绝」代替「删除钩子」）。文档 `docs\phase2\bugfix.md:44-56` 的修复需同步反转。

4. **补权限请求超时（补 G6）**
   加在 `crates\aionui-ai-agent\src\manager\acp\permission_router.rs:55-111` 的 pending 循环：`pending_permissions` 已是 `HashMap<call_id, PendingPermission>`，加一个 timewheel/`tokio::time::sleep` 即可在超时后 `send(PermissionDecision::Cancelled)`。同一处也适用于 `crates\aionui-session\src\backend\claude_conn.rs` 的 `can_use_tool` 与 `codex_conn.rs:2581-2694` 的 approval 表。

5. **把「禁用原生委派」升级为启动前硬校验（补 G9 的一半）**
   在 `crates\aionui-session\src\backend\descriptor.rs:43-132` 的 descriptor 里加一个 `native_delegation_disabled: bool` 字段，启动前若为 false 则拒绝把该后端放进团队模式（复用 `crates\aionui-team\src\capability.rs:8-18` 已有的 team-capable 判定）。这至少让「无法关闭原生委派的后端」不能被静默接入团队。

6. **`spawn.rs` 加 PATH/环境收敛（补 G9）**
   `crates\aionui-runtime\src\spawn.rs:97-130`（`Builder::new` / `clean_cli`）目前只剥 4 个 Node 变量（`:18-20` 注释）。可在团队会话上启用更严的 env 白名单（`is_blocked_override_env_key`（`registry.rs:1378-1387`）已经给出了一份可复用的名单模式）。注意这只是「减少便利」，**不是 OS 隔离** —— 真要堵住需要 Job Object / cgroup，而 `containment.rs:5-8` 明确说这两档「intentionally not built」。

---

## 未找到 / 未验证清单

以下条目**已 grep 并有意识地判定为「不存在」或「无法从本仓证实」**，列出以免被误读为「已实现」：

**未找到（不存在）**
1. 任何专用于关闭 claude `Task`/`Workflow` 的参数或环境变量。
2. 任何专用于关闭 codex `spawnAgent` / collab agent 的参数或环境变量。
3. `agent_metadata` / `BehaviorPolicy`（`crates\aionui-api-types\src\agent_discovery.rs:65-86`）中任何「禁用原生委派 / 禁用内置工具」字段。
4. 对 subagent 的数量、并发、嵌套深度、调用次数、运行时长的任何限制或计数器。
5. per-agent workspace、per-agent git worktree。
6. OS 级隔离（seccomp / namespace / cgroup / Job Object / AppContainer）。
7. 权限请求超时与自动拒绝逻辑。
8. `iFlow` 后端在 AionCore catalog 中的任何行（`crates\aionui-db\migrations\*.sql` 全量 grep 无 `iflow`）。注意：`vendor\AionUi\AionUi-main\docs\prds\conversations\acp\permissions.md:93` 声称 iFlow 支持免确认模式 —— 与源码 catalog 不一致，**以源码为准：未找到**。
9. AionUi 桌面端自身 spawn 任何 CLI Agent 的代码（它只 spawn `aioncore` 与自带 MCP/browser server：`packages\desktop\src\process\startup\singleInstanceGating.ts:11`、`packages\desktop\src\process\resources\builtinMcp\browserServer.ts:150-156`）。所有 CLI Agent 启动都在 AionCore。

**未验证（本仓无法证实，不得当作事实）**
1. **`@agentclientprotocol/claude-agent-acp`、`@zed-industries/codex-acp` 及各 vendor 的 ACP server 是否会为「创建子 Agent」发起 `session/request_permission`。** 本仓只有「问了就会浮到审批层」的证据（`acp_conn.rs:1556-1607`），没有任何桥源码、探针数据或 fixture 证明它们会问。按 `vendor\AionCore\AionCore-main\AGENTS.md` 的「NEVER guess an agent CLI's behavior」规则，此项标注未验证。
2. claude 直连的 `can_use_tool` 是否覆盖 `Task` 工具。fixture `crates\aionui-session\tests\fixtures\claude_2.1.169_single_tool_turn.ndjson:3` 只证明 claude 2.1.169 的 `init.tools` 里**存在** `Task`（`"tools":["Task","AskUserQuestion","Bash",…]"`），**不能**证明调用它会触发 permission 提问。
3. `claude_2.1.176_workflow_multiagent_3parallel_1fail.ndjson` 与 `claude_2.1.220_background_bash_turn.ndjsonl` 显示 claude 确实会跑多 agent Workflow，但**没有捕获到任何针对 Task 的 permission 帧**。
4. 各 CLI 是否有「官方支持的禁用子 Agent flag」。本仓未验证对应 flag 是否存在 —— 第 1 条最小改动点必须先跑 `--help` 确认。
5. agy 是否具备「子 Agent」能力以及其子任务是否走 `PreToolUse`（`matcher:"*"` 理论上覆盖所有工具，见 `aionui_hook.rs:101-120`），但**未验证** agy 的内部委派是否经过 hook。

---

## 附：本报告用到的核心证据索引

| 主题 | 位置 |
|---|---|
| ACP 反向 RPC 唯一入口（权限拦截点） | `crates\aionui-session\src\backend\acp_conn.rs:1544-1621` |
| ACP 审批自动放行白名单（仅 team MCP） | `crates\aionui-ai-agent\src\manager\acp\permission_router.rs:15,66-77,180-200` |
| host→CLI 的 `session/set_mode` 写入 | `crates\aionui-session\src\backend\acp_conn.rs:2321-2341` |
| mode 别名归一 | `crates\aionui-ai-agent\src\manager\acp\mode_normalize.rs:6-41` |
| claude argv 拼装（fail-closed 权限模式） | `crates\aionui-session\src\backend\claude_conn.rs:154-269` |
| claude 直连 `can_use_tool` → Permission（无工具名过滤） | `crates\aionui-session\src\adapter\claude.rs:930-1020,1101-1105` |
| codex argv / thread-start 策略 | `crates\aionui-session\src\backend\codex_conn.rs:47-72,551-562,2357-2360` |
| codex collab agent → SubagentUpdate（仅观测） | `crates\aionui-session\src\backend\codex_conn.rs:6287-6329` |
| claude Task/Workflow → SubagentUpdate（仅观测） | `crates\aionui-session\src\backend\claude_conn.rs:1791-1797` |
| subagent roster reducer 读臂 | `crates\aionui-session\src\reducer.rs:455-492,1942-1944` |
| team 工具角色强制的唯一门 | `crates\aionui-team\src\mcp\server.rs:523,1018-1020` |
| team 工具权限描述符 | `crates\aionui-api-types\src\team_tools.rs:40-47,277-283,373,505-555` |
| governance 提示词（非强制） | `crates\aionui-team-prompts\src\governance.rs:1-24` |
| 「别用平台内置工具」提示词 | `crates\aionui-team-prompts\src\team_tool_usage.rs:18-21` |
| 团队强制 full-auto | `crates\aionui-team\src\service\spawn_support.rs:68-79`；`crates\aionui-common\src\enums.rs:127-136`；`crates\aionui-team\src\provisioning.rs:632-640` |
| agy 钩子装卸（团队=卸） | `crates\aionui-ai-agent\src\factory\antigravity.rs:64-99`；`crates\aionui-ai-agent\src\antigravity_hook.rs:69-120` |
| 团队共享 workspace | `crates\aionui-team\src\workspace.rs:50-77`；`crates\aionui-conversation\src\service.rs:452-457` |
| 无 OS 隔离（自述） | `crates\aionui-process\src\containment.rs:1-8,74-83` |
| 团队会话被挡在 `conversation create` 外 | `crates\aionui-conversation\src\runtime_create.rs:188-193` |
| runtime token 作用域 | `crates\aionui-ai-agent\src\runtime_token.rs:11`；`crates\aionui-ai-agent\src\task_manager.rs:142-144` |
| CLI 服务端参数来自 catalog | `crates\aionui-db\migrations\001_initial_schema.sql:186-230`；`025_sync_and_add_acp_registry_agents.sql:5-23` |
| PRD：团队模式无超时 / 免确认模式 | `vendor\AionUi\AionUi-main\docs\prds\conversations\acp\permissions.md:10,33,41,70-104` |
