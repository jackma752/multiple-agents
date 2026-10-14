# App Server 接入：可行性评估与最小原型（离线）

日期：2026-10-14。分支：`feat/appserver-proto`（worktree `.worktrees/appserver-proto`），
基于 `feat/p3-wrapup` @ `7d56ad9`。**本轮未启动任何真实模型回合、未修改凭据、未合并、未推送。**

范围：评估 Codex **app-server** 传输（相对我们现用的 `codex exec`）能否作为
**可选**接入方式，并交付一个**离线可验证**的最小客户端 + 桩服务 + 测试。

---

## 1. 三层证据分类（不得混淆）

本文件对每一项都标注它属于哪一层。**只有第三层能说"支持"。**

| 层 | 含义 | 本轮的来源 |
|---|---|---|
| **A. 文档支持** | 官方文档/官方源码明确写了 | openai/codex 仓库的 `codex-rs/app-server/README.md` 与 `codex-rs/exec/src/exec_events.rs`（版本线 `rust-v0.162.0`，与本机 `0.162.0-alpha.17.2` 对应） |
| **B. 本机接口存在** | 本机 schema/help 捕获里有 | `var/codex-schema/`（`codex app-server generate-json-schema --experimental` 产物）、`var/codex-help-app_server.txt` |
| **C. 实际行为已验证** | 真实运行/桩运行**观测到** | 本轮：**只有桩**（`tests/adapters/test_appserver_client.py`，32 项）。真实 app-server 会话**尚未运行** |

> **取证限制（如实声明）**：`developers.openai.com` 在本环境 DNS 解析失败
> （`getaddrinfo EAI_AGAIN`）、`github.com` 为 `ENOTFOUND`、`mintlify.wiki` 取回超时。
> 上表 A 层引用的是 `raw.githubusercontent.com` 上的**官方仓库文件**；
> 第三方镜像（mintlify 等）**不作为官方依据**，仅在正文中标注为镜像。

## 2. 兼容性表（四项能力）

| 能力 | A. 文档支持 | B. 本机接口存在 | C. 实际行为已验证 |
|---|---|---|---|
| **会话恢复** | **是**：`thread/start` 开新会话，"If you're continuing an existing conversation, call `thread/resume` with its ID instead"；`thread/resume` 必填 `threadId` | **是**：`ClientRequest.json:8146/8170`、`v2/ThreadResumeParams.json` required `threadId`；`thread/fork`、`thread/list`、`thread/read` 亦在 | **否**（桩只证明客户端能发起并解析；真实 resume 未运行） |
| **消息增量** | **是**：`item/agentMessage/delta`，"concatenate `delta` values for the same `itemId` in order to reconstruct the full reply"；生命周期 "`item/started` → zero or more item-specific deltas → `item/completed`" | **是**：`ServerNotification.json` 含 `item/started`/`item/completed`/`item/agentMessage/delta`/`item/commandExecution/outputDelta`/`item/fileChange/patchUpdated` 等 | **部分（仅桩）**：桩上验证了增量按到达顺序拼接、通知顺序不被重排；**真实顺序无文档承诺** |
| **命令/文件审批** | **是**：`item/commandExecution/requestApproval`、`item/fileChange/requestApproval`；**由客户端决定，服务端等待**："The turn will proceed after the server receives a response"；决策 `accept`/`decline`/… | **是**：`ServerRequest.json:1906/1931`；params required `itemId,startedAtMs,threadId,turnId`；决策枚举含 `accept`/`acceptForSession`/`acceptWithExecpolicyAmendment`/`applyNetworkPolicyAmendment`/`decline`/`cancel` | **部分（仅桩）**：审批被**显式**回答、沉默不算批准、拒绝后回合继续、不可重复回答、结构化决策被拒 |
| **turn 中断** | **是（但只承诺"请求"）**：`turn/interrupt {threadId, turnId}`，"success is an empty `{}` response and the turn finishes with `status: \"interrupted\"`"；"The server **requests** cancellations for running subprocesses, then emits a `turn/completed` … Rely on the `turn/completed` to know when Codex-side cleanup is done" | **是**：`ClientRequest.json:10356`、`v2/TurnInterruptParams.json` required `threadId,turnId`、`v2/TurnInterruptResponse.json` 为**空对象**（无 description、无保证） | **部分（仅桩）**：桩上验证"接受 ≠ 已停"的判定与名额保留；**真实是否真停未验证** |

