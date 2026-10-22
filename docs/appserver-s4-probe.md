# S4：单次回合中断探针

2026-10-22。分支 `feat/appserver-s4-probe`，基于 S3 的 `85bfae9`。

## 当前状态

- **离线准备完成，真实 S4 尚未授权，也未执行。** S1–S3 的证据和结论保持不变。S5 不会自动执行。
- 被测程序沿用 S3 的固定文件：`bin\6257d9e8f63c0f2b\codex.exe`，`codex-cli 0.163.0-alpha.11`。步骤 A 的证据为 `identity-hkzdmoqj`（COMPATIBLE），这次只做离线复用，不重新执行程序。
- S4 依赖的协议事实，已离线核对上述捕获，13/13 全部存在（见“协议事实”）。

## 目的与固定范围

验证一件事：向一个正在进行的真实回合发送一次 `turn/interrupt` 之后，**该回合自己的** `turn/completed` 是否以 `interrupted` 结束。

同时把以下三件事分开记录，互不代替：

- 主回合的终态；
- 工具子进程的状态；
- 本地清理。

模型说了什么，不作为判断依据。

工具：

- 探针：`tools/probes/appserver_s4.py`
- 证据归集：`packages/adapters/agenthub_adapters/appserver_s4_evidence.py`
- 离线桩：`tools/demo/stub_appserver_s4.py`

握手、单回合限制、有界的进程清理和最小化的结果保存，都复用 S2/S3 的运行器。S1–S3 的文件没有改动。

- 只启动一个固定身份的 app-server，最多一个新 thread、一次 `turn/start`、一次 `turn/interrupt`。
- 参数与 S3 相同：thread 的 `sandbox=read-only`；turn 的 `sandboxPolicy={type:readOnly,networkAccess:false}`；两者都是 `approvalPolicy=on-request`、`approvalsReviewer=user`。
- 创建 thread 后，先回读实际生效的设置；设置不符时，**0 次模型回合**。
- 进程参数仍为 `app-server --stdio --disable multi_agent`。

唯一的提示词是一个较长的纯文本任务，不需要任何工具：

```text
Write a numbered list of 400 different English nouns, one per line, as plain text. Answer directly: do not use any tools, code execution, commands, files, web search or subagents, and do not ask questions.
```

## 什么时候中断

在 `turn/start` 应答绑定了 turn ID、并且收到本回合的 `turn/started(inProgress)` 之后，满足以下任一条件就立即发送中断：

1. 第一个 `item/agentMessage/delta`（`first_delta`）；
2. 出现任何工具 item、工具输出通知（`tool_activity`），或服务端请求（`server_request`）；
3. 回合开始 5 秒后仍没有可见输出（`timer`）。

中断请求为 `turn/interrupt {threadId, turnId}`，只发一次，绑定当前 thread 和 turn。如果回合在此之前已经结束，就不再发送。

## 发送限制

- 唯一可能的发送序列：`initialize → initialized → thread/start → turn/start → 一次 turn/interrupt`。
- 不回答任何服务端请求：审批、征询、认证刷新都不回答。
- 不发送第二次中断，不手工拼接中断，不开第二个回合，不 resume，不重试。
- 部分写入也算作一次尝试，不会重发。
- 中断请求不阻塞主循环：在客户端的请求表中登记，应答按 ID 匹配。

## 判定规则

结果分为四部分，各自独立：

| 部分 | 内容 |
|---|---|
| `interrupt` | 是否已发送、触发原因、服务端的应答（`ok` 或 `error` 加错误码），以及应答在终态之前还是之后 |
| `main_turn` | 本 turn 的 `turn/completed` 是否按 ID 匹配、它的状态、是否在中断之后，以及是否收到用量。这是唯一能证明“已停止”的依据 |
| `tool_activity` | 工具 item、工具输出通知、服务端请求（只记方法名，`server_requests_answered` 恒为 0），以及工具结束状态：`none` / `ended` / `unknown`。工具结束状态只看工具 item 自己的 `item/completed`，不从主回合的终态推断 |
| `local_cleanup` | 由运行器判定，与前三部分分开：自有进程是否已停止及其退出码、工作目录是否为空及是否已删除，以及 app-server 后代进程在关闭前后的名单（只记映像名） |

