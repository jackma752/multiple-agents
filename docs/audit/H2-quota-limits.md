# 硬条件 2 审计：数量限制能否覆盖整棵任务树

审计对象（只读，未编译、未运行）：
- AionCore 后端：`E:\ds_agent\workplace\vendor\AionCore\AionCore-main`
- AionUi 桌面端：`E:\ds_agent\workplace\vendor\AionUi\AionUi-main`

审计时间：2026-10-13。所有结论均给出 `文件路径:行号`，并标注 **代码已实现 / 仅文档或提示词 / 未找到**。

---

## 结论

**未实现。** AionCore 后端不存在任何针对"Agent 数量"的配额：既没有**子 Agent 总量**上限，也没有**整棵任务树累计**上限，也没有**并发运行成员数**上限，也没有**嵌套深度**上限；所有落库/落内存的成员创建路径（`TeamSessionService::create_team` / `add_agent`、`TeamSession::spawn_agent`、`provision_initial_agents`）都只做"非空 + 恰好一个 lead + 名字不重复"的形式校验，**没有任何计数比较**。

唯一的"结构性刹车"是角色权限：`team_spawn_agent` 是 **LeadOnly** 工具（`crates/aionui-api-types/src/team_tools.rs:504-505`、`crates/aionui-team/src/mcp/server.rs:1018-1020`、`crates/aionui-team/src/session.rs:1851-1853`），因此**通过团队 MCP 通道**建出的树天然是"1 个 lead + N 个平铺 teammate"的两层结构——但这**不是配额机制**（没有计数、没有 depth 字段、没有可配置项），而且宽度 N 完全不受限。

系统里存在的各类"limit / max / timeout"常量**全部与 Agent 数量无关**：它们是列表分页上限、收件箱截断、批处理投递重试、单次 wake 的 60s 看门狗、提示词行数截断、HTTP/会话消息限流、以及一个**无人写入**的 `max_turns` 字段（详见下表）。

状态：**未实现**。

---

## 限制总表

按题目要求逐项穷举。方向说明：**作用域**列指该限制被计算/比较时的边界。

