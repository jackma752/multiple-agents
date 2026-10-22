# S6 方案：把 app-server 作为 `build_adapter()` 的可选传输（离线规划）

2026-10-23。分支 `plan/appserver-s6`，基于 S5 记录 `425281e`，本分支只有文档。

**本文是规划和评审，不构成任何真实运行的授权。**

**修订 1（2026-10-23，按用户指示）**：
- 新增 D11：调用快照绑定 app-server 固定身份，在消费门禁之前做身份复核；
- 完成有边界的 multi_agent / v2 源码核查（第 2.1 节），并且不把会话的模式字段写成实际派生的证据；
- 写明监督模式中已有的停止边界和 CLI 绕过边界（第 4.11 节），这两项不是新增偏差；
- 修正第 4.4 节“作废卡一律 decline”与工具决定关闭规则的冲突；
- 更正 D6：首次调用时还不知道 thread id，原先“按 thread id 建目录”的写法不可行。

用户随后指示继续推进 S6-0 和 S6-1（离线），不追加真实探测。S4 的收口结论和 S1–S5 的历史证据保持不变。

## 0. 结论摘要

- **建议**：
  - 真实 app-server **只在用户监督模式（supervised）下放行**，严格模式（strict）继续阻止；
  - 第一阶段，工具审批**只允许拒绝**；
  - 固定身份写在经过评审的代码常量里，不能被环境变量覆盖，并且绑定进调用快照（D11）。
- **理由**：S1–S5 各验证了一条窄路径（都只认定当次）。以下都没有验证：原生子 Agent 的控制（H3）、工具的事前拦截、工具 accept 链、输出进行中的中断、工具子进程的停止。
- **multi_agent**：按 alpha.11 源码，`--disable multi_agent` 关闭的是 Collab 功能。这个功能只在最后一级回退中才被参考，模型声明了版本时它不起作用（第 2.1 节）。因此这个参数既不能作为控制手段，也不能作为控制证据。
- **现有产品适配器**（`feat/interrupt-stop` 的 `appserver_adapter.py`）与实测结论有 **11 处差距**，见第 3 节。其中四处必须先修：
  - **D1**：拒绝会被改成 `cancel`；
  - **D2**：沙箱和审批设置既不设置也不核验；
  - **D7**：固定身份已经过期；
  - **D11**：快照不绑定适配器身份。
- **实施顺序**：
  - S6-0：合并两条分支线（离线）；
  - S6-1：适配器加固（离线）；
  - S6-2 到 S6-5：经过 Hub 的真实运行，每次单独批准。

## 1. 现状

**分支**：两条线都从 S2 的 `023d0ab` 分出，都没有合并到 main（`1d4a303`），也没有 remote。

- **产品线**：`feat/chat-workbench`（c38d510）→ `feat/async-tool-approval`（4ba8099）→ `feat/chat-frontend`（0a25e84）→ `feat/interrupt-stop`（98fbc44）。包含内核、API、网页、`AppServerCodexAdapter`、工具审批卡、中断和原生续接。
- **探针线**：`feat/appserver-s3-probe` → `feat/appserver-s4-probe` → `fix/pip-proxy-trust` → `feat/appserver-s5-probe`（425281e）。包含 S3–S5 的探针、证据、记录和证书环境。
- 自 `023d0ab` 起，产品线改了 54 个文件，探针线改了 37 个，**重叠的只有 `README.md` 和 `docs/HANDOFF_CURRENT.md`**。

**`build_adapter()`**（产品线 `apps/worker/agenthub_kernel/adapters.py`）：

- `AGENT_HUB_ADAPTER=codex` 并且 `AGENT_HUB_CODEX_TRANSPORT=app-server` 时，进入 `_build_appserver`。设置了 `AGENT_HUB_APPSERVER_STUB=<场景>` 时用离线桩，否则用 `CodexAppServerFactory`。
- 固定身份取自 `config.codex_settings()`，与 exec **共用**：`PINNED_CODEX_*` 还是旧的 `bin\2e5e00daee91c61d`（0.162.0-alpha.17.2），而且可以被环境变量 `AGENT_HUB_CODEX_PATH`、`AGENT_HUB_CODEX_SHA256`、`AGENT_HUB_CODEX_SIZE` 覆盖。

**准入**（`packages/contracts` 的 `execution_admission`）：

- 适配器代码声明 strict、supervised 或 simulation；请求、能力标志和环境变量都不能授予准入。
- 现状：两个可信离线桩按 simulation 放行；真实 app-server 在 strict 和 supervised 下都是 `appserver_real_unverified`。
- exec 适配器在 supervised 下的条件是：固定身份、read-only、自有进程、传入禁用子 Agent 的参数、没有命令前缀、本地时限不超过 600 秒；批准时还需要 `accept_unmanaged_native_agents: true`（见 `docs/manual-control-boundaries.md`）。

**快照**：`InvocationSnapshot.adapter_version` 的默认值是 `"unknown"`，产品代码中没有任何地方给它赋值（会话提交、`/api/invocations`、子任务提议、重试都不赋值）。方案第 4.1 节要求审批绑定适配器版本，而现状是重新固定程序之后，旧审批仍然可以执行。

## 2. S1–S5 已验证与未验证

以下都只针对当次运行；S1、S2 用的是旧版本，S3–S5 用的是 `codex-cli 0.163.0-alpha.10.2` 或 `0.163.0-alpha.11`。

