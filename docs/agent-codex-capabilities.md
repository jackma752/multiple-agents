# Codex 适配器能力表（P3 受控接入验证 · 第一批）

> **2026-10-17 app-server 独立探针更新**：S1 与经单独批准的一次 S2 均 PASS。S2 验证了同一消息 item 的增量/最终 OK、匹配 turn 终态和独立真实 token 用量，见 [实录](appserver-s2-2026-10-17.md)。现装文件身份与历史 exec 不同，旧版信息仍作历史记录。app-server 未接内核，工具审批/取消/恢复/原生派生控制未升级；普通真实执行准入继续关闭。

> **2026-10-15 结论更新**：当前配置在请求禁用后仍创建了原生子 Agent，故 `control_subagent=false`，
> 不满足严格协作要求，新的真实启动已被后端拦截。以下较早的“待验证”表述是历史记录，
> 以 [最新观测](p3-native-subagent-incident.md) 和 [准入契约](p3-strict-adapter-admission.md) 为准。

日期：2026-10-14。**本表的每一行都来自本机实测的自描述输出或真实调用捕获**，不是记忆或推测。
凡未能实测的一律标 **未知**，不标"支持"。

- 本机 Codex：`C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe`
- 版本：**codex-cli 0.162.0-alpha.17.2**（sha256 `d13914ce…a7daec`，size 334121776）
- 复现命令（自描述，无模型调用）：`.\.venv\Scripts\python.exe tools\probe_codex.py`
  （输出：`var/probe-codex.json`）；本次追加的自描述证据：`codex exec --help`、
  `codex features list`、**`codex debug prompt-input`（渲染模型可见提示词）**、
  **`codex app-server generate-json-schema --experimental`（协议 schema）**、
  以及 parse-time 参数对照（`codex exec <flag> --zzz-bogus-flag`）

> **重要边界**：自描述（`--help` / `features list` / `debug prompt-input` / schema）
> 只能确认"接口与开关**存在**"以及"它们如何被渲染"，**不能确认"行为是否如描述"**。
> 行为验证必须真实调用，且**逐项逐次批准**；方案与判定条件见
> **`docs/p3-permission-probes.md`**。
> 另注：`debug prompt-input` 渲染出的 read-only 与 workspace-write 权限档案**完全相同**，
> 因此离线连"两种沙箱是否不同"都区分不了——这正是必须真实运行的理由。

---

## 1. 计划 §2 要求的 8 项能力

