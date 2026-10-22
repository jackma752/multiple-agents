# S6 方案：把 app-server 作为 `build_adapter()` 的可选传输（离线规划，未实施）

2026-10-23。分支 `plan/appserver-s6`，基于 S5 记录 `425281e`，本分支只有文档。

**本文只是规划和评审：没有改代码，没有真实运行，也不构成任何授权。**

## 0. 结论摘要

- **建议**：
  - 真实 app-server **只在用户监督模式（supervised）下放行**，严格模式（strict）继续阻止；
  - 第一阶段，工具审批**只允许拒绝**；
  - 固定身份写在经过评审的代码常量里，不能被环境变量覆盖。
- **理由**：S1–S5 各验证了一条窄路径（都只认定当次）。原生子 Agent 的控制（H3）、工具的事前拦截、工具 accept 链、输出进行中的中断、工具子进程的停止，都没有验证。
- **现有产品适配器**（`feat/interrupt-stop` 的 `appserver_adapter.py`）与 S3–S5 的实测结论有 **10 处差距**，见第 3 节。其中三处会在真实环境中直接出错，必须先修：
  - **D1**：拒绝会被改成 `cancel`；
  - **D2**：沙箱和审批设置既不设置也不核验；
  - **D7**：固定身份已经过期。
- **建议的实施顺序**：
  - S6-0：合并两条分支线（离线）；
  - S6-1：适配器加固（离线）；
  - S6-2 到 S6-5：经过 Hub 的真实运行，每次单独批准。
- 有 4 个问题需要用户决定，见第 8 节。

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
| 输出进行中的中断、工具子进程的停止、重复中断、被中断回合的用量 | **未验证** | S4 收口 |
| 原生子 Agent 的事前控制（H3）、工具的事前拦截（不发审批、直接在沙箱内运行的命令） | **未验证** | — |
| 恢复时换用另一个 cwd、分页历史、长对话、多次恢复 | **未验证** | — |
| 经过 Hub 内核、API、网页的端到端运行（审批、配额、事件流、卡片） | **未验证**（只在离线桩上跑过） | — |
| 其他版本或模型 | **未验证** | — |

## 3. 差距：产品适配器与实测结论

| # | 严重度 | 产品线现状 | 实测结论 | S6 要做的事 |
|---|---|---|---|---|
| D1 | **高** | `_fit_decision`：可选列表里没有 decline 时，回退为 `cancel` | alpha.11 的默认列表是 `[accept, (amendment), cancel]`，不含 decline；服务端不拿客户端的决定去对照列表，**decline 照样生效**（S3）。`cancel` 会中止整个回合 | 用户拒绝一律发 `decline`；只有用户明确批准才发 `accept`；永远不自动发 `cancel` |
| D2 | **高** | `thread/start` 只传 cwd，`turn/start` 不传任何策略，`thread/resume` 只传 threadId；不回读实际设置（`sandbox="unknown"`） | 探针都传了 read-only、on-request、reviewer user，并回读核验，任何一项不符就 0 回合 | 三处都传固定设置，并按 S3–S5 的规则核验实际设置；不符就拒绝开回合，判 definitive failed |
| D3 | 中 | 卡片中直接展示 `command` 原文，即 shlex 拼接后的外层 argv；`reason` 是模型写的说明，与命令并排展示 | 命令是 `"…powershell.exe" -Command '<内层>'`，需要按 argv 拆开才能看懂（S3） | 卡片展示拆开后的外层程序和内层命令，原文折叠显示；`reason` 标为“模型提供的说明” |
| D4 | 中 | 不观测 commandExecution 等工具 item 和输出，只观测原生子 Agent | 有些命令可能不经审批、直接在沙箱内运行；中断时进行中的 item 不会有完成事件；Codex 自己的提示是“unified exec 进程可能仍在后台运行” | 记录工具活动（类型、是否有 processId、是否结束）；结算时如实写明“工具子进程状态未知”，不声称已停止 |
| D5 | 中 | 中断用阻塞请求，5 秒超时；超时就记“未被接受” | 中断要到回合真正中止时才应答，应答先于终态（S4）；应答本身不能证明已停止 | 保持“以终态为准”。超时不再写成“未被接受”，改为“尚未应答”；可选改为非阻塞发送 |
| D6 | 中 | 每次调用在 `workdir_root/<invocation_id>` 新建 cwd；续接时 cwd 会变 | S5 只验证了恢复时 cwd 不变 | 二选一：thread 绑定固定的工作目录（推荐，与 S5 一致）；或者另行验证换 cwd 后恢复。见第 8 节 |
| D7 | **高** | 固定身份是旧的 exec 文件（已被自动更新删除），并且可以被环境变量覆盖 | 探针预检要求“固定身份未被覆盖”；被测文件是 `bin\6257d9e8f63c0f2b`（alpha.11） | app-server 单独固定一个身份常量（路径、SHA-256、大小、版本），由代码评审决定；supervised 准入时拒绝任何环境变量覆盖 |
| D8 | 低 | `ours()` 在 threadId 缺省时也算本回合的事件 | 探针要求 threadId 必须存在且一致 | 收紧：绑定事件必须带 threadId 和 turnId |
| D9 | 低 | 恢复时重放的用量，因 turnId 不同而被忽略（正确），但没有记录 | S5 证实恢复时恰好重放一次阶段 1 的用量 | 单独记为 `replayed_usage`，不算本回合的用量 |
| D10 | 低 | 本地停止只关闭自己的进程，不记录后代进程 | S4/S5 用关闭前后的快照和宽限期判断残留 | 停止和结算时记录后代进程名单，只记录、不按名称结束 |