| 项目 | 状态 | 证据 |
|---|---|---|
| 握手和空 thread | 已验证（S1） | `evidence/s1-real-pass` |
| 一个最小回合：增量、最终回复、匹配的终态、服务端用量 | 已验证（S2，旧版本） | `evidence/s2-real-pass` |
| 命令审批，只拒绝一次：请求 → decline → resolved → declined → 回合完成，没有执行。可选决定列表里没有 decline 时仍然生效；`environmentId=local`；命令为包装后的 argv | 已验证（S3 第三次，alpha.11） | `evidence/s3-real-run-3-pass` |
| 在首个输出**之前**中断：一次中断 → 应答 `{}` → `turn/completed(interrupted)`，没有残留后代进程 | 已验证（S4 第一次） | `evidence/s4-real-run-1-pass` |
| 冷恢复：A 退出后 B 恢复同一个 thread，历史被带回，模型答出暗号；恢复时会重放一次阶段 1 的用量 | 已验证（S5 第二次） | `evidence/s5-real-run-2-pass` |
| 工具 **accept** 链，以及批准后命令的真实执行 | **未验证** | — |
| 输出进行中的中断、工具子进程的停止、重复中断、被中断回合的用量；中断时仍待决的审批请求如何收尾 | **未验证** | S4 收口 |
| 原生子 Agent 的事前控制（H3）、工具的事前拦截（不发审批、直接在沙箱内运行的命令） | **未验证** | 第 2.1 节只是源码依据 |
| 恢复时换用另一个 cwd、分页历史、长对话、多次恢复 | **未验证** | — |
| 经过 Hub 内核、API、网页的端到端运行（审批、配额、事件流、卡片） | **未验证**（只在离线桩上跑过） | — |
| 其他版本或模型 | **未验证** | — |

### 2.1 multi_agent / v2 源码核查（alpha.11，有边界）

**范围**：事先固定 5 个问题，只读标签 `rust-v0.163.0-alpha.11` 中下面列出的文件。没有运行 Codex，没有查看模型目录，也没有扩展到其他版本。

| 问题 | 源码结论 | 依据（alpha.11） |
|---|---|---|
| Q1 两个功能的定义 | `multi_agent` 对应 `Feature::Collab`（stable，默认开）；`multi_agent_v2` 对应 `Feature::MultiAgentV2`（stable，默认关） | [features/src/lib.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/features/src/lib.rs) |
| Q2 `--disable multi_agent` 改的是什么 | 只关闭 Collab | 同上（功能键 `multi_agent` 就是 Collab） |
| Q3 rollout 里的 `multi_agent_version` 是什么 | 这是会话的模式字段，取值为 `disabled`、`v1` 或 `v2`，写在 SessionMeta 和 TurnContextItem 里。新 thread 依次取：①配置覆盖（开了 `MultiAgentV2` 功能就是 V2；`[agents] enabled = false` 就是 Disabled）；②模型声明的版本；③按功能回退（开了 Collab 是 V1，否则是 Disabled） | [protocol/src/protocol.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/protocol/src/protocol.rs)、[core/src/session/mod.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/core/src/session/mod.rs)（`resolve_multi_agent_version`、`resolve_multi_agent_version_for_model`）、[core/src/config/mod.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/core/src/config/mod.rs)（`multi_agent_version_override`、`multi_agent_version_for_model`、`multi_agent_version_from_features`） |
| Q4 什么决定是否注册子 Agent 工具 | `collab_tools_enabled` 按这个模式字段判断：Disabled 时不注册；V2 时，只要没有设置 `multi_agent_v2.disable_native_tools`，并且是根会话（或模型声明 V2），就注册 `spawn_agent`、`send_message`、`followup_task`、`wait_agent`、`interrupt_agent`、`list_agents` | [core/src/tools/spec_plan.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/core/src/tools/spec_plan.rs) |
| Q5 有没有关闭路径 | 源码层面有一条**候选路径**：`[agents] enabled = false`（默认为 true），在没有开启 `multi_agent_v2` 功能时得到 Disabled，于是不注册子 Agent 工具 | `config/mod.rs`（`agents_enabled`） |

**结论**（按 10-19 回顾的口径）：**有一条可验证的修复路径，但尚未验证。** 没有验证的内容包括：
- app-server 是否接受通过 `-c` 传入的这个配置；
- `thread/start` 或模型能否把它改回去；
- 不注册工具是否就等于不会派生；
- 通过 Shell 启动其他 Agent CLI 的入口，这与本问题无关，见第 4.11 节。

**用词边界**：
- S3、S4 的 rollout 中，`multi_agent_version` 都是 `v2`。按源码，这说明当次会话的工具面按 v2 配置，也就是说子 Agent 工具很可能已经注册。
- 本机没有开启 `multi_agent_v2` 功能（P3 时 features list 显示为 false；alpha.11 上未复核），所以 v2 很可能来自模型声明的版本。这一点是推断，没有核对模型目录。
- 这个字段**不是**派生发生过的证据。派生只能以 `collabAgentToolCall`、`subAgentActivity` 这类 item 或子 thread 的记录为证据。S3–S5 各次运行中有没有派生，以各自的运行记录为准，本节不重新解释。
- `--disable multi_agent` 在模型声明了版本时不起作用。因此它只是附带的请求，审批卡和能力表不能写“已禁用子 Agent”。

**处理**：
- S6-1 保持 argv 与 S3–S5 的验证形态一致（`app-server --stdio --disable multi_agent`），不在本轮加入 `agents.enabled=false`，避免真实运行同时改变两个变量。
- 候选路径列为 **S6-V**（第 5 节）：先做一次零模型核验，确认会话的模式字段变为 `disabled`，并且工具列表中没有子 Agent 工具，然后才考虑写入 argv。这一步需要单独批准，并且即使通过也不解除 strict 的阻止：还需要 H3 的行为证据。

## 3. 差距：产品适配器与实测结论