| 能力 | 判定 | 证据 |
|---|---|---|
| 发消息 | **已验证** | `codex exec` 实测**成功**（退出码 0，11.3s），提示词要求"只回复 OK"，最终回复 `agent_message.text = "OK"` |
| 流式输出 | **已验证（成功路径）** | 一次成功运行的完整 JSONL 序列（4 事件）：`thread.started` → `turn.started` → `item.completed`（agent_message）→ `turn.completed`。输出为可解析 JSONL，可被适配器消费 |
| 恢复会话 | **接口存在 + 句柄已实测获得；恢复本身未验证** | `codex exec resume [SESSION_ID] [PROMPT]` / `--last`、`codex resume` / `fork`（自描述确认）；成功调用实测返回 `thread_id = 01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce`。**"用该 id 恢复"尚未实测，适配器也未接入** |
| 取消任务 | **未知 / 当前模式不具备验证条件** | `exec --help` 与 `exec resume --help` 中**没有任何**取消/中断选项；协议里唯一的取消是 app-server 的 `turn/interrupt {threadId, turnId}`，我们不用 app-server。适配器 `cancel` 如实返回 `unsupported`。方案见 `docs/p3-permission-probes.md` §5 |
| 限制文件目录 | **未验证（已做 1 次真实探测：无法判断）** | `-C/--cd`、`--add-dir`、`-s/--sandbox {read-only,...}` 存在；2026-10-14 的真实探测（read-only，1 次调用）**文件未被创建**，但 JSONL 里**没有任何工具/文件类 item**、驱动通道无与写入相关的拒绝证据，助手只说 `REFUSED`（自我陈述，不算证据）→ **无法判断**，能力仍未知。离线也无法区分 read-only 与 workspace-write（权限档案渲染相同；`codex doctor` 曾报 `sandbox backend disabled`）。判定规则见 `docs/p3-permission-probes.md` §1，记录见 `docs/p3-probe-readonly-write-2026-10-14.md` |
| 拦截工具操作 | **未知，且强烈提示"exec 模式不支持"** | 批准通道 `item/commandExecution/requestApproval` 在 schema 中明确写着只服务 **`turn/start`**（app-server）；`codex exec` **拒绝** `-a` / `--ask-for-approval`（parse-time 实测）且没有读决策的输入通道。**未做真实调用实测**（方案见 probes §4；判定规则见 §1） |
| 控制内部子 Agent | **开关可关闭已本地验证；行为未验证** | `multi_agent`（stable, **true**）；`--disable multi_agent` / `-c features.multi_agent=false` 实测可翻为 **false**。**但**：`debug prompt-input` 显示禁用后模型可见提示词里的 `<multi_agent_role>`（含 `spawn_agent` / `followup_task` / `send_message`）**依然存在** → 不是结构性阻断。已禁用但**"是否真能阻止派生"仍未验证**（方案见 probes §3；判定规则见 §1） |
| 返回真实用量 | **已验证** | 成功调用返回 `turn.completed.usage`：`input_tokens=14481`、`cached_input_tokens=12544`、`cache_write_input_tokens=0`、`output_tokens=5`、`reasoning_output_tokens=0`（**真实字段，非推测**） |

---

## 2. 与硬条件相关的特性开关（`codex features list` 实测）

本机共解析到 **167** 个特性开关。与计划硬条件相关的如下：

| 开关 | 阶段 | 当前 | 与计划的关系 |
|---|---|---|---|
| `multi_agent` | stable | **true** | **内部子 Agent / fan-out**；存在可能的关闭手段 |
| `multi_agent_v2` | stable | false | 子 Agent 另一实现 |
| `enable_fanout` | removed | false | 已移除（旧 fan-out） |
| `agent_message_board` | under development | false | Agent 间消息板 |
| `guardian_approval` | stable | true | 审批路由 |
| `write_stdin_approval` | stable | true | 写入 stdin 需审批 |
| `exec_permission_approvals` | under development | **false** | **逐次执行审批（未启用）** |
| `request_permissions_tool` | under development | false | 模型可主动请求权限 |
| `experimental_windows_sandbox` | **removed** | false | **Windows 沙箱** |
| `windows_sandbox_service` | under development | false | Windows 沙箱服务 |
| `network_proxy` | experimental | false | 网络出口控制 |
| `hooks` | stable | true | 钩子 |

### ⚠️ 由此得到的两个重要结论

1. **子 Agent 存在可比 AionUi 更好的线索。** AionUi 审计（P0）的结论是"原生委派只被翻译成
   事件、不可控"。Codex 自身则**存在 `multi_agent` 开关且当前为启用**。
   但**"关闭该开关是否真的能阻止子 Agent 派生"尚未验证**——这正是可能满足计划硬条件 3 的关键，
   必须用真实调用验证。
2. **Windows 上的沙箱能力存疑。** `experimental_windows_sandbox` 与
   `elevated_windows_sandbox` 均为 **removed**，`windows_sandbox_service` 为 under development。
   因此 `-s/--sandbox` 在 **Windows 上是否真正生效未知**——
   计划中"限制可读写目录"这一条**不能仅凭 flag 存在就判定为支持**。

---

## 3. 集成路线对比（均来自实测）

