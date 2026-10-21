# 适配器能力矩阵（P0）

**2026-10-21 app-server 可选传输**：新增 `AppServerCodexAdapter`（`AGENT_HUB_CODEX_TRANSPORT=app-server`），作为 exec 之外的另一种传输。下表 Codex 行描述的仍是 exec。app-server 传输各项能力如下：

| 能力 | 状态 |
|---|---|
| 发消息 / 流式增量 | 离线桩已验证（内存桩与 stdio 子进程桩）；对真实 Codex，S2 只验证过一次单回合，**本适配器未对真实 Codex 运行** |
| 恢复会话 | 只验证了协议调用：发出 `thread/resume`、只发新消息文本；桩不保存历史，**真实续接效果未验证（S5）** |
| 取消 | 2026-10-22 起：对本进程正在驱动的回合可发送 `turn/interrupt`，只有该回合自己的 `turn/completed`（`interrupted`）才算确认停止，确认后结算为 `canceled`；服务端接受了请求但没有终态时，不声称已停止，名额保留。没有活动回合时（例如进程重启后）仍为 `unsupported`。只在离线桩上验证，真实中断行为**未验证（S4）**。本地停止只关闭自有传输 |
| 拦截工具 | 2026-10-22 起：有 Worker gate 时，每个工具审批请求生成一张用户审批卡，只有用户批准的那张卡回 `accept`，其余回 `decline`；没有 gate 时全部回 `decline`。只在离线桩上验证；provider 是否每次都先请求审批，以及真实审批往返，**未验证（S3）** |
| 控制内部子 Agent / 限制目录 | **不能控制**。2026-10-22 起可以**观测**：按本机 schema 的 `collabAgentToolCall`（`spawnAgent` 等，`receiverThreadIds`）和 `subAgentActivity`（`agentThreadId`）记录原生子 Agent，并发出 `stream.native_agent_observed` 事件（不复制 prompt）。只在离线桩上验证；结算规则不变。限制目录未验证；`sandbox` 显示为 `unknown`（`thread/start` 尚未传入 sandbox 参数） |
| 准入 | 只有两种可信离线桩工厂按 simulation 放行；真实 app-server 在严格和监督模式下都被拒（`appserver_real_unverified`） |

[契约与验证](conversation-backend.md)

**2026-10-21 本地历史补充**：Hub 新增会话/消息持久化与已确认终态回复的恢复投影，适配器仍使用现有 `last_result` 记录。离线覆盖 Codex 捕获解析；没有新增真实调用，不升级 `resume_session`、取消、拦截或派生控制能力。[后端契约](conversation-backend.md)

**2026-10-20 用户监督边界**：exec 适配器新增 `stop_local_process`，只终止当前 API 进程持有句柄的本地入口进程；以本地 Python 子进程验证，不等于 Codex 远端取消。`cancel=false`、原生派生不受控、只读拒写未验证保持不变。严格准入仍关闭；用户可逐调用显式接受监督边界。Hub 协作扩展默认关闭，不是原生 Agent 的隔离插件。[接口与限制](manual-control-boundaries.md)

规范（`AGENT_HUB_PLAN.md` §2）：**每个适配器先提交能力表，未验证的能力显示"未知"，不显示"支持"。**
本表只记录已读源码或已读官方文档确认的内容，其余一律 `未知`。

状态取值：`已实现` / `部分实现` / `未实现` / `未知`
来源标注：`源码` = 本次审计的 AionUi/AionCore 源码；`文档` = 官方文档（未实测）。

**2026-10-17 原型验证补充**：独立 app-server 探针已通过 S1，以及另行批准的一次 S2：消息增量/最终回复 OK、匹配完成事件、服务端 token 用量均已实测。见 [S2 实录](appserver-s2-2026-10-17.md)。它尚未成为内核适配器；取消、工具审批、恢复与原生派生控制不能据此升级，普通真实执行继续阻止。表 B 的 Codex 行仍描述现有 exec 适配器。

## 表 A：中间层（AionUi / AionCore）—— 已审计

这一层决定"中转站能否管住 Agent"，是本次 P0 的审计对象。结论见 `docs/audit-aionui.md`。

