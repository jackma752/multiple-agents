# 适配器能力矩阵（P0）

**2026-11-05，0.8.9 保存回复恢复**：论文页可预览并将已成功返回、结构/来源完整校验的旧回复接回原 checkpoint；此离线操作不启动适配器，也不修改阶段授权或预算。缺失的 reason 字段改为局部模板补齐，继续走原审批/consume/32K 上限，不重生成整份讲解。未扩展任何适配器、工具或原生子 Agent 权限，未增加在线模型质量结论。实现及边界见 [恢复入口](issue-paper-recovery-runtime.md)。

**2026-11-03 当前：Claude `file-hooks-v1`**：固定 2.1.308/SHA 支持严格文件工具准入；真实 CLI + 本机合成模型验证了文件允许/拒绝、故障阻断、关闭原生 Agent、启动及子内部工具审批、终态和全树计数。仅一层前台 general-purpose 子任务，默认关闭；无 Shell/MCP/原生续接/子任务独立 Token 预留。未知版本保持拒绝。Codex 实际零模型回读确认 agents.enabled=false，但完整工具控制未验证，严格准入保持拒绝。具体边界及证据见 [当前实现](native-strict.md)。下方 0.8.1/P3 表为当时的历史记录，不覆盖这一更新。

**2026-11-03，0.8.1 严格模式诊断**：对当前采用的 Codex/Claude 程序只读核验，文件身份均匹配，监督准入条件通过，严格控制仍未验证；无新模型回合。论文预检提前返回 `mode_admissions`，不可用就拒绝创建，新草稿默认选严格可用的真实通道。没有改变任何适配器的能力声明、工具授权或原生子 Agent 配额。传输通知、gzip 和实际性能/限制见 [本轮记录](strict-transmission-0.8.1.md)。

**2026-10-30，0.7.0 论文指导路径**：模型步骤继续走现有阶段授权、配额与预算，未扩展任何适配器的工具或原生子 Agent 权限。核心讲解/局部审阅/方案为新模板；项目保存与再导出不调用模型；追问生成普通待审批调用。没有新增真实模型验收、OCR、形式化证明或自动投稿能力。见 [开发与实际验证](p6-guidance-implementation.md)。

**2026-10-28 整合版 0.5.4**：执行时限扩展至桌面用户采用的 Codex/Claude Code，默认 0 不限；旧明确配置保留，DeepSeek 网络空闲超时独立保留。根任务累计调用/子任务总数和阶段调用数新默认 0 不限，并发、层级、审批和预算照常生效。显示与删除改进见[线上测试需求](issue-online-test-improvements.md)。下方 0.5.3 为前一版记录。

**2026-10-28 安装版 0.5.3 执行时限**：用户采用的 Codex app-server 可在本机设置调整单次总执行时限，0 为不限时，默认 600 秒；批准值进入快照，适配器与后台超时检查一致。手动中断、工具卡有效期与连接超时保留。Claude/DeepSeek 以及固定评审运行时边界不扩展。本次为离线协议与发行检查，未新增真实模型验收。详见[问题与验证记录](issue-desktop-execution-timeout.md)。

**2026-10-26 集成入口**：`fix/model-collaboration-integration` 组合模型选择、分模型预算、
协作恢复与 DeepSeek 接入。DeepSeek 本次 model/effort、按模型归账及预算已离线验证；候选为
配置/官方文档列表，不宣称账号实时可用。详见[集成与复制修复](model-collaboration-integration.md)。

## 2026-10-26：P4 模型管理与分模型预算

`feat/p4-model-management` 新增模型选择、分模型明细和预算，首次真实运行仍待用户验收。实现及实际测试记录见 [P4 模型管理](p4-model-management.md)。