**关键结论**：四项**全部有文档支持且本机接口存在**，但**没有任何一项在真实 app-server 上验证过行为**。
按 AGENTS.md，适配器**不得**据此宣称"支持取消/支持拦截"。

### 2.1 传输与握手（本轮已按官方口径确定）

| 项 | 结论 | 依据 |
|---|---|---|
| stdio 编码 | **JSONL**（一行一个 JSON 对象）。客户端与 `StdioTransport` 的 framing 默认值都是 JSONL；`FRAMING_AUTO`（嗅探首行）与 LSP 头**仅作诊断**保留，**不用于真实服务器** | 本机 `--help` 给出传输（默认 `stdio://`、`unix://`、`ws://`）；编码按官方 JSONL 约定 |
| 握手顺序 | **`initialize` 请求 → `initialized` 通知**，两步缺一不可；**握手完成前禁止**发送 `thread/*` 与 `turn/*` | 本机 schema：`ClientNotification.json` 的 `oneOf` **只有一个**变体——`initialized`（仅 `method`，无 params）；`InitializeParams` required `clientInfo`（`ClientRequest.json:1861/1878`） |
| 顺序由谁保证 | **两侧都保证**：客户端在握手完成前**本地拒绝**（抛 `HandshakeRequired`，请求不上线）；桩服务在收到 `initialized` 之前对 `thread/*`、`turn/*` 回 `-32002` | 原型实现 + 离线测试（含两个反面对照，见 §5） |

**因此修正此前的两处表述**：

1. ~~"framing 未说明，只能自动识别"~~ → **官方规定 stdio 为 JSONL**，默认即 JSONL；自动识别降级为诊断选项。
2. ~~"initialize 可选、是否必须未验证"~~ → **握手是必须的两步**（`initialized` 是协议里唯一的客户端通知），
   原型与桩都强制它。仍有待真实观测的是：**真实服务器是否也强制**（S1 就是为此设计）。

### 2.2 审批决策：本客户端只发送三种

`SUPPORTED_DECISIONS = {accept, decline, cancel}`。**`acceptForSession` 与结构化决策
（`acceptWithExecpolicyAmendment`、`applyNetworkPolicyAmendment`）一律不发送**，
理由：它们改变的是**后续命令**是否需要提示（会话级/策略级放行），比"回答一次提示"大得多。
拒绝在**三个入口**都强制，并有测试：① 直接应答 `answer_approval`；② 策略构造（`ApprovalPolicy`
的 `default_decision`/`rules` 在 `__post_init__` 里校验）；③ 自动应答 `auto_answer_pending`
（策略被事后篡改也会在 `decide()` 处被拒）。

## 3. 与 `codex exec` 的对照（含一处重要更正）

`codex exec --json` 的官方事件/条目分类（`exec_events.rs`，版本线与本机一致）：

* 顶层事件：`thread.started`、`turn.started`、`turn.completed`、`turn.failed`、`item.started`、`item.updated`、`item.completed`、`error`
* 条目类型：`agent_message`、`reasoning`、`command_execution`、`file_change`、`mcp_tool_call`、`collab_tool_call`、`web_search`、`todo_list`、`error`
* `file_change`："The item is emitted only as a completed event **once the patch succeeds or fails**"；`command_execution`：命令被派生时开始、进程退出时完成