| 能力 | 状态 | 证据 |
|---|---|---|
| 发消息（用户 → Agent） | 已实现 | AionCore `aionui-conversation` 域 + WS 事件 |
| 流式输出 | 已实现 | WebSocket + 事件广播（`ARCHITECTURE.md`） |
| 恢复会话 | 部分实现 | ACP `session/new` + resume；claude/codebuddy 用 `_meta.claudeCode.options.resume`，codex 用 `session/load`（team `docs/mcp.md:171-176`） |
| 取消任务 | 部分实现 | 有 kill/stop 路径；团队 MCP 无取消语义，permission pending 无超时（`permission_router.rs:124-146`） |
| 限制文件目录 | **未实现** | 团队共享单一 workspace，无 per-agent 目录/worktree（`aionui-team/src/workspace.rs:50-77`）；无 OS 级沙箱（`aionui-process/src/containment.rs:1-8` 自述 lifecycle-only） |
| 拦截工具操作 | **未实现** | 单聊有 ACP 审批卡片；但 team MCP 被硬编码自动放行（`permission_router.rs:15,73-76`），团队成员被强制 full-auto（`service/spawn_support.rs:68-79`），agy 钩子被主动删除（`factory/antigravity.rs:66-79`） |
| 控制内部子 Agent | **未实现** | 原生委派仅翻译为 `SubagentUpdate` 事件、不计数不拦截（`claude_conn.rs:1791-1797`、`codex_conn.rs:6287-6329`）；全仓无关闭开关 |
| 委派需用户批准 | **未实现** | `session.rs:1846` spawn 无审批分支；仅存于工具描述 `team_tools.rs:9,19` |
| 全树数量/并发/深度配额 | **未实现** | 全 `crates` 数量类命名 0 命中；无 `parent_id/root_id/depth` 字段 |
| 审批的凭证边界 | **未实现** | 本地模式不挂 CSRF（`routes.rs:428-435`）、CSRF 客户端恒为空桩（`httpBridge.ts:202-204`）、IPC 整桥透传（`preload/main.ts:18-32`） |
| 返回真实用量 | 未知 | 本次未审计计费/用量路径；费用类信息不得假定可用 |

## 表 B：各产品适配器 —— 多数单元格未知

本次**只审计了中间层源码，没有安装或运行任何 CLI**，因此产品自身能力大多无法确认。
下表 `未知` 的单元格就是 P0 待办（测试方法见最后一列）。

| 产品 | 发消息 | 流式 | 恢复会话 | 取消 | 限制目录 | 拦截工具 | 控制内部子 Agent | 真实用量 |
|---|---|---|---|---|---|---|---|---|
| Codex | **已验证**（历史） | **已验证**（JSONL） | 接口存在、**未验证** | **未知**（exec 无接口） | **未验证**（探测无法判断） | **未知**（exec 未接入） | **当前配置阻断失败，真实新调用已拒绝** | **已验证**（历史） |
| CC / Claude Code | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | **已知不可控**（`Task`/`Workflow` 仅事件） | 未知 |
| WorkBuddy | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 |
| DSH | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 |
| Ink Stone | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 | 未知 |
| 自建 Agent | 待实现 | 待实现 | 待实现 | 待实现 | 待实现 | 待实现 | **可完全控制**（自己实现） | 待实现 |

> **Codex 行的更新（2026-10-14，P3 收尾）**："已验证"指**我们适配器**经真实调用观测到的结果；
> 逐项证据见 `docs/agent-codex-capabilities.md`（能力表与自描述证据）、
> `docs/p3-codex-real-run-2026-10-14.md`（一次经批准的真实联调）、
> `docs/p3-acceptance.md`（P3 验收分类）。
> **2026-10-15**：原生子 Agent 阻断在当前配置观测到失败，见 [观测](p3-native-subagent-incident.md) 和 [执行准入](p3-strict-adapter-admission.md)。取消 / 拦截工具 / 只读拒写仍未验证，方案、判定条件与
> "不具备验证条件"的说明见 `docs/p3-permission-probes.md`。
> 其中"拦截工具"有强反向线索：`codex exec` **拒绝** `-a/--ask-for-approval`，
> 而批准通道在 schema 中只服务 app-server 的 `turn/start`。

> Codex 与 CC 的"控制内部子 Agent"标为**已知不可控**，指的是**经 AionUi 接入时**观测到的行为
> （只观测不拦截）。它们自身是否提供禁用开关（如 CLI 参数、环境变量）**未知**，需按 AGENTS.md 规则
> 以真实 `--help` / 捕获样本为准，不得靠记忆推断。

### 每个适配器接入前必须回答的问题（P0 出口条件）

1. 能否发消息、是否流式、会话能否恢复、任务能否取消（不支持必须返回 `unsupported`，不得伪造）。
2. 能否限制可读写目录（是"受限工作目录"还是"无限制"）。
3. 工具操作能否在**启动前**拦截（不是提示词约定）。
4. **产品自带的子 Agent / 后台委派能否关闭或在启动前拦截**；不能则标记"不满足严格协作要求"。
5. 能否返回真实用量（token/费用）；不能则用次数与时限约束，并显示"费用未知"。
6. 是否为子进程净化环境、是否能阻止 Agent 直接 fork 另一个 CLI 绕过中转站。

### 验证方法（不得用记忆代替）

- CLI 自身 `--help` / 自描述 schema 的实际输出。
- 真实调用的一次捕获样本（wire 级），而非手写 mock。
- 参考实现的源码（若存在）。

## 结论对选型的影响

表 A 中三个硬条件全部 `未实现`，且其中两项属"数据模型缺失"而非"开关未开"。
因此**不建议**把 AionUi 当作满足方案 §6 安全阀用例的基座；它的价值在 UI、会话管理、ACP 接入与任务板，可作为参考实现。
详见 `docs/audit-aionui.md` §5 的两条路线比较。
