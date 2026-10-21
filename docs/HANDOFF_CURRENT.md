# 当前交接：S3 执行前复核与身份重新固定（2026-10-22）

- 在 `1fee92e`（10-18 暂存工作的 wip 提交）之上复核：离线测试、13 个 stub 场景、拒绝路径全部符合文档。
- **直接阻塞**：S1/S2 固定的 Codex 文件已被 10-19 的自动更新删除。S3 单独重新固定为当前安装文件（`896a0166b774037a`，SHA-256 `f6dbf48c…`，341312816 字节，OpenAI 签名有效，被动核验）；S1/S2 身份保持不变。新增零模型身份检查脚本 `tools/probes/appserver_s3_identity.py`。详见 [S3 方案](appserver-s3-probe.md)。
- 已按用户的一次性授权执行：步骤 A 为 COMPATIBLE（`codex-cli 0.163.0-alpha.10.2`，21/21 项）；步骤 B 结果为 **FAIL**（越界）：模型在任何审批请求之前就开始文字回复，探针中止，没有发出任何响应，也没有执行证据，终态未确认。拒绝链没有被触发。详见 [S3 实录](appserver-s3-2026-10-22.md)。授权已用完，没有重试；S4/S5 未运行。
- 判定修订（离线）：文字不再中止、也不决定结论；`text_format`、`approval_chain`、`terminal` 三部分分别记录，并带原因代码。S1/S2 的规则没有改动。修订测试共 21 项，相关回归 385 passed。详见 [S3 方案](appserver-s3-probe.md) 的修订一节。新的单次申请等待用户批准。

---

# 历史交接：S3 离线工具完成，等待单次真实授权（2026-10-18）

- 分支 `feat/appserver-s3-probe`，基于 `023d0ab`；目录 `E:\plug\agent-hub\.worktrees\appserver-s3-probe`。
- 解释器 `E:\plug\agent-hub\.venv\Scripts\python.exe`，worktree 内无 .venv。
- S1/S2 已完成，**不得重跑**。S2 一次性授权已使用；本轮仅完成 S3 编码、桩进程与离线测试，未启动真实 Codex 或读取凭据。
- 新工具 `tools/probes/appserver_s3.py`：新 thread、固定 read-only/on-request/user reviewer，回读有效设置后才允许一次固定主回合；唯一命令审批响应为匹配请求的 decline，所有 accept/会话策略修订均不可发送。
- 判定要求实际审批链、完整发送的 decline、resolved、匹配 declined item、拒绝后的最终 DECLINED/增量/用量/终态及空目录；自述/缺证据不可 PASS。详细固定提示词、命令、风险与清理见 [S3 方案](appserver-s3-probe.md)。
- 修复复核发现的三处缺口：未回读 effective reviewer/sandbox；最终回复可能早于拒绝；未区分初始命令与 writeStdin/子命令回调。非空 exitCode 可能是合成拒绝码，单独记录，不伪称命令已执行。
- 新增 64 测试，全套 **553 passed in 34.56s**。独立 stub CLI → S3 PASSED；13 场景含权限回读反例，另验证非空工作目录保留。用量均为合成 fixture。
- 下一步：取得一次真实 S3 的单独批准后，执行方案中的固定命令；失败即停，无重试。上轮 S2/笼统“继续”不扩大此前逐次批准的限制。
- 本工具独立于生产内核/数据库，无生产配额行；缺匹配终态时 unconfirmed、人工核对，不能宣称保留生产配额或已停远端。
- 严格真实准入仍关闭。旧 8080、共享 daemon、历史子 Agent、长期 home 均不处理；两项 DLL 记录保持 OPEN。未合并、未推送，无 remote。

---

# 历史交接：S2 工具与一次真实验证完成（2026-10-17）

- 分支：`feat/appserver-s2-probe`；基于 `bdc6b40`，工具提交 `f65bca8`，本提交归档真实结果。
- 目录：`E:\plug\agent-hub\.worktrees\appserver-s2-probe`；解释器 `E:\plug\agent-hub\.venv\Scripts\python.exe`。
- S1/S2 已通过，**不要重跑**。S2 准备完成后用户另行批准一次，已执行并 PASS。工具未读取/复制/删除凭据内容；Codex 自身复用登录并可能写入专用 home 运行状态。
- 普通执行准入仍默认拒绝真实 Codex；本探针未接入内核或生产数据库。原目录、8080 服务及其他分支未动，未合并/推送。

## 本轮成果

独立 `tools/probes/appserver_s2.py` 在发送前固定握手、新 thread 和一个固定提示词 turn，真实模式必须同时确认一次主回合与控制风险。禁止额外 turn、恢复、fork、工具审批响应；部分写入也不能重试。证据归集器精确匹配 thread/turn/item，分别判断匹配终态与本地进程退出；保存独立 usage 通知，缺少证据不补造。仓库外空临时工作目录仅在确认可安全清理时非递归移除，长期 home 不删除。

复核修复：额外消息 item 的 delta 被忽略、turn/started 未检查状态、finally 中工作目录检查异常丢失审计。均有回归。

