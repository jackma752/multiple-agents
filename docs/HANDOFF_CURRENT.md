# 交接：当前状态与下一步（app-server 原型）

日期：2026-10-14。用途：**交接给下一轮**。只记录事实与可执行步骤，不含未验证的结论。

---

## 1. 分支 / worktree / 提交

| 项 | 值 |
|---|---|
| 工作分支 | `feat/appserver-proto` |
| worktree | `E:\plug\agent-hub\.worktrees\appserver-proto` |
| 当前提交 | **`5ecc330eb69a42d80960fd0ab6c3e0d06fcc8762`**（`fix(appserver): mandatory two-part handshake, JSONL stdio, no session-scoped consent`） |
| 上一提交 | `31f5b06`（可行性与最小原型） |
| 工作树 | **干净**（无未提交改动） |
| remote | **无**；**未合并、未推送** |
| 其它分支 | `feat/p3-wrapup` @ `42a61bb`（P3 口径修正）、`feat/p3-codex-probe` @ `8b24d0a`、`main` @ `1d4a303` 均未改动 |
| 解释器 | `E:\plug\agent-hub\.venv\Scripts\python.exe`（**worktree 内没有 `.venv`，必须用绝对路径**） |

## 2. 已完成内容（本轮范围）

### 2.1 两处指定修复（已在 `5ecc330` 中，无需重做）

| 修复 | 实现位置 | 行为 |
|---|---|---|
| **`initialize` → `initialized` 两步握手强制** | `appserver.py:58`（`HandshakeRequired`）、`:626`（`INITIALIZED = "initialized"`）、`AppServerSession.initialize/_require_handshake` | 握手完成前 `thread/start`、`thread/resume`、`turn/start`、`turn/interrupt` **本地拒绝**且**请求不上线**（测试断言服务端 `received == []`）；`AppServerSession.initialize()` 先发请求、再发 `initialized` 通知，之后才置 `handshake_complete=True`。依据：本机 schema `ClientNotification.json` 的 `oneOf` **只有一个**变体 `initialized` |
| **禁止 `acceptForSession`** | `appserver.py:665` `SUPPORTED_DECISIONS = frozenset({"accept", "decline", "cancel"})`，`ApprovalPolicy.__post_init__/validate/decide`，`answer_approval` | 三个入口全部拒绝：①直接应答 ②策略构造/事后篡改（`rules` 被改也会在 `decide()` 处拒）③自动应答 `auto_answer_pending()`；每个入口都断言"未发出任何应答"。理由：它放宽的是**后续**命令的提示 |

桩服务同样强制握手：未收到 `initialized` 前的线程/回合请求回 **`-32002`** 并记录违规（`appserver_stub.py`，字段 `initialize_seen`/`initialized_seen`/`handshake_violations`/`handshake_ok()`）。

### 2.2 本轮其余已完成项

* **stdio 明确为官方 JSONL**：`AppServerClient` 与 `StdioTransport` 的 framing **默认值均为 JSONL**；LSP 头与嗅探（`FRAMING_AUTO`）降级为**显式诊断**，不会自动选中。修正了文档中"framing 未说明""initialize 可选"两处错误表述。
* **S1 验证脚本**：`tools/probes/appserver_s1.py`（`--list` 可先审阅），含真实模式与 stub 模式。
* **可行性文档**：`docs/appserver-feasibility.md`（三层证据分类表、内核衔接设计、原型说明、S1–S6 验收计划、引用来源与取证限制）。
* **原型**：`agenthub_adapters/appserver.py`（客户端/会话/判定）、`agenthub_adapters/appserver_stub.py`（12 场景桩）、`tools/demo/stub_appserver.py`（stdio 桩）。
* **`codex exec` 适配器一行未改**，仍是默认；app-server **未**接入 `build_adapter()`。

## 3. 实际测试结果

| 命令 | 结果 |
|---|---|
| `python -m pytest tests/adapters/test_appserver_client.py`（本轮唯一一次运行，按要求只跑受影响范围） | **39 passed in 1.34s** |
| 上一轮完整套件（`5ecc330` 提交前最后一次全量运行） | **343 passed** |
| `tools/probes/appserver_s1.py --mode stub`（离线正反对照，已实跑，exit 0） | 正面：握手 + 1 个空 thread 全 PASS；反面①握手前请求→本地 `HandshakeRequired` 且 `received=[]`；反面②只发 `initialize`→服务端 `-32002` |

命令（worktree 内）：
```bat
cd /d E:\plug\agent-hub\.worktrees\appserver-proto
E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest tests\adapters\test_appserver_client.py -q
```