| 路线 | 证据 | 适配器适用性 |
|---|---|---|
| `codex exec`（非交互） | 有 `--json`（JSONL 事件）、`-o/--output-last-message`、`--output-schema`、`--ephemeral`、`resume`/`fork` | **最适合首版适配器**：一次性进程、结构化输出、可恢复 |
| `codex app-server` | 顶层存在，标 `[experimental]`；另有 `codex remote-control`（experimental） | 计划 §2 的优先评估对象；本轮**未验证**其协议 |
| `codex mcp` / `plugin` | 存在 | 后续"Agent 请求中转站协助"路线（MCP 工具）可复用 |
| `codex exec --help` 无 `-a/--ask-for-approval` | 实测：**顶层有，exec 无** | exec 模式下审批策略只能走**配置**（`config.toml` / `-c`），不能靠命令行 flag |

---

## 4. 授权与本地状态

| 项 | 观察 |
|---|---|
| 认证文件 | 存在 `C:\Users\Administrator\.codex\auth.json`（**未读取内容**） |
| 配置 | 存在 `config.toml`（**未读取内容**） |
| 登录状态 | **未知**；`codex login` 子命令存在，`codex doctor` 可诊断（本轮未运行） |
| 沙箱警告 | 每次运行均打印 `failed to clean up stale arg0 temp dirs: 拒绝访问 (os error 5)` 与 `could not create PATH aliases` → **当前沙箱拒绝 Codex 写 `.codex\tmp`**，可能影响真实运行，需在真实调用前确认 |

---

## 5. 真实调用验证计划（**等待用户逐次批准**）

按交接要求"真实调用继续逐次由我批准"，以下每一步都需要用户明确同意。

### 5.1 第 1 步：已获批准并已执行 —— **失败**

**批准范围**：仅一次真实调用，只读沙箱、禁用原生子 Agent、工作区外空临时目录、
失败即停不追加、返回退出码/JSON 事件/用量。

**实际执行**（单次，未重试）：

| 项 | 值 |
|---|---|
| 工作根 `-C` | `C:\Users\Administrator\AppData\Local\Temp\dsh-NDA5C5\codex-probe-20261014-143737`（工作区外、**事前为空、事后仍为空**） |
| 完整命令 | `codex.exe exec --json --sandbox read-only --disable multi_agent --skip-git-repo-check -C <上述目录> "Reply with exactly: OK. Do not modify any files."` |
| **退出码** | **1（失败）** |
| 耗时 | 0.2s |
| **JSON 事件** | **0 个** |
| **用量信息** | **无** |

**完整原始输出**：

```
WARNING: failed to clean up stale arg0 temp dirs: 拒绝访问。 (os error 5)
WARNING: proceeding, even though we could not create PATH aliases: 拒绝访问。 (os error 5) at path "C:\\Users\\Administrator\\.codex\\tmp\\arg0\\codex-arg0Nk1CDn"
Reading additional input from stdin...
Error: failed to initialize in-process app-server client: 拒绝访问。 (os error 5)
```

**根因（本次可确认的部分）**：
`Error: failed to initialize in-process app-server client: 拒绝访问 (os error 5)` ——
Codex 启动时需要在用户目录下创建/清理临时文件与 PATH 别名
（`C:\Users\Administrator\.codex\tmp\arg0\...`），而**当前执行环境的文件沙箱拒绝写入该位置**，
因此它在**初始化阶段**即退出，**根本没有进入模型调用**。

**由此确认的事实**：

- 失败发生在 Codex 自身初始化，**不是**审批/配额逻辑的问题。
- 临时目录**未被写入**（前后均为 0 项），read-only 断言未被真正考验。
- 未产生任何 JSON 事件与用量信息，因此**"流式事件"与"真实用量"两项仍为未知**。
- 观察到 `Reading additional input from stdin...`：即使已把 prompt 作为参数传入，
  Codex 仍检测到 stdin 非 TTY。后续调用应显式使用 `stdin=DEVNULL`，
  避免语义歧义（可能被当作 `<stdin>` 块追加）。

**按用户指令：失败后停止，未追加调用、未重试。**

### 5.2 第 2 步（重试，已获批准并执行）—— **仍失败，但定位更前进了一层**

**批准范围**：仅一次重试；为本进程设置专用、可写、Git 已忽略的 `CODEX_HOME`；
保留只读沙箱与禁用原生子 Agent；先核实有效配置与登录状态；失败即停、不扩大权限、不输出凭据。