## 实际验证

- 新增 93 项测试，最终全量 **489 passed in 26.59s**。
- 八个真实 Python 子进程桩场景：happy PASS；failed/no-usage/foreign-turn/approval/tool/disconnect/timeout 均正确拒绝，所有本次自有桩进程退出。
- 独立 CLI `--mode stub` → S2 PASSED；证据 `var/appserver-s2/stub-gxqegnrt/result.json`，仅合成用量。
- 真实 S2 → **S2 PASSED**，恰好一次 turn/start；最终 OK、同一 item 增量拼接 OK、匹配终态、服务端用量；自有 PID 7692 退出码 0，仓库外空临时目录已清理。证据 `var/appserver-s2/real-5j0vung8/result.json`，完整身份/授权/ID/用量见 [真实记录](appserver-s2-2026-10-17.md)。

## 下一步

用户在 S2 工具离线准备完成后明确回复“批准这一次 S2，接受上述限制”。此次一次性授权已用完。**下一步准备 S3 工具审批探针及离线正反对照，再申请一次真实 S3；不得重跑 S2或直接执行 S3。** [S2 方案](appserver-s2-probe.md) 中的真实命令只供审计，不是持续授权。

此探针不能保证工具或原生派生事前受控；历史 `--disable multi_agent` 绕过仍是实证。确认 S2 不等于解除 Hub 准入。没有匹配终态则标记 unconfirmed，保留证据、人工核对；此次终态已确认，无需该项裁决。它不占用生产内核名额，不能称“已保留生产配额”。继续保留历史子 Agent 状态未知、两项 DLL 问题 OPEN。

S3 审批、S4 中断、S5 恢复以及 SSE/第二个真实 Agent 尚待完成；本次不预授权这些回合。

---

# 历史交接：app-server S1 真实验证通过（2026-10-17）

## 分支与目录

- `fix/appserver-s1-verification`，基于 `fix/strict-adapter-gate` 的 `b6e6c88`。
- worktree：`E:\plug\agent-hub\.worktrees\appserver-s1-verification`。
- Python：`E:\plug\agent-hub\.venv\Scripts\python.exe`。
- 本次提交即代码/文档交付。原目录和其它分支未切换；未合并、未推送、未重启 8080。

## 已完成与真实边界

1. S1 发送前白名单、精确 home 预检、独占随机写探针、脱敏结果、reader join 与可信进程清理；修复 QueueStream 分片忙等及 stdio 短写。
2. 内存桩与真正 Python 子进程管道均已验证，`--mode process-stub` → `S1 PASSED`，自有进程退出。
3. 旧 Codex 固定文件不存在，首次仅预检失败（启动 0）；用户另行明确授权后，核验现装文件并建立 **S1 专用身份**，只启动 **一次** app-server，真实 S1 PASS，**模型回合 0**。
4. thread `01a1469e-eb8b-7dd1-a645-ffa3c3134b27`；发送恰为 `initialize → initialized → thread/start`，本次进程 PID 13632 已退出，退出码 0。证据 `var/appserver-s1/s1-real-euxv5bb_/result.json`。
5. 长期 home 保留；工具未读取/复制/删除凭据内容，Codex 自身复用登录并可写运行状态。没有触碰历史 DB 或清理共享 daemon。

完整身份、权限审核、命令、输出与限制见 [S1 实录](appserver-s1-2026-10-17.md)。现装文件的版本字符串和旧版本相同，但哈希不同；**exec 旧身份与后端拒绝真实调用的规则完全保留**。

## 验证

- 新增 33 项（S1 18 + stdio/QueueStream 15），相关三文件 **72 passed in 1.80s**。
- 全量 **396 passed in 16.07s**。
- 首次全量为 395 passed / 1 failed，原因是新测试用 Windows 默认 GBK 读取 UTF-8 JSON；已显式指定 UTF-8 后全量通过。
- `tools/probes/appserver_s1.py --mode process-stub`：PASS；真实 S1：PASS（以上单次，没有重试握手）。

## 下一步

**S1 已完成，不要重复运行。** 接下来准备 S2 单回合探针及离线正反对照，再单独申请一次真实模型回合。用户此次授权仅到零模型 S1，不包含 S2。现有 S1 工具按设计拒绝 `turn/start`，不得去掉它的白名单以凑通 S2。

S2 验证真实回合事件与载荷；S3 工具审批；S4 中断和确切终态；S5 thread 恢复；S6 满足严格控制条件后再接内核。app-server 尚未接入 `build_adapter()`。原生子 Agent 控制未解决，S1 不能作为恢复真实执行准入的依据。

SSE、会话恢复、第二个真实 Agent 仍未完成；历史原生子 Agent 状态未知；两项 `0xc0000142` 记录继续 OPEN。保持长期 home、现有会话和审计证据。勿将本地 app-server 进程已退出外推为远端任务、共享 daemon 或历史子 Agent 已停止。

---

# 历史交接：严格执行准入修复（2026-10-15）

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