| 接入 | 查看与选择 | 来源与边界 |
|---|---|---|
| Codex app-server | 缓存目录、显式 `model/list` 刷新、线程/回合 model 和 effort 参数、配置回读 | GET 不启动进程；用户刷新可启动元数据进程但不发模型回合。读取目录不保证账号可用。配置回读不等于逐推理实际模型证明；真实新参数未验收 |
| Claude Code | 当前进程 `--model` / `--effort`、CLI 别名/历史 ID 候选和手填完整 ID、用量报告模型；强度候选 low/medium/high/xhigh/max（2026-10-30 静态读取固定的 2.1.308 二进制得到，CLI 对所选模型不支持的档位自动降级） | 无账号目录 RPC；不改 CLI 全局设置。设置模型预算后用完整 ID，不能用未解析的 opus/sonnet/haiku 别名绕开归属检查。真实新参数与降级后的实际档位未验收 |
| Codex exec | 通过已有 `-m` 参数选择模型 | 本次未开放推理强度；旧固定可执行文件缺失问题仍 OPEN，不因离线验证称在线可用 |
| 未声明模型选择的适配器 | 明确拒绝所请求的模型/强度选项 | Worker 在消耗审批前拒绝；不静默忽略选项 |

用量区按 Agent → 模型明细及模型预算 → Agent 合计展示，跨 Agent 总计可选；模型明细是调用账本的拆分，不与调用总数再次相加。新表由现有 `create_all` 添加，不修改或删除旧账本；模型未知的旧数据仍未知。当前 provider 键表示适配器通道（codex/claude-code），本次没有增加同一 Agent 的多账号/多网关配置。

**2026-10-26 工具允许链更新**：真实 app-server 的用户监督模式已实现本请求的命令/文件允许与拒绝，范围为单次 `accept/decline`；相关离线端到端与页面验证通过，真实工具允许待用户验收。严格模式准入不变，会话级授权/规则修订不放行。细节见 [工具审批](codex-tool-approval.md)。以下历史阶段的验证结论仍只认定各次原运行。

## 2026-10-26 P4.6 自动阶段与额度恢复（离线）

| 能力 | Codex app-server | Claude Code CLI |
|---|---|---|
| 用户阶段规则内自动调用 | 离线已验证；仍走原执行门禁 | 离线已验证；仍走原执行门禁 |
| 自动工具决定 | 本请求的命令/文件审批可按阶段规则决定；额外权限、网络和会话范围不隐式放行。真实自动工具执行待验收 | 无新增工具能力，CLI 工具禁用边界保留 |
| 明确额度错误分类 | 失败事件/RPC 错误归一化；真实错误格式未验证 | 失败结果归一化；离线错误与等待恢复已验证，真实格式未验证 |
| 等待持久化与到点恢复 | Hub 调度器离线已验证；无只读额度查询接口 | 同左 |
| 额度后的上下文恢复 | 绑定失败调用的原生 thread；离线验证 | 不支持原生续接，以原目标、依赖结果及失败记录新开独立调用 |
| 停止阶段 | 阻止新启动，向在途调用请求中断，以返回证据为准 | 阻止新启动；不因此声称支持原生取消或远端停止 |

详见 [P4.6 设计](p46-autopilot.md)。本次不升级任何真实运行、原生派生或远端停止能力的结论。

**2026-10-24 T-0 用量、历史、缓存和压缩盘点**（离线，分支 `feat/t0-usage-baseline`）：完整能力表、字段口径和用量基线见 [T-0 基线](t0-usage-baseline.md)，下面两张表中有关用量的行以它为准。要点：
- Claude Code 的 `input_tokens` 不含缓存，总输入要加上缓存写和缓存读：两次真实运行分别为 1,752 和 1,798，页面只显示了 2。
- Codex 的 `inputTokens` 已包含缓存。`tokenUsage.last` 只是单次模型请求，多请求的回合会被少记：S3 第三次实耗输入 30,539，只记了 15,328（缺陷 D-T0-1，在 T-1a 修复）。
- 中断或失败时没有用量，应该显示为未知，目前不显示。
- 两家的缓存都只能观测；原生压缩都未接入。

