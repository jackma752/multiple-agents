# Codex 适配器能力表（P3 受控接入验证 · 第一批）

日期：2026-10-14。**本表的每一行都来自本机实测的自描述输出**，不是记忆或推测。
凡未能实测的一律标 **未知**，不标"支持"。

- 本机 Codex：`C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe`
- 版本：**codex-cli 0.162.0-alpha.17.2**
- 复现命令：`.\.venv\Scripts\python.exe tools\probe_codex.py`
  （输出：`var/probe-codex.json`，含 version / help / exec --help / features list 的**原始文本**）

> **重要边界**：本次只执行了 CLI 的 `--version` / `--help` / `features list`，
> 这些是**本地自描述**，**没有发起任何模型调用、没有运行任何 Agent 任务**。
> 因此本表能确认"接口是否存在"，**不能确认"行为是否如描述"**——后者需要真实调用，
> 须逐次由用户批准后进行（见 §5）。

---

## 1. 计划 §2 要求的 8 项能力

| 能力 | 判定 | 证据 |
|---|---|---|
| 发消息 | **接口存在** | `codex exec [PROMPT]`，或 stdin 传入指令；`codex [OPTIONS] [PROMPT]` |
| 流式输出 | **接口存在** | `codex exec --json` — "Print events to stdout as JSONL" |
| 恢复会话 | **接口存在** | `codex exec resume`（`--last` 取最近）；顶层 `codex resume` / `codex fork` |
| 取消任务 | **未知** | `--help` 未列出取消/中断命令。存在 `instant_interrupt`（under development, **false**）与 `code_mode_interrupt`（under development, false）。**是否可取消未验证** |
| 限制文件目录 | **接口存在** | `-C/--cd <DIR>` 指定工作根；`--add-dir <DIR>` 追加可写目录；`-s/--sandbox {read-only,workspace-write,danger-full-access}` |
| 拦截工具操作 | **部分可见，行为未知** | 存在 `guardian_approval`（stable, **true**）与 `write_stdin_approval`（stable, true）；但 `exec_permission_approvals`（under development, **false**）、`request_permissions_tool`（under development, false）。**能否在启动前拦截未验证** |
| 控制内部子 Agent | **存在开关候选，行为未知** | `multi_agent`（stable, **true，当前启用**）；`multi_agent_v2`（stable, false）；`enable_fanout`（**removed**）；`agent_message_board`（under development, false） |
| 返回真实用量 | **未知** | `--help` 未列出用量/计费输出。需真实调用后从其输出或会话记录核实 |

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

### 5.2 已定位的可能解法（**需另行批准后才能尝试**）

| 方案 | 说明 | 需用户决定 |
|---|---|---|
| 重定向 `CODEX_HOME` | 帮助文本确认支持 `$CODEX_HOME`。把它指到**工作区内可写目录**（如 `E:\plug\agent-hub\var\codex-home`），Codex 就不必写用户目录 | 是否允许再次尝试（仍是一次真实调用） |
| 放宽文件沙箱 | 以更宽的文件权限运行（`danger-full-access`），允许 Codex 写 `~/.codex/tmp` | 权限更高，需明确同意 |
| 在沙箱外运行 | 由用户在自己的终端里执行同一命令 | 无沙箱冲突，但需用户手动操作 |

**在获得新的批准之前，不会发起任何后续调用。**

### 5.3 其余待验证步骤（同样需逐次批准）

| # | 目的 | 状态 |
|---|---|---|
| 2 | 目录限制是否真生效 | 未执行 |
| 3 | **关闭 `multi_agent` 是否真能阻止子 Agent 派生** | 未执行（用户明确要求另行申请） |
| 4 | 取消能力 | 未执行 |
| 5 | 工具启动前拦截 | 未执行 |

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

- 本机**唯一可用的真实 Agent 是 Codex**（0.162.0-alpha.17.2）；
  PATH 上没有 claude / gemini / workbuddy 等，`.claude` 目录不存在。
- Codex **接口层面具备**：非交互执行、JSONL 事件流、会话恢复、目录限定 flag、
  沙箱模式 flag、`multi_agent` 特性开关（**已本地验证可关闭**）。
- **首次真实调用失败**（退出码 1，0 事件，无用量），根因是**当前文件沙箱拒绝 Codex
  写自己的临时/PATH 别名目录**，导致其在初始化阶段退出。**失败后已停止，未重试。**
- Codex 会**常驻并自动重启后台 daemon**（实测），其生命周期不在我们的控制面内。
- **仍为未知（不得当作支持）**：取消能力、工具启动前拦截是否可强制、
  关闭 `multi_agent` 是否真能阻止子 Agent、真实用量返回、Windows 上沙箱是否生效、
  以及 app-server 协议。流式事件虽在接口上存在，但因初始化失败**尚未实测**。
- 后续验证**须逐次经用户批准**；§5.2 已列出三种可能解法供选择。