| # | 严重度 | 产品线现状 | 实测或源码结论 | S6 要做的事 |
|---|---|---|---|---|
| D1 | **高** | `_fit_decision`：可选列表里没有 decline 时，回退为 `cancel` | alpha.11 的默认列表是 `[accept, (amendment), cancel]`，不含 decline；服务端不拿客户端的决定去对照列表，**decline 照样生效**（S3）。`cancel` 会中止整个回合 | 拒绝一律发 `decline`；只有用户批准、而且 accept 已开放时才发 `accept`；永远不自动发 `cancel` |
| D2 | **高** | `thread/start` 只传 cwd，`turn/start` 不传任何策略，`thread/resume` 只传 threadId；不回读实际设置（`sandbox="unknown"`） | 探针都传了 read-only、on-request、reviewer user，并回读核验，任何一项不符就 0 回合 | 三处都传固定设置，并按 S3–S5 的规则核验实际设置；不符就不开回合，判 definitive failed |
| D3 | 中 | 卡片中直接展示 `command` 原文，即 shlex 拼接后的外层 argv；`reason` 是模型写的说明，与命令并排展示 | 命令是 `"…powershell.exe" -Command '<内层>'`，需要按 argv 拆开才能看懂（S3） | 卡片展示拆开后的外层程序和内层命令，原文另附；`reason` 标为“模型提供的说明” |
| D4 | 中 | 不观测 commandExecution 等工具 item 和输出，只观测原生子 Agent | 有些命令可能不经审批、直接在沙箱内运行；中断时进行中的 item 不会有完成事件 | 记录工具活动（类型、是否有 processId、是否结束）；结算时如实写明工具子进程状态，未知就写未知 |
| D5 | 中 | 中断用阻塞请求，5 秒超时；超时就记“未被接受” | 中断要到回合真正中止时才应答，应答先于终态（S4）；应答本身不能证明已停止 | 保持“以终态为准”。超时记为“尚未应答”，不写成“未被接受”，后台继续跟随回合 |
| D6 | 中 | 每次调用在 `workdir_root/<invocation_id>` 新建 cwd，续接时 cwd 会变；stderr 日志写在 cwd 里 | S5 只验证了恢复时 cwd 不变，并且工作目录为空 | thread 沿用首次调用的工作目录（第 4.7 节）；stderr 日志移到 cwd 之外 |
| D7 | **高** | 固定身份是旧的 exec 文件（已被自动更新删除），并且可以被环境变量覆盖 | 探针预检要求“固定身份未被覆盖”；被测文件是 `bin\6257d9e8f63c0f2b`（alpha.11） | app-server 单独固定一组身份常量，由代码评审决定；supervised 准入拒绝任何环境变量覆盖 |
| D8 | 低 | `ours()` 在 threadId 缺省时也算本回合的事件 | 探针要求 threadId 必须存在且一致 | 收紧：绑定事件必须带 threadId 和 turnId |
| D9 | 低 | 恢复时重放的用量，因 turnId 不同而被忽略（正确），但没有记录 | S5 证实恢复时恰好重放一次阶段 1 的用量 | 单独记为 `replayed_usage`，不算本回合的用量 |
| D10 | 低 | 本地停止只关闭自己的进程，不记录后代进程 | S4、S5 用关闭前后的快照和宽限期判断残留 | 停止和结算时记录后代进程名单，只记录、不结束（监督模式既有边界，见第 4.11 节） |
| D11 | **高** | `adapter_version` 恒为 `"unknown"`，审批不绑定适配器身份；启动时也不复核 | 方案第 4.1 节：审批绑定适配器版本，快照变化后审批立即失效；Codex 自动更新已两次替换固定文件 | 快照写入 app-server 的绑定身份；消费门禁之前做身份复核（比较双方），启动前再复核文件（第 4.12 节） |

## 4. 设计

### 4.1 准入

| 模式 | 真实 app-server |
|---|---|
| strict | **继续阻止**：H3 没有解决，工具事前拦截没有保证 |
| supervised | **有条件放行**，`code=user_supervised_boundary`，详见下文 |
| simulation | 不变，两个可信离线桩 |

supervised 放行的条件，全部由适配器代码判断：

- factory 的类型必须恰好是 `CodexAppServerFactory`；
- 路径和身份等于代码常量，**没有任何环境变量覆盖**。准入时检查文件存在和大小；完整的 SHA-256 在启动进程前计算。准入在数据库写锁内执行，不适合计算约 340 MB 文件的哈希（S6-1 实施时的调整）；
- argv 与验证形态一致：`app-server --stdio --disable multi_agent`。这个参数只是附带的请求，不作为控制手段（第 2.1 节）；
- 代理和 CA 可读；
- 本地时限大于 0 且不超过 600 秒；
- 固定设置的核验逻辑在代码中，不可关闭；
- 第一阶段工具决定只允许拒绝（第 4.4 节）。

批准沿用现有的 `accept_unmanaged_native_agents: true` 显式同意流程。审批卡另外写明第 2 节的未验证项和第 4.11 节的边界。

### 4.2 固定身份与重新固定

- 新增 app-server 专用常量：`PINNED_APPSERVER_PATH`、`PINNED_APPSERVER_SHA256`、`PINNED_APPSERVER_SIZE`、`PINNED_APPSERVER_VERSION`，取 alpha.11 的值。不复用 exec 的常量，也不读环境变量。
- Codex 自动更新已经两次删除固定文件。删除后准入自动变为 blocked，这是期望的失败方式。重新固定按 S3 的流程：
  1. 被动核验文件；
  2. 用户批准后跑步骤 A（零模型）；
  3. 版本变化时，按新版本的标签逐个比较 S3–S5 依据文件的 blob，以及第 2.1 节的文件；
  4. 改常量，走代码评审；
  5. 至少重跑一次 S6-2。
- 常量一变，绑定身份就变（第 4.12 节），旧版本下创建的待审批调用全部不能执行。

### 4.3 thread 与 turn 的参数，以及实际设置的核验

- `thread/start`：`{cwd, sandbox:"read-only", approvalPolicy:"on-request", approvalsReviewer:"user"}`。
- `thread/resume`：`{threadId, cwd, sandbox, approvalPolicy, approvalsReviewer}`。源码表明请求中的值优先（S5 依据）。
- `turn/start`：`{threadId, input, approvalPolicy:"on-request", approvalsReviewer:"user", sandboxPolicy:{type:"readOnly", networkAccess:false}}`。
- 回读响应中的 approvalsReviewer、approvalPolicy、sandbox（只允许 readOnly，且禁止联网）和 cwd，规则与 S3–S5 的 `validate_thread_settings` 相同。不一致就不发 `turn/start`，判 definitive failed（`appserver_settings_mismatch`），并释放名额。
- 恢复时还要核验：返回的 thread ID 一致；`thread.turns` 中包含上一个回合。不满足就拒绝续接，判 failed，不消耗模型回合。