**预检（无模型调用）结果**：

| 检查 | 结果 |
|---|---|
| 专用 `CODEX_HOME` | `E:\plug\agent-hub\var\codex-home`（**可写**；已在其中创建 `tmp` 子目录） |
| 默认 home 登录状态 | `Logged in using ChatGPT`（退出码 0；**未输出任何凭据**） |
| 新 home 下 `features list` | 退出码 0，`multi_agent` = **false**（开关在重定向下同样生效） |
| 凭据传递 | 将 `auth.json` **本地复制**到该 gitignored 目录（大小 3915 字节，**内容未打印**）。
验证结束后**已删除该副本**，避免凭据留存 |

**实际执行**（单次，未重试）：

| 项 | 值 |
|---|---|
| 工作根 `-C` | `C:\Users\Administrator\AppData\Local\Temp\dsh-NDA5C5\codex-retry-20261014-144729`（事前空、**事后仍空**） |
| `CODEX_HOME` | `E:\plug\agent-hub\var\codex-home` |
| **退出码** | **1（失败）** |
| 耗时 | 20.1s |
| **JSON 事件** | **14 个**（见下） |
| 用量信息 | **无**（0 个 usage 事件） |

**关键结果 1：上一次的初始化失败已被消除。**
`os error 5` 与 `failed to initialize in-process app-server client` **完全消失**——
证明「沙箱拒绝写 `~/.codex/tmp`」这一根因判断正确，`CODEX_HOME` 重定向是有效解法。

**关键结果 2：JSONL 事件流现已实测产生**（此前为"未知"）：

```
{"type":"thread.started","thread_id":"01a1392a-83f3-7160-b836-c987cfcef25b"}
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"error"}}
{"type":"error","message":"Reconnecting... 5/5 (workspace routing discovery failed)"}
{"type":"turn.failed","error":{"message":"workspace routing discovery failed"}}
```

事件类型统计：`error` 10、`thread.started` 1、`turn.started` 1、`item.completed` 1、`turn.failed` 1。
说明 `--json` 输出**是可解析的 JSONL**，适配器可以消费；且 CLI 会返回
`thread_id`（对"恢复会话"有用）。

**关键结果 3：本次失败发生在网络层，不是配置层。**

```
ERROR codex_models_manager::manager: failed to refresh available models: Connection failed: error sending request
ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed,
      http/request failed: error sending request for url (https://chatgpt.com/backend-api/ps/mcp)
{"type":"error","message":"Reconnecting... 5/5 (workspace routing discovery failed)"}
{"type":"turn.failed","error":{"message":"workspace routing discovery failed"}}
```

即：**当前执行环境无法完成到 `chatgpt.com` 的出站连接**，
Codex 在重试 5 次后放弃，整个 turn 失败——**模型未被调用，因此无用量、无费用**。

**按用户指令：失败后停止，未追加调用、未扩大权限、未重试。**

### 5.3 仍未验证的步骤（需逐次批准）

| # | 目的 | 状态 |
|---|---|---|
| 2 | 目录限制是否真生效 | 未验证（调用未进入执行阶段） |
| 3 | **关闭 `multi_agent` 是否真能阻止子 Agent 派生** | 未验证（用户明确要求另行申请） |
| 4 | 取消能力 | 未验证 |
| 5 | 工具启动前拦截 | 未验证 |

### 5.4 真实调用失败于网络层：**连接失败，原因待确定**

两次尝试合计把失败点从**配置层**推进到了**网络层**。本机观测结果：

| 目标 | DNS | TCP 443（本机观测） | 判定 |
|---|---|---|---|
| `chatgpt.com` | OK → `128.121.243.235` | **超时** | **连接失败** |
| `api.openai.com` | **解析失败** | — | **连接失败** |
| `pypi.org`（对照） | OK | OK / TLSv1.3 / 200 | 连通 |

### 结论：**连接失败，原因待确定**

