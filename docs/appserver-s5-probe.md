# S5：thread 冷恢复与历史上下文探针

2026-10-22。分支 `feat/appserver-s5-probe`，基于 `fix/pip-proxy-trust`（`f4a2866`）；后者基于 S4 记录 `4a5b1ce`。

## 当前状态

- **离线准备完成，真实 S5 尚未授权，也未执行。** S1–S4 的证据和结论保持不变。
- 被测程序沿用固定文件 `bin\6257d9e8f63c0f2b\codex.exe`（`codex-cli 0.163.0-alpha.11`），复用步骤 A 的证据 `identity-hkzdmoqj`（COMPATIBLE），这次不执行程序。
- S5 依赖的协议事实，已在现有捕获上离线核对，11/11。

## 要证明什么

一个已经结束的 thread，在**另一个新启动的** app-server 进程中通过 `thread/resume` 冷恢复之后，模型在新回合里**确实看得到之前的对话**。

`thread/resume` 返回成功，本身不算 PASS。必须同时满足两点：

1. 结构证据：恢复响应中带着之前那个回合（状态 completed），并且其中有我们写入的那条用户消息。
2. 语义证据：新回合的提示词里没有暗号，而模型的回复中出现了暗号。

## 专用测试 thread

- **每次运行都新建一个专用 thread**，在阶段 1 用 `thread/start` 创建，存放在长期 home（`%LOCALAPPDATA%\AgentHub\codex-home`）中。
- **不恢复 S2–S4 的 thread。** 恢复会向它们的 rollout 追加内容，从而改变已登记的证据哈希。
- 两个阶段使用同一个仓库外的新建空目录作为 cwd。运行结束后，如果该目录为空，就做非递归删除。
- 这个 thread 和它的 rollout 留在长期 home 中，不删除；运行记录会登记 rollout 的路径和哈希。

## 进程关系与回合上限

| 步骤 | 进程 | 发送 |
|---|---|---|
| 阶段 1：写入 | 进程 A（新启动） | `initialize → initialized → thread/start → turn/start`（写入暗号） |
| 关闭 A | — | 只通过本次句柄关闭；记录退出码和后代进程名单 |
| 检查 | — | 阶段 1 正常结束、没有工具活动、A 的退出码为 0，并且进程快照中 **A 的 PID 已不存在**；否则不进入阶段 2 |
| 阶段 2：恢复与回忆 | 进程 B（A 退出之后才启动，PID 不同） | `initialize → initialized → thread/resume(threadId=阶段 1 的 thread) → turn/start`（询问暗号） |

- **模型回合上限为 2**：每个阶段 1 个，客户端在写出之前强制检查，总数超过 2 判 FAIL。
- 两个阶段都不回答服务端请求，不 fork，不开第二个 thread，不 resume 其他 thread，不重试。
- 两个阶段的权限与 S3/S4 相同：read-only、on-request、reviewer user、禁止联网；回读实际生效的设置，不符就不开回合。
- 每个阶段的时限为 60 秒，每个 RPC 最多 20 秒。

## 提示词与暗号

- 暗号格式为 `S5-` 加 10 个随机字符（字符集不含易混字符），约 49 位熵，没有历史上下文就无法猜中。每次运行重新生成。
- 阶段 1 的提示词：

  ```text
  This is a memory test. Remember this code word for later in this conversation: <code>. Do not use any tools, commands, files or subagents. Reply with exactly: STORED
  ```

- 阶段 2 的提示词（**不含暗号**，客户端在写出前会检查）：

  ```text
  What is the code word I asked you to remember earlier in this conversation? Do not use any tools, commands, files or subagents. Reply with only the code word.
  ```

- 暗号只在内存中与回复比对。结果中只保存暗号的 SHA-256 与长度，以及两个布尔值：`contains`（回复中出现暗号，**参与判定**）和 `exact`（回复恰好等于暗号，只记录）。模型的原文一律不保存。

## 判定规则

**PASS** 需要同时满足：

- **阶段 1**：设置已核验；只有一次 `turn/started`；匹配的 `turn/completed` 状态为 `completed`；没有工具活动；进程 A 退出码为 0；后代进程名单可以取得，关闭后没有残留。
- **进程关系**：A 已从进程表中消失之后才启动 B，且 B 的 PID 与 A 不同。
- **恢复**：没有被拒绝；返回的 thread ID 与阶段 1 相同；设置已核验；`thread.turns` 中有阶段 1 的回合，状态为 completed，且其中有包含暗号的 userMessage。
- **阶段 2**：只有一次 `turn/started`，turn ID 与阶段 1 不同；匹配的终态为 `completed`；没有工具活动；收到用量；**回复中出现暗号**；进程 B 退出码为 0；关闭后没有残留后代进程。
- 两个阶段合计恰好 2 个回合；工作目录为空，并已删除。