**2026-10-24 T-1a 用量账本**（离线，分支 `feat/t1-usage-ledger`；见 [会话后端契约](conversation-backend.md) 末节）：D-T0-1 到 D-T0-7 已在离线桩上修复。
- 改动：
  - app-server 按 thread 累计值的差计算回合用量，并与各次请求之和交叉核对；
  - 不再读取 `turn/completed` 中的用量；
  - Claude Code 按总输入（含缓存）入账；
  - 每次调用一行账本，失败、中断时也写；
  - 缺失一律为“未知”。
- **都没有在真实 Codex 或 Claude Code 上运行过**，真实对照在 T-1h 单独申请。

**2026-10-24 T-1d 上下文预览**（离线；见 [会话后端契约](conversation-backend.md) 的 T-1d 一节）：可选扩展 `context_profile()` 声明适配器是否原样发送 `task_input`，并附同一配置下的历史观测。app-server 和 exec 的 Codex、Claude Code 都原样发送，不添加任何提示文本；只添加固定参数，例如 thread 设置和 CLI 标志。观测值：app-server 为 15,183 和 15,753，Claude Code 为 1,752 和 1,798；exec 无观测。Mock 不声明，预览中它的附加文本显示为“未知”。provider 侧的指令、工具定义和 thread 历史对 Hub 一律未知，不计入估算。

**2026-10-24 T-1c token 预算**（离线；见 [会话后端契约](conversation-backend.md) 的 T-1c 一节）：严格预算模式只接受报告用量的适配器，即实现了 `usage_report` 的 app-server、Claude Code 和 exec；Mock 等适配器在严格模式下被拒（`budget_unmeterable`）。`describe()` 中用 `usage_reporting` 标出。任何适配器都**没有**经验证的输出或总量硬上限：单次预算只是预留量，不能在回合中途截断。预算开启（或关闭后重新开启）之前已获准、仍在执行的调用没有预留，按“用量未知且无预留”计，结算后再按账本计入；这一点与适配器无关（`TreeSpend.unreserved_in_flight`）。

**2026-10-23 Claude Code 最小接入**（离线，分支 `feat/claude-code-min`；见 [Claude Code 适配器](claude-code-adapter.md)）。下表描述本仓库自己的 CC 适配器；表 B 的 CC 一行（经 AionUi 接入时的观察）保留为历史。**2026-10-24 更新**：做过一次零模型身份核验（INCONCLUSIVE，用户决定记录）和一次真实单回合（PASS，只认定当次，见 [单回合实录](claude-code-turn-2026-10-24-run1.md)）；下表其余项仍未验证。

| 能力 | 状态 |
|---|---|
| 准入 | 严格模式拒绝（`claude_code_controls_unverified`）。监督模式只认代码常量 `PINNED_CLAUDE_CODE`（2.1.308，静态核对），并要求零模型身份核验已记录；2026-10-24 已记录（`b57a1d5f…`）。离线桩为 simulation |
| 发消息 / 最终回复 | 已实现（`claude -p`，提示词走 stdin，取 `--output-format json` 的 `result`）。**已验证一次（只认定当次，`evidence/cc-turn-real-run-1-pass`）**：经过 Hub，监督模式，固定提示词，回复恰好为 `OK`，`num_turns=1` |
| 真实用量 | 已实现：result 带 `usage` / `total_cost_usd` 时记录，`total_cost_usd` 是客户端估算。**已验证一次**：当次返回 input 2、cache write 1750、cache read 0、output 4，估算 $0.014088。**口径（T-0）**：`input_tokens` 不含缓存，总输入 = input + cache write + cache read，即 1,752；双 Agent 第二次为 2 + 635 + 1,161 = 1,798。**T-1a（离线）**：账本按总输入入账；另存 `usage_raw` 和 `model_usage` 的数字；启动失败为已知零值；多个模型为 partial。真实运行**未验证**。**P4 真实运行（2026-10-25）观测到**：`--max-turns 1` 下两次各出现一次两个模型、`num_turns=2`，原因未知，账本按规则记为 partial；第二次那条调用是被安全防护拒答（`is_error`），记为 failed，名额释放 |
| 流式 | 未实现（不用 `stream-json`） |
| 恢复会话 | 未实现（`--no-session-persistence`），不报告 `native_session` |
| 取消 | `unsupported`；本地停止只结束本次入口进程，远端未确认停止时保留名额 |
| 限制目录 | 未知。每次调用一个仓库外的空工作目录，不等于沙箱 |
| 拦截工具 | 不能。`--tools ""`、`--permission-mode dontAsk` 只是请求，**未验证** |
| 控制内部子 Agent | 不能。`--tools ""` 不提供 Agent 工具只是请求；不能声称已禁用子 Agent |
| 宿主配置隔离 | 请求：`--safe-mode`、`--strict-mcp-config`、`--disable-slash-commands`、专用 `CLAUDE_CONFIG_DIR`、剥离继承的 `CLAUDE*`/`ANTHROPIC*` 变量。**未验证** |