### 4.4 工具审批（修订：与工具决定的关闭规则一致）

- 卡片绑定 request、thread、turn 和 item，沿用现有设计。命令按 S3 的 `classify_command` 拆开展示，只用于展示，不作为安全依据。
- **第一阶段只允许拒绝**：真实 app-server 的卡片只提供“拒绝”。卡片的可选决定写成 `["decline"]`，provider 提供的原始列表另附在卡片详情中；这样，现有的 `accept_not_offered` 校验会在 API 层拒绝批准。
- 发送规则以现有的工具决定关闭规则为准（`tool_approvals.py`），即**唯一的发送路径是发送前的认领**：

  | 卡片状态 | 工具决定未关闭（没有关闭记录） | 工具决定已关闭（中断或停止的生效点之后） |
  |---|---|---|
  | 用户拒绝 | 认领后发一次 `decline` | 不发（记 `blocked`） |
  | 过期 | 认领后发一次 `decline` | 不发（记 `blocked`） |
  | 作废：请求已随回合或传输结束 | 不发：已经没有可回答的请求 | 不发 |
  | 作废：在生效点被关闭 | — | 不发（记 `not_sent`） |
  | 生效点之前已认领（`sending`） | 正常写出 | 可能仍会写出，记为“在途”：既不算已写出，也无法撤回 |
  | 没有卡片的请求（建卡失败、非本回合） | `may_answer()` 确认工具决定未关闭后，才自动发 `decline` | 什么都不发 |

- 原文“卡片过期、作废，都发 decline”与上表冲突，已删除。**作废卡永远不发送任何决定。**
- 不再执行“没有 decline 就改成 cancel”。
- 工具决定关闭之后仍待决的 provider 请求不作回答，由中断后的回合收尾，这一点在真实环境中未验证（第 2 节）。
- 只有用户批准的卡片才可能发 `accept`。这条路径要等用户另行决定，并完成一次单独的真实验证（S7）之后才开放。在此之前，即使数据库里出现伪造的 approved 状态，适配器也不发 `accept`，改发 `decline`，并记 `accept_suppressed`。

### 4.5 工具活动与子进程

- 观测 commandExecution、fileChange、mcpToolCall、dynamicToolCall 等 item，以及这些类型的 item 级通知（例如 `item/commandExecution/outputDelta`）。只记录类型、ID、是否有 processId、是否见过生命周期事件、输出通知的条数、结束状态，不保存命令内容和输出原文。只有输出通知、没有生命周期事件的 item，按缺少结束证据处理，状态为 unknown（按评审补上）。
- 结算时，`detail` 和事件流如实写明工具状态：`none`（没有工具 item）、`items_completed`（所有工具 item 都报告完成）、`unknown`（有 item 开始后没有完成）。item 完成是 provider 对这个 item 的报告，不单独证明它启动的进程已经退出，所以不用 “ended” 这个词（S6-1 实施时的调整）。出现 unknown 时，即使主回合已经 completed 或 interrupted，也附带说明，不声称工具已停止。
- 本地停止和结算时，记录 app-server 后代进程在关闭前后的名单，宽限 3 秒，与 S5 相同。只记录，不结束（第 4.11 节）。

### 4.6 中断

- 保持现有契约：应答不等于停止；只有本回合的 `turn/completed(interrupted)` 才结算为 canceled；没有终态就是 unknown，并保留名额。
- 修正 D5：应答超时只记为“尚未应答”，后台继续跟随这个回合。
- 能力表注明：只验证了首个输出之前的中断。

### 4.7 原生续接（修订：首次调用时还不知道 thread id）

- 首次调用：在 `workdir_root/<invocation_id>` 创建 cwd，传给 `thread/start`；拿到 thread id 后，把“thread id → cwd”记入执行库。
- 续接：按 `resume_thread_id` 从执行库取回同一个 cwd，传给 `thread/resume`，并核验响应中的 cwd。查不到记录，或者目录已不存在，就拒绝续接，判 failed，0 个模型回合，不改用新目录。这与 S5 验证过的“恢复时 cwd 不变”一致。
- app-server 的 stderr 日志写到 `workdir_root/_logs/`，不放在 cwd 里，保持 cwd 与验证时一样为空。
- 恢复时的那一条用量重放，按 S5 修订后的规则处理：本 thread、上一个回合的 ID、在 `turn/started` 之前到达、最多一条。单独记录，不算本回合的用量。
- 现有规则不变：409 `no_native_session` 和 `native_session_busy`、进程内 thread 互斥、thread ID 进入快照哈希。

### 4.8 事件绑定

所有影响结算的事件，都必须带与本 thread、本 turn 一致的 threadId 和 turnId。D9 的重放是唯一例外，并单独记录。终态之后的事件记为 `late_events`，只统计结算时已经收到的事件，之后到达的不再读取。“已经收到”包括：与终态同一批取出、排在终态之后的事件（按评审修正，原实现会丢弃它们），以及结算时队列中剩下的事件。迟到事件不计入本回合的工具活动或用量。

### 4.9 网络与证书

子进程环境沿用现有设置：代理加 `CODEX_CA_CERTIFICATE=<venv certifi>`。证书包已升级到 certifi 2026.10.19（`50f79f98…`）。`pip_trust.py --negative-control` 可以作为部署前的检查，见 `docs/codex-proxy-setup.md` 第 7.1 节。

### 4.10 能力表与文案

- `docs/adapter-capabilities.md`：app-server 一行逐项标注“已验证（只认定当次，附证据标签）”或“未验证”。不得写成“支持”，也不得写“已禁用子 Agent”。
- `capabilities()["verified"]` 也按此更新。
- `describe()` 中的 `subagent_disable_requested` 保持“只是请求”的含义；多 Agent 模式字段只按第 2.1 节的口径说明。

### 4.11 监督模式已有的边界（不是新增偏差）

这两项边界早在 2026-10-20 已由用户授权定为监督模式的范围（方案开头的修订说明、`docs/manual-control-boundaries.md`、AGENTS.md 的“关键区分”）。S6 沿用，不扩大，也不缩小：