另有 `text_format`：只记录消息数、增量数和字符数，**不参与判定**。中断时尚未完成的助手消息没有 `item/completed`，这是被测版本的正常行为，不算缺陷。

**结论**：

- **PASS** 需要同时满足：
  - 实际设置已核验；
  - 恰好一次 `turn/started(inProgress)`；
  - 回合进行中发出了唯一一次中断；
  - 服务端应答 `{}`；
  - 匹配的终态为 `interrupted`，并且在中断之后；
  - 没有任何工具活动或服务端请求；
  - 本地清理：自有进程退出码 0、工作目录为空且已删除、后代进程清单可以取得、关闭后没有残留后代进程。
- **FAIL**：协议矛盾，或探针拒绝过外发。例如：外来或未绑定的 thread/turn ID、第二个终态、终态之后还有 item 或 turn 事件、探针拒绝了外发消息（回答请求、第二次中断、手工中断），或只读运行的工作目录中出现了文件。
- **INCONCLUSIVE**：其他情况，带原因代码。

| 情形 | 原因代码 |
|---|---|
| 回合抢先完成，没有发送中断 | `turn_completed_before_interrupt:<状态>` |
| 中断已发出，但终态不是 interrupted | `interrupt_not_effective:<状态>` |
| 只有应答、没有终态 | `terminal_not_matched`（再加上 `timeout` 或 `disconnect_before_terminal`） |
| 没有应答 | `interrupt_not_answered` |
| 应答为错误 | `interrupt_rejected` |
| 工具没有结束证据 / 工具已结束 | `tool_process_unknown` / `tool_activity_observed` |
| 未回答的服务端请求 | `server_request_unanswered:<方法>` |
| 服务端 `error` 通知 | `provider_error` |
| 本地清理问题 | `descendants_survived_close`、`process_tree_unavailable`、`process_not_stopped`、`process_exit:<码>`、`cleanup_error:<类型>` |
| 设置不符 | `settings_mismatch` |
| FAIL 类 | `foreign_thread`、`foreign_turn`、`unbound_event`、`duplicate_terminal`、`event_after_terminal`、`outgoing_response_rejected`、`outgoing_interrupt_rejected`、`duplicate_interrupt_attempt`、`workdir_not_empty` |

没有匹配终态时，`terminal_confirmed=false`，legacy 判定为 `unconfirmed`，并要求人工核对。自有进程已停止，**不能**说明远端已经停止。

## 与 interrupt-stop 既有契约的对照

既有契约见 `feat/interrupt-stop`（`98fbc44`）的 `docs/conversation-backend.md`，“中断与原生子 Agent 观测”一节。