| 限制名 | 文件:行 | 数值/默认值 | 作用域 | 用户可配置 | 后端强制 |
|---|---|---|---|---|---|
| **（Agent 数量类）** | | | | | |
| `max_members` | **未找到**（全仓 grep，含 `aionui-team` / `aionui-api-types` / `aionui-db` / AionUi 前端） | — | — | — | — |
| `member_limit` | **未找到** | — | — | — | — |
| `max_agents` / `max_teammates` / `max_subagents` / `task_team_size` 等同类命名 | **未找到**（`crates` 全量 grep：`max_members\|member_limit\|max_teammates\|teammate_limit\|max_team_size\|max_subagents\|max_agents\|agent_quota\|team_quota\|spawn_limit\|max_spawns\|total_agents\|agent_budget` → 仅命中 `aionui-process/src/proc_tree.rs:31` 的 `MAX_DEPTH`，属 OS 进程树遍历，见下） | — | — | — | — |
| `team_size` | **未找到** | — | — | — | — |
| "至少一个 agent"（唯一的最小值校验，非上限） | `crates/aionui-team/src/service.rs:763-765` | `>= 1` | 单团队创建 | 否 | 是（创建前 `InvalidRequest`） |
| "恰好一个 lead" | `crates/aionui-team/src/provisioning.rs:250-259` | `== 1` | 单团队创建 | 否 | 是（`let [leader_idx] = leaders.as_slice()`） |
| `team_spawn_agent` 仅 Lead 可用（**结构性深度刹车，非配额**） | `crates/aionui-api-types/src/team_tools.rs:504-505`；`crates/aionui-team/src/mcp/server.rs:1018-1020`；`crates/aionui-team/src/session.rs:1851-1853` | 角色门 | 单团队 | 否 | 是（MCP dispatch + session 双重校验） |
| 成员名唯一（按 normalize 后比较） | `crates/aionui-team/src/session.rs:1866-1869` | — | 单团队内存 roster | 否 | 是 |
| **（并发 / 运行时类）** | | | | | |
| `max_concurrent` / `parallel` / `semaphore` 形式的成员并发上限 | **未找到**（`crates/aionui-team/src` grep `max_concurren\|max_parallel\|semaphore\|parallelism` → 0 命中；`tokio::sync::Semaphore` 仅出现在 `crates/aionui-team/tests/session_service_integration.rs:1510` 的测试脚手架里） | — | — | — | — |
| 每个 slot 同时只允许 1 个 active batch | `crates/aionui-team/src/work_coordinator/coordinator.rs:578`（`slot.active = Some(ActiveBatch{..})`）、`crates/aionui-team/src/work_coordinator/model.rs:240` | 1 batch/slot | 单成员 | 否 | 是（`is_current_batch` 见 `coordinator.rs:1586-1597`） |
| **slot 总数**（每个 slot 一个常驻 `tokio::task`，无上限） | `crates/aionui-team/src/event_loop.rs:72-94`（`for agent in agents { spawn }` 语义，`handles.insert(slot_id, handle)`） | 无上限 | 单团队 | 否 | **无限制** |
| `WAKE_TIMEOUT_MS`（单次 wake 无流活动看门狗） | `crates/aionui-team/src/scheduler/mod.rs:33`；实现 `crates/aionui-team/src/scheduler/wake.rs:40-76` | `60_000 ms`（每次收到非终止事件即 reset，`wake.rs:64-68`） | 单次 wake | 否 | 是（触发 `handle_inactivity_timeout`） |
| `handle_inactivity_timeout` 后果 | `crates/aionui-team/src/scheduler/crash_recovery.rs:68-103` | 置 `Error` + 释放 wake lock + 给 lead 写一条 "timed out after 60s" | 单成员 | 否 | 是（不杀进程，不释放任何"名额"——因为没有名额概念） |
| `ACTIVE_TURN_SLOW_THRESHOLD_MS`（仅 UI 标记，不中断） | `crates/aionui-team/src/team_run/manager.rs:20`，消费处 `:294-295` | `10 * 60 * 1000 ms` | 单 slot 单轮 | 否 | 仅提示（`active_turn_slow: bool`） |
| `FINALIZE_DEDUP_WINDOW`（防重复 finalize，非限制） | `crates/aionui-team/src/scheduler/mod.rs:35` | `5 s` | 单 slot | 否 | 是 |
| `MAX_MESSAGE_DELIVERY_FAILURES`（批投递重试上限 → slot 被 pause） | `crates/aionui-team/src/work_coordinator/model.rs:13`；比较处 `crates/aionui-team/src/work_coordinator/coordinator.rs:1538` | `3` | 单 mailbox 消息 × 单 slot | 否 | 是（结果：**暂停该 slot**，`model.rs:15-25` 注释） |
| `is_rate_limited` 正则（429/quota 归类，非配额） | `crates/aionui-team/src/crash_detection.rs:11-25` | 正则 `(?i)429\|rate.?limit\|quota\|too many requests` | 单条 stream error | 否 | 是（归类为 `Failed`，不走 crash 恢复，见 `docs/internals.md:116`） |
| MCP 帧大小上限 | `crates/aionui-team/src/mcp/protocol.rs:86-93` | `10 * 1024 * 1024` 字节 | 单帧 | 否 | 是（`InvalidData`） |
| MCP 请求超时 | **未找到**（`crates/aionui-team/src/mcp` grep `timeout\|deadline\|elapsed` → 仅 `server.rs:1831` 的 payload 字段） | — | — | — | — |
| **（列表 / 提示词截断类，与 Agent 数量无关）** | | | | | |
| `MAX_INBOX_MESSAGES` | `crates/aionui-team/src/mcp/server.rs:605`，应用处 `:688` | `50` | 单次 `team_read_messages` | 否 | 是（截断并保持未读） |
| `MAX_INBOX_CONTENT_CHARS` | `crates/aionui-team/src/mcp/server.rs:606`，应用处 `:743-750` | `4_000` | 单条消息预览 | 否 | 是 |
| `MAX_TASK_LIST_LIMIT` | `crates/aionui-team/src/mcp/server.rs:954`，clamp 处 `:981-989` | `200` | 单次 `team_task_list` | 参数可选，上限硬编码 | 是（`.min(MAX_TASK_LIST_LIMIT)`） |
| `DEFAULT_ACTIVITY_LIMIT` / `MAX_ACTIVITY_LIMIT` | `crates/aionui-team/src/service.rs:67-69`，clamp 处 `:542`、`:560`、`:615` | `500` / `1000` | 单次 REST 读 | 参数可选，上限硬编码 | 是（`.clamp(1, MAX_ACTIVITY_LIMIT)`） |
| `MAX_TASK_ID_LOOKUP` | `crates/aionui-team/src/service.rs:72` | `200` | 单次活动查询 | 否 | 是 |
| `MAX_WAKE_TASK_ROWS` / `MAX_RECENT_COMPLETED_ROWS` / `MAX_TEAMMATE_RECENT_COMPLETED_ROWS` / `MAX_SUBJECT_CHARS` / `MAX_BLOCKED_BY_IDS` | `crates/aionui-team/src/prompts/wake_summary.rs:6-10` | `40` / `8` / `3` / `120` / `5` | 单次 wake 提示词内容 | 否 | 是（仅影响提示词，不影响任何 Agent 的创建或运行） |
| `WAKE_TIMEOUT` 文案 "60s of inactivity" | `crates/aionui-team/src/scheduler/crash_recovery.rs:88-91` | 字符串常量 | — | 否 | 仅文案 |
| **（LLM 回合类）** | | | | | |
| `max_turns` | 定义 `crates/aionui-api-types/src/agent_build_extra.rs:114`；读取 `crates/aionui-conversation/src/session_context.rs:451`；透传 `crates/aionui-ai-agent/src/factory/aionrs.rs:192`、`crates/aionui-ai-agent/src/manager/aionrs/agent.rs:186` | `Option<usize>`，默认 `None` | **单个 aionrs 会话的一次运行** | 字段可从 conversation `extra` JSON 反序列化，但**未找到任何写入方**（UI、团队代码、REST 接口均无） | 仅当值非 `None` 时由 aionrs 后端生效；默认不生效 |
| `max_tool_call_malformed_turns` / `max_tool_call_failure_turns` | `crates/aionui-api-types/src/agent_build_extra.rs:116-118` | `Option<usize>`，默认 `None` | 同上 | 同上（无写入方） | 同上 |
| `max_iterations` / `token_limit` / `max_total_tokens` / `agent_budget` | **未找到**（全仓 grep；`token_limit` 仅命中 `crates/aionui-ai-agent/src/manager/acp/agent_session_flow.rs:695,711,714` 的 `token_limit_signal`，是 ACP 后端上报的**空回合原因标识**，不是我们可以设置的配额） | — | — | — | — |
| **（深度 / 嵌套类）** | | | | | |
| `depth` / `nest` / `recursion` / `parent_id` / `root_id` / `max_depth` / `nesting_depth` 等 | **未找到**（`crates/aionui-team` 整 crate grep `(?i)(depth\|nest\|recursion\|parent_id\|root_id\|root_task\|parent_team)` → **0 命中**；`crates` 全量 grep `max_depth\|team_depth\|max_nesting\|tree_limit` → 仅 `aionui-process/src/proc_tree.rs:31`） | — | — | — | — |
| `MAX_DEPTH`（**反例说明：与 Agent 嵌套无关**） | `crates/aionui-process/src/proc_tree.rs:31`，用途见 `:33-45` | `64` | OS 进程树遍历（`collect_descendants`）深度护栏 | 否 | 是（仅防止进程表异常导致 teardown 卡死） |
| **（HTTP / 消息层限流）** | | | | | |
| Auth 限流 | `crates/aionui-auth/src/rate_limit.rs:42-45` | 5 次失败 / 15 分钟 | 全局（按 key，即用户/IP） | 否 | 是（中间件） |
| API 限流 | `crates/aionui-auth/src/rate_limit.rs:47-50` | 60 请求 / 分钟 | 全局（按 key） | 否 | 是 |
| 已鉴权动作限流 | `crates/aionui-auth/src/rate_limit.rs:52-55` | 20 请求 / 分钟 | 全局（按 key） | 否 | 是 |
| 跨会话消息限流 `OUTBOUND_LIMIT` / `PAIR_LIMIT` | `crates/aionui-session-message/src/rate_limit.rs:25-29` | 10 分钟窗口内：单会话出站 `20`；单 `(from,to)` 对 `10` | **单个会话 / 单个会话对**（不跨团队累计） | 否 | 是（`check_and_record`，`rate_limit.rs:60-96`）——**但明确把团队会话排除在外**：团队成员作发送方会被 `SenderIsTeam` 拒绝（`service.rs:159-173`），作接收方会被 `TargetIsTeam` 拒绝（`service.rs:224-226`），即该限流器**根本不覆盖 `team_send_message` / 团队邮箱通道** |
| **（前端）** | | | | | |
| 团队成员数量限制（UI 层） | **未找到**。`E:\ds_agent\workplace\vendor\AionUi\AionUi-main\packages` 全量 grep `max_members\|maxMembers\|max_agents\|maxAgents\|memberLimit\|maxTeammates\|teamSize\|maxConcurrent\|concurrencyLimit\|maxSubAgents\|maxAgentsPerTeam` → 仅 `packages/desktop/src/process/utils/utils.ts:157,165,196,224` 的 `maxDepth`，属**文件搜索**参数 | — | — | — | — |
| 前端"团队已满"提示 / 禁用"添加成员" | **未找到**（`packages/desktop/src/renderer/pages/team` grep `limit\|max\|toast\|warn\|disabled` → 无任何与成员数相关的分支；`TeamAddMemberPopover.tsx` 无上限判断） | — | — | — | — |
| 前端每用户团队数限制 | **未找到**（`POST /api/teams` → `crates/aionui-team/src/service.rs:762-765` 只校验 `agents` 非空；`list_teams_by_user`（`crates/aionui-db/src/repository/sqlite_team.rs:54`）无 count 校验） | — | — | — | — |
| **（提示词）** | | | | | |
| 团队规模 / 成员数建议（"最多 N 个 teammate"之类） | **未找到**。`crates/aionui-team-prompts` 全量 grep `limit\|maximum\|at most\|no more than\|不超过\|上限\|最多` → 仅 1 命中：`crates/aionui-team-prompts/src/team_tool_usage.rs:57` 的 "retry at most once"（工具调用纠错重试，与 Agent 数量无关） | — | — | — | — |
| Lead 必须先提案并等待用户确认才 spawn | `crates/aionui-team-prompts/src/role_prompt.rs:48-56`、`:99-110`（第 10 条 "End your turn after the proposal. Do NOT call team_spawn_agent in that same turn"、第 11 条 "Wait for explicit confirmation"） | — | 单团队 | 否 | **仅提示词**（后端 `spawn_agent` 不检查任何审批状态，见下节） |
| "Lead-only tools are unavailable to teammates" | `crates/aionui-team-prompts/src/team_tool_usage.rs:38-39,71-72`、`crates/aionui-team-prompts/src/governance.rs:19` | — | — | 否 | 仅提示词（真正强制在 `mcp/tools.rs:29-37`） |
| 文档中提及的 `MAX_MCP_MESSAGE_SIZE=64MB` / 300s 请求超时 | `crates/aionui-team/docs/phase1/backend-audit.md:32`、`:486` 记为**参考实现（AionUi TS）有、AionCore 后端没有**（后端为 10MB、无请求超时）——与本次读到的 `mcp/protocol.rs:86-93` 一致 | — | — | — | 仅文档 |