- **停止**：
  - 只关闭本次调用自己持有的入口进程（app-server 的 stdio 句柄）；
  - 不遍历 PID，不按进程名结束，不碰共享的 daemon；
  - 不保证后代进程和远端停止；
  - 未确认就保留名额；
  - D10 只是把后代进程名单记录下来，不结束它们。

  原方案第 4.4 节“本地进程要停止整个进程树”仍然是 **strict 模式**的要求；真实 app-server 在 strict 下被阻止，所以两者不冲突。
- **CLI 绕过**：
  - 监督模式不承诺逐项拦截工具操作，也不承诺“只运行主 Agent”；
  - 第一阶段只允许拒绝，只覆盖**会发出审批**的命令；
  - 不发审批、直接在沙箱内运行的命令（D4），只能观测，不能阻止，其中可能包括启动另一个 Agent 的 CLI；
  - 只读沙箱和禁止联网是请求的设置，经过回读核验（D2），但它们能否挡住这类启动，没有验证。

  原方案第 4.4 节“Agent 不能通过直接运行另一个 Agent 的 CLI 绕过中转站”，仍然是 strict 模式的要求。
- 审批卡和能力表照此写明：“工具操作无法逐项拦截，可能包括启动其他 Agent 的 CLI；这类活动不在 Hub 配额内；停止只关闭本次的入口进程。”

### 4.12 D11：身份绑定与身份复核

- **绑定身份**：适配器提供 `binding_identity()`，返回一个确定的字符串。
  - 真实 app-server：`codex-app-server@<版本>;sha256=<哈希>;size=<大小>`，取自 4.2 的常量；
  - 离线桩：`appserver-stub:in-memory` 或 `appserver-stub:stdio`；
  - 没有声明绑定的适配器（exec、Mock）记为 `"unknown"`，以保持现有快照的哈希；exec 的绑定列为后续事项；
  - 声明了却取不到值（抛出异常、空字符串，或者把自己声明成 `"unknown"`）记为 `"unavailable"`。
- **写入快照**：会话提交、`/api/invocations`、子任务提议这三处创建快照时，从当前适配器取绑定身份，写入 `adapter_version`。它参与 `payload_hash`，所以审批绑定这个身份。重试复制原快照，不更换身份。
- **身份复核（三层，比较双方）**：
  1. Worker 在**消费门禁之前**比较快照记录的身份和当前适配器的身份，两边都参与比较，不因当前适配器没有绑定接口就跳过：`"unknown"` 只与 `"unknown"` 相等，`"unavailable"` 永不放行。不一致就返回 409 `adapter_identity_changed`，审批保持原状，启动次数为 0；旧审批永远无法执行，需要重新提交。
     - 修订 1 的实现原本只在当前适配器提供绑定接口时才比较。切换到 exec 适配器就会跳过比较，让 app-server 下批准的调用被消费并启动。这一处已按评审修正，并补了回归测试。
  2. 适配器自身再比较一次，作为纵深防御：app-server 适配器在 `start_streaming` 中比较；exec 适配器拒绝任何 `adapter_version` 不是 `"unknown"` 的快照。到这一层时审批已经消费，不一致就判 terminal failed，释放名额，启动次数仍为 0。
  3. factory 在启动进程前，重新计算一次文件的大小和 SHA-256；不符就拒绝启动。在复核和启动之间文件被替换的窗口仍然存在，在文档中如实说明。

## 5. 实施分步（每一步单独提交；带“真实”字样的步骤都需要用户单独批准）

| 步骤 | 内容 | 验证方式 |
|---|---|---|
| **S6-0 合并**（离线，用户已指示推进） | 新建 `feat/appserver-s6`：以 `feat/interrupt-stop` 为基础，合并本分支（其中已包含 `feat/appserver-s5-probe`），预计只有 README 和 HANDOFF 冲突。不改动 main | 全量测试；S1–S5 的探针测试照常通过 |
| **S6-1 加固**（离线，用户已指示推进） | 处理 D1–D11，以及 4.1 的准入、4.2 的常量、4.11 的文案。复用 S3–S5 的形态（包装 argv、默认决定列表、`local` 环境、重放用量、中断时序），在适配器级桩中实现 | 见第 6 节；全量测试；准入矩阵 |
| **S6-V 真实**（另议）：子 Agent 关闭路径的零模型核验 | `-c agents.enabled=false` 后，确认会话的模式字段为 `disabled`、工具列表中没有子 Agent 工具；不发 `turn/start` | 不解除 strict 的阻止；通过后才考虑改 argv |
| **S6-2 真实**：经过 Hub 的一个最小回合 | 网页或 API 发起 supervised 调用 → 批准（显式同意）→ 一个无工具的回合 → 增量进入 SSE → 结算为 succeeded → 名额释放 | 与 S2 同形，但走完整的内核链路 |
| **S6-3 真实**：经过 Hub 的原生续接 | 同一会话两次调用：第二次 native_resume 回忆暗号 | 与 S5 同形 |
| **S6-4 真实**：经过 Hub 的工具审批拒绝 | 固定命令 → 卡片 → 用户点击拒绝 → declined → 回合完成 | 与 S3 同形 |
| **S6-5 真实**：经过 Hub 的中断 | 运行中调用中断接口 → canceled | 与 S4 同形；争取在输出进行中中断，但不作为前置条件 |
| S7（另议） | 工具 accept 链：在空目录中执行一条无害命令，证据要包括执行结果和工作目录 | 需要先由用户决定是否开放 accept |

每一次真实运行都要：先被动复核固定身份和证书包；失败即停，不重试；写运行记录，登记哈希，打 evidence 标签。

## 6. 离线测试计划（S6-1）

- **准入矩阵**：

  | 情形 | 结果 |
  |---|---|
  | strict，任何真实 factory | blocked |
  | supervised，并且所有条件都满足 | supervised |
  | 有环境变量覆盖、身份不符、文件缺失、时限超过 600 秒、factory 类型不对、argv 被改 | blocked |
  | 离线桩 | simulation |

  另外覆盖：准入通过之后、启动之前文件被替换，结果为启动被拒，0 次模型回合。