**FAIL**：协议矛盾或越界：

- 外来或未绑定的 ID、重复终态、终态之后还有事件；
- `thread/resume` 返回了别的 thread（`recall:resume_thread_mismatch`）；
- 阶段 2 复用了阶段 1 的 turn ID（`recall_turn_reused`）；
- 探针拒绝了外发消息，或回合总数超过 2；
- 工作目录中出现了文件。

**INCONCLUSIVE**：带原因代码。

| 情形 | 原因代码 |
|---|---|
| 恢复成功，但回复中没有暗号 | `context_not_recalled` |
| 恢复响应中没有阶段 1 的回合 / 没有写入的消息 / 回合状态不对 | `history_not_returned` / `planted_message_missing` / `planted_turn_status:<状态>` |
| 恢复被拒 | `resume_rejected` 加 `recall:request_rejected` |
| 某阶段设置不符 | `<阶段>:settings_mismatch` |
| A 仍在进程表中，或进程表取不到 | `first_process_alive_before_resume`；阶段 1 不完整时为 `resume_not_attempted` 或 `recall:not_run` |
| 某阶段的终态问题 | `<阶段>:terminal_not_matched`、`<阶段>:turn_status:<状态>`、`<阶段>:timeout` |
| 工具活动：一出现就**立即停止**该阶段，因为工具可能直接从磁盘读到历史，回忆就不再可信 | `<阶段>:tool_activity_observed`、`<阶段>:server_request_unanswered:<方法>` |
| 阶段 2 缺用量 | `recall:usage_missing` |
| 本地清理 | `<阶段>:process_exit:<码>`、`<阶段>:descendants_survived_close`、`<阶段>:process_tree_unavailable` |

## 与既有契约的关系

`feat/chat-workbench` 的 `docs/conversation-backend.md` 中定义了 `native_resume`：

- 提交时，从该 Agent 最新的回复中取出 thread ID 并绑定；没有可用的 thread 时返回 409 `no_native_session`；
- 适配器只发送 `thread/resume`，然后在该 thread 上只发送新的文本。

该文档明确写着，桩不保存 thread 历史，**真实的续接效果属于 S5，标为“未验证”**。S5 正是用真实进程验证这条路径的前提：thread 在进程重启之后可以恢复，并且模型能用上历史。S5 本身不接内核，也不改变准入。

## 被测版本的源码依据（`rust-v0.163.0-alpha.11`，离线核对）

[app-server/src/request_processors/thread_processor.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/app-server/src/request_processors/thread_processor.rs) 中的 `thread_resume_inner`：

- 对没有加载的 thread，按 thread ID（或路径）从 thread store 读取，即磁盘上的 rollout；完整历史由 `load_resume_initial_history_from_stored_thread` 作为初始上下文加载。
- `include_turns = !exclude_turns`：不传 `excludeTurns` 时，响应的 `thread.turns` 会包含历史回合。
- 请求中给出的 approvalPolicy、approvalsReviewer、sandbox 和 cwd 优先，其余使用持久化的设置；模型等元数据从持久化记录恢复。
- 对已经在运行的 thread，覆盖参数会被忽略并记录不一致。S5 用进程 B 冷恢复，正是为了避开“还在内存中”的情况。

这些只说明源码层面的预期行为，所以需要用暗号做语义验证。