**更正（我此前的推断过强）**：只读拒写探针那次真实运行只出现 4 个事件
（`thread.started`/`turn.started`/`item.completed(agent_message)`/`turn.completed`）。
官方文档表明**条目只在实际发生该活动时才发出**，因此"没有 `file_change`/`command_execution` 条目"
**与文档行为一致**，**不能**据此推断"本机版本与文档存在差异"，
也**不能**据此推断"该模式下工具事件一律不可观测"。
准确表述仍是：**本次探针未取得实际尝试及拒绝证据，无法判断**。

**version-check 清单**（需要真实调用才能回答，本轮不做）：
1. 本机 `exec --json` 是否会出现 `item.started`/`item.updated`（或只有 `item.completed`）；
2. 用一个**明确要求执行命令**的提示词能否产生 `command_execution` 条目；
3. `collab_tool_call` 需多 Agent（我们禁用了，缺席属预期）；
4. 重新生成 `generate-json-schema` 与 `var/codex-schema` 比对决策枚举；
5. `turn/interrupt` 的真实终态与**子进程是否真的停止**（文档只承诺"请求"）。

## 4. 与现有内核的衔接（设计）

**原则：复用现有门禁，不新增第二个放行点。**

```
控制台/API → 内核 Worker
    Phase 1  PolicyService.consume()            ← 唯一放行点（审批 + 配额，已提交）
             └ 产出 gate_token
    Phase 2  传输层启动一次 turn
             ├ exec 适配器：同步 Popen（现状，保留）
             └ app-server 适配器（原型）：thread/start（或 thread/resume）→ turn/start
    Phase 3  依"终态语义"结算
             terminal=True  → SUCCEEDED/FAILED/CANCELLED，释放名额一次
             terminal=False → UNKNOWN，**保留名额**（人工裁决）
```

具体规则（原型已实现为纯函数，可直接复用）：

| 事件 | 判定 | 名额 |
|---|---|---|
| `turn/completed` 且 `turn.status = completed` | `finished`，terminal | 释放 |
| `turn/completed` 且 `turn.status = failed` | `failed`，terminal | 释放 |
| `turn/completed` 且 `turn.status = interrupted` | `cancelled`，terminal | 释放 |
| `turn/completed` 但 status 为 `inProgress`（非法/未完成） | unconfirmed | **保留** |
| `turn/interrupt` 返回 `{}`（成功应答） | unconfirmed（**接受 ≠ 已停**） | **保留** |
| 传输断开 | unconfirmed | **保留** |
| 超过等待时限 | unconfirmed | **保留** |

**工具审批单独处理，绝不自动批准**：

* 服务端→客户端的审批请求由客户端回答；原型的策略**默认是 `decline`**，
  只有调用方显式给出决策才可能 `accept`；
* `acceptForSession` 与结构化决策（`acceptWithExecpolicyAmendment` /
  `applyNetworkPolicyAmendment`）**本原型不支持**：它们会为**后续**命令持久化策略，
  比"回答一次提示"大得多，不能顺手发生（`SUPPORTED_DECISIONS` 有测试断言）；
* 该审批与"批准 Agent 运行"是**两次独立同意**（AGENTS.md：批准运行 ≠ 批准工具操作）。

**会话恢复**：`thread/resume{threadId}` + `turn/start`，与现有 `invocations.adapter_invocation_id`
（我们存的就是 thread id）天然衔接；**恢复行为本身未验证**，因此原型只提供方法、不做承诺。

**中断**：`turn/interrupt` 只作为**请求**发出；调用方必须等 `turn/completed` 的终态。
真实行为未验证前，适配器 `cancel` 继续返回 `unsupported`（现状不变），
控制台继续显示"取消：不支持"。

## 5. 原型（本轮交付）