- **D11**：
  - 三处创建快照都写入绑定身份；
  - 身份变化后，在消费门禁之前就被拒（409 `adapter_identity_changed`），审批未消费，启动 0 次；
  - 切换到没有绑定接口的适配器（exec）时同样被拒，exec 适配器自身也拒绝；
  - `"unavailable"` 永不放行；
  - 绕过 Worker 直接调用适配器时也被拒；
  - 现有 strict 和 exec 快照的哈希不变。
- **设置**：三种请求的参数逐字段断言；回读不符时，`turn/start` 发出次数为 0，结算为 failed，名额释放。
- **工具审批**（按 4.4 的表）：
  - 默认决定列表下点击拒绝，发一次 `decline`，绝不发 `cancel`；
  - 过期且工具决定未关闭时，发一次 `decline`；
  - 作废卡（回合结束、生效点关闭）发 0 条消息；
  - 关闭之后再点击，返回 409 `tool_decisions_closed`，发 0 条消息；
  - 生效点前已认领的，记为“在途”；
  - 第一阶段任何路径都发不出 `accept`：API 返回 `accept_not_offered`；伪造的 approved 状态被改发 `decline` 并记 `accept_suppressed`；
  - 卡片展示拆开后的命令；
  - 现有的工具决定关闭竞态测试全部保留。
- **工具活动**：不经审批直接执行的命令 item，以及中断时进行中的 item，结算 detail 中都标为 unknown。
- **中断**：应答超时之后终态才到，仍结算为 canceled；只有应答、没有终态，结算为 unknown 并保留名额（已有测试）。
- **续接**：
  - 工作目录复用，响应中的 cwd 经过核验；
  - 找不到“thread → cwd”的记录就拒绝，0 个模型回合；
  - 恢复响应中缺少上一个回合，就拒绝续接，0 个模型回合；
  - 第一条重放被容许，第二条重放、在开始之后才到的重放、其他 turn 的重放，都不计入用量并记为异常；
  - stderr 日志不在 cwd 中。
- **绑定**：threadId 缺省的事件不计入本回合。
- **固定身份**：常量与 alpha.11 一致；exec 的常量不受影响。
- **文案**：能力表、审批卡和 `describe()` 中没有“支持”“已禁用子 Agent”这类表述；卡片写有第 4.11 节的边界。
- **HTTP 与网页**：supervised 批准缺少显式同意时，返回 `boundary_consent_required`；真实 app-server 的卡片只显示“拒绝”。

## 7. 风险

- **自动更新**：固定文件随时可能被删除，准入会变为 blocked。这是安全的，但会让功能突然不可用，需要文档化的重新固定流程（4.2），以及界面上的明确提示。D11 保证旧审批不会落到新程序上。
- **未验证面**：supervised 模式本质上是“边界豁免”，不是“控制通过”。审批卡和能力表必须把第 2 节的未验证项和第 4.11 节的边界写清楚，不能因为 S3–S5 的 PASS 而扩大表述。
- **子 Agent 工具面**：按源码，当前会话很可能注册了 v2 子 Agent 工具（第 2.1 节）。supervised 下派生的子 Agent 只能观测，不在 Hub 配额内；没有自身结束证据时，调用保持 unknown 并保留名额（现有规则）。
- **内层命令是否真的不会运行**：S3 只证明了那一条固定命令在审批阶段被拒绝。不经审批、直接在沙箱内运行的命令（D4）只能观测，不能阻止。
- **两条线合并后的回归**：重叠文件很少，但产品线的测试与探针线的工具共用 `.venv`；pip、certifi 升级之后的全量测试只在探针线上跑过。
- **模型和提示行为**：模型可能改用代码模式的 `exec`，没有嵌套调用时在协议上看不到。

## 8. 用户决定的记录

用户在 2026-10-23 指示继续推进 S6 离线整合与加固，没有对四项逐一作答。S6-0 和 S6-1 按推荐项执行，因为其余选项都超出本轮的授权：

1. **工具 accept**：第一阶段只允许拒绝。另一个选项会开放未验证的 accept 链。
2. **续接时的 cwd**：thread 沿用首次调用的工作目录（第 4.7 节）。另一个选项需要另做一次真实验证。
3. **固定身份的来源**：代码常量，不允许环境变量覆盖。另一个选项会保留覆盖入口。
4. **合并方式**：在新的 `feat/appserver-s6` 上以 interrupt-stop 为基础做合并。rebase 会改写历史，需要另行批准。不推送，也不动 main。

用户可以随时改变上述任何一项；改变后，按新的决定修订方案和代码。

## 9. 实施记录

- **S6-0**（`2afba44`）：`feat/appserver-s6` 以 `feat/interrupt-stop` 为基础合并本分支，冲突只有 HANDOFF。合并后全量 977 passed。
- **S6-1**（本提交）：D1–D11 都已在离线桩上实现，说明见[会话后端契约](conversation-backend.md)的“S6-1”一节。实施中有三处调整，已改入上文：
  - 准入只检查文件存在和大小，完整哈希在启动前计算（第 4.1 节）；
  - 工具状态用 `items_completed`，不用 “ended”（第 4.5 节）；
  - 终态之后的事件只统计结算时已收到的部分（第 4.8 节）。
- **评审修正**（S6-1 之后的提交），三处：
  - 身份复核只在当前适配器有绑定接口时才比较，切换到 exec 就会跳过。改为比较双方，`"unavailable"` 永不放行，exec 适配器自身也拒绝带身份的快照；
  - 工具输出通知没有被记录。改为计入工具活动，没有生命周期证据时状态为 unknown；
  - 同一批通知中排在终态之后的事件被丢弃。改为计入 `late_events`。
  
  同时统一了“门禁”相关的用词，见[会话后端契约](conversation-backend.md)“S6-1”一节中的术语。