---

## 作用域分析

### 事实 1：所有创建路径都以 `team_id` 为唯一边界，没有任何跨团队的累计字段

团队成员的创建只有 4 个入口，全部收敛到 `TeamAgentProvisioner::provision_new_agent`：

1. 建团队时的初始成员：`crates/aionui-team/src/provisioning.rs:235-377`（`provision_initial_agents`）
2. REST 手动加成员：`crates/aionui-team/src/provisioning.rs:379-422`（`add_agent`）
3. Lead 通过 MCP 派生：`crates/aionui-team/src/provisioning.rs:450-482`（`persist_spawned_agent`）
4. 落库包装：`crates/aionui-team/src/provisioning.rs:699-731`（`provision_new_agent`）

调用链：
- `TeamSessionService::create_team` → `provision_initial_agents`（`crates/aionui-team/src/service.rs:785-788`）
- `TeamSessionService::add_agent` → `provisioner().add_agent`（`crates/aionui-team/src/service.rs:974`）
- `TeamSession::spawn_agent` → `service.persist_spawned_agent`（`crates/aionui-team/src/session.rs:1900-1910`）

这 4 处**没有任何一处比较计数**。加入成员的实际动作是往一个 JSON 数组 push 后整列覆写：

```rust
// crates/aionui-team/src/provisioning.rs:419-420
team.agents.push(agent.clone());
self.persist_agents(&team.id, &team.agents).await?;
```
```rust
// crates/aionui-team/src/provisioning.rs:479-480
team.agents.push(agent.clone());
self.persist_agents(&req.team_id, &team.agents).await?;
```
```rust
// crates/aionui-team/src/provisioning.rs:914-932
async fn persist_agents(&self, team_id: &str, agents: &[TeamAgent]) -> Result<(), TeamError> {
    let agents_json = serde_json::to_string(agents)?;
    ...
    .update_team(&row.user_id, team_id, &UpdateTeamParams { agents: Some(agents_json), ..Default::default() })
```

内存侧同样无上限：`TeamSession::spawn_agent` 直接把新 agent 塞进调度器：

```rust
// crates/aionui-team/src/session.rs:1912-1916
let reservation = self.reserve_dynamic_member_attach(&new_agent);
// Step 5: attach to the in-memory scheduler so wake-from-lead finds
// the new slot immediately.
self.scheduler.add_agent(&new_agent).await;
```

`TeammateManager::add_agent` / `list_agents`（`crates/aionui-team/src/scheduler/mod.rs:172-175`）是对 `HashMap<String, AgentSlot>` 的插入与全量 `collect()`，无容量参数；`TeammateManager::new`（`scheduler/mod.rs:130-162`）按传入 roster 逐个 `slots.insert`，也不校验长度。

### 事实 2：不存在任何 `root`/`parent` 字段，因此"整棵任务树"这个概念在数据模型里不存在

- `TeamAgent` 结构体（`crates/aionui-team/src/types.rs:88-116`）字段为：`slot_id / name / role / conversation_id / backend / model / assistant_id / status / conversation_type / cli_path`——**没有 `parent_id`、没有 `depth`、没有 `root_id`、没有 `spawned_by`**。
- `crates/aionui-team` 整个 crate 对 `parent_id|root_id|depth|nest|recursion` 的 grep 结果为 **0 命中**。
- `WorkSource` 枚举（`crates/aionui-team/src/work_source.rs:5-27`）里唯一的 "Spawn" 痕迹是 `SpawnWelcome`，它描述的是"给新成员的欢迎消息"这一**工作来源**，不带任何调用者血缘字段。
- `team_tasks` 表（`crates/aionui-db/migrations/001_initial_schema.sql:524-538`）只有 `team_id / subject / description / status / owner / blocked_by / blocks / metadata`——**任务之间是同一团队内的依赖图，不是跨层的父子树**，且 `blocked_by` 是 JSON 文本而非可 join 的列。

结论：即使想加"整树累计"校验，**当前 schema 里也没有可供 WHERE 的字段**（详见「数据库结构」）。

### 事实 3：反例分析——"Leader 建 3 个，每个再建 3 个，总数是多少？"

**通过团队 MCP 通道：不会变成 9，而是变成 1 + 3 = 4（或 1 + 3 + N）。**

理由是 `team_spawn_agent` 是 LeadOnly：

```rust
// crates/aionui-api-types/src/team_tools.rs:503-505
TeamToolSpec {
    name: TeamToolName::TeamSpawnAgent,
    permission: TeamToolPermission::LeadOnly,
```
```rust
// crates/aionui-team/src/mcp/server.rs:1014-1020
// Lead-only at the MCP dispatch layer. `TeamSession::spawn_agent` also
// re-checks via `TeamError::LeaderOnly` ...
if caller_role != TeammateRole::Lead {
    return Err(ToolCallError::from_message("Only Lead can spawn agents"));
}
```
```rust
// crates/aionui-team/src/session.rs:1850-1853
let caller = self.scheduler.get_agent(caller_slot_id).await?;
if caller.role != TeammateRole::Lead {
    return Err(TeamError::LeaderOnly("spawn_agent".into()));
}
```

且新成员的角色被**硬编码**为 `Teammate`，不存在"生成第二个 lead"的路径：

```rust
// crates/aionui-team/src/provisioning.rs:462-469（persist_spawned_agent）
.role: TeammateRole::Teammate,
```
`TeamSession::spawn_agent` 的 `SpawnAgentInput`（`crates/aionui-team/src/mcp/tools.rs:75-82`）用 `#[serde(deny_unknown_fields)]` 明确拒绝 `role` / `model` / `agent_type` 参数（测试证据：`mcp/tools.rs:225-245`、`mcp/server.rs:1021-1035`）。

所以 3 个 teammate 各调 `team_spawn_agent` 会得到 `Only Lead can spawn agents` —— **子 Agent 无法再派生孙 Agent**。