| 文件 | 作用 |
|---|---|
| `packages/adapters/agenthub_adapters/appserver.py` | 最小客户端：**两种 framing 自动识别**（`Content-Length` 或按行 JSON —— `--help` 只写了 `stdio://`，**没写 framing**，所以不猜）、请求 id 精确匹配、通知队列（保序）、服务端请求（审批）收集与显式应答、断开/超时分类、以及内核面向的判定函数与 `AppServerSession` |
| `packages/adapters/agenthub_adapters/appserver_stub.py` | 脚本化桩服务：12 个场景（happy / 审批允许·拒绝·沉默 / 中断接受·终态 / 断开 / 超时 / 重复 id / 乱序通知 / 回合失败 / LSP framing），**方法名与载荷形状照本机 schema** |
| `tools/demo/stub_appserver.py` | 把桩服务跑在 stdio 上，供手工驱动（`--list` 列场景） |
| `tests/adapters/test_appserver_client.py` | **39 项离线测试**（见下） |

**保持现状**：`codex exec` 适配器一行未改，仍是默认；app-server 原型是**可选第三件**，
未接入 `build_adapter()`（避免在真实行为未验证前改变生产路径）。

### 离线测试覆盖（实际运行，全部通过）

| 主题 | 覆盖 |
|---|---|
| 请求 id 匹配 | 两个并发请求各得其响应；**未知 id 的迟到/重复响应被丢弃**（不会被算作别的调用） |
| 事件顺序 | 通知可以**先于**引起它的响应到达；通知之间**不重排** |
| 消息增量 | 按到达顺序拼接得到完整文本；`item/started → item/completed` 生命周期 |
| 审批 | 请求被呈现且**未预先应答**；`accept` 后回合完成；`decline` 后回合**继续**；沉默**不算批准**且策略默认 `decline`；不可重复应答；非法/结构化决策被拒 |
| 中断终态 | `interrupt` 返回 `{}` 后无终态 → **unconfirmed**；随后 `turn/completed(status=interrupted)` → `cancelled` 且 terminal；`inProgress` **不是**终态 |
| 断线 / 超时 | 中途断线 → unconfirmed 且后续请求报 `AppServerDisconnected`；无终态 → 超时 unconfirmed |
| 握手 | `initialize` + `initialized` **两步都要**；握手前 `thread/*`/`turn/*` 本地拒绝且**不上线**；桩对跳过通知的客户端回 `-32002` |
| 审批决策 | 仅 `accept`/`decline`/`cancel`；**`acceptForSession` 在三个入口都被拒**（直接应答、策略构造、自动应答），且未发出任何应答 |
| framing | JSONL 为默认；LSP 与嗅探仅作诊断；垃圾输入报 `AppServerProtocolError`；空流报断线 |

运行命令（worktree 内没有 `.venv`，用绝对解释器）：

```powershell
$py = 'E:\plug\agent-hub\.venv\Scripts\python.exe'
cd E:\plug\agent-hub\.worktrees\appserver-proto
& $py -m pytest tests/adapters/test_appserver_client.py -q
& $py -m pytest -q            # 全套 343 passed
& $py tools\demo\stub_appserver.py --list
```

## 6. 未验证 / 缺口（不得当作已支持）

1. **真实 app-server 会话从未启动**：framing、握手（schema 里有 `initialize`/`InitializeParams`，
   `clientInfo` 必填 —— **是否需要、何时需要，未验证**）、方法名是否与本机版本一致，全部未验证。
2. **本环境可能根本跑不了 stdio 传输**：本沙箱拒绝原生进程的**管道** stdio
   （`StdioTransport` 因此未被离线执行）。真实验证需要在允许管道的终端里做。
3. 通知**顺序/时序/原子性**无任何文档承诺（本轮只验证"客户端不重排"）。
4. `turn/interrupt` 无停止保证、无幂等/错误语义、清理时延无上界。
5. 审批：无超时/不回复时服务端行为、`availableDecisions` 是否总给出、某决策是否真被采纳，均无文档。
6. `thread/resume` 的"是否重放未决 item 请求/是否发 `thread/readState/changed`"在文档不同版本间有差异。
7. `--experimental` 的 schema dump 里含 EXPERIMENTAL/UNSTABLE 字段（如 `experimentalRawEvents`），
   **在 dump 中存在 ≠ 非 experimental server 上存在**。
8. 桩的 `turn/completed` 载荷形状是照 schema 写的；真实载荷的**字段完整性**未验证。