| 契约 | S4 中对应的检查 |
|---|---|
| 适配器向本进程驱动的回合发送 `turn/interrupt`（threadId + turnId）；“服务端接受请求不等于已经停止” | 只发一次绑定的中断；`interrupt.response=ok` 只是一条应答记录，单独出现时结论为 INCONCLUSIVE（`terminal_not_matched`） |
| 只有该回合自己的 `turn/completed` 报告 `interrupted`，才结算为 `canceled` | PASS 的必要条件：匹配的终态为 `interrupted`，并且在中断之后 |
| 没有终态时不声称任何结果，记为 unknown 并保留名额 | `terminal_not_matched`，判 INCONCLUSIVE 并需人工核对；探针没有生产名额，不声称保留或释放了名额 |
| 完成与中断竞争时按实际结果结算（succeeded） | `turn_completed_before_interrupt:completed` 或 `interrupt_not_effective:completed`，判 INCONCLUSIVE：本次没能验证中断，如实记录实际状态 |
| 等待中的工具卡置为 void，之后不再回答 | 服务端请求一律不回答，只记录方法名，并触发中断 |
| 子 Agent 只有自己的状态（`NATIVE_CHILD_END_STATES`）才算结束证据 | 工具类 item 只有自己的 `item/completed`，或 completed、failed、declined、interrupted、errored、shutdown 等状态，才算结束；否则为 `unknown`，不从主回合终态推断 |
| 重复中断：每次都发 `turn/interrupt`，只结算一次 | S4 有意只发一次，第二次在本地被拒（离线已测）。真实服务端对重复中断的行为，S4 不验证 |
| 本地停止只关自己的传输 | `local_cleanup` 单独判定；只通过本次进程句柄清理，不按名称结束任何进程。后代进程只记录，不处理 |

## 被测版本的源码依据（`rust-v0.163.0-alpha.11`，离线核对）

- [app-server/src/request_processors/turn_processor.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/app-server/src/request_processors/turn_processor.rs)：
  - `turn/interrupt` 先把请求 ID 记入 `pending_interrupts`，再向 core 提交 `Op::Interrupt`；对回合中断**不立即应答**（注释：回合中断在 TurnAborted 时应答）。
  - 回合已经结束或不在运行时，返回错误 `no active turn to interrupt`；turn ID 不符时，返回 `expected active turn id … but found …`。
- [app-server/src/bespoke_event_handling.rs](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/app-server/src/bespoke_event_handling.rs) 的 `TurnAborted` 分支，按代码顺序：
  1. 中止所有挂起的服务端请求；
  2. 对 `pending_interrupts` 逐个应答 `TurnInterruptResponse {}`；
  3. 发出 `turn/completed`，状态 `Interrupted`，turn ID 取事件本身的 ID。

  正在进行的 item 不会收到自己的完成通知。
- 因此，应答与终态通常相继到达。但按既有契约，判定仍以匹配的终态为准；应答在终态之后到达也接受，只记录先后顺序。
- 第二次 S3 运行的 rollout 第 15 行，是 Codex 在中断后写给模型的提示，原文为“Any running unified exec processes may still be running in the background.”。这说明主回合中断**不等于**工具子进程已经停止，所以 S4 把两者分开记录。

这些只说明源码层面的预期行为；版本号与标签一致，不能证明本机二进制就是从该标签构建的。

