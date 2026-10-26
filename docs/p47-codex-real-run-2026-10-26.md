# 第一次真实 app-server 回合（2026-10-26 12:00，用户监督模式）

**一句话**：一条真实在线回合经 `codex` / `app-server` 通道完成，拿到 `turn/completed with status
completed`、真实 thread id 和真实 token 用量；同日早些时候同一通道失败于"进程读不到/写不了目录"，用户
调整权限（python 进程完整性级别 `Low` → `Medium`）后恢复。

本文件只记录**实际观测到的东西**，并把"未验证"逐条列出。没有做的验证不写在成功里。

## 调用事实

| 项 | 值 |
|---|---|
| 调用 id | `inv-371025d822df` |
| 适配器 / 传输 | `codex` / `app-server` |
| 执行模式 | 用户监督模式（supervised） |
| 审批 | consumed（一次人工批准） |
| 会话 | `conv-069c280164cb`，thread `01a1765d-493e-7bb2-be9a-127cb19749cb` |
| 发出的提示 | `你好，gpt，你现在是什么模型？` |
| 结束 | `detail: "turn/completed with status completed"`，`status: finished`，`available: true` |
| 最终回复 | `你好！我是 Codex，基于 GPT-6 的编程助手。当前会话没有提供更具体的模型版本标识。` |
| 用量 | 输入 16,787（缓存读 12,416（74%），缓存写 0）/ 输出 77（其中 reasoning 42）/ 合计 16,864 |
| 服务端 stderr | `var/p46-user-demo/data/codex-runs/_logs/inv-371025d822df-appserver-stderr.log`，**0 字节** |

## 哪些算证据，哪些不算

算：

- `adapter-result` 的 `detail` / `status` / `summary.native_session.transport`；
- thread id（`summary.thread_id` 与 `summary.native_session.thread_id` 一致）；
- usage 的具体数字（含缓存读比例）；
- 审批 `consumed` 记录；
- 该次调用对应的 stderr 日志为空文件（对照失败那次是两行致命错误）。

不算：

- 模型自称"基于 GPT-6"——这是**模型对自己的陈述**，未核实，也不能反推运行时版本；
- 页面上的"已完成"标签本身（它与 `adapter-result` 一致才算旁证）。

## 与同日失败那次对照

| | `inv-89fb6ef66f72`（2026-10-26T02:58Z，失败） | `inv-371025d822df`（本次，成功） |
|---|---|---|
| app-server stderr | `WARNING: ... could not create PATH aliases ... os error 5`（已知无害）+ `Error: failed to initialize sqlite state runtime under <CODEX_HOME>`（致命） | 空 |
| 内核结论 | `server refused setup: disconnected` | `turn/completed with status completed` |
| 变化 | python 进程完整性级别 `Low`（`Mandatory Label\Low`） | `Medium`（用户调整权限后实测） |

**因果边界**：只观测到"权限改成 Medium 之后同一通道成功"，没有做多轮 A/B 对照，所以不能宣称这是
唯一原因；能说的是"Low 级别下确定失败、Medium 下确定成功"。

## 仍然未验证（不得写成已具备）

- **取消 / 中断**：本次没有发起停止；
- **工具调用拦截与审批**：本次提示不涉及工具，全程没有工具事件，因此"批准运行 ≠ 批准工具操作"这条
  本次没有被执行到；
- **原生子 Agent 是否受控**：没有触发；严格模式准入仍是 `blocked / native_subagents_uncontrolled`，
  只有"用户监督模式"放行（徽标显示 `blocked` 指的是严格模式，与本次成功不矛盾）；
- **会话恢复**（同 thread 续接新一轮）；
- **流式增量**：页面按事件渲染，本次未验证逐 token 流；
- **适配器版本标识**：本次未采 `/api/adapter`，无法确认跑的是被钉住的哪一份运行时；
- **evidence 字段的完整性**：app-server 通道下 `event_count: 0`、`thread_ids: []`、`item_types: {}`、
  `elapsed: null`、`exit_code: null`。会话元数据走 `summary.native_session`，不是 `exec` 的 JSONL
  事件流；这是**通道差异**，不代表没有事件发生，但也不能拿这些空字段当作"零事件"的证据。

## 复现前提

1. 服务进程的完整性级别不低于 `Medium`（`Low` 下读不到 MSIX 包目录、写不了 `CODEX_HOME` 状态库）。
2. 网络与证书：`HTTPS_PROXY` / `HTTP_PROXY` = `http://127.0.0.1:8090`、`NO_PROXY` =
   `localhost,127.0.0.1`、`AGENT_HUB_CA_BUNDLE` 指向 venv 的 `certifi/cacert.pem`
   （或 `CODEX_CA_CERTIFICATE` 同值）。
3. `AGENT_HUB_CODEX_SANDBOX=read-only`。
4. 启动（multi 会把 codex 强制到 app-server；单 kind codex 需 `--codex-transport app-server`）：

   ```powershell
   & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools/demo/serve.py --port 8080 `
       --adapter codex --data-dir var/p46-user-demo/data --files-root docs
   ```

5. 页面里执行模式必须选**用户监督模式**，且每次调用仍需人工点批准。

## 环境阻断（待写成 issue）

- `codex exec`：钉住的 `%LOCALAPPDATA%\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe`
  （`0.162.0-alpha.17.2`）已被 Codex 自动更新**移除** → 真实调用被 `refused: executable missing` 挡住。
- `codex app-server`：需要在 `CODEX_HOME` 下初始化 sqlite 状态库；进程为 `Low` 完整性级别时 `os error 5`。
- `claude-code`：钉住的二进制在 MSIX 包缓存目录，`Low` 级别的 python 连目录都穿不过去
  （`GetFileAttributesW` / `os.stat` 均 `winerror=5`），适配器据此报 `executable_missing` 并拒绝执行
  —— 该拒绝行为**正确**；`Medium`/`High` 下能否读取未在本次复验。

## 未提交

本文件与 `docs/HANDOFF_CURRENT.md` 的更新**尚未提交**：本次会话的 shell 不可用（所有命令返回
`0xC0000142`），git 操作需由用户在 PowerShell 执行。