- **S6-2**：2026-10-23 执行一次，PASS，只认定当次（`evidence/s6-2-real-run-1-pass`）。按用户要求以现有证据收口，不追加重复运行。
- **S6-3 离线准备**：
  - 驱动 `tools/probes/appserver_s6_3.py` 和反例测试；
  - 适配器新增审计字段 `opening_method`、`input_sha256`、`resume_check`、`turn_id_reused`、`process_id`；
  - 修正后代进程判定中 PID 复用导致的误报（说明见[会话后端契约](conversation-backend.md)“S6-3 的离线准备”）。
- **仍未验证**：S6-3 到 S6-5、子 Agent 关闭路径（S6-V）、accept 链（S7）。

## 10. S6-2 单次真实运行申请

**状态（2026-10-23）**：用户批准在 `26a9553` 上执行一次，已执行，结果 **PASS**，只认定本次。见 [S6-2 第一次实录](appserver-s6-2-2026-10-23-run1.md)，标签 `evidence/s6-2-real-run-1-pass`。授权已用完，S6-3 到 S6-5 仍需逐项申请。下文保留申请原文。

**本节只是申请，不构成授权。** 用户单独批准之后才执行，只执行一次。

- **被测提交**：`feat/appserver-s6` 上包含本节的提交（哈希在聊天中给出）。执行前确认 HEAD 和工作树都没有变化。
- **固定程序**：`codex-cli 0.163.0-alpha.11`，`bin\6257d9e8f63c0f2b\codex.exe`，SHA-256 `00c0f1e4…cd67c`，342187312 字节，就是代码常量 `PINNED_APPSERVER`。复用步骤 A 证据 `identity-hkzdmoqj`（S3–S5 也是复用它），驱动会在执行前再被动核验一次大小和哈希。
- **证书包**：certifi 2026.10.19，`cacert.pem` 的 SHA-256 为 `50f79f98…0785`，驱动执行前核验。
- **CODEX_HOME**：`%LOCALAPPDATA%\AgentHub\codex-home`，与 S2–S5 相同。只检查登录文件是否存在，不读取。
- **命令**（PowerShell，一次）：

  ```
  Set-Location E:\plug\agent-hub\.worktrees\appserver-s6
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s6_2.py --mode real --approve-one-model-turn --acknowledge-supervised-boundary
  ```

- **路径**：在进程内用 `create_app` 搭起 API、Worker 和内核，适配器由 `build_adapter()` 构造（app-server 传输，代码常量身份）。经过 HTTP 接口依次：
  1. 新建一个会话；
  2. 提交一条 supervised 调用；
  3. 带显式同意 `accept_unmanaged_native_agents: true` 批准；
  4. `run?wait=true`。

  驱动只发这一次调用，不回答任何工具卡，不重试，不发第二个回合。
- **范围与上限**：
  - 一个新 thread，最多 1 个模型回合；
  - 固定提示词与 S2 相同（`Reply with exactly: OK. …`）；
  - 本地时限 300 秒；
  - 数据目录（数据库、工作目录、执行库）放在系统临时目录下，在仓库之外，保留供审计；
  - 结果写在 `var/appserver-s6-2/real-*/result.json`，不入库，只登记哈希。
- **执行前检查**（任何一项不满足就停止：不建数据库，不启动进程）：
  - 没有身份环境变量覆盖；
  - factory 恰好是用常量构造的真实 factory；
  - 程序的大小和哈希一致；
  - CA 哈希一致；
  - CODEX_HOME 在仓库之外，有登录文件，可写；
  - 监督准入放行，严格准入仍然阻止。
- **PASS 条件**（全部满足）：
  - 调用为 `succeeded`；适配器启动 1 次；名额已释放；
  - 事件顺序为 `starting` < `stream.turn_started` < 第一条 `stream.delta` < `stream.turn_ended` < `succeeded`；
  - 回复恰好是 `OK`；
  - 没有工具活动、工具卡、被拒或未答的请求，也没有观测到原生子 Agent；
  - 没有不绑定的事件、迟到事件或用量异常，也没有恢复相关的记录；
  - 有服务端用量；本地进程确认退出；关闭后没有残留的后代进程；
  - 快照中的绑定身份等于固定身份；工作目录为空。
- **只认定**：这一次经过 Hub（API、内核、适配器、事件库）的 supervised 最小回合可用。
- **不认定**：网页点击路径（本次走进程内 API，不经浏览器）、strict 模式、工具审批、中断、续接（分别是 S6-3 到 S6-5），以及子 Agent 控制。
- **停止规则**：任何失败或异常都立即停止，不重试，不换模型，不放宽权限，不接着执行 S6-3。出现工具请求时，Hub 只提供“拒绝”；卡片在 120 秒后过期，由 Hub 按规则发送一次 decline，本次判 FAIL（`tool_activity`）。
- **风险**：
  - 按源码，会话很可能注册了 v2 子 Agent 工具（第 2.1 节）。如果模型派生子 Agent，只能观测；子 Agent 没有自身的结束证据时，调用为 unknown 并保留名额。
  - 不经审批、直接在沙箱内运行的命令只能观测。
  - 会消耗一次模型回合的额度，经代理 8090 访问网络。
- **已知差异**（不属于本次范围）：产品默认的数据目录是仓库内的 `var/`，模型的 cwd 会落在仓库里、继承仓库指令。驱动把数据目录放在了仓库外，与 S2–S5 一致。是否把“工作目录必须在仓库外”加入监督准入条件，需要你另行决定。
- **证据**：登记 `result.json`、`_logs` 下的 stderr 日志和 rollout 的 SHA-256；写运行记录；打 `evidence/s6-2-*` 标签。

## 11. S6-3 单次真实运行申请（未执行）

**本节只是申请，不构成授权。** 用户单独批准之后才执行，只执行一次。S6-2 已按现有证据收口，不重复运行。