## 协议事实（离线，基于现有捕获）

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s4.py --schema-facts 'E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\identity-hkzdmoqj\schema'
```

结果为 COMPATIBLE 13/13：

- `turn/interrupt` 请求；
- `TurnInterruptParams` 要求 `threadId` 和 `turnId`；
- `TurnInterruptResponse` 为对象；
- `TurnStatus` 包含 inProgress、interrupted、completed、failed；
- `TurnCompletedNotification` 含 threadId 和 turn；`Turn` 含 id 和 status；
- 通知：turn/started、turn/completed、item/agentMessage/delta、item/started、item/completed、serverRequest/resolved、thread/tokenUsage/updated。

没有执行任何程序。

## 离线验证

新增 `tests/test_appserver_s4.py`，共 65 项：

- **真实子进程桩的 16 个场景**，各有预期结论和原因代码：
  - happy 为 PASS；
  - 应答晚于终态，仍为 PASS（只记录先后顺序）；
  - 只有应答、没有终态，INCONCLUSIVE；
  - 应答为错误，INCONCLUSIVE；
  - 回合抢先完成，INCONCLUSIVE，并且没有发送中断；
  - 完成与中断竞争（错误应答加 completed），INCONCLUSIVE；
  - 终态为 failed，INCONCLUSIVE；
  - 终态带错误 turn ID，FAIL；外来 thread，FAIL；
  - 重复终态，FAIL；终态后的迟到增量，FAIL；
  - 断线，INCONCLUSIVE；超时，INCONCLUSIVE；
  - 工具 item（有 processId、没有结束），INCONCLUSIVE，主回合仍记为 interrupted；
  - 服务端请求：从未回答，并触发中断，INCONCLUSIVE；
  - 设置不符，0 回合。
- **计时触发**：没有可见输出时，由计时器触发中断，PASS。
- **每次运行的不变量**：最多一个回合、最多一次中断、从不回答。桩一旦收到回答或第二次中断，就以退出码 3 退出。模型文字和服务端错误文字不写入结果。
- **客户端规则**：中断绑定 ID 且只发一次；回合开始前不中断；回答请求、手工中断、第二个回合都在写出之前被拒。
- **证据规则**：
  - 文字不参与判定，“已停止”之类的回复不能代替终态；
  - 只有应答不算停止；
  - 触发规则；终态之后不再触发；用量可以晚于终态到达；
  - 重复或迟到事件判 FAIL；错误 ID 判 FAIL；未绑定的事件在绑定时复核；
  - 工具结束状态只看工具自己的 item；没有 item 的工具输出记为 unknown，且不保存输出；
  - 错误应答只记录错误码；`error` 通知阻止 PASS，但不判 FAIL。
- **本地清理**：
  - 后代进程只沿父进程链计算；
  - 关闭前后的名单分别测量；
  - 残留后代、名单取不到、进程未停止、退出码非 0，都判 INCONCLUSIVE；
  - 工作目录非空，或探针拒绝过外发，判 FAIL。
- **离线协议事实**：齐全与缺项两种情况。
- 实际输出：
  - S4 测试：**65 passed in 25.66s**；
  - 全量：**725 passed in 74.94s**；
  - 独立 CLI `--mode stub`：`S4 PASSED`。桩启动器 `python.exe` 的子进程，在关闭前作为后代被记录，关闭后没有残留。
  - 不带确认参数的真实模式，退出码 2。

## 授权、限制与清理

真实模式会消耗账号用量，**需要单独批准这一次 S4 主回合**。已知限制：

- 原生工具和子 Agent 的事前控制仍未验证。模型可能不照提示词做，例如调用代码模式的 `exec` 生成列表。
  - 代码模式的 JS 如果没有嵌套工具调用，不会产生 thread item，协议上看不到，只能事后从 rollout 人工核对。
  - 一旦看到任何工具活动，结果就不可能是 PASS。
- 回合可能在中断之前就结束了，或者模型先长时间推理才开始输出。前者判 INCONCLUSIVE；后者由 5 秒计时触发中断。
- app-server 的辅助进程（例如 `codex-code-mode-host.exe`、`codex-command-runner.exe`）可能是后代进程。S4 只记录它们，不按名称结束；关闭后仍有残留时判 INCONCLUSIVE，需要人工核对。
- 中断后，长期 home 中会保留这次的 rollout，包含提示词和部分模型输出，不入库。
- 第二次 S3 和每次运行都出现过的 `codex_skills_extension` os error 32 仍未处理。
- 即使 PASS，也只证明这一次主回合的中断。它不能证明工具子进程或原生子 Agent 的停止，不能证明重复中断的行为，也不能代表真实聊天功能已验收。
- 总时限 60 秒，每个 RPC 最多 20 秒，终态后观察 1 秒；清理只通过本次进程句柄进行；长期 home、用户 `.codex` 和历史会话全部保留。

## 获批后才执行的固定命令

```powershell
Set-Location -LiteralPath 'E:\plug\agent-hub\.worktrees\appserver-s4-probe'
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s4.py --mode real --approve-one-model-turn --acknowledge-unverified-native-control --codex-home 'C:\Users\Administrator\AppData\Local\AgentHub\codex-home'
```

本文档和 CLI 的确认开关本身都不构成授权。