## 4. 设计

### 4.1 准入

| 模式 | 真实 app-server |
|---|---|
| strict | **继续阻止**：H3 没有解决，工具事前拦截没有保证 |
| supervised | **有条件放行**，`code=user_supervised_boundary`，详见下文 |
| simulation | 不变，两个可信离线桩 |

supervised 放行的条件，全部由适配器代码判断：

- factory 的类型必须恰好是 `CodexAppServerFactory`；
- 路径和身份等于代码常量，**没有任何环境变量覆盖**，准入时和启动时都要复核大小和 SHA-256；
- 带 `--disable multi_agent`（只是请求，不作为阻断保证）；
- 代理和 CA 可读；
- 本地时限大于 0 且不超过 600 秒；
- 固定设置的核验逻辑在代码中，不可关闭；
- 第一阶段工具决定只允许拒绝（见 4.4）。

批准沿用现有的 `accept_unmanaged_native_agents: true` 显式同意流程。审批卡上要另外写明本表列出的未验证项。

### 4.2 固定身份与重新固定

- 新增 app-server 专用常量：`PINNED_APPSERVER_PATH`、`PINNED_APPSERVER_SHA256`、`PINNED_APPSERVER_SIZE`、`PINNED_APPSERVER_VERSION`，取 alpha.11 的值。不复用 exec 的常量，也不读环境变量。
- Codex 自动更新已经两次删除固定文件。删除后准入自动变为 blocked，这是期望的失败方式。重新固定按 S3 的流程：
  1. 被动核验文件；
  2. 用户批准后跑步骤 A（零模型）；
  3. 版本变化时，按新版本的标签逐个比较 S3–S5 依据文件的 blob；
  4. 改常量，走代码评审；
  5. 至少重跑一次 S6-2（第 5 节）。

### 4.3 thread 与 turn 的参数，以及实际设置的核验