**但这不是数量限制，宽度完全不受约束**：Lead 可以在同一回合内串行调用 `team_spawn_agent` 任意多次，`persist_spawned_agent` 每次都成功 push 一个新成员。没有计数、没有预算、没有速率闸门——**而且团队通道被明确排除在系统唯一的消息限流之外**：`aionui-session-message` 的限流器会拒绝团队会话作为发送方（`crates/aionui-session-message/src/service.rs:159-173`，`SessionMessageError::SenderIsTeam`，注释原文 "A teammate's agent holds a ConversationHelper token too, so without this check it could use this CLI to reach ordinary conversations, bypassing the team mailbox"）与接收方（`service.rs:224-226`，`TargetIsTeam`）。也就是说 `team_send_message` / 团队邮箱通道**完全不经过任何速率闸门**，`team_spawn_agent` 就更不会。因此现实的上限是：**模型上下文 / 时间 / 用户忍耐**，而不是任何代码约束。

**两条需要注意的旁路（均为代码事实，可利用性未验证）：**

1. **Lead 身份来自客户端自报的 `slot_id`，而整个团队共享同一个 `auth_token`。** MCP 握手时 `auth_token` 与 `slot_id` 都由调用方在 `initialize` 参数里提供（`crates/aionui-team/src/mcp/server.rs:269-299`），角色再用这个自报的 `slot_id` 回查调度器（`mcp/server.rs:342-347`、`:429-431`，查不到时**默认降级为 Teammate**）。也就是说：只要一个 teammate 进程拿到同团队的 `auth_token`（它自己启动时就被注入了这个 token，见 `mcp/server.rs:36` 的 `auth_token` 字段）并声明 lead 的 `slot_id`，MCP 层就会把它当 Lead 放行。我没有实际执行该路径（**未验证**），但"共享 token + 自报身份"这个组合是读到的代码事实。若成立，"子 Agent 不能再派生"这一结构性刹车即可被绕过，成员数变成真正无界。**注意：即便绕过，树依然是平铺的（新成员仍被硬编码为 `Teammate`），只是宽度不再受"lead 必须是唯一调用者"这一事实约束。**

2. **计数链完全看不到 CLI 原生的子 Agent。** 每个 teammate 是一个独立的 CLI 进程（claude / codex / …），成员计数只发生在 `team_spawn_agent` 这一条通道上。CLI 自身若具备背景委派/子 Agent 能力，它派生出的进程**既不落 `teams.agents`、也不落 `scheduler.slots`、也不经任何 `MAX_*` 常量**。这属于硬条件 3 的范畴，此处仅记录它对本条件的影响：所谓"全树总量"即便在团队通道上做出来，也仍覆盖不到这部分。

### 事实 4：作用域判定结论

| 维度 | 判定 | 证据 |
|---|---|---|
| 单团队 vs 单会话 | 团队成员的 roster 存在 `teams.agents`（一团队一份），调度器 `TeammateManager` 也是**一个 team 一个实例**（持有 `team_id`，`scheduler/mod.rs:115-127`） | `migrations/001_initial_schema.sql:493-505`、`scheduler/mod.rs:116` |
| 跨团队累计 | **不存在**。没有任何按 `user_id` 统计团队/成员总数的代码路径 | `service.rs:762-765`（只校验非空）、`service.rs:840-841`（只 list） |
| **整棵根任务树累计** | **不存在**。没有 root/parent 概念，也没有任何跨团队 or 跨层级计数器 | `types.rs:88-116`、crate 级 grep 0 命中 |
| 后端强制 vs 仅提示 | 上面表中的所有 `MAX_*` / timeout 都是**后端强制**（clamp / truncate / 校验 / 中断）；**唯独"是否允许 spawn"的审批环节是仅提示词**（`role_prompt.rs:48-56`），后端 `spawn_agent` 不检查任何用户同意状态 | 见「配置与自扩权」 |

---

## 深度与嵌套

**结论：不存在任何显式的深度限制机制；"孙 Agent"被阻止是角色权限的副作用，不是深度检查。**

题目指定要查的 4 个文件，逐一给出结论：

### 1. `crates/aionui-team/src/provisioning.rs` —— 创建成员的真正落点，**不检查深度**

- `provision_initial_agents`（`:235-377`）：只解析 roles、要求恰好一个 lead（`:250-259`），其余全部按 `Teammate` 建（`:319-346`）。没有传入任何"父节点"参数——函数签名里就没有深度/父子概念：
  ```rust
  // crates/aionui-team/src/provisioning.rs:235-241
  pub(crate) async fn provision_initial_agents(
      &self, user_id: &str, team_id: &str,
      inputs: &[TeamAgentInput], shared_workspace: Option<&str>,
  ) -> Result<InitialProvisioningResult, TeamError> {
  ```
- `add_agent`（`:379-422`）：只校验 `role != Teammate → 报错`（`:388-392`），注释明确写着 "add_agent only supports teammate role"，这是**角色白名单**而非深度检查。
- `persist_spawned_agent`（`:450-482`）：`role: TeammateRole::Teammate` 硬编码（`:469`），不读调用者深度。
- `provision_new_agent`（`:699-731`）：纯粹的"建会话 + 组装 TeamAgent"，无校验。

### 2. `crates/aionui-team/src/member_runtime.rs` —— **不创建成员**，只管理运行期状态机

`MemberRuntimeRegistry` 的全部公开方法（`:144-533`）是 `reserve_attach / reserve_restart / reserve_repair / commit_ready / commit_failed / begin_remove / finish_remove / snapshot / stop`。它维护的是每个 `agent_id` 的 attach/remove 租约与 operation_id，**没有任何 capacity / max / len 阈值**（该文件 grep `MAX|limit|capacity|len()` → 0 命中）。所以它既不管数量，也不管深度。

### 3. `crates/aionui-team/src/team_run/manager.rs` —— **不创建成员**，是"运行状态记录器"

全文 354 行，只维护**单个** `TeamRunRecord`（`state: Mutex<Option<TeamRunRecord>>`，`:49`），用于广播 `team_run.accepted/started/completed/failed/cancelled` 事件。文件中唯一的常量是 `ACTIVE_TURN_SLOW_THRESHOLD_MS`（`:20`，仅产出 `active_turn_slow: bool`，`:294-295`），**没有任何 max/spawn/depth 逻辑**。

### 4. `crates/aionui-team/src/work_coordinator/coordinator.rs` —— **不创建成员**，是"每 slot 工作批调度器"

它决定"哪个 slot 的哪批消息该跑了"（`next()` → `ReconcileDecision::Claim(batch)`），其中 `slot.active = Some(ActiveBatch{..})`（`:578`）保证**单个 slot 同时只有一个 active batch**。但它：
- 不创建 agent（crate 内 `spawn_agent` 只在 `session.rs:1846` 与 `mcp/server.rs:531` 出现，coordinator 里没有）；
- 不做跨 slot 的并发上限（`slots: HashMap` 里有多少 slot 就能有多少个并发 batch）；
- 也没有 `depth`。

唯一与"重试/放弃"相关的常量是 `MAX_MESSAGE_DELIVERY_FAILURES = 3`（`work_coordinator/model.rs:13`），语义是"一条消息投递失败 3 次 → 该 slot 被 pause"（`coordinator.rs:1530-1545`），**不删除成员、不减少成员数**。

### 综合

