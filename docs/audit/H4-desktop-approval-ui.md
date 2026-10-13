# H4 — AionUi 桌面端「审批 / 权限 / 团队」界面能力审计

审计对象：本机已下载源码，只读、未运行、未编译。

**路径约定**（下表所有相对路径的基准）：

- UI 侧：`E:\ds_agent\workplace\vendor\AionUi\AionUi-main\`（下称 **UI/**）
- 后端侧：`E:\ds_agent\workplace\vendor\AionCore\AionCore-main\`（下称 **CORE/**）

---

## 结论

**桌面端的审批能力是「纯前端的敏感工具操作确认卡片 + 一个可被持久化的全自动模式开关」，只能约束文件写/命令执行这类工具调用，对「把任务派给另一个 Agent」没有任何拦截点——团队委派的"先征求我同意"只写在 MCP 工具描述和系统提示词里，代码层无强制（仅提示词）；同时桌面端不存在全局免确认开关，但助手级「默认权限模式」与团队级 `session_mode` 都能把 yolo 固化成"以后每次新建的都是免确认"，且审批答复本身只是一次 loopback 无鉴权 HTTP POST，不构成安全边界。**

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| 敏感工具操作的审批卡片（ACP + legacy） | **已实现** | UI/`packages/desktop/src/renderer/pages/conversation/Messages/acp/MessageAcpPermission.tsx:21`；`.../Messages/components/MessagePermission/index.tsx:22` |
| 「始终允许」记忆 | **已实现（后端）／前端类为死代码** | UI/`.../common/adapter/ipcBridge.ts:515-518`；`.../common/chat/approval/ApprovalStore.ts:37`（无调用点） |
| 团队模式下的集中确认（徽标/计数/列表） | **已实现** | UI/`.../pages/team/hooks/useTeamPendingPermissions.ts:14`；`.../pages/team/components/TeamTabs.tsx:165` |
| 团队任务板 / mailbox 界面 | **已实现（只读）** | UI/`.../pages/team/TeamPage.tsx:831-835`；`.../pages/team/activity/ActivityBoardLayout.tsx:92-98` |
| 「派活给另一个 Agent 前需用户确认」 | **未实现**（仅提示词） | CORE/`crates/aionui-api-types/src/team_tools.rs:6-24`；CORE/`crates/aionui-team/src/prompts/mod.rs:261` |
| 免确认模式 / YOLO 开关（会话级、引导页、助手级默认、团队级） | **已实现** | UI/`.../platforms/acp/AcpSendBox.tsx:863`；`.../guid/components/GuidActionRow.tsx:298-317`；`.../settings/AssistantSettings/editor/DefaultsSection.tsx:174-221`；`.../pages/team/hooks/TeamPermissionContext.tsx:41-49` |
| 全局免确认设置（应用级） | **未实现**（i18n 键残留、零引用） | UI/`.../locales/en-US/settings.json:136-137`；PRD 声明见 `docs/prds/conversations/acp/permissions.md:141` |
| 子 Agent 数量 / 并发 / 深度上限 | **未实现** | 见「数量限制界面」章节（桌面端与团队后端均未找到） |
| 审批答由前端签名 / 有凭证校验 | **未实现** | UI/`.../common/adapter/httpBridge.ts:202-204,226-241`；CORE/`crates/aionui-app/src/router/routes.rs:428-435` |

---

## 审批相关界面与通道清单

### 一、约束「敏感工具操作」的审批界面（这是卡片真正管的东西）

| 名称 | 文件:行 | 约束对象 | 是否需用户点击 |
| --- | --- | --- | --- |
| `MessageAcpPermission`（ACP 权限卡） | UI/`packages/desktop/src/renderer/pages/conversation/Messages/acp/MessageAcpPermission.tsx:21`，提交逻辑 `:44-54` | ACP `session/request_permission` 对应的**单个工具调用**（`tool_call.tool_call_id`、`raw_input.command`） | **是**（每个选项一次点击即提交，无二次确认） |
| `MessagePermission`（legacy `permission` 卡） | UI/`.../Messages/components/MessagePermission/index.tsx:22`，`:46-57` | 后端 `permission` 消息（`action`/`command_type`/`options`） | **是** |
| `PermissionRequestPanel`（共享卡片壳） | UI/`.../Messages/components/MessagePermission/PermissionRequestPanel.tsx:35`，单击提交 `:77-106`，选项渲染 `:141-153` | 展示层，无独立语义 | **是**（注释 `:72-76` 明确"每个选项单击即提交，没有单独的确认步骤"） |
| 权限意图/操作类型分类 | UI/`.../MessagePermission/permissionOptions.ts:20-39`（`proceed_once/proceed_always/cancel/deny` 等）、`:56-71`（`execute/edit/read/fetch/tool`） | 决定卡片显示的操作类别徽标 | 否（纯映射） |
| 「安全选项」选择器 | UI/`.../permissionOptions.ts:73-74` `getSafePermissionOptionId`（只挑 `allow-once`） | 供**其他 UI** 选取"最保守的允许项" | 否（工具函数） |
| `MessageQuestion`（`ask` 卡） | UI/`.../Messages/MessageQuestion.tsx:45`，通道说明注释 `:68` | **不是审批**：`AskUserQuestion` 走独立 `ask` 通道，不走 confirm 端点 | **是**（回答问题） |
| 待确认列表恢复 | UI/`.../Messages/usePendingConfirmationsRecovery.ts:76-113`（`confirmation.list` + `confirmation.remove` 订阅） | 会话打开时把后端仍处于 pending 的确认重建为卡片 | 否（自动） |
| 会话内待确认计数 | UI/`.../pages/conversation/runtime/conversationRuntimeViewStore.ts:161`；`.../GroupedHistory/hooks/useConversationListSync.ts:134,214` | 侧栏"等待确认"状态 | 否 |
| 桌宠悬浮确认窗（第二条审批 UI） | UI/`packages/desktop/src/renderer/pet/petConfirmRenderer.ts:34-85`（渲染）、`:90-101`（回传）、快捷键 `:106-157`（**Enter = 第一个选项**） | 同上的工具操作确认 | **是**（键盘 Enter/Y/A/N 即可答复） |
| 桌宠确认 preload | UI/`packages/desktop/src/preload/petConfirmPreload.ts:11-25` | 暴露 `onConfirmationAdd/Update/Remove` + `respond()` | — |
| 桌宠确认主进程转发 | UI/`packages/desktop/src/process/pet/petConfirmManager.ts:328-369`（`:353-359` 转发到 `confirmation.confirm`） | 把桌宠窗的答复**直接签发**成后端确认 | **是**（但无需主窗口） |

### 二、审批相关的 IPC / HTTP 通道

| 通道 | 文件:行 | 作用 | 说明 |
| --- | --- | --- | --- |
| `conversation.confirmation.confirm` | UI/`packages/desktop/src/common/adapter/ipcBridge.ts:513-519` → `POST /api/conversations/{id}/confirmations/{call_id}/confirm`，body `{msg_id, data, always_allow}` | **签发审批决定** | 唯一的"同意"出口 |
| `conversation.confirmMessage` | UI/`.../common/adapter/ipcBridge.ts:401`（`IConfirmMessageParams`，`confirm_key`） | ACP 卡走这条（`MessageAcpPermission.tsx:46`） | 同上语义 |
| `conversation.confirmation.list` | UI/`.../common/adapter/ipcBridge.ts:520-522` | 拉取待确认列表 | 只读 |
| `conversation.confirmation.remove`（WS） | UI/`.../common/adapter/ipcBridge.ts:523` | 后端确认被处理后广播移除 | 只读订阅 |
| `conversation.approval.check` | UI/`.../common/adapter/ipcBridge.ts:525-530` → `GET /api/conversations/{id}/approvals/check?action=&command_type=` | 查询"该操作是否已被始终允许" | **定义存在，但 desktop 包内未找到任何调用点**（grep `approval.check` 仅命中该定义） |
| `team.listMailbox` / `listTasks` / `listActivity` | UI/`.../common/adapter/ipcBridge.ts:2314-2320` | 只读拉取 mailbox、任务板、活动流 | 只读 |
| `team.sendMessage` / `sendMessageToAgent` / `interruptAgent` | UI/`.../common/adapter/ipcBridge.ts:2339-2353`；调用点 UI/`.../pages/team/components/TeamChatView.tsx:160,165,200` | 用户 → Leader/成员 发消息、打断 | 用户输入本身即意图，无审批环节 |
| `channel.approvePairing` / `rejectPairing` | UI/`.../common/adapter/ipcBridge.ts:2185-2192`；调用点如 `.../settings/SettingsModal/contents/channels/TelegramConfigForm.tsx:260` | **IM 渠道设备配对审批**（Telegram/Slack/Discord/企微/钉钉/飞书/微信） | 与 Agent 工具审批无关，属另一套 |
| `remoteAgent.handshake` (`pending_approval`) | UI/`.../common/adapter/ipcBridge.ts:1328` | **远端 Agent 网关设备审批**（OpenClaw Gateway 侧批准） | 同上，非工具审批 |
| `extensions.getPermissions` | UI/`.../common/adapter/ipcBridge.ts:2098` | 扩展权限摘要（只读展示） | 只读 |
| `BaseApprovalStore`（"始终允许"内存缓存类） | UI/`packages/desktop/src/common/chat/approval/ApprovalStore.ts:37`，`allApproved/approveAll` `:71-81` | 设计上用于会话级 `always allow` 记忆 | **死代码**：全仓 grep `BaseApprovalStore|approveAll|allApproved` 仅命中自身定义与 `index.ts:7` 的 re-export，**无任何使用点**；实际记忆由后端承担（CORE/`crates/aionui-ai-agent/src/session_agent.rs:822-832`，`always_allow` 为 true 时强制 AllowAlways） |
| 后端确认路由与业务 | CORE/`crates/aionui-conversation/src/routes.rs:141`、`crates/aionui-conversation/src/service.rs:3443` | 服务端确认处理 | — |
| 团队无超时（仅文档） | UI/`docs/prds/conversations/acp/permissions.md:33`「团队模式下：权限请求无超时限制」；该文档 `:8-10` 自标 F-PERM-01「部分实现」（缺 30 分钟超时自动拒绝） | — | **仅文档**，未找到实现代码 |

### 三、明确回答：卡片约束的是什么

**只约束敏感工具操作，不包含"把任务派给另一个 Agent"。** 证据链：

1. ACP 卡片的数据源是 `session/request_permission` 的工具调用（UI/`.../Messages/acp/MessageAcpPermission.tsx:23,60-70` 读的是 `tool_call.title / raw_input.command`），提交时把 `call_id = tool_call.tool_call_id`（`:26,50`）回给后端——即**卡片与某个具体工具调用一一对应**。
2. 团队委派走的是**另一条链路**：Agent（Leader）通过 MCP HTTP 调用 `team_spawn_agent` / `team_send_message` / `team_task_create`，入口在 CORE/`crates/aionui-team/src/mcp/server.rs:449-459`（`team_tool_call_from_name` → `:515-543` 分派）。
3. 该链路**唯一的强制检查是角色授权**：CORE/`crates/aionui-team/src/mcp/tools.rs:29-34` `authorize_tool(caller_role, tool_name)`，只判断 `LeadOnly` / `AnyTeamAgent`（枚举 CORE/`crates/aionui-api-types/src/team_tools.rs:38-49`）。
4. 「先征求用户同意」**只存在于工具描述文本**：CORE/`crates/aionui-api-types/src/team_tools.rs:6-24`（`TEAM_SPAWN_AGENT_DESCRIPTION` 要求"呈表格 → 询问 → 不要在同一轮调用"），以及系统提示词 CORE/`crates/aionui-team/src/prompts/mod.rs:261`（测试断言提示词含 `Wait for explicit confirmation before using team_spawn_agent`）；CORE/`crates/aionui-team/src/mcp/tools.rs:198-199` 也只是一个**断言描述文本里含 "explicitly approved" 的单元测试**。
   → 归类：**仅文档/提示词**，不是实现。绕过方式：Leader 直接调用 `team_spawn_agent` 不会被任何代码拒绝。
5. 团队全量工具清单（13 个）见 CORE/`crates/aionui-api-types/src/team_tools.rs:66-81`，其中与"派活"直接相关的 `team_spawn_agent`、`team_task_create`、`team_send_message`、`team_interrupt_agent`、`team_shutdown_agent` **均无审批前置**。

---

## 团队模式界面能力

**界面暴露了什么**

| 能力 | 文件:行 | 说明 |
| --- | --- | --- |
| 成员列表（Leader 头衔 + 成员列） | UI/`packages/desktop/src/renderer/pages/team/TeamPage.tsx:886-934`；`.../team/components/TeamAgentIdentity.tsx:87,124`（Leader 皇冠） | 并行多列，每列一个真实 Agent 会话 |
| 成员标签页 + 拖拽排序 | UI/`.../team/components/TeamTabs.tsx:118,262-264,340` | 仅 teammate 可排序，Leader 固定 |
| 新建团队（选成员、指定 1 名 Leader） | UI/`.../team/components/TeamCreateModal.tsx:114-127`（`role: leader/teammate`）、`:312`（提交校验） | 用户**直接创建**，这就是"用户批准"本身 |
| 会话中追加成员 / 「告诉 Leader」 | UI/`.../team/components/memberPicker/TeamAddMemberPopover.tsx:54-76`（直接 `addAssistant`）、`:90-105`（预填提示词给 Leader） | **分派入口只有"用户自己加"和"让 Leader 去加"，没有"先给我确认"** |
| 只读任务板 + mailbox | UI/`.../team/TeamPage.tsx:831-835`（注释 `:832`「看板视图：只读展现全队 mailbox 与 task-board」）；`.../team/activity/ActivityBoardLayout.tsx:92-98`；`.../team/activity/TaskCard.tsx:87-171` | `TaskCard` 仅有"展开/折叠描述"与"跳转到 blocker"（`:147-153`），**无任何分配/批准/驳回按钮** |
| 团队活动流 | UI/`.../team/activity/useTeamActivityFeed.ts:170`（订阅 `team.mailboxChanged`） | 只读 |
| 成员生命周期确认弹窗（**注意：这是团队里唯一存在的 Modal.confirm 类审批，与委派无关**） | UI/`.../team/TeamPage.tsx:248-253`（运行时重连确认）、`:266-271`（重置成员上下文确认）、`:983-989`（**移除团队成员**确认）；建队提交 `.../team/components/TeamCreateModal.tsx:315` | 约束的是"用户对自己可见的破坏性 UI 操作"（重连/重置/移除成员/建队），**不是** Agent 的委派行为 | **是** |
| 团队待确认徽标/计数 | UI/`.../team/TeamPage.tsx:757-758,798`；`.../team/hooks/useTeamPendingPermissions.ts:14,56,79,92`；`.../team/hooks/useSiderTeamBadges.ts:9`；`.../team/components/TeamTabs.tsx:29,165` | 显示"某成员有待批权限"的数量 —— 仍然是**工具操作审批**，不是委派审批 |
| 团队权限上下文（模式传播） | UI/`.../team/hooks/TeamPermissionContext.tsx:5-20`（`allConversationIds`、`propagateMode`）、`:41-49`、`:102-104` | 团队特有的权限机制：把 Leader 的模式写入团队记录 |
| 发送给 Leader / 指定成员 | UI/`.../team/components/TeamChatView.tsx:160,165,200` | 用户直接下达任务，无确认步骤 |

**有没有"派活前需我确认"的入口或开关：没有。** 已核对：

- `pages/team/**` 全部 48 个文件逐项核对；在该目录内 grep `confirm|approval|approve|delegat` 共 34 处命中，**全部落在三类无关语义上**：(a) 工具待确认计数（`useTeamPendingPermissions.ts:8,56,74-79,91`、`TeamTabs.tsx:29,226`、`useSiderTeamBadges.ts:9,16,26,61-66`、`TeamPage.tsx:757`）；(b) 模式传播上下文注释（`TeamPermissionContext.tsx:12`）；(c) **成员生命周期/破坏性操作的本地 `Modal.confirm`**（`TeamPage.tsx:248-253`、`:266-271`、`:983-989`，以及 `TeamCreateModal.tsx:315` 的建队提交按钮）。**没有任何一处是对"Agent 要把任务派给谁"发起的询问。**
- 桌面端全仓 grep `delegat|Delegate` 仅命中无关注释（语音流策略、WebviewHost 导航、终端卡说明、预览 tab 路径）；`approve` 仅命中 IM 渠道配对与远端 Agent 握手（见「审批相关 IPC / HTTP 通道」表）。
- 唯一能被解读为"用户先点确认"的团队交互是 `TeamCreateModal.tsx:315` 的**建队提交**——即"批准建队成员名单"，而非"批准每一次下派"。

---

## 免确认模式传染性分析

### 1. 模式在哪儿设置（5 个入口，全部找到）

| 层级 | 位置 | 文件:行 | 落到哪里 |
| --- | --- | --- | --- |
| **会话级**（ACP 会话） | 会话头部/发送框的权限胶囊下拉 | UI/`.../conversation/platforms/acp/AcpSendBox.tsx:863`（`onModeChanged`）→ `.../components/agent/AgentModeSelector.tsx:173-221` 的 `handleModeChange` → `runtimeConfig.setConfigOption(runtimeMode.id, mode)`（`:194`） | 通过 ACP `config_options` 的 `mode` 项写进该会话运行时 |
| **会话级**（aionrs 会话） | 发送框 + 移动端动作面板 | UI/`.../platforms/aionrs/AionrsSendBox.tsx:487-501`（`handleSheetModeChange`，`:491` setConfigOption，`:493` propagateMode）、`:515-536`（备用模式列表含 `{value:'yolo',label:'YOLO'}` `:529`）、`:816` | 同上；**且在团队内会额外触发全队传播（见下）** |
| **会话创建（引导页）** | 引导页权限选择器 | UI/`.../guid/components/GuidActionRow.tsx:298-317`（Permission 子菜单，桌面端 `:633` 注释）；生效点 UI/`.../guid/GuidPage.tsx:432-442` | 新会话初始模式；引导页选择会覆盖助手默认（`:435` 带 `persistPreference:false`） |
| **助手级默认**（≈"长期默认"） | 设置 → 助手 → 编辑器 → Defaults → Permission | UI/`packages/desktop/src/renderer/pages/settings/AssistantSettings/editor/DefaultsSection.tsx:174-221`（`<Select data-testid='select-assistant-default-permission'>`，`:191-192` 写入 `fixed`+`value`）；装配 UI/`.../AssistantSettings/index.tsx:137-142`；解析 UI/`.../guid/utils/assistantDefaults.ts:33-38`（`fixed` → 用固定值；`auto` → 用 `preferences.last_permission_value`） | 持久化到后端：CORE/`crates/aionui-db/src/repository/sqlite_assistant.rs:963-978`（列 **`last_permission_value`**）；读写服务 CORE/`crates/aionui-assistant/src/service.rs:1433-1472`；新建会话时套用 CORE/`crates/aionui-conversation/src/service.rs:1737,1830-1835,2099-2108` |
| **团队级** | 通过 `TeamPermissionContext.propagateMode` | UI/`.../team/hooks/TeamPermissionContext.tsx:41-49` → `ipcBridge.team.setSessionMode`（UI/`.../common/adapter/ipcBridge.ts:2309` → `POST /api/teams/{team_id}/session-mode`） | 注释 `:43`「Persist session_mode on the team record so newly spawned agents inherit it」——**写入团队记录，之后新生成的成员都继承** |

补充入口（自动、用户无感）：**定时任务**用助手对应的全自动模式名。UI/`.../pages/cron/ScheduledTasksPage/CreateTaskDialog.tsx:363-369`（`resolveAutoApproveModeFromAgentMetadata` 返回 `agent?.yolo_id || 'yolo'`）、使用点 `:554`；后端在任务执行时把 `mode` 写进 `extra.session_mode`：CORE/`crates/aionui-cron/src/executor.rs:1133,1233`、模式来源 CORE/`crates/aionui-cron/src/service.rs:2088-2101`。

### 2. 有没有全局免确认设置，作用范围多大

- **桌面端没有应用级全局开关。** 证据：i18n 里确实存在 `settings.yoloMode`（UI/`.../locales/en-US/settings.json:136`）与 `settings.claudeYoloMode`（`:137`），但**全仓 grep `yoloMode` 只命中 14 处 i18n JSON/类型声明，代码零引用**；设置相关 UI 内也未出现引用该 key 的组件。→ 归类：**未实现（遗留文案）**。
- PRD 声称存在「之前在设置中开启过全局免确认 -> 新建会话自动继承该设置」（UI/`docs/prds/conversations/acp/permissions.md:141`），但按 F-PERM-06 的验收项在代码里找不到对应开关；能被找到的等价机制是**助手级默认权限**（上表第 4 行），其作用是「该助手之后新建的每一个会话都从免确认开始」——即**作用范围 = 该助手（跨会话、跨项目、长期持久化）**。
- **团队级 `session_mode` 的作用范围 = 该团队全体成员 + 该团队之后新 spawn 的成员**（UI/`.../TeamPermissionContext.tsx:43`）。
- 另有两条"用户无感进入免确认"的路径（非 UI 开关，但扩大了作用范围）：
  - 定时任务复用会话时自动 yolo（上节）；
  - **IM 渠道会话被后端强制 yolo**：CORE/`crates/aionui-channel/src/message_service.rs:337-349`（`build_channel_extra` 硬写 `"session_mode": "yolo"`，注释：渠道用户没有交互式确认 UI）；CORE/`crates/aionui-channel/src/orchestrator.rs:233`。

### 3. Agent 能否自己开启它

分三条路径回答，前两条有代码依据，第三条标为未验证：

- **(a) 写设置文件** — **未找到** Agent 可写的设置文件承载该开关。权限默认值存于后端 SQLite 列 `last_permission_value`（CORE/`crates/aionui-db/src/repository/sqlite_assistant.rs:963`），会话模式存于会话/团队记录与 ACP 运行时；桌面端 `common/config/configKeys.ts` 一类 localStorage 配置项里未找到任何 yolo/权限模式键。
- **(b) 调用本地 HTTP 接口** — **机制存在，端到端未验证。** 代码事实：
  - Agent CLI 子进程的 spawn env 里被注入 **`AIONUI_BASE_URL`** 与 `AIONUI_RUNTIME_TOKEN`：CORE/`crates/aionui-ai-agent/src/types.rs:98-102`（常量名）、`:84-93`（写入 `runtime_env`）；注入到进程 env 的路径 CORE/`crates/aionui-ai-agent/src/factory/acp_launch_policy.rs:22-27`（`append_runtime_env`）、CORE/`crates/aionui-ai-agent/src/session_agent.rs:1841`（`session_config.spawn_env = assemble_spawn_env(...)`）、CORE/`crates/aionui-runtime/src/spawn.rs:142`（`inner.envs(resolved.env...)`）。token 的发放见 CORE/`crates/aionui-ai-agent/src/task_manager.rs:137-160`（scope：`ConversationHelper`；团队会话追加 `TeamContext`/`TeamCall`）。
  - 桌面端以 **local 模式**跑后端且**免鉴权**：CORE/`crates/aionui-app/src/config.rs:23`（`is_local()`）、CORE/`crates/aionui-app/src/router/routes.rs:428-435`（local 模式不加 CSRF 中间件）、`:456-468`（local 模式 `CorsLayer::allow_origin(Any)`）；测试 CORE/`crates/aionui-app/tests/local_mode.rs:7`（`test_local_mode_skips_auth`）。
  - 模式切换接口：会话级 `POST /api/conversations/{id}/config-options/{option_id}`（CORE/`crates/aionui-conversation/src/routes_aux.rs:23`）、团队级 `POST /api/teams/{id}/session-mode`（CORE/`crates/aionui-team/src/routes.rs:229`）。
  → 因此：**Agent 进程在自己的环境变量里就能拿到后端地址，且该地址上的接口在 local 模式无需任何凭证**。我在本次只读审计中没有实际发起请求，故标 **未验证**（机制成立、可利用性未实测）。`AIONUI_RUNTIME_TOKEN` 的 scope 未包含确认/模式接口（CORE/`crates/aionui-ai-agent/src/runtime_token.rs:11` 定义的 scope 仅 `ConversationHelper`/`TeamContext`/`TeamCall`，校验点仅 skill-runtime/session-message/runtime_routes 等少数路由），但在 local 模式下 token 并非必需。
- **(c) 自然语言让用户误开** — **代码层无任何防护，也未找到约束性提示词。** 已核对：CORE/`crates/aionui-api-types/src/team_tools.rs:6-24` 只对 `team_spawn_agent` 写了"先询问用户"的措辞；在 `team_tools.rs` 全量 13 个工具描述与 `crates/aionui-team/src/prompts/` 中**未找到任何针对"建议用户开启免确认/YOLO"的禁止性约束**（grep `yolo` 于 `prompts/` 无命中）。同时 UI 侧对"当前处于全自动"只有胶囊文字（UI/`.../components/agent/AgentModeSelector.tsx:278-296`）与 i18n 文案 `agentMode.yolo`，没有额外的二次确认弹窗——**用户点一下下拉即在当前会话生效；在团队内由 Leader 点一下即写进团队记录并传染给后续所有成员。**

### 4. 团队内模式传播的一个额外缺口（值得单独记录）

UI/`.../conversation/platforms/acp/AcpSendBox.tsx:208` 与 `:863` 都把传播**限定在 Leader**（`isLeaderInTeam`，定义 `:131`），但 **aionrs 路径没有这个限制**：UI/`.../platforms/aionrs/AionrsSendBox.tsx:145`、`:493`、`:816` 直接把 `teamPermission?.propagateMode` 接上——即**任一名 aionrs 成员的列里切换模式，都会写团队级 `session_mode`**。这是"模式传染"范围大于预期的一处实现差异（属于用户点击触发，不是 Agent 触发）。

---

## 数量限制界面

**结论：没有。** 桌面端**不存在**任何「子 Agent 数量上限」「并发上限」「嵌套深度上限」「调用次数上限」「运行时长上限」的用户可配置项；后端团队服务也未找到对应常量。

已核对的负向证据：

| 检查项 | 范围 | 结果 |
| --- | --- | --- |
| `maxSubAgent / maxAgents / max_agents / maxDepth / max_depth / concurren / parallel / quota / 子代理` | UI/`packages/desktop/src/renderer/pages/settings/**` | 仅 1 处命中：UI/`.../settings/ToolsSettings/McpManagement.tsx:104` 的 `{ concurrency: 4 }`——**MCP 连接测试**的并发参数，与 Agent 数量无关 |
| `subagent / sub_agent / SubAgent` | UI/`packages/desktop/src/**`（全部 ts/tsx） | 仅命中 i18n 键 `team.create.step.subAgents` / `team.create.subAgentsPlaceholder`（创建团队时的**成员选择步骤**标签），无上限逻辑 |
| 团队创建校验 | UI/`.../team/components/TeamCreateModal.tsx:81-94` | 只校验：团队名非空、至少 1 名成员、恰好 1 个 Leader；`:312` 禁用条件同这三条。**无人数上限** |
| 设置页是否存在 limits/quota 区块 | UI/`.../settings/**` | 未找到；grep `上限\|限制\|并发\|数量` 于 zh-CN locales 仅命中技能导入大小、预览标签页、token 上限等无关文案 |
| 后端团队常量 | CORE/`crates/aionui-team/src/**` grep `MAX_\|limit` | 仅命中无关常量：`mcp/server.rs:605` `MAX_INBOX_MESSAGES=50`、`:954` `MAX_TASK_LIST_LIMIT=200`、`service.rs:69` `MAX_ACTIVITY_LIMIT=1000`、`work_coordinator/model.rs:13` `MAX_MESSAGE_DELIVERY_FAILURES=3`、`prompts/wake_summary.rs:6-10` 摘要行数。**没有成员数 / spawn 次数 / 嵌套深度的上限常量** |

---

## IPC 与凭证边界

### 1. preload 暴露了什么

| 暴露面 | 文件:行 | 能力 | 宽泛程度 |
| --- | --- | --- | --- |
| `window.electronAPI.emit(name, data)` | UI/`packages/desktop/src/preload/main.ts:18-32` | **任意名字**的事件透传到主进程（`ipcRenderer.invoke(ADAPTER_BRIDGE_EVENT_KEY, JSON.stringify({name,data}))`） | **宽泛通道，无白名单** |
| 主进程侧处理 | UI/`packages/desktop/src/common/adapter/main.ts:93-96`（`ipcMain.handle` → `emitter.emit(name, data)`）；分发器 UI/`.../common/platform/bridge.ts:138-149`（`invoke` 用任意 `name` 组请求/回调名） | 无 name 校验、无权限分级 | 同上 |
| `window.electronAPI.on(cb)` | UI/`.../preload/main.ts:33-41` | 订阅桥事件 | 宽泛 |
| `getPathForFile` | UI/`.../preload/main.ts:43` | 拖拽文件绝对路径 | 中 |
| `collectFeedbackLogs` / `captureFeedbackScreenshot` | UI/`.../preload/main.ts:45-47` | 收集日志、截屏 | 中（敏感） |
| `recoverCorruptedDatabase` | UI/`.../preload/main.ts:51` | 触发数据库恢复 | 中（敏感） |
| `window.__backendPort` | UI/`.../preload/main.ts:56-60` | **把后端端口号直接交给渲染进程** | 关键：它同时告诉任何读到它的代码"审批接口在哪" |
| `__initialLanguage` / `__backendStartupBridge` | UI/`.../preload/main.ts:61-79` | 语言、启动状态 | 低 |
| 托盘事件 → DOM 事件 | UI/`.../preload/main.ts:83-95` | 5 个托盘导航事件 | 低 |
| 桌宠三套 preload | UI/`.../preload/petPreload.ts:9-18`、`petHitPreload.ts:9-18`、`petConfirmPreload.ts:10-31`（含 `respond()`） | 桌宠状态、拖拽、**审批答复** | `petConfirmPreload.respond` 是第二条审批出口 |

**是否任意 shell / 任意文件写 / `invoke` 透传？**

- **没有**任意 shell 执行通道（未找到 `child_process`/`shell` 型 preload 通道；`ipcBridge` 里的 shell 相关项只有 UI/`.../common/adapter/ipcBridge.ts:156-160` 的 `open-file / show-item-in-folder / open-external / check-tool-installed / open-folder-with`，都是"打开"语义，需走 `/api/shell/*`，具体执行在后端）。
- **有对整条桥的 `invoke` 透传**（`emit(name,data)` + `bridge.invoke(name)`），粒度为"整桥"，**未找到按通道的 allowlist**。
- **"任意文件写"更多来自 HTTP 而非 IPC**：UI/`.../common/adapter/ipcBridge.ts:874` 定义 `fs.writeFile` → `POST /api/fs/write`，`readFile` `:873`，`copyFilesToProject` `:897`。这些是渲染进程的**直接 fetch**，不经过 preload。

### 2. 关键结构性事实：渲染进程并不通过 IPC 访问后端

UI/`packages/desktop/src/common/adapter/httpBridge.ts:310-335` 显示 `httpGet/httpPost` 的 `provider()` 是空函数、`invoke()` 直接 `httpRequest(...)`；`getBaseUrl()` `:54-61` 返回 `http://127.0.0.1:${getBackendPort()}`，端口来自 `window.__backendPort`（`:37-43`）。而 `httpRequest` 只设置 `Content-Type` 与调用方显式传入的头，**从不注入 `Authorization`**（`:220-240`）；CSRF 解析器是恒返回空串的桩（`:187-204`），`sessionRefresh.ts:10` 明确"desktop app runs the backend in local mode (no tokens)"、`:66-70` 在非浏览器模式下直接返回 false。

→ 因此审批的"边界"**不是 IPC 沙箱**，而是 **127.0.0.1 上的一个 local 模式免鉴权 HTTP 端口**。

### 3. 对"Agent 能否绕过审批"的影响

- 渲染进程若被 XSS/恶意扩展攻陷，可以直接 `fetch('http://127.0.0.1:PORT/api/conversations/.../confirmations/.../confirm')` 替用户点"始终允许"——无需触碰 preload；preload 只是额外给了它端口号。**未找到**任何"审批答复必须由特定 webContents 发出"的校验（主进程 `petConfirmManager.ts:328-369` 与渲染进程走的是同一个 HTTP 端点，二者互为旁路，见「审批相关界面与通道清单」表）。
- Agent 进程侧：见「免确认模式传染性分析 §3(b)」——它自己的 env 里就有 `AIONUI_BASE_URL`；配合 local 免鉴权，同样具备调用审批/模式接口的条件。**端到端未验证。**

---

## 缺口清单

按对目标场景（"每次委派必须批准 + 全树总量限制"）的严重度排序：

1. **委派无审批拦截点（最严重）**：`team_spawn_agent` / `team_task_create` / `team_send_message` 在 CORE/`crates/aionui-team/src/mcp/server.rs:449-543` 只过角色授权（CORE/`.../mcp/tools.rs:29-34`），"先问用户"仅存在于 CORE/`crates/aionui-api-types/src/team_tools.rs:6-24` 与 CORE/`.../prompts/mod.rs:261` 的**文本**中。UI 侧没有任何对应的确认卡片类型（UI/`.../Messages/MessageList.tsx:293-298` 只处理 `permission` / `acp_permission` / `ask`）。
2. **无"委派审批"开关**：`pages/team/**` 全部 48 个文件内只有工具待确认计数与成员生命周期 `Modal.confirm`（重连/重置上下文/移除成员/建队），**没有**"每次派活需我确认"这类组件、开关或 IPC 通道。
3. **审批答复无凭证、无来源校验**：`confirmation.confirm` 是 loopback 明文 POST（UI/`.../ipcBridge.ts:513-519` + UI/`.../httpBridge.ts:202-204` + CORE/`.../router/routes.rs:428-435`）。
4. **"始终允许"的会话级记忆在前端是死代码**（UI/`.../common/chat/approval/ApprovalStore.ts:37` 无调用点），语义完全由后端 `always_allow` 决定；前端无法据"本会话已批准过什么"做二次判断。
5. **免确认可通过"助手默认权限"跨会话固化**（UI/`.../settings/AssistantSettings/editor/DefaultsSection.tsx:174-221` → DB 列 `last_permission_value`），且**没有确认弹窗、没有醒目告警**。
6. **团队模式传染范围超出 Leader**：aionrs 的 `propagateMode` 未做 Leader 判定（UI/`.../platforms/aionrs/AionrsSendBox.tsx:145,493,816`），任何成员列切一次模式就写团队记录并传染后续 spawn 的成员。
7. **无任何数量/并发/深度上限**（桌面端与团队后端均未找到，见上节）。
8. **团队确认无超时**：UI/`docs/prds/conversations/acp/permissions.md:33` 记载"团队模式下权限请求无超时"，PRD 自标 F-PERM-01 缺 30 分钟自动拒绝（`:8-10`）——**仅文档，未找到实现**。
9. **审批 UI 的第二出口（桌宠）无额外确认**：UI/`.../pet/petConfirmRenderer.ts:106-157` 里 `Enter` 直接选中**第一个选项**（通常是"允许"），`A` 直接"始终允许"。
10. **渠道会话强制 yolo**：CORE/`crates/aionui-channel/src/message_service.rs:337-349`、CORE/`.../orchestrator.rs:233`——经由 IM 发起的任务全部免确认。

---

## 最小改动点

> 前提：前端只能"呈现与开关"，**真正的强制必须在后端**（团队 MCP 工具入口）。以下给出前端的动刀位置。

### A. 加"每次委派审批"

1. **新增确认卡片类型**（复用现有卡片壳，成本最低）
   - 复用：UI/`packages/desktop/src/renderer/pages/conversation/Messages/components/MessagePermission/PermissionRequestPanel.tsx:35`（已支持任意 `options` + `onConfirm`）。
   - 新增：`pages/conversation/Messages/delegation/MessageDelegationApproval.tsx`（仿 `Messages/acp/MessageAcpPermission.tsx:21-96` 的 24 行结构）。
   - 挂载：UI/`packages/desktop/src/renderer/pages/conversation/Messages/MessageList.tsx:293-298` 增加 `case 'delegation_approval':`。
   - 消息类型：UI/`packages/desktop/src/common/chat/chatLib.ts:66`（消息类型联合）新增一个类型；待确认恢复逻辑 UI/`.../Messages/usePendingConfirmationsRecovery.ts:23-46` 增加分支（否则刷新页面后待批卡片会退化成普通权限卡）。
2. **团队看板里就地批准**
   - UI/`pages/team/activity/TaskCard.tsx`：在 `:138-169` 的底部操作行加入"批准 / 驳回"两个按钮（当前只有展开/跳转）。
   - 数据来源 UI/`pages/team/activity/useTeamActivityFeed.ts`（现订阅 `team.mailboxChanged`，需新增委派待批事件）。
3. **通道与类型**
   - UI/`packages/desktop/src/common/adapter/ipcBridge.ts:2339-2353` 附近新增 `team.approveDelegation` / `rejectDelegation`（或在 `:512-530` 的 `confirmation` 命名空间下扩展）。
   - 后端必须新增：MCP 工具调用前的阻塞等待 + 确认记录（改动点在 CORE/`crates/aionui-team/src/mcp/server.rs:515-543`、CORE/`crates/aionui-team/src/mcp/tools.rs:29-34`）。
4. **开关入口（"以后每次都要我批"）**
   - UI/`pages/team/components/TeamCreateModal.tsx:163-197`（`teamFields` 区块，现只有"团队名 + 工作空间"）加一个开关；或 UI/`pages/team/components/TeamViewToggle.tsx` 旁加常驻开关。
   - 持久化：仿 `TeamPermissionContext.tsx:41-49` 的 `setSessionMode` 模式，在 UI/`common/adapter/ipcBridge.ts:2309` 邻位新增 `team.setDelegationApproval`。

### B. 加"总量 / 并发 / 深度上限"

1. **设置界面**（当前完全没有）
   - 新建 UI/`packages/desktop/src/renderer/pages/settings/TeamLimitsSettings/`，表单结构照抄 UI/`.../settings/AssistantSettings/editor/DefaultsSection.tsx:140-221`（`ConfigRow` + `Select`/`InputNumber`）；注册到设置页 tab 列表（参考 UI/`.../settings/AssistantSettings/index.tsx` 的路由装配方式）。
   - 需要的最小字段：`max_total_subagents`、`max_concurrent`、`max_depth`、`max_calls_per_task`、`max_runtime_minutes`；作用域选择：团队 / 全局（对应 `TeamPermissionContext` 的 `team_id` 维度）。
2. **建队时的前置校验**
   - UI/`pages/team/components/TeamCreateModal.tsx:81-94`（现有三条校验）追加成员数上限校验；禁用条件同步 `:312`。
   - 追加成员：UI/`pages/team/components/memberPicker/TeamAddMemberPopover.tsx:54-76` 在 `addAssistant` 前查上限。
3. **Agent 侧提示（仅辅助，不能替代强制）**
   - 团队提示词模板：CORE/`crates/aionui-team/src/prompts/mod.rs`（现有 `:261` 的措辞断言处）加入"到达上限即停止派生"。
   - **强制仍必须落在后端**（CORE/`crates/aionui-team/src/service/spawn_support.rs` 与 MCP 工具入口），前端计数不可信。

### C. 顺手要收紧的两处

1. 桌面端移除或改造 `window.__backendPort` 的直接暴露（UI/`.../preload/main.ts:56-60`），改为经主进程代理的按端点白名单通道——否则渲染进程永远能直连审批接口（UI/`.../common/adapter/httpBridge.ts:54-61`）。
2. 让 aionrs 的团队模式传播与 ACP 对齐（UI/`.../platforms/aionrs/AionrsSendBox.tsx:145,816` 补 `isLeaderInTeam` 判定，参照 UI/`.../platforms/acp/AcpSendBox.tsx:131,208`）。

---

## 未找到 / 未验证清单

**未找到（明确不存在，已做负向 grep 与逐文件核对）**

| 项 | 核对方式 |
| --- | --- |
| 桌面端全局（应用级）免确认/YOLO 开关 | 全仓 grep `yoloMode` 仅命中 14 处 i18n JSON + `i18n-keys.d.ts`，代码零引用 |
| 任何"派活/委派前需我确认"的 UI 入口、开关或 IPC 通道 | `pages/team/**` 48 个文件全量列举 + 该目录内 grep `confirm\|approval\|approve\|delegat`（34 处命中已逐条归类为工具待确认计数 / 成员生命周期 Modal.confirm / 建队提交）+ 全仓 grep `delegat\|Delegate`（命中均为无关注释） |
| preload 的按通道 allowlist | UI/`.../preload/main.ts:18-52` 全文；UI/`.../common/adapter/main.ts:93-96`；UI/`.../common/platform/bridge.ts:138-149` |
| 子 Agent 数量 / 并发 / 嵌套深度 / 调用次数 / 运行时长的任何 UI 配置项 | 桌面端 settings 与 team 目录逐项 grep（见「数量限制界面」表） |
| 后端团队侧的成员数 / spawn 次数 / 深度上限常量 | CORE/`crates/aionui-team/src/**` grep `MAX_\|limit`（命中常量均已排除为无关） |
| 针对"建议用户开全自动"的提示词禁止条款 | CORE/`crates/aionui-team/src/prompts/**` grep `yolo` 无命中；`team_tools.rs` 13 个工具描述中仅 `team_spawn_agent` 有"先批准"措辞 |
| 前端可写入的、承载权限模式的设置文件 | 模式落点为后端 SQLite（CORE/`sqlite_assistant.rs:963`）与运行时/团队记录；桌面端 `common/config/configKeys.ts` 无相关键 |
| 20 分钟/30 分钟型审批超时实现 | PRD 自标缺失（UI/`docs/prds/conversations/acp/permissions.md:8-10,33`）；未找到定时扫描代码 |

**未验证（代码路径成立，但未运行、未实测）**

| 项 | 说明 |
| --- | --- |
| Agent 进程实际 POST 模式/审批接口是否成功 | 已确认 env 注入 + local 免鉴权 + 路由存在（三处引用见「免确认模式传染性分析 §3(b)」），但本次为只读审计，**未发起请求** |
| Agent 是否真能在被拒后重试委派（提示词约束的实际有效性） | 需要真实跑一次 Leader 会话才能判断模型是否遵守 CORE/`team_tools.rs:6-24` 的描述 |
| 团队 MCP server 的 HTTP 端口是否也进入 Agent 可见范围 | 已知 MCP server 绑 `127.0.0.1:0`（CORE/`crates/aionui-team/src/mcp/server.rs:67,96`）并带 `x-slot-id` 校验（CORE/`.../mcp/server.rs` 及测试 `tests/mcp_server_integration.rs:155`），但**未追踪端口下发到 Agent 的完整路径** |
| `conversation.approval.check` 是否有其他客户端（WebUI/mobile）在用 | desktop 包内无调用点；`packages/web-cli`、`mobile/` 未逐一核对 |
| 桌宠确认窗是否在打包配置中默认开启 | 已读 `petConfirmManager.ts`，未核对窗口创建的启用条件 |

**仅文档 / 仅提示词（不算实现）**

- 团队委派"必须先获用户批准"：CORE/`crates/aionui-api-types/src/team_tools.rs:6-24`、CORE/`crates/aionui-team/src/prompts/mod.rs:261`。
- 团队模式审批无超时、F-PERM-01 缺 30 分钟自动拒绝：UI/`docs/prds/conversations/acp/permissions.md:8-10,33`。
- F-PERM-06「设置中开启过全局免确认则新会话继承」：UI/`docs/prds/conversations/acp/permissions.md:141`（代码中未找到该开关；实际存在的等价物是助手级默认权限）。