**失败原因尚未确定。** 候选原因包括（现有证据**均不能排除**）：
本执行环境/沙箱的网络限制、本机或网络层的过滤与 DNS 策略、目标侧不可达或地区限制、
Codex 自身网络栈问题。

**重要更正**：本节早先写过"这不是文件沙箱行为，因为文件沙箱会给出显式 `[sandbox: ...]` 标记"。
该推断**不成立**——**缺少 sandbox 标记不能排除执行环境的限制**：标记只覆盖已显式实现的
文件访问策略，网络出口限制可能以超时/解析失败静默生效，也可能来自环境之外的网络层。
故这里只保留"**连接失败，原因待确定**"。

（`chatgpt.com` 解析为 `128.121.243.235`、`api.openai.com` 解析失败，与正常公共解析不符，
**提示**存在 DNS 层干预，但**仅属提示，不作结论**。）

**若后续要在本环境内重试，需要放行的目标（仅两个）**：`chatgpt.com:443`、`api.openai.com:443`。
在环境提供授权入口之前，已改为提供**手工执行工具与结果模板**（见下）。

- `tools/codex_one_call.py` — 只跑一次、只读沙箱、禁用子 Agent、拒绝非空工作目录，
  输出结构化结果与原始捕获；
- `docs/p3-codex-manual-run.md` — 执行步骤、需放行目标、**结果记录模板**、以及
  "本次能回答哪些未知"的对照表。

其余可选路线（优先级低于放开上述两个目标）：

| 方案 | 说明 |
|---|---|
| 在沙箱外运行 | 由操作者在网络可达的终端执行（推荐，见手工指南） |
| 换成本机可达的端点 | `--oss` / `--local-provider <lmstudio\|ollama>`，无需外网。
**本机实测无 ollama/lmstudio**（PATH 无、11434/1234 端口无监听），故当前不可用 |

---

### 5.5 第 3 次调用（**成功**）：一次最小真实探测

**批准范围**：一次最小真实调用；`--codex` 固定为预检通过的同一可执行文件；
只读沙箱、禁用子 Agent、工作区外空目录、进程专用代理与证书；失败/超时即停、
不切换模型、不放宽权限、不额外重试。

**可执行文件身份核对（执行前，先确认一致）**：

| 项 | 值 |
|---|---|
| 绝对路径 | `C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe` |
| 版本 | `codex-cli 0.162.0-alpha.17.2` |
| 大小 / SHA-256 | `334121776` / `d13914ced6c7af174d8d638db938284231227312104b06a25e9608c632a7daec` |
| 本机 `bin\*\codex.exe` 数量 | **1**（无第二个候选） |
| 与预检是否同一文件 | **一致（True）** |

**实际 argv**（`var/codex_onecall_result.json` 可核对）：

```
codex.exe exec --json --disable multi_agent --sandbox read-only
           --skip-git-repo-check
           -C C:\Users\Administrator\AppData\Local\Temp\dsh-NDA5C5\codex-onecall-20261014-193754
           "Reply with exactly: OK. Do not modify any files."
```

进程专用环境（**仅本进程**，未改注册表 / 系统代理 / `~/.codex/config.toml`）：
`CODEX_HOME=var\codex-home`、`HTTPS_PROXY=HTTP_PROXY=http://127.0.0.1:8090`、
`NO_PROXY=localhost,127.0.0.1`、`CODEX_CA_CERTIFICATE=<certifi cacert.pem>`。

**结果：成功**

| 项 | 值 |
|---|---|
| **退出码** | **0** |
| 耗时 | 11.3s |
| **事件序列** | `thread.started` → `turn.started` → `item.completed`(agent_message) → `turn.completed`（共 4 个，全部为合法 JSONL） |
| **thread_id** | `01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce` |
| **最终回复** | `OK`（与提示词要求完全一致） |
| **实际 usage 字段** | `input_tokens=14481`、`cached_input_tokens=12544`、`cache_write_input_tokens=0`、`output_tokens=5`、`reasoning_output_tokens=0` |
| **目录变化** | **工作目录事后仍为空（0 条目）**；仓库工作区未被改动 |
| 内部重连 / DLL 错误 | **原始捕获中 0 次**：`Reconnecting` / `DLL` / `c0000142` / `error` / `fatal` / `UnknownIssuer` / `timed out` / `os error` 全部 0 命中 |