- `thread/start`：`{cwd, sandbox:"read-only", approvalPolicy:"on-request", approvalsReviewer:"user"}`。
- `thread/resume`：`{threadId, cwd, sandbox, approvalPolicy, approvalsReviewer}`。源码表明请求中的值优先（S5 依据）。
- `turn/start`：`{threadId, input, approvalPolicy:"on-request", approvalsReviewer:"user", sandboxPolicy:{type:"readOnly", networkAccess:false}}`。
- 回读响应中的 approvalsReviewer、approvalPolicy、sandbox（只允许 readOnly，且禁止联网）和 cwd，与 S3–S5 的 `validate_thread_settings` 相同。不一致就不发 `turn/start`，判 definitive failed（`appserver_settings_mismatch`），并释放名额。
- 恢复时还要核验：返回的 thread ID 一致；`thread.turns` 中包含上一个回合。不在就拒绝续接，判 failed，不消耗模型回合。

### 4.4 工具审批

- 卡片绑定 request、thread、turn 和 item，沿用现有设计。命令按 S3 的 `classify_command` 拆开展示，只用于展示，不作为安全依据。
- **第一阶段只允许拒绝**：真实 app-server 的卡片只提供“拒绝”。另一种做法见第 8 节。
  - 用户拒绝，或卡片过期、作废，都发送 `decline`，只发一次，并绑定请求 ID。
  - 不再执行“没有 decline 就改成 cancel”。
  - 关闭门禁和 `may_answer` 的规则不变。
- 只有用户批准的卡片才可能发 `accept`。这条路径要等第 8 节的决定和一次单独的真实验证（S7）之后才开放。

### 4.5 工具活动与子进程

- 观测 commandExecution、fileChange、mcpToolCall、dynamicToolCall 等 item 和输出通知，只记录类型、ID、是否有 processId、结束状态，不保存命令输出。
- 结算时，`detail` 和事件流中如实写明“工具子进程状态：none / ended / unknown”。出现 unknown 时，即使主回合已经 completed 或 interrupted，也要附带说明，不声称工具已停止。
- 本地停止和结算时，记录 app-server 后代进程在关闭前后的名单，宽限 3 秒，与 S5 相同。

### 4.6 中断

- 保持现有契约：应答不等于停止；只有本回合的 `turn/completed(interrupted)` 才结算为 canceled；没有终态就是 unknown，并保留名额。
- 修正 D5：应答超时只记为“尚未应答”，后台继续跟随这个回合。
- 能力表中注明：只验证了首个输出之前的中断。

### 4.7 原生续接

- 每个 provider thread 绑定一个工作目录：`workdir_root/threads/<thread_id>`，首次调用时创建，续接时复用。这与 S5 验证过的形态一致。
- 恢复时的那一条用量重放，按 S5 修订后的规则处理：本 thread、上一个回合的 ID、在 `turn/started` 之前、最多一条。单独记录，不算本回合的用量。
- 现有规则不变：409 `no_native_session` 和 `native_session_busy`、进程内 thread 互斥、thread ID 进入快照哈希。

### 4.8 事件绑定

所有影响结算的事件，都必须带与本 thread、本 turn 一致的 threadId 和 turnId。D9 的重放是唯一例外，并单独记录。终态之后的 item 或 turn 事件，记为异常并写入 detail。

### 4.9 网络与证书

子进程环境沿用现有设置：代理加 `CODEX_CA_CERTIFICATE=<venv certifi>`。证书包已升级到 certifi 2026.10.19（`50f79f98…`）。`pip_trust.py --negative-control` 可以作为部署前的检查，见 `docs/codex-proxy-setup.md` 第 7.1 节。

### 4.10 能力表与文案

- `docs/adapter-capabilities.md`：app-server 一行逐项标注“已验证（只认定当次，附证据标签）”或“未验证”。不得写成“支持”。
- `capabilities()["verified"]` 也按此更新。

## 5. 实施分步（每一步单独提交；带“真实”字样的步骤都需要用户单独批准）