- **显式深度限制**：未找到。
- **实际可达到的深度**：通过团队 MCP 通道为 **2 层**（lead → teammate），因为 `team_spawn_agent` 是 LeadOnly 且新成员角色硬编码为 Teammate。这是**角色模型的副作用**，不是深度配额；代码里没有任何地方读/写"当前深度"。
- **每条创建路径是否检查深度**：`provisioning.rs` 的三个入口、`session.rs::spawn_agent`、`service.rs::create_team` / `add_agent` —— **全部不检查**（因为它们连深度这个概念都没有）。
- **`docs` 中是否有深度约定**：未找到。`crates/aionui-team/docs/` 全量 grep `数量|上限|最多|limit|配额|quota|并发` 的 54 条命中全部是列表分页、429 限流、并发锁（`addAgentLocks`）、MCP 帧大小，无一条涉及 Agent 嵌套深度。

---

## 配置与自扩权

### 这些限制从哪里来？

三类来源，逐条对应：

**(a) 编译期硬编码常量（绝大多数）**
- `scheduler/mod.rs:33,35`、`mcp/server.rs:605,606,954`、`service.rs:67,69,72`、`prompts/wake_summary.rs:6-10`、`work_coordinator/model.rs:13`、`team_run/manager.rs:20`、`mcp/protocol.rs:88`、`crash_detection.rs:14`、`aionui-auth/src/rate_limit.rs:42-55`、`aionui-session-message/src/rate_limit.rs:25-29`。
- **无任何 API 可改，无环境变量开关**（未找到对这些常量的读取式覆盖）。

**(b) 每个 conversation 的 `extra` JSON（唯一"数据驱动"的一类）**
- `AionrsBuildExtra` 从 conversation 的 `extra` 反序列化：`crates/aionui-conversation/src/session_context.rs:451`。
- 字段 `max_turns` / `max_tool_call_malformed_turns` / `max_tool_call_failure_turns`（`crates/aionui-api-types/src/agent_build_extra.rs:114-118`）——**但这三个字段没有任何写入方**：仓库内对 `AionrsBuildExtra` 的构造只有 `AionrsBuildExtra::default()` 与测试（`crates/aionui-ai-agent/tests/factory_provider_integration.rs:145,175,196`、`crates/aionui-ai-agent/src/factory/aionrs.rs:1889,1919,1957,2009,2069`），团队侧的 `build_team_extra`（`provisioning.rs:750-762` 调用）不写这些键。因此**当前是死字段**：除非二次开发主动写入 conversation.extra，否则恒为 `None`。
- 作用域：单 conversation（即单个成员、单次运行），**不跨树累计**。

**(c) 数据库设置**
- `system_settings` 表（`crates/aionui-db/migrations/001_initial_schema.sql:26-34`）只有 `language / notification_enabled / cron_notification_enabled / command_queue_enabled / save_upload_to_workspace`——**没有任何 Agent 数量相关列**。
- `client_preferences`（`:36-40`）是 `key/value` 文本表，但代码中**未找到**任何写入 team/agent 配额 key 的位置。
- 结论：**配额不可由用户配置——因为不存在配额。**

### API 可改性 / 谁能改

| 接口 | 认证 | 是否可改变数量上限 | 证据 |
|---|---|---|---|
| `POST /api/teams` | 用户 JWT（`Extension<CurrentUser>`） | 否（只校验 `agents` 非空、恰好一个 lead） | `routes.rs:181`、`service.rs:762-765`、`provisioning.rs:242-259` |
| `POST /api/teams/:id/agents` | 用户 JWT | 否（只校验 `role == teammate`） | `routes.rs:415-424`、`provisioning.rs:386-392` |
| `team_spawn_agent`（MCP） | **团队共享 `auth_token` + 客户端自报 `slot_id`** | 否（只校验是 Lead、名字不重、有 `assistant_id`） | `mcp/server.rs:269-299,1010-1045`、`session.rs:1846-1876` |
| 任何把 `max_members` 之类写进设置的接口 | **未找到** | — | — |

### Agent 能不能给自己提高上限？

**不能——因为没有上限可提高。** 逐条排除：
- 没有任何"配额"字段暴露给 MCP 工具：13 个工具的描述符见 `crates/aionui-api-types/src/team_tools.rs:319-567`（`team_members / team_read_messages / team_send_message / team_interrupt_agent / team_task_create / team_task_update / team_task_list / team_list_assistants / team_describe_assistant / team_spawn_agent / team_rename_agent / team_clear_agent_context / team_shutdown_agent`），**没有任何一个能写配置**。
- `SpawnAgentInput` 用 `#[serde(deny_unknown_fields)]`（`mcp/tools.rs:75-77`）显式拒绝 `role` / `model` / `agent_type`（测试：`mcp/tools.rs:225-245`），Lead 也不能通过 spawn 参数扩权。
- 团队内**无法**通过 MCP 新建团队（工具列表里没有 create_team；`TeamSessionService::create_team` 只在 REST 路由 `routes.rs:181` 暴露给用户 JWT）。
- **唯一可被利用的扩权面**是上面「作用域分析 · 旁路 1」所述的**身份冒充**：`auth_token` 是全团队共享的（`mcp/server.rs:36,79,140`），`slot_id` 由客户端自报（`mcp/server.rs:294-299`），角色由该自报 id 反查（`mcp/server.rs:342-347,429-431`）。teammate 若声明 lead 的 slot_id，即可获得 Lead 的 `team_spawn_agent` 权限。**这不是"提高上限"，而是"取得原本不给自己开放的工具"**——效果上等价于把成员宽度从"仅 lead 可控"放开为"任意成员可控"。可利用性**未验证**（未构造实际连接测试）。

### 关于"审批"（与硬条件 1 的交叉点，此处仅记录对数量的影响）

后端 `TeamSession::spawn_agent` 的注释明确列出了它校验的 4 件事（`session.rs:1834-1845`："1. Caller must exist and carry Lead; 2. name 不冲突; 3. assistant_id 必须存在"），**没有第 4 条关于"用户是否已同意"**。用户同意完全靠提示词（`crates/aionui-team-prompts/src/role_prompt.rs:52,54`："Do NOT call team_spawn_agent in that same turn"、"Wait for explicit confirmation before using team_spawn_agent"）。**属"仅提示词"**。对数量限制的影响：没有任何"审批通过的配额"这类对象存在，所以也不存在"审批一次解锁 N 个额度"的机制。

---

## 数据库结构

### `teams`（`crates/aionui-db/migrations/001_initial_schema.sql:493-505`）

```sql
CREATE TABLE IF NOT EXISTS teams (
    id             TEXT PRIMARY KEY NOT NULL,
    user_id        TEXT    NOT NULL DEFAULT 'system_default_user',
    name           TEXT    NOT NULL,
    workspace      TEXT    NOT NULL DEFAULT '',
    workspace_mode TEXT    NOT NULL DEFAULT 'shared',
    agents         TEXT    NOT NULL DEFAULT '[]',     -- ← 成员 roster，JSON 数组，无长度约束
    lead_agent_id  TEXT,
    session_mode   TEXT,
    agents_version TEXT    NOT NULL DEFAULT '1.0.0',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
```
后续迁移对该表只做过：`002_legacy_data_normalize.sql:272-301`（重建，列集合不变）、`028_project_bind.sql:88-89`（`ADD COLUMN project_id / folder_id`）、`040_sidebar_ordering_and_archive.sql:37`（`ADD COLUMN archived_at`）。**没有任何 limit / quota / max / parent / root 列。**