**原始捕获（逐字）**：

```
Reading additional input from stdin...
{"type":"thread.started","thread_id":"01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}
{"type":"turn.completed","usage":{"input_tokens":14481,"cached_input_tokens":12544,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}
```

**本次未能回答的（需另行批准）**：取消能力、工具栏截、
`multi_agent` 关闭是否真能阻止子 Agent 派生、用 `thread_id` 实际恢复会话。

**证据强度提示**：本次提示词**未要求写文件**，因此"只读沙箱拒绝写入"只得到
**弱证据**（目录为空是必要但非充分条件）。要强验证需让 Codex 尝试写文件并观察是否被拒——**需另行批准**。

---

## 6. 副作用：Codex 常驻后台 daemon（实测）

即使在**没有发起任何模型调用**的情况下，仅执行自描述命令
（`--version` / `--help` / `features list`）就会在后台留下 `codex.exe` 进程。

实测记录：

| 观察 | 结果 |
|---|---|
| 探测后残留进程 | 2 个 `codex.exe`（同安装路径，无监听端口，CPU 很低） |
| 按 PID 精确终止后 | **立即自动重启**，仍是 2 个新进程（PID 31488、5564） |
| 15 秒后再查 | 仍是这 2 个，稳定不增长 |
| 相关特性开关 | `daemon_auto_start` — **stable, true** |

**对适配器设计的影响（重要）**：

1. **不能以"没有 codex 进程"作为"已停止"的判据** —— daemon 会常驻并自动重启，
   其生命周期独立于我们发起的单次调用。
2. 我们的"停止/暂停"只能控制**自己发起的 invocation**，
   Codex 的共享 daemon 在**我们的控制面之外**（计划 §6 要求"不错误宣称远程停止"，
   这里正是一个必须如实声明的例子）。
3. 终止 daemon 无实际意义（会立即重启），且属于对**用户机器上其他 Codex 使用**的干扰，
   因此**不再重复终止**。若需彻底停用，应通过 `--no-daemon` 或
   `-c features.daemon_auto_start=false`（**尚未验证**）。

---

## 7. 结论

- 本机**唯一可用的真实 Agent 是 Codex**（`codex-cli 0.162.0-alpha.17.2`，
  SHA-256 `d13914ce…2daec`）；PATH 上没有 claude / gemini / workbuddy 等。
- **三次真实调用**：两次失败（初始化、网络），**第三次成功**（退出码 0）。
- **失败原因已定位并解决**：Codex 此前**未走代理**（无代理环境变量），
  加上代理的证书需要显式信任。解法为**进程专用**的两个环境变量
  （见 `docs/codex-proxy-setup.md`）：
  `HTTPS_PROXY=http://127.0.0.1:8090` 与 `CODEX_CA_CERTIFICATE=<可读 PEM>`。
  注意 `-c network.proxy_url` **无效**（doctor：`session-flags: network is ignored`）。
- **已验证**：`exec` 发消息、JSONL 事件流（成功路径序列）、**真实用量返回**、
  `thread_id` 句柄、只读沙箱下目录保持为空（弱证据）。
- **仍为未知（不得当作支持）**：**取消任务**、**工具启动前拦截**、
  **关闭 `multi_agent` 是否真能阻止子 Agent 派生**、用 `thread_id` 恢复会话、
  写文件是否真被拒、以及 app-server 协议。
- Codex 会**常驻并自动重启后台 daemon**（实测），其生命周期不在我们的控制面内。
- **真实适配器整体状态：仍未验收。** 一次成功的极简探测**不等于**适配器可接入；
  适配器代码、审批对接、取消与配额联动**均未实现**。
- 后续验证**须逐次经用户批准**。