## 4. 尚未修完 / 未验证的问题

**代码层面（已知、未修）**

1. **真实 app-server 从未运行**：framing 是否真为 JSONL、握手是否被服务器强制、`thread/start` 的真实载荷，全部**未验证**。
2. **`StdioTransport` 未经离线执行**：本沙箱拒绝原生进程的**管道** stdio，因此真实传输路径只在代码层写就，未被跑过；stderr 已落文件句柄（可用），但 stdin/stdout 管道需要在允许管道的终端里验证。
3. **内核未接入**：`build_adapter()` 仍只返回 Mock/Codex(exec)；app-server 的 `AppServerSession` 尚未与 `PolicyService.consume()` / 配额门禁串联（设计已写在 `docs/appserver-feasibility.md` §4，实现未做）。
4. **`turn/steer`、`review/start`、`thread/fork`、`thread/list`、`thread/read`** 未在客户端提供。
5. **审批等待语义**未测：服务端在客户端不回复时的行为（文档未承诺）、`availableDecisions` 是否总给出。
6. **中断的真实效果**未测：`turn/interrupt` 只被文档描述为"请求"，适配器 `cancel` 仍返回 `unsupported`（正确现状）。

**能力结论（保持未验证，不得改口径）**

* 只读拒写：**未验证**；那次真实探测结果为 **INCONCLUSIVE**（探针未取得实际尝试及拒绝证据）。
* 取消、工具启动前拦截、原生子 Agent 阻断：**未验证**。
* `0xc0000142` ×2：`docs/issue-python-0xc0000142.md`、`docs/issue-codex-0xc0000142.md` **均保持 OPEN**。

**环境限制（如实记录）**：`developers.openai.com` 与 `github.com` 在本机 DNS 失败；官方依据取自 `raw.githubusercontent.com` 的官方仓库文件；`mintlify.wiki` 镜像**不作官方依据**（其 exec 形状与官方源码冲突）。

## 5. S1 准备进度与下一步准确操作

**进度**：脚本与离线正反对照**已完成**；真实模式**尚未执行**（本轮明确不执行）。S1 不产生任何模型回合。

**下一步（由你手工执行，一行 CMD）**：

```bat
cd /d E:\plug\agent-hub\.worktrees\appserver-proto && E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\appserver_s1.py --mode real --i-understand-this-starts-an-app-server-process --codex-home "%LOCALAPPDATA%\AgentHub\codex-home"
```

* 若想顺带删除本次运行目录，末尾加 ` --clean`（默认保留证据）。
* **脚本会做**：校验可执行文件路径 + sha256 + size → 校验 `CODEX_HOME` 是目录、有 `auth.json`、本进程可写 → 启动 **1 个** `codex.exe app-server --stdio` → `initialize` + `initialized` → `thread/start` **最多一次**（cwd 为本次专用空目录）→ 线级断言禁止项 → 终止自己启动的进程。
* **脚本不会做**：任何 `turn/start`（**0 次模型回合、不消耗配额**）、`thread/fork`、`command/exec`、审批请求、第二个 thread；不重试；不放宽。
* **证据位置**：`var/appserver-s1/<run>/appserver-stderr.log`、`var/appserver-s1/<run>/wire-transcript.json`。
* **清理边界**：只终止本脚本启动的进程；`--clean` 仅删本次运行目录，且拒绝删除 `var/appserver-s1/` 之外或位于 `%LOCALAPPDATA%`/`%USERPROFILE%` 内的路径。**长期 `CODEX_HOME` 与用户 `~/.codex` 一律保留。**

**S1 之后（逐次批准，勿跳步）**：S2 一次最小回合（验证真实事件顺序与载荷）→ S3 审批往返（真实审批请求上 `decline`，不批准执行命令）→ S4 中断（测真实终态与子进程是否真停）→ S5 `thread/resume` + `turn/start` → S6 通过后才考虑把 app-server 接入 `build_adapter()` 作为可选传输。详细判定与批准点见 `docs/appserver-feasibility.md` §7。

## 6. 交接注意事项（硬性）

1. **不要**在真实模式跑 S1 之外的东西；S2+ 每次都要单独批准。
2. **不要**把 `acceptForSession` / 结构化决策加成"支持"——它们被刻意排除。
3. **不要**把"文档支持 + 本机接口存在"写成"行为已验证"；三层证据必须分开。
4. **不要**删除长期 `CODEX_HOME` 或 `~/.codex`；不要在未确认停止的情况下释放配额。
5. 提交前至少运行受影响范围的测试；未经批准不合并、不推送。