**2026-10-23 S6-1 app-server 加固**（离线；见 [S6 规划](appserver-s6-plan.md)和[会话后端契约](conversation-backend.md)末节）。下面这张表取代 2026-10-21 那张表中 app-server 的各行；那张表保留在后面，作为历史记录。

| 能力 | 状态 |
|---|---|
| 准入 | 严格模式：真实 app-server 被拒（`appserver_real_unverified`）。监督模式：只有用代码常量构造的 `CodexAppServerFactory` 才放行（`user_supervised_boundary`），条件是固定身份为 AgentHub 副本 `runtimes\codex\sha256-c81ed464e5e7cafd`（`codex-cli 0.163.0-alpha.11.2`，2026-10-23 重新固定，见 [Codex 运行时](codex-runtime.md)；S6-2、S6-3 的验证是在 alpha.11 上做的）、没有环境变量覆盖、启动参数与验证时一致、代理和 CA 可读、时限不超过 600 秒；批准时仍需显式同意。离线桩为 simulation |
| 经过 Hub 的最小回合 | **在 alpha.11.2 的 AgentHub 副本上又验证一次（S6-2 第二次，2026-10-24，只认定当次，`evidence/s6-2-real-run-2-pass`）**。**第一次（alpha.11）已验证（S6-2，只认定当次）**：进程内的 API、内核和适配器上走通一次 supervised 调用：设置核验、增量事件、结算 succeeded、名额释放、绑定身份一致（`evidence/s6-2-real-run-1-pass`）。网页点击路径、工具、中断、续接都不在其中 |
| 经过 Hub 的原生续接 | **已验证一次（S6-3，只认定当次）**：两次各自审批的 supervised 调用；第二次通过 `native_resume` 冷恢复同一个 thread（新进程、同一工作目录），恢复结构包含上一回合，新 turn ID，在提示词不含暗号的情况下答出了暗号；重放用量单独记录（`evidence/s6-3-real-run-1-pass`）。长对话、多次恢复、换 cwd、网页路径都没有验证 |
| 身份绑定 | 调用快照的 `adapter_version` 写入绑定身份。消费门禁之前做身份复核，比较双方（`"unknown"` 只等于 `"unknown"`，`"unavailable"` 永不放行），切换到 exec 等没有绑定接口的适配器也会被拒；不一致返回 409 `adapter_identity_changed`。启动前重新计算文件哈希。只在离线环境中验证 |
| 固定设置 | `thread/start`、`thread/resume`、`turn/start` 都传入 read-only、on-request、reviewer user；回读不符就不开回合。只在离线桩上验证 |
| 工具审批 | 用户监督模式下，命令/文件请求显示单次允许与拒绝，批准携带卡片 hash，发送前认领并复核范围；过期、作废和关闭后不新发允许，拒绝不改发 cancel。原始选项保留在鉴权详情。单次允许链离线已验证，**真实允许待用户验收**。独立 S3 拒绝链的历史结果保留；不经审批的操作仍只能观测 |
| 工具活动 | 观测工具 item 及其输出通知（不保存原文），结算时写明 `none / items_completed / unknown`；只有输出、没有生命周期事件时为 unknown。item 完成不等于进程已退出；**不能拦截，不能停止** |
| 中断 | 以本回合 `turn/completed(interrupted)` 为准；应答超时记为“尚未应答”。独立探针 S4 只验证了首个输出之前的中断（`evidence/s4-real-run-1-pass`）；**输出进行中的中断、工具子进程的停止：未验证** |
| 恢复会话 | thread 沿用首次调用的工作目录；核验返回的 cwd 和上一个回合；恢复时的一条用量重放单独记录。独立探针 S5 验证过一次冷恢复（`evidence/s5-real-run-2-pass`）；**经过 Hub 的续接、换 cwd、长对话：未验证** |
| 控制内部子 Agent | **不能控制**，只能观测。`--disable multi_agent` 只关闭 Collab 功能，模型声明了版本时不起作用（S6 规划第 2.1 节）：它只是请求，**不是**“已禁用子 Agent”。会话中的 `multi_agent_version` 是模式字段，不是派生证据 |
| 停止 | 只关闭本次调用自有的入口进程；后代进程只记录、不结束；远端停止不保证（监督模式既有边界） |
| 用量（T-0 补充） | 来源为 `thread/tokenUsage/updated`，每次模型请求一条。`inputTokens` 已包含 `cachedInputTokens`，`outputTokens` 已包含推理用量。T-0 时适配器只取最后一条 `last`，多请求的回合会少记（D-T0-1）；`turn/completed` 不带用量（D-T0-4）。中断或拒绝的回合没有用量，为未知。见 [T-0 基线](t0-usage-baseline.md) |
| 用量（T-1a，离线） | 回合用量 = thread `total` 之差（新 thread 从 0 开始，续接从重放的 `total` 开始），与各次请求之和交叉核对，不一致时为 partial；重复投递只算一次；迟到的用量照样计入，判为 partial；子 thread 的用量另存；没有发 `turn/start` 时为已知零值。只在离线桩上验证（包括 S3 第三次的真实数字）；**真实运行未验证** |