| 步骤 | 内容 | 验证方式 |
|---|---|---|
| **S6-0 合并**（离线） | 新建 `feat/appserver-s6`：以 `feat/interrupt-stop` 为基础，合并 `feat/appserver-s5-probe`（预计只有 README 和 HANDOFF 冲突）。不改动 main | 全量测试；S1–S5 的探针测试照常通过 |
| **S6-1 加固**（离线） | 处理 D1–D10，以及 4.1 的准入、4.2 的常量。复用 S3–S5 的桩形态（包装 argv、默认决定列表、`local` 环境、重放用量、中断时序），改造为适配器级的 stdio 桩 | 见第 6 节；全量测试；准入矩阵 |
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
  | 有环境变量覆盖、身份不符、文件缺失、时限超过 600 秒、factory 类型不对 | blocked |
  | 离线桩 | simulation |

  另外覆盖：准入通过之后、启动之前文件被替换，结果为启动被拒，0 次模型回合。
- **设置**：三种请求的参数逐字段断言；回读不符时，`turn/start` 发出次数为 0，结算为 failed，名额释放。
- **工具审批**：
  - 默认决定列表下点击拒绝，发送 `decline`，绝不发 `cancel`；
  - 卡片过期、作废，都发 `decline`；
  - 第一阶段任何路径都发不出 `accept`，包括伪造的 approved 状态；
  - 卡片展示拆开后的命令；
  - 现有的关闭门禁竞态测试全部保留。
- **工具活动**：不经审批直接执行的命令 item，以及中断时进行中的 item，结算 detail 中都标为 unknown。
- **中断**：应答超时之后终态才到，仍结算为 canceled；只有应答、没有终态，结算为 unknown 并保留名额（已有测试）。
- **续接**：
  - 工作目录复用；
  - 恢复响应中缺少上一个回合，就拒绝续接，0 个模型回合；
  - 第一条重放被容许，第二条重放、在开始之后才到的重放、其他 turn 的重放，都不计入用量并记为异常。
- **绑定**：threadId 缺省的事件不计入本回合。
- **固定身份**：常量与 alpha.11 一致；exec 的常量不受影响。
- **HTTP 与网页**：supervised 批准缺少显式同意时返回 `boundary_consent_required`；卡片只显示“拒绝”；能力表文案中没有“支持”字样。

## 7. 风险

- **自动更新**：固定文件随时可能被删除，准入会变为 blocked。这是安全的，但会让功能突然不可用，需要文档化的重新固定流程（4.2），以及界面上的明确提示。
- **未验证面**：supervised 模式本质上是“边界豁免”，不是“控制通过”。审批卡和能力表必须把第 2 节的未验证项写清楚，不能因为 S3–S5 的 PASS 而扩大表述。
- **内层命令是否真的不会运行**：S3 只证明了那一条固定命令在审批阶段被拒绝。不经审批、直接在沙箱内运行的命令（D4）只能观测，不能阻止。
- **两条线合并后的回归**：重叠文件很少，但产品线的测试与探针线的工具共用 `.venv`；pip、certifi 升级之后的全量测试只在探针线上跑过。
- **模型和提示行为**：模型可能改用代码模式的 `exec`，没有嵌套调用时在协议上看不到。

## 8. 需要用户决定

1. **工具 accept**：第一阶段只允许拒绝，等 S7 验证通过后再开放（推荐）；还是在 supervised 中直接允许用户批准卡片（accept 链未验证，批准后的命令可能在沙箱外运行）？
2. **续接时的 cwd**：thread 绑定固定的工作目录（推荐，与 S5 一致）；还是每次调用使用新目录，再另做一次换 cwd 的恢复验证？
3. **固定身份的来源**：代码常量、不允许环境变量覆盖（推荐）；还是沿用现有的、可被环境变量覆盖的配置？
4. **合并方式**：在新的 `feat/appserver-s6` 上以 interrupt-stop 为基础，合并 s5-probe 线（推荐，保留两条线各自的历史）；还是 rebase？无论哪种，都不推送，也不动 main。

## 9. 本方案不做的事

- 不改动 strict 模式的结论，不声称原生子 Agent 已被控制。
- 不开放工具 accept（等待第 8 节的决定和 S7）。
- 不合并到 main，不推送，不改写历史（这些需要用户另行批准）。
- 本轮没有任何代码改动，也没有真实运行。
