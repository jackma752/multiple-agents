# S6-2 第一次真实运行：PASS（只认定本次经过 Hub 的 supervised 最小回合）

2026-10-23 02:48（本地时间）。按用户批准执行一次，申请见 [S6 规划](appserver-s6-plan.md)第 10 节。

**批准原文**：“批准在 26a9553 上执行一次 S6-2：复用 identity-hkzdmoqj，一个新会话、一次 supervised 调用、最多 1 个模型回合，按申请中的命令和停止规则执行；任何异常立即停止，不重试，不接着执行 S6-3。”

## 执行前确认

| 项目 | 结果 |
|---|---|
| 提交与工作树 | HEAD `26a9553`（`feat/appserver-s6`），工作树干净 |
| 身份环境变量 | 没有设置任何 `AGENT_HUB_*` 变量 |
| 固定程序 | `bin\6257d9e8f63c0f2b\codex.exe`，342187312 字节，SHA-256 `00c0f1e4…cd67c`，与常量一致 |
| 步骤 A 证据 | `identity-hkzdmoqj/report.json` 的 SHA-256 为 `804e69a7…5d3b`，没有变化（复用） |
| 证书包 | `cacert.pem` 的 SHA-256 为 `50f79f98…0785` |
| 代理 | 127.0.0.1:8090 在监听（只做了一次本地 TCP 连接检查） |
| CODEX_HOME | `%LOCALAPPDATA%\AgentHub\codex-home` 存在，登录文件存在（没有读取内容） |

## 命令

```
Set-Location E:\plug\agent-hub\.worktrees\appserver-s6
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s6_2.py --mode real --approve-one-model-turn --acknowledge-supervised-boundary
```

输出 `S6-2 PASSED`，退出码 0。只执行了这一次。

## 结果

- **驱动预检**：9 项全部通过：
  - 没有身份环境变量覆盖；
  - factory 是用常量构造的真实 factory；
  - 程序的大小和哈希一致；
  - CA 哈希一致；
  - CODEX_HOME 在仓库之外，有登录文件，可写；
  - 监督准入放行，严格准入仍然阻止。
- **调用**：`inv-a27fe6d27d50`，状态 `succeeded`；`run?wait=true` 返回 200；适配器启动 1 次；名额已释放（未释放的预留为 0）。
- **绑定身份**：快照的 `adapter_version` 是 `codex-app-server@codex-cli 0.163.0-alpha.11;sha256=00c0f1e4…;size=342187312`，与固定身份一致。
- **事件顺序**（会话事件流，共 11 条，从数据库按 seq 读出）：
  1. `conversation.created`
  2. `message.created`
  3. `approval.status`（pending → approved）
  4. `invocation.status`（awaiting_approval → queued）
  5. `invocation.status`（queued → starting）
  6. `stream.turn_started`
  7. `stream.delta`
  8. `stream.turn_ended`（completed，finished）
  9. `invocation.status`（starting → running）
  10. `invocation.status`（running → succeeded）
  11. `message.created`

  满足 starting < turn_started < 第一条 delta < turn_ended < succeeded。starting → running 这一步记录在回合结束之后，这是内核现有的结算顺序，不是本次的异常。
- **回复**：恰好是 `OK`。结果文件只记录是否完全一致，提示词只记录哈希 `75e0d5a3…33ce`。
- **thread 和 turn**：thread `01a164f1-496b-7792-9165-6de8c824dd23`，turn `01a164f1-51b3-7e53-a886-bb86e0387826`，`turn/completed` 的状态为 completed。
- **用量**（服务端）：input 15760，cached 12416，output 5，reasoning 0，total 15765。没有重放用量，没有用量异常。
- **执行记录**：
  - 工具活动为空，状态 `none`；
  - 没有工具卡，没有被拒或未答的请求；
  - 没有观测到原生子 Agent；
  - 没有不绑定的事件，也没有迟到事件；
  - 本地进程确认退出；
  - 关闭前后都没有后代进程（宽限 3 秒）。
- **工作目录**：`<系统临时目录>\agenthub-s6-2-data-b7bjc7bt\codex-runs\inv-a27fe6d27d50`，在仓库之外，运行后为空。数据目录保留，供审计。
- **rollout 结构核对**（只统计记录类型，不读取正文）：
  - `turn_context` 中：approval 为 on-request，sandbox 为 read-only，cwd 就是上述工作目录；
  - `multi_agent_version` 为 `v2`。这只是会话的模式字段，**不是**派生的证据（S6 规划第 2.1 节）；
  - payload 类型中没有 `function_call` 或 `custom_tool_call`，也没有子 Agent 相关的 item。

## 观察到的现象

- **stderr 中有 5 行 ERROR**：`codex_skills_extension::host_service: failed to install system skills ... (os error 32)`，即重装 CODEX_HOME 中的系统 skills 时文件被占用。
  - S3 和 S4 的真实运行 stderr 中有完全相同的 5 行（大小同为 1150 字节），说明这是环境里已有的现象，不是本次新出现的。
  - 回合正常完成，没有影响判定，本次也没有去处理它。
- 运行结束后，系统中有 2 个 `codex.exe`。它们是 `ChatGPT.exe` 在 2026-10-22 19:50 启动的子进程，与本次运行无关，没有动它们。

## 只认定 / 不认定

- **只认定**：这一次经过 Hub（进程内的 API、Worker、内核、app-server 适配器、事件库）的 supervised 最小回合可用。具体包括：
  - 固定身份和设置的核验在真实程序上能通过；
  - 审批（带显式同意）消费、启动、增量事件、结算和名额释放这条链路走通了一次。
- **不认定**：
  - 网页点击路径（本次没有经过浏览器）；
  - strict 模式；
  - 工具审批与拒绝（S6-4）、中断（S6-5）、续接（S6-3）；
  - 原生子 Agent 的控制；
  - 其他版本或模型；
  - 长时间或多次运行的稳定性。

## 证据（不入库，只登记哈希）

| 文件 | SHA-256 |
|---|---|
| `var/appserver-s6-2/real-c3z0vehw/result.json` | `2ce7828649600857e4bcc96950cece83f0b14bcc8ec7ca2f646b4c31bb01aa60` |
| `<数据目录>/codex-runs/_logs/inv-a27fe6d27d50-appserver-stderr.log`（1150 字节） | `9e6336ca9339e06e261856eada7e8180cf24113f976b3b41b0835dcd89396a79` |
| CODEX_HOME 中这个 thread 的 rollout（50832 字节） | `50f7dcdbb30778540baf7b94459a080331230b2801bff5981adb4cb1e18697c4` |

授权已用完。没有重试，没有执行 S6-3。S1–S5 的证据保持不变。