## 7. 下一轮验收计划（需要真实调用，逐次批准）

### S1（脚本已就绪；本轮已完成离线正反对照）

```bat
:: CMD 一行；worktree 内没有 .venv，故用绝对解释器
cd /d E:\plug\agent-hub\.worktrees\appserver-proto && E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\appserver_s1.py --mode real --i-understand-this-starts-an-app-server-process --codex-home "%LOCALAPPDATA%\AgentHub\codex-home"
```

* **允许**：启动 **1 个** app-server 进程（固定路径 + sha256/size 校验通过）、完成握手、
  创建**最多 1 个空 thread**，然后停止。
* **禁止（线级断言；违反即 FAIL 并立即停止）**：任何 `turn/start`（因此**没有任何模型回合、不消耗配额**）、
  `turn/steer`/`turn/interrupt`、`thread/fork`、`command/exec`、`process/spawn`、
  任何审批请求、第二个 thread。
* **证据**：子进程 stderr 落文件；线级 transcript 打印并保存到
  `var/appserver-s1/<run>/wire-transcript.json`。
* **失败即停**：第一项检查失败即退出，不重试、不放宽。
* **清理**：只终止**本脚本自己启动**的进程（持有的句柄）；`--clean` 只删本次运行目录，
  并带守卫（**拒绝**删除 `var/appserver-s1/` 之外、或位于 `%LOCALAPPDATA%`/`%USERPROFILE%`
  之内的路径）；**长期 `CODEX_HOME` 与用户 `~/.codex` 一律保留**。
* **离线反面对照（已执行）**：①握手前请求 `thread/start` → 本地 `HandshakeRequired` 且**不上线**；
  ②只发 `initialize` 不发 `initialized` → 服务端回 `-32002`。

| 步骤 | 内容 | 真实回合 | 需要你批准的点 |
|---|---|---|---|
| **S1** | 握手 + 一个空 thread（见上） | **0** | 允许启动 app-server 进程 |
| S2 | 一次最小回合：验证 `turn/started → item/agentMessage/delta → item/completed → turn/completed` 的真实顺序与载荷 | 1 | 1 次真实回合 |
| S3 | 审批往返：要求执行无害命令并在真实审批请求上 `decline`，确认"拒绝后回合继续" | 1 | 1 次真实回合 + **不批准执行命令** |
| S4 | 中断：长任务 + `turn/interrupt`，测真实终态与**子进程是否真的停止**（此前 `cancel` 一直返回 `unsupported`） | 1 | 1 次真实回合（可能进入 unknown 并保留名额） |
| S5 | 恢复：用真实 `thread_id` 走 `thread/resume` + `turn/start` | 1 | 1 次真实回合 |
| S6 | 通过后再考虑把 app-server 接入 `build_adapter()` 作为可选传输（默认仍是 exec） | 0 | 代码评审 |

**每步的判定与清理**沿用现有口径：只有明确终态才释放名额，否则 `unknown` 并保留；
清理只删本轮探测/验证自己创建的运行目录，**长期 `CODEX_HOME` 与用户 `~/.codex` 一律保留**。

## 8. 引用来源

* 官方（openai/codex 仓库文件）：
  [`codex-rs/app-server/README.md`](https://raw.githubusercontent.com/openai/codex/8fe5066bcc85f625a1fe161027f7365b8bb11f88/codex-rs/app-server/README.md)、
  [`codex-rs/exec/src/exec_events.rs` @rust-v0.162.0](https://raw.githubusercontent.com/openai/codex/rust-v0.162.0/codex-rs/exec/src/exec_events.rs)
* 本机捕获：`var/codex-schema/`（`generate-json-schema --experimental`）、`var/codex-help-app_server.txt`
* 第三方镜像（**不作为官方依据**）：`mintlify.wiki/openai/codex/api/*`（该页的 exec 形状与官方源码冲突，属过期）
* **无法取回**：`developers.openai.com/codex/app-server`（DNS 失败）、`github.com`（DNS 失败）
