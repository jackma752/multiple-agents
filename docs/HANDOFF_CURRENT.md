# 当前交接：严格执行准入修复（2026-10-15）

## 当前工作目录

- 分支：`fix/strict-adapter-gate`
- 路径：`E:\plug\agent-hub\.worktrees\strict-adapter-gate`
- 基于 app-server 交接提交 `f364aea4b966544282fcf16f7bd0621228e7b971`；本文件所在提交即本轮交付。
- 解释器：`E:\plug\agent-hub\.venv\Scripts\python.exe`。
- 原主目录、其它分支、长期凭据、真实数据库和现有服务均未更改；未合并、未推送。

## 本轮完成

1. 按计划 §4.3 增加 `ExecutionAdmission` 契约：未知或未通过控制要求的适配器默认拒绝。Worker 在审批消费/配额预留之前检查；Codex 直接 `start()` 再检查。当前真实 Codex 不可启动；Mock 与固定离线桩可以继续验证。
2. API 返回明确准入原因；UI 不再把“传入禁用参数”显示成“子 Agent 已禁用”。历史真实调用工具也在接触凭据/启动进程前拒绝，避免侧门。
3. 复核原始 rollout 第 12 行，纠正漏报：子 thread 为 `01a13b43-e437-7c82-8f45-8deeeaa40082`，路径 `/root/standby`。现状仍未知，未关闭、未删除记录，也未将历史父调用改写为 unknown。
4. 按用户要求检索 GitHub 的 Codex 相关报告与 Vibe Kanban 执行器；只阅读参考，未引入第三方代码。来源和边界在 [实现说明](p3-strict-adapter-admission.md)。

## 验证

- 全量离线测试：`python -m pytest -o addopts='' -q` → **363 passed in 15.57s**。
- 最后整理后的受影响范围：准入、历史工具拒绝及停止测试 → **31 passed in 1.51s**。
- 新增准入/工具回归覆盖共 20 项；首次全量发现旧 NoCancel 测试桩未声明准入，已让该惰性测试桩显式声明 simulation，继续验证原 tree-stop 拒绝路径。
- `codex_verify.py --mode stub --codex-home <本轮独立临时目录> --port 8151` → **12/12 PASS，VERIFICATION PASSED**。只运行本仓库 Python 桩，无真实模型/用量。临时 HTTP 服务已由验证脚本关闭。
- app-server 代码未改；本轮没有启动真实 app-server 或真实 Codex，也未读取/复制凭据内容。

## 启动本轮版本（CMD）

**旧 8080 服务不会自动获得本轮修改。** 新版本独立端口和独立数据库用于验收：

```bat
cd /d "E:\plug\agent-hub\.worktrees\strict-adapter-gate" && "E:\plug\agent-hub\.venv\Scripts\python.exe" tools\demo\serve.py --adapter mock --port 8150
```

打开 `http://127.0.0.1:8150/ui`，体验 Mock 审批/调度流程。若改为 `--adapter codex`，页面应显示“已阻止新调用”，直接 HTTP drain 也会拒绝真实启动。不要继续在旧 8080 服务发起真实调用。

## 下一步与未决

下一项是 app-server 的真实 S1（0 个模型回合），具体方案仍见下方历史交接；S2+ 每个真实模型回合需单独批准。**即使 app-server 握手成功，也不能恢复严格执行准入**；必须先验证原生派生可关闭或完整受控，工具权限/终态也不能用协议存在代替行为验证。

SSE、会话恢复、第二个真实 Agent 尚未实现；历史子 Agent 停止状态未知；Python/Codex 的 `0xc0000142` 两条记录均 OPEN。未合并前，后续开发从本分支继续，不要回到主目录的旧实现。

---

# 历史交接：app-server 原型（以下为 2026-10-14 记录）

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