- **能承担配额的列**：**无。** 最接近的是 `agents`（JSON 文本），可以在应用层 `json_array_length(agents)` 上做校验，但**当前没有任何 SQL 或 Rust 代码这样用**（`sqlite_team.rs` 对 `agents` 的操作只有 `bind(&row.agents)`（`:34`）、`set_clauses.push("agents = ?")`（`:92`）与 `query.bind(agents)`（`:125`），全是整列读写）。
- 且 `agents` 的更新是读-改-写全量覆写（`service.rs:959-974` 靠 `add_agent_locks` 串行化），并发下用 SQL 计数做配额需要额外事务保证。

### `mailbox`（`001_initial_schema.sql:509-522`）

`id / team_id / to_agent_id / from_agent_id / type / content / summary / files / read / created_at`。**无父/根/配额列。** 索引 `idx_mailbox_team_to_read(team_id,to_agent_id,read)`（`:521`）。

### `team_tasks`（`001_initial_schema.sql:524-538`）

`id / team_id / subject / description / status / owner / blocked_by / blocks / metadata / created_at / updated_at`。

- `blocked_by` / `blocks` 是 **JSON 文本**（`:532-533`，默认 `'[]'`），不是可 join 的外键。任务之间只有同一团队内的依赖关系，**没有父任务列**。
- **能承担配额的列**：只有 `team_id`（可做 `SELECT COUNT(*) FROM team_tasks WHERE team_id = ?` 的**单团队**任务数配额）。**没有任何列能表达"某个根任务及其全部后代"**——因为不存在 root/parent。

### 其它相关表

- `system_settings`（`:26-34`）：**无配额列**。
- `client_preferences`（`:36-40`）：`key/value/updated_at` 通用 KV，理论上可塞配额 key，但**未找到任何读写方**。
- `teams.lead_agent_id`（`:500`）：唯一表示"结构角色"的列，**不表示层级**（只有一个 lead，且 `provisioning.rs:250-259` 强制全局唯一）。

### 结论

数据库层**完全不支持"整棵任务树"的配额**：既没有 `parent_id`/`root_id`/`depth` 可供递归 CTE，也没有任何计数列或约束。要在 DB 层做树级配额，必须先加列（见「最小改动点」）。

---

## 缺口清单

### A. 后端强制（代码已实现，但**与 Agent 数量无关**）

| 项 | 位置 | 强制方式 |
|---|---|---|
| `team_spawn_agent` 仅 Lead | `mcp/server.rs:1018-1020` + `session.rs:1851-1853` + `team_tools.rs:504-505` | 调用前校验并返回错误 |
| `team_interrupt_agent` / `team_shutdown_agent` / `team_rename_agent` / `team_clear_agent_context` 仅 Lead | `team_tools.rs:372-373,521-522,538-539,553-555`；dispatch `mcp/server.rs:886-887,1359-1360` | 同上 |
| 新成员角色硬编码 Teammate（拒绝生成第二个 lead） | `provisioning.rs:462-469`；`provisioning.rs:388-392` | 入库前校验/硬编码 |
| 恰好一个 lead | `provisioning.rs:250-259` | 创建前校验 |
| 至少一个 agent | `service.rs:763-765` | 创建前校验 |
| 成员名唯一 | `session.rs:1866-1869` | 创建前校验 |
| 不能移除 lead | `service.rs:1035-1037` | 操作前校验 |
| `Slot` 单批并发 | `coordinator.rs:578,1586-1597` | 状态机保证 |
| 单 mailbox 消息投递失败 3 次 → pause slot | `model.rs:13`、`coordinator.rs:1538` | 计数比较 |
| 单次 wake 60s 无流 → 置 Error + 通知 lead | `scheduler/mod.rs:33`、`wake.rs:40-76`、`crash_recovery.rs:68-103` | 看门狗 |
| 收件箱/任务列表/活动列表截断与 clamp | `mcp/server.rs:605-606,954,987`、`service.rs:542,560,615` | 截断 |
| 跨会话消息对限流（20/会话/10min、10/对/10min） | `aionui-session-message/src/rate_limit.rs:25-29,76-87` | 计数比较（但作用域是**会话对**，且**明确排除团队会话**：`service.rs:159-173,224-226`，不累计到团队/树） |
| HTTP 限流 | `aionui-auth/src/rate_limit.rs:42-55` | 中间件 |
| MCP 帧 10MB | `mcp/protocol.rs:88` | 帧解析拒绝 |

### B. 仅前端限制

**无。** `packages/desktop` 全量 grep 未命中任何团队成员数上限、无"团队已满"分支、无禁用"添加成员"的逻辑（`TeamAddMemberPopover.tsx`、`useTeamSession.ts:132` 直接调用 `ipcBridge.team.addAgent.invoke` 无前置判断）。

### C. 仅文档 / 提示词

| 项 | 位置 | 性质 |
|---|---|---|
| "先提案、等用户确认再 `team_spawn_agent`" | `aionui-team-prompts/src/role_prompt.rs:48-56,99-110` | **仅提示词**。后端 `session.rs:1846-1876` 不检查审批状态 |
| "不要仅因为任务看起来大就 spawn" | `role_prompt.rs:99-100` | 仅提示词 |
| "Lead-only tools are unavailable to teammates" | `team_tool_usage.rs:38-39,71-72`、`governance.rs:19` | 仅提示词（真实强制在 `mcp/tools.rs:29-37`） |
| 团队规模建议（如"最多 N 人"） | **未找到** | — |
| MCP 64MB / 300s 请求超时（参考实现有、后端无） | `docs/phase1/backend-audit.md:32,486` | 仅文档（与 `mcp/protocol.rs:88` 实际代码一致：后端 10MB、无超时） |
| `docs/internals.md:116` 429/rate limit 表项 | 描述 `is_rate_limited` 分类器 | 与 `crash_detection.rs:11-25` 一致 |

### D. 无（本应存在但完全缺失）

1. **子 Agent 总量上限**——未找到。无任何"当前成员数 >= N"比较（`crates/aionui-team/src` 全量 grep `(count\(\)|\.len\(\))\s*(>=|>)\s*[A-Z0-9_]` → 仅 1 命中，是 `wake_summary.rs:315` 的 `blocked_by.len() > MAX_BLOCKED_BY_IDS`，截断提示词列表）。
2. **整棵根任务树（含所有后代/所有团队）累计上限**——未找到，且**数据模型不具备**（无 root/parent/depth 字段）。
3. **并发运行成员数上限**——未找到。每个 slot 一个 `tokio::task`（`event_loop.rs:72-94`），无全局 semaphore、无 `max_concurrent`。
4. **嵌套深度上限**——未找到（显式 depth 检查 0 处）；实际深度 2 是 LeadOnly 角色门的副作用。
5. **每位用户的团队数 / 总成员数上限**——未找到（`service.rs:762-765` 只校验非空）。
6. **任何用户可配置的配额界面或设置项**——未找到（`system_settings` 无相关列，前端无相关 UI）。
7. **审计/统计"某次根任务的累计 spawn 次数"的计数器**——未找到（`crates/aionui-team/src` grep `total_agents|agent_budget|spawn_limit|max_spawns` → 0 命中）。
8. **MCP 请求超时**——未找到（后端无，`mcp` 目录 grep `timeout|deadline` → 0 相关命中）。
9. **团队工具通道（`team_spawn_agent` / `team_send_message`）上的任何速率闸门**——未找到。系统唯一的消息限流器**主动排除**团队会话（`aionui-session-message/src/service.rs:159-173,224-226`），而团队 crate 内没有任何自己的 spawn/消息速率限制（`crates/aionui-team` grep `rate.?limit` → 仅 `crash_detection.rs:11-25` 的上游 429 归类器，那是对**外部 LLM 报错文本**的分类，不是我们自己的闸门）。