## 协议事实（离线，基于现有捕获）

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s5.py --schema-facts 'E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\identity-hkzdmoqj\schema'
```

COMPATIBLE 11/11：

- `thread/resume` 请求；
- `ThreadResumeParams` 要求 `threadId`，并支持 cwd、approvalPolicy、approvalsReviewer、sandbox、excludeTurns；
- `ThreadResumeResponse` 要求 thread、cwd、approvalPolicy、approvalsReviewer、sandbox；
- `Thread` 要求 id 和 turns；`Turn` 含 id、status、items；
- `ThreadItem` 含 userMessage 和 agentMessage；
- 通知：turn/started、turn/completed、item/completed、thread/tokenUsage/updated。

没有执行任何程序。

## 网络与证书

S5 的子进程环境与 S1–S4 相同：代理 `127.0.0.1:8090`，加上 `CODEX_CA_CERTIFICATE=<venv certifi>`。

`f4a2866` 复核了项目的 pip、certifi 和代理证书：代理不拦截 TLS，因此没有需要信任的私有代理 CA；两个 certifi 包都没有被改动。现在 venv 的 `pip.ini` 让 pip 走代理，验证保持开启。详见 `docs/codex-proxy-setup.md` 第 7 节。

## 离线验证

新增 `tests/test_appserver_s5.py`，共 47 项：

- **双进程桩的 16 个场景**，每个都有预期结论和原因代码。桩会把 thread 写入它自己的 home，作为“磁盘”。
  - happy 为 PASS；措辞不同但含暗号，也为 PASS（`exact=false` 单独记录）。
  - 恢复看起来完美，但模型答不出暗号：INCONCLUSIVE。这条直接证明“resume 成功不等于 PASS”。
  - 不返回历史，或返回的历史缺少那条消息：INCONCLUSIVE。
  - 恢复了别的 thread：FAIL。
  - 恢复被拒，或恢复后设置不符：INCONCLUSIVE，且不开回忆回合。
  - 阶段 1 失败或超时：INCONCLUSIVE，且不启动第二个进程。
  - 复用 turn ID、外来 turn、终态后的迟到事件：FAIL。
  - 阶段 2 出现工具，或出现审批请求（从不回答）：INCONCLUSIVE，并立即停止。
  - 阶段 2 断线：INCONCLUSIVE。
- **每次运行的不变量**：
  - 暗号和任何回复文字都不写入结果；
  - 回合总数不超过 2；
  - 第二个进程的 PID 与第一个不同，并且在第一个退出之后才启动；
  - 从不回答服务端请求。桩一旦收到回答，或在回忆提示词中看到暗号，就以退出码 3 退出。
- **进程关系**：模拟 A 仍然存在或进程表取不到时，不启动 B。
- **真实模式门控**：缺少确认参数时，在产生任何副作用之前就拒绝运行；预检使用 alpha.11 的固定身份；真实模式只允许 happy 场景。
- **客户端规则**：在恢复阶段发 thread/start、恢复别的 thread、写入阶段发 thread/resume、回答请求、未恢复就开回合、fork，都在写出之前被拒。
- **证据规则**：暗号的格式与随机性；对恢复响应只做结构检查；回复只在内存中比对；出现工具就立即停止；回合总数超过 2 判 FAIL。
- **离线协议事实**：齐全与缺项两种情况。

实际输出：

- S5 测试：**47 passed in 42.70s**；
- 全量：**781 passed in 118.72s**（另含 `tests/test_pip_trust.py` 9 项）；
- 独立 CLI `--mode stub`：`S5 PASSED`（A 为 PID 18260，B 为 PID 18020）；
- 不带确认参数的真实模式，退出码 2。

## 授权、限制与清理

真实模式最多产生 **2 个模型回合**，会消耗账号用量，需要单独批准。已知限制：

- **事前控制未验证**：原生工具和子 Agent 的事前控制仍未验证。出现工具活动会立即停止，判 INCONCLUSIVE；但如果是代码模式的 JS 且没有嵌套调用，协议上看不到，只能事后从 rollout 人工核对。
- **答不出暗号的含义**：只说明这一次没有证明上下文已恢复，分不清是上下文缺失还是模型没有照做。判 INCONCLUSIVE，不外推为“恢复失效”。
- **新 thread 的去留**：新建的 thread 和 rollout 会留在长期 home。rollout 中含有暗号、提示词和模型输出，不入库。
- **历史模式**：被测版本对“分页历史”的 thread 走另一条加载路径；新 thread 会选择哪种模式，事前不确定。结果都会如实记录。
- **multi_agent**：进程参数中仍带 `--disable multi_agent`；S3 和 S4 中实际观察到的 `multi_agent_version` 仍为 v2。
- **结论范围**：即使 PASS，也只证明这一次冷恢复之后历史可用。它不证明 Hub 内核的 `native_resume` 已接入，不证明长对话或分页历史的行为，也不代表真实聊天功能已验收。
- **清理**：只通过各自的进程句柄清理；不按名称结束进程；长期 home、用户 `.codex` 和历史会话全部保留。

## 获批后才执行的固定命令

```powershell
Set-Location -LiteralPath 'E:\plug\agent-hub\.worktrees\appserver-s5-probe'
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s5.py --mode real --approve-two-model-turns --acknowledge-unverified-native-control --codex-home 'C:\Users\Administrator\AppData\Local\AgentHub\codex-home'
```

本文档和 CLI 的确认开关本身都不构成授权。
