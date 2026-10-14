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

按交接要求"真实调用继续逐次由我批准"，以下**尚未执行**，每一步都需要用户明确同意：

| # | 目的 | 拟执行 | 风险 |
|---|---|---|---|
| 1 | 会话/输出/用量 | `codex exec --json --sandbox read-only -C <临时目录> "回复 OK，不要改任何文件"` | 一次真实模型调用；read-only 沙箱；无文件写入 |
| 2 | 目录限制是否真生效 | 同上但在工作目录外写入，观察是否被拒 | 可能产生文件；需 read-only 下应为拒绝 |
| 3 | **子 Agent 开关是否有效** | 对比 `multi_agent=true/false` 时是否会派生内部子 Agent | 可能触发嵌套调用与费用 |
| 4 | 取消能力 | 启动一个长任务并尝试中断 | 可能无法停止，需人工介入 |
| 5 | 工具拦截 | 让 Codex 执行一条 shell 命令，观察是否先请求批准 | 可能直接执行 |

**在用户批准第 1 项之前，不会发起任何真实调用。**

---

## 6. 结论

- 本机**唯一可用的真实 Agent 是 Codex**（0.162.0-alpha.17.2）；
  PATH 上没有 claude / gemini / workbuddy 等，`.claude` 目录不存在。
- Codex **接口层面具备**：非交互执行、JSONL 事件流、会话恢复、目录限定 flag、
  沙箱模式 flag、`multi_agent` 特性开关。
- **仍为未知（不得当作支持）**：取消能力、工具启动前拦截是否可强制、
  关闭 `multi_agent` 是否真能阻止子 Agent、真实用量返回、Windows 上沙箱是否生效、
  以及 app-server 协议。
- 这些未知项**只能靠真实调用验证**，且须**逐次经用户批准**。