---

## 最小改动点

目标：作为二次开发基座，实现 **total（全树总量）/ concurrent（并发）/ depth（嵌套深度）** 三种后端强制配额。以下按"必改"优先级给出文件与函数名。

### 0. 前置：先建立"树"这一数据结构（否则 total/depth 无从计算）

**必改 A —— 给成员加血缘字段**
- 文件：`crates/aionui-team/src/types.rs`
- 目标：`pub struct TeamAgent`（`:88-116`）新增 `parent_slot_id: Option<String>` 与 `root_slot_id: Option<String>`（或 `depth: u8`）；`#[serde(default)]` 保证旧数据兼容。
- 连带：`to_response` / `to_response_with_icon`（`:118-137`）决定是否暴露给前端（可只内部用）。

**必改 B —— 落库**
- 文件：`crates/aionui-team/src/provisioning.rs`
- 函数：`persist_agents`（`:914-932`）→ 因为 roster 是整列 JSON，新字段随 `serde_json::to_string` 自动落进 `teams.agents`，**无需新迁移**。
- 若希望 SQL 可查（推荐，便于做原子配额）：新增迁移 `crates/aionui-db/migrations/045_team_agent_tree.sql`，把 `teams.agents` 拆出 `team_agents` 表（`slot_id PK, team_id, parent_slot_id, root_slot_id, depth, role, status`）+ 索引 `(root_slot_id)`、`(team_id)`；并在 `crates/aionui-db/src/repository/sqlite_team.rs` 增加计数方法（该文件已是 `ITeamRepository` 的实现处：`:47-125`）。

### 1. total / depth 配额：四个创建入口 + 一个共享门

**必改 C —— 唯一的强制点（这是最关键的一处）**
- 文件：`crates/aionui-team/src/provisioning.rs`
- 函数：`TeamAgentProvisioner::provision_new_agent`（`:699-731`）
- 做法：在 `create_team_conversation_for_agent` **之前**插入配额校验。因为 4 个创建入口**全部**经过它（调用方：`:277,333,403,463`），在这里做"最后一道门"可以覆盖所有路径，避免遗漏。需要把配额上下文作为新参数传入（`NewAgentProvisioning` 结构体目前在 `:402-418`、`:462-478` 两处构造，扩字段成本低）。
- 校验内容：
  - `depth = caller.depth + 1`，若 `depth > MAX_TEAM_DEPTH` → `TeamError::InvalidRequest`
  - `COUNT(*) WHERE root_slot_id = <caller.root_slot_id>`（或 `parent` 链递归）→ 若 `>= MAX_TREE_AGENTS` → 拒绝
  - 顶层创建（`create_team` 的初始 roster）时 `root_slot_id = leader_slot_id`、`depth = 0`

**必改 D —— 每个入口补充"调用者/根"的解析**
- `crates/aionui-team/src/service.rs::create_team`（`:762`）→ 给 lead 赋 `root_slot_id = Some(lead_slot_id)`、`depth = 0`；若同时支持一次建多人，需校验 `req.agents.len() <= MAX_TREE_AGENTS`（目前在 `:763-775` 只校验非空与 conversation_id）。
- `crates/aionui-team/src/service.rs::add_agent`（`:959-1018`）→ 在已有的 `add_agent_locks` 临界区内（`:965-970`）完成计数与插入，**保持"计数-插入"在同一把锁内**，否则并发 add 会突破配额。这是现成的正确同步点。
- `crates/aionui-team/src/session.rs::spawn_agent`（`:1846`）→ 在 Step 4 `persist_spawned_agent`（`:1900-1910`）之前校验，并传入 `parent_slot_id = caller_slot_id`、`root_slot_id = caller.root_slot_id`。
- `crates/aionui-team/src/provisioning.rs::provision_initial_agents`（`:235`）→ 初始化根与深度。

**必改 E —— 配额取数（原子性）**
- 若走 JSON roster：在 `crates/aionui-team/src/provisioning.rs` 内加 `async fn count_tree_members(&self, root_slot_id: &str) -> Result<usize, TeamError>`，遍历该团队 roster 的 `root_slot_id`。
- 若走新表：在 `crates/aionui-db/src/repository/sqlite_team.rs` 加 `count_agents_by_root(root_slot_id) -> i64` / `max_depth_by_root(root_slot_id)`，并在 `TeamAgentProvisioner` 内调用。**更好的做法是把计数与 INSERT 放进一个 SQLite 事务**（`sqlite_team.rs` 已有 `self.pool` 可用），以消除读-改-写窗口。

### 2. concurrent 配额：运行期并发门

**必改 F —— 并发许可的唯一收口**
- 文件：`crates/aionui-team/src/work_coordinator/coordinator.rs`
- 函数：`WorkCoordinator::next`（claim 处 `:508-600`，`slot.active = Some(...)` 在 `:578`）
- 做法：在返回 `ReconcileDecision::Claim(batch)` 之前，统计当前 `starting_batch_count + running_batch_count`（这两个数**已经算好了**，见 `work_coordinator/snapshot.rs:61-103` 与 `model.rs:251-252`），若 `>= MAX_TREE_CONCURRENT` 则返回"不 claim"（让 slot 保持 `Queued`），而不是新增计数逻辑。**这是最小改动的天然插点**，因为它已经是全局（跨 slot）的调度决策点。
- 释放语义天然正确：`complete_batch`（`:766`）/ `fail_batch`（`:851`）/ `cancel_batch`（`:860`）/ `interrupt_batch`（`:872`）都会把 slot 从 active 迁出，`terminalize_batch`（`:1500-1562`）统一收口，因此"失败或结束自动释放名额"**无需额外代码**。

**必改 G —— 进程级并发（可选，防御 CLI 子 Agent）**
- 文件：`crates/aionui-team/src/event_loop.rs::EventLoopRegistry::spawn`（`:72-94`）
- 做法：在此处加 `tokio::sync::Semaphore`（或对 `self.handles.len()` 与上限比较），因为每个 slot 一个常驻 task，这里是**所有成员运行时的唯一注册点**。
- 注意：这一步只能约束"通过团队通道注册的成员"，**约束不到 CLI 原生子 Agent 进程**（见「作用域分析 · 旁路 2」）——那需要在 `aionui-ai-agent` 的 spawn 构建器（`crates/aionui-ai-agent/src/factory/`）或 `aionui-process` 层加进程预算，属硬条件 3 的范畴。

### 3. 配额值从哪来（可配置）