**2026-10-21 app-server 可选传输**：新增 `AppServerCodexAdapter`（`AGENT_HUB_CODEX_TRANSPORT=app-server`），作为 exec 之外的另一种传输。下表 Codex 行描述的仍是 exec。app-server 传输各项能力如下：

| 能力 | 状态 |
|---|---|
| 发消息 / 流式增量 | 离线桩已验证（内存桩与 stdio 子进程桩）；对真实 Codex，S2 只验证过一次单回合，**本适配器未对真实 Codex 运行** |
| 恢复会话 | 只验证了协议调用：发出 `thread/resume`、只发新消息文本；桩不保存历史，**真实续接效果未验证（S5）** |
| 取消 | 2026-10-22 起：对本进程正在驱动的回合可发送 `turn/interrupt`，只有该回合自己的 `turn/completed`（`interrupted`）才算确认停止，确认后结算为 `canceled`；服务端接受了请求但没有终态时，不声称已停止，名额保留。没有活动回合时（例如进程重启后）仍为 `unsupported`。只在离线桩上验证，真实中断行为**未验证（S4）**。本地停止只关闭自有传输 |
| 拦截工具 | 2026-10-22 起：有 Worker gate 时，每个工具审批请求生成一张用户审批卡，只有用户批准的那张卡回 `accept`，其余回 `decline`；没有 gate 时全部回 `decline`。只在离线桩上验证；provider 是否每次都先请求审批，以及真实审批往返，**未验证（S3）** |
| 控制内部子 Agent / 限制目录 | **不能控制**。2026-10-22 起可以**观测**：按本机 schema 的 `collabAgentToolCall`（`spawnAgent` 等，`receiverThreadIds`、`agentsStates`）和 `subAgentActivity`（`agentThreadId`、`kind`）记录原生子 Agent，并发出 `stream.native_agent_observed` 事件（不复制 prompt 和 message）。父回合结束时，只要有子 Agent 没有自身的结束证据，调用就为 unknown 并保留名额，父回合的实际终态单独记录。不计入配额，也不声称子 Agent 已停止。只在离线桩上验证。限制目录未验证；`sandbox` 显示为 `unknown`（`thread/start` 尚未传入 sandbox 参数） |
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