- **被测提交**：`feat/appserver-s6` 上包含本节的提交。它只在 `8a5256c`（S6-3 离线准备）之上增加了本节文档，代码与 `8a5256c` 相同。执行前确认 HEAD 和工作树都没有变化。
- **固定程序与环境**：与 S6-2 相同，执行前由驱动再被动核验一次：
  - `codex-cli 0.163.0-alpha.11`，`bin\6257d9e8f63c0f2b\codex.exe`，SHA-256 `00c0f1e4…cd67c`，342187312 字节；
  - certifi 包 `50f79f98…0785`；
  - CODEX_HOME 为 `%LOCALAPPDATA%\AgentHub\codex-home`，只检查登录文件是否存在，不读取；
  - 代理为 127.0.0.1:8090。

  如果这些都没有变化，就复用步骤 A 证据 `identity-hkzdmoqj`（`report.json` 的 SHA-256 为 `804e69a7…5d3b`），执行前核对一次。有任何变化就停止，另行申请。
- **命令**（PowerShell，一次）：

  ```
  Set-Location E:\plug\agent-hub\.worktrees\appserver-s6
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s6_3.py --mode real --approve-two-model-turns --acknowledge-supervised-boundary
  ```

- **路径**：全部经过 Hub，不直接调用适配器：在进程内用 `create_app` 搭起 API、Worker、内核，适配器由 `build_adapter()` 构造，使用代码常量中的身份。
  1. 新建一个专用会话。
  2. **调用 1**：independent，`thread/start` 新建 thread。提示词与 S5 相同，带随机暗号 `S6-` 加 10 位（约 49 位熵），要求回复 `STORED`。这次调用有自己的审批，批准时显式同意 `accept_unmanaged_native_agents: true`。
  3. 健康检查：
     - 状态为 succeeded，回复恰好是 `STORED`；
     - 实际发出的是 `thread/start`；
     - 没有工具活动、子 Agent 或异常事件，有用量；
     - 本地进程确认退出，没有后代进程残留，名额已释放，回复中记录了 native_session。
  4. 等待调用 1 的 app-server 进程（适配器记录的 PID）离开进程表，最多 10 秒。
  5. **调用 2**：`context_mode=native_resume`，由内核绑定到调用 1 的 thread，沿用调用 1 的工作目录。输入是固定的回忆提示词，不含暗号，也不重新拼入调用 1 的内容；发送前驱动会检查这一点。这次调用也有自己的审批和显式同意。
- **不会续接其他 thread**：数据目录、数据库和执行库都是新建的；内核只把本会话最近一次回复的 thread 绑定进来；适配器只能恢复执行库里有记录的 thread。所以不可能续接 S1–S5 或 S6-2 的证据 thread。
- **预算与时限**：
  - 最多 2 次调用、2 个模型回合（每次调用 1 个），2 个 app-server 进程，依次执行；
  - 每次调用本地时限 300 秒；调用 1 之后等待进程退出最多 10 秒；整次最长约 11 分钟；
  - 不重试，不发第三次调用。
- **停止规则**（任何一条触发都立即停止，不重试，不换模型，不放宽权限）：
  - 预检失败：0 个回合；
  - 调用 1 没有提交或没有批准、健康检查不通过，或进程退出无法确认：1 个回合，不提交调用 2；
  - 回忆输入检查不通过，或内核绑定的 thread 不是调用 1 的：不发送调用 2；
  - 调用 2 没有批准或没有启动：不再尝试；
  - 无论结果如何，都不接着执行 S6-4、S6-5、S6-V 或 S7。
- **PASS 条件**（全部满足）：
  - 调用 2 实际发出了 `thread/resume`，恢复的是调用 1 的 thread；
  - 恢复应答中包含调用 1 的回合；
  - 调用 2 的回合 ID 与调用 1 不同，没有复用；上一回合等于调用 1 的回合；
  - 实际发送的输入哈希等于回忆提示词的哈希；工作目录没有变；
  - 两次调用都是 succeeded，名额都已释放，本地进程都确认退出，没有后代进程残留，工作目录为空；
  - 上一回合的合法用量重放最多一条，单独记录，不计入调用 2 的用量，并且没有用量异常；
  - 调用 2 的回复中含有暗号（是否恰好等于暗号另行记录）。
- **INCONCLUSIVE**：
  - 结构都正确但回复中没有暗号；
  - 任一次调用出现工具或原生子 Agent 活动：这种情况下不能认定回忆成功；子 Agent 没有自身结束证据时，调用为 unknown 并保留名额，需要你裁决；
  - 状态不是 succeeded、本地清理未确认、名额未释放、停在了调用 2 之前。
- **FAIL**：没有走 resume、thread 不对、回合 ID 被复用、恢复结构中没有上一回合、实际输入不对、重放用量被误计、工作目录变了，或恢复在开回合前被拒。
- **只认定**：这一次经过 Hub 的冷恢复（新进程、同一 thread、同一工作目录）后，历史可用。
- **不认定**：网页点击路径、长对话或分页历史、多次恢复、换 cwd 恢复、工具、中断，以及子 Agent 控制。
- **风险**：
  - 按源码，会话很可能注册了 v2 子 Agent 工具（第 2.1 节）；如果模型派生子 Agent，只能观测。
  - 会消耗最多 2 个模型回合的额度。
  - stderr 很可能出现与 S3、S4、S6-2 相同的“系统 skills 重装时文件被占用”的 ERROR，这是已有现象，不作为异常。
- **证据**：
  - 审计结果 `var/appserver-s6-3/real-*/result.json` 只包含暗号的 SHA-256 和匹配结论（含有、恰好等于），没有暗号原文和回复原文。
  - 原始证据都不入库：仓库外数据目录中的数据库（含两次的提示词和回复）、`_logs` 下两份 stderr、CODEX_HOME 中这个 thread 的 rollout。
  - 登记 result.json、两份 stderr 和 rollout 的 SHA-256；写运行记录；打 `evidence/s6-3-real-run-1-<pass|fail|inconclusive>` 标签。

## 12. 本方案不做的事

- 不改动 strict 模式的结论，不声称原生子 Agent 已被控制，也不把会话的模式字段当作派生证据。
- 不开放工具 accept（等用户的决定和 S7）。
- 不改变 argv（`agents.enabled=false` 等 S6-V）。
- 不改动 S4 的收口结论和 S1–S5 的历史证据。
- 不合并到 main，不推送，不改写历史（这些需要用户另行批准）。
- 不追加任何真实探测。