**必改 H —— 配置载体**
- 硬编码最小实现：`crates/aionui-team/src/service.rs:66-72` 旁边（已有 `DEFAULT_ACTIVITY_LIMIT` / `MAX_ACTIVITY_LIMIT` / `MAX_TASK_ID_LOOKUP` 的常量区）加 `pub const MAX_TREE_AGENTS / MAX_TREE_CONCURRENT / MAX_TREE_DEPTH`。
- 要用户可配置：加迁移给 `system_settings` 增列（`crates/aionui-db/migrations/`，当前该表定义在 `001_initial_schema.sql:26-34`），并在 `TeamSessionService` 构造时读入（构造点 `crates/aionui-team/src/service.rs:193-207`，装配在 `crates/aionui-app` 的 `build_*_state()`）。
- **关键**：一定要在 `TeamSessionService` 内部持有配额并在**服务端**校验，绝不能只把值下发给前端/提示词——现有代码已经证明"提示词约束"（`role_prompt.rs:48-56`）在缺失后端校验时形同虚设。

### 4. 顺带必须修的一致性缺口（否则配额形同虚设）

**必改 I —— MCP 调用者身份不可自报**
- 文件：`crates/aionui-team/src/mcp/server.rs`
- 函数：`handle_initialize`（`:269-316`）与 `caller_role_for_tools_list`（`:342-347`）、`handle_tools_call` 的 role 解析（`:429-431`）
- 问题：`slot_id` 由客户端在 `initialize` 参数里自报（`:294-299`），`auth_token` 全团队共享（`:36`），角色用自报 id 反查。要做**后端强制**的 total/depth 配额，必须先让 `slot_id` 由服务端可信地绑定到连接（例如给每个 slot 签发独立 token，或在 stdio/http 传输层用服务端已知的 slot 而非请求参数）。
- 否则配额会被"声明成别人的 slot_id"绕过——这一点与「作用域分析 · 旁路 1」是同一处修复。

### 改动顺序建议

1. `types.rs`（加血缘字段）→ 2. `provisioning.rs::provision_new_agent`（统一校验门）+ 4 个入口传参 → 3. `service.rs::add_agent` 锁内计数 → 4. `coordinator.rs::next`（并发门）→ 5. `service.rs` 常量/配置 → 6. `mcp/server.rs::handle_initialize`（可信身份）。
其中第 2 步与第 6 步是**不可省略**的；只做第 4 步会得到"并发受限但总量无界"，只做第 2 步会在身份可伪造时被绕过。

---

## 未找到 / 未验证清单

### 明确"未找到"（grep 无命中，且已读相关处理器确认）

1. 任何名为或语义等价于 `max_members` / `member_limit` / `max_agents` / `max_teammates` / `team_size` / `max_subagents` / `max_team_size` / `agent_quota` / `team_quota` / `spawn_limit` / `max_spawns` / `total_agents` / `agent_budget` 的常量、字段或配置项（`crates` 全量 grep）。
2. 任何 `depth` / `nest` / `recursion` / `parent_id` / `root_id` / `parent_team` / `root_task` 标识符（`crates/aionui-team` 全量 grep **0 命中**）。
3. 任何 `max_concurrent` / `max_parallel` / `parallelism` / 成员级 `Semaphore` / `throttle`（团队运行期）。
4. 任何 `max_iterations` / `token_limit` / `max_total_tokens`（作为可配置配额）。
5. 任何 `heartbeat`（团队 crate 内 0 命中；活性判断用的是 `WAKE_TIMEOUT_MS` 看门狗 + stream 事件，不是心跳）。
6. 团队成员数的前端限制、"团队已满"提示、Toast、禁用按钮。
7. `system_settings` / `client_preferences` 中与团队/Agent 数量相关的 key。
8. `teams` / `mailbox` / `team_tasks` 表中任何可承担配额的列（`limit` / `max` / `quota` / `parent_id` / `root_id` / `depth`）。
9. 团队 crate 内对 `.count()` / `.len()` 与上限常量做 `>=` / `>` 比较的位置（仅 1 处，且是提示词列表截断 `wake_summary.rs:315`）。
10. `crates/aionui-team-prompts` 中任何关于团队成员数量的数字约束（grep `limit|maximum|at most|no more than|不超过|上限|最多` → 仅 `team_tool_usage.rs:57` 的 "retry at most once"）。
11. `crates/aionui-team/docs/` 中任何关于 Agent 数量/深度配额的说明。
12. 后端 MCP 请求超时（`mcp` 目录 grep `timeout|deadline` → 0 相关命中）。

### 未验证（有线索但未执行/未确证，**不得当作已实现**）

1. **`max_turns` 是否存在隐藏写入方**：仓库内对 `AionrsBuildExtra` 的构造点只找到 `Default` 与测试（`crates/aionui-ai-agent/src/factory/aionrs.rs:1889,1919,1957,2009,2069`、`crates/aionui-ai-agent/tests/factory_provider_integration.rs:145,175,196`），且 `crates/aionui-team` 侧的 `build_team_extra` 不写该键。但前端/客户端理论上可以直接往 conversation 的 `extra` JSON 里塞 `max_turns`（因为 `session_context.rs:451` 是全量反序列化）——**未追踪前端是否有此类写入**，故标注为未验证。即便存在，作用域也仅是**单个 aionrs 会话**，不构成树级配额。
2. **Lead 身份冒充能否实际得逞**（`mcp/server.rs:269-299` 自报 `slot_id` + 共享 `auth_token`）：机制在代码里成立，但**未构造实际 MCP 连接验证**。
3. **CLI 原生子 Agent 是否真的不被计数**：这一点从团队侧代码（成员只由 `team_spawn_agent` 与 REST add 产生）可推断，但**我未审计 `aionui-ai-agent` / 各 CLI 适配器的子 Agent 能力**（属硬条件 3），故不对"实际进程数上限"下结论。
4. **`MAX_MCP_MESSAGE_SIZE=64MB` 与 300s 请求超时是否真在后端缺失**：我读到后端为 10MB / 无超时（`mcp/protocol.rs:88`，`mcp` 目录无 timeout），与 `docs/phase1/backend-audit.md:32,486` 的自述一致；**未审计客户端侧（AionUi 参考实现 TS）**，因其源码不在本机检出中（`AionUi-main` 里 grep 不到 `TeammateManager`）。
5. **`teams.agents` JSON 在极端规模下的行为**：未见任何 `json_array_length` 校验，也未见对 roster 大小的上限；未做压测，仅记录"存在无界增长这一结构"。
6. **`add_agent_locks` 是否覆盖 spawn_agent 路径**：`add_agent_locks` 只在 `TeamSessionService::add_agent`（`service.rs:965-970`）与 `remove_agent`（`:1021-1027`）使用；`TeamSession::spawn_agent` 走 `persist_spawned_agent`（`provisioning.rs:450-482`），**读-改-写没有共享该锁**。两路径并发时是否丢成员，本次**未验证**（`docs/phase1/backend-audit.md:470` 记录过历史同类缺陷，`docs/phase1/README.md:121` 的验收项是"并发 add_agent 10 次 → 长度 10"）。这对后续加"锁内计数"是**必须确认的前置**：如果 spawn_agent 与 add_agent 不共享同一把锁，仅在 add_agent 内加配额会被 spawn_agent 绕过。
