# S4 真实运行记录·第一次（2026-10-22）：PASS——本次主回合中断通过（发生在首个输出之前）

- 所测提交：`feat/appserver-s4-probe` @ `36766dab336a2496e91a05bcc0bf5d16fc5c692c`。
- 被测程序：`bin\6257d9e8f63c0f2b\codex.exe`，SHA-256 `00c0f1e4…d67c`，342187312 字节，`codex-cli 0.163.0-alpha.11`。复用步骤 A 的 `identity-hkzdmoqj`（COMPATIBLE），S4 协议事实离线核对 13/13。
- 授权（用户原话）：“I approve one real S4 run on 36766da, reusing identity-hkzdmoqj: one new thread, at most one model turn, one bound turn/interrupt at most, no answers to server requests. I accept the listed limits. Stop on any anomaly, no retry, no S5.”
- 执行通道：Agent 自己的 shell，未经沙箱；命令行与获批内容逐字一致；只执行了一次。
- **结论：PASS**。`reason_codes` 为空，退出码 0。只认定这一次主回合的中断通过。**中断在模型产生任何可见输出之前发出**：由计时器触发，增量和推理增量都是 0。所以，输出进行中的中断这次没有验证到。

## 执行前确认

- HEAD 为 `36766da`，工作树干净。
- 固定程序的大小、SHA-256 和签名都没有变化；步骤 A 报告的哈希没有变化（`804e69a7…5d3b`）。
- 长期 home 和凭据文件存在（只检查了是否存在）。8090 代理在监听。没有残留的工作目录。
- 运行前已存在的 codex 相关进程：PID 1628 和 10904（`codex.exe`），PID 11244（1628 的 `codex-code-mode-host.exe`），PID 1960（`codex-computer-use-swift.exe`），以及系统服务 `codex-windows-sandbox-service.exe`。它们都不是本次启动的，本次没有触碰。

## 执行

```powershell
Set-Location -LiteralPath 'E:\plug\agent-hub\.worktrees\appserver-s4-probe'
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s4.py --mode real --approve-one-model-turn --acknowledge-unverified-native-control --codex-home 'C:\Users\Administrator\AppData\Local\AgentHub\codex-home'
```

预检 11 项全部通过。程序输出 `S4 PASSED`，退出码 0，时间为 2026-10-22 21:49（本地）。

| 部分 | 结果 |
|---|---|
| 发出的消息 | `initialize → initialized → thread/start → turn/start → turn/interrupt`，共 5 条。中断只有一次，请求 id 为 4，参数绑定本 thread 和 turn。没有回答任何服务端请求（本次也没有服务端请求） |
| thread 实际设置 | 已核验。rollout 第 8 行显示：on-request、reviewer user、sandbox read-only、文件系统受限、网络受限，模型 `gpt-6.1-sol`。另外 `multi_agent_version` 为 `v2`，与 S3 各次相同，尽管启动参数带了 `--disable multi_agent` |
| 回合 | `turn_attempts=1`；thread `01a163df-b41f-7050-803a-c7d3022bf26b`，turn `01a163df-be21-7cf3-a452-d4926eb9db81`；`turn/started(inProgress)` 只有一次 |
| `interrupt` | 已发送，触发方式为 `timer`：看到 `turn/started` 后约 5 秒内，没有增量、工具活动或服务端请求。服务端应答为 `{}`，应答**先于**终态到达，与源码中的顺序一致 |
| `main_turn` | 收到按 ID 匹配的 `turn/completed`，状态 **`interrupted`**，位于中断之后，`terminal_confirmed: true`。没有收到用量（`usage_received: false`；rollout 第 12 行的 `token_count.info` 也为空） |
| `tool_activity` | `none`：没有工具 item、工具输出或服务端请求。rollout 中也没有任何 `custom_tool_call` 或 `function_call`，即没有代码模式的 `exec` |
| `text_format`（不参与结论） | 助手消息 0 条，增量 0，推理增量 0。rollout 中没有助手输出 |
| `local_cleanup` | 自有进程 PID 27600 已退出，退出码 0；工作目录为空，已删除；后代进程名单可以取得，关闭前和关闭后都为空。运行后的 codex 相关进程与运行前完全相同 |
| 其他通知 | `mcpServer/startupStatus/updated` 两次、`thread/status/changed` 两次、`account/rateLimits/updated` 一次，都只记录了方法名 |

rollout 共 14 行：

- 第 14 行为 `turn_aborted`，reason 为 `interrupted`，turn ID 相同，`duration_ms` 为 5082；
- 第 13 行是通用的中断提示；
- 用户消息之后，没有任何模型输出或工具调用。

stderr 中只有 5 行与 S3 各次相同的 `codex_skills_extension` 错误（os error 32），没有其他内容。

## 这次运行证明了什么、没证明什么

- **已证明（只针对本次运行）**：对 `codex-cli 0.163.0-alpha.11`、固定提示词和固定权限：
  - 向一个已经开始、尚未输出的回合发送一次绑定的 `turn/interrupt` 后，服务端应答 `{}`；
  - 该回合自己的 `turn/completed` 以 `interrupted` 结束；
  - 没有任何工具活动；
  - 本地进程退出，没有残留的后代进程，工作目录为空。
- **没有证明**：
  - 输出进行中的中断（本次在首个输出之前就中断了）；
  - 工具子进程或原生子 Agent 的停止（本次没有工具，也没有后代进程，这一部分的检查是空的）；
  - 重复中断时服务端的行为；
  - 中断回合的用量上报（本次没有用量）；
  - 其他版本或其他模型上的行为；
  - 真实聊天功能的验收。
- 本次一次性授权已经用完；没有重试，没有换模型，没有运行 S5。S1–S3 的证据保持不变。

## 证据（文件不入库，只登记哈希）

| 文件 | 字节 | 修改时间 | SHA-256 |
|---|---|---|---|
| `var\appserver-s4\real-8ruik_o8\result.json` | 4476 | 2026-10-22 21:49:36 | `908d71972ac30ace0b4544ccfefddb52ee66195af21b6521bbd68f63e8d9aafa` |
| `var\appserver-s4\real-8ruik_o8\stderr.log` | 1150 | 2026-10-22 21:49:30 | `b0849ca7b378a3bbfd511fdf82799c357f7475c91d467021303ac8f746f97bdd` |
| 长期 home 下 `sessions\2026\10\22\rollout-2026-10-22T21-49-29-01a163df-b41f-7050-803a-c7d3022bf26b.jsonl`（14 行，含提示词和账号限额信息，不入库） | 48961 | 2026-10-22 21:49:30 | `149d311951d5537334e327a83a13338fc2ee31cb30f7ea4ff75fe259a945f8f4` |
| `E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\identity-hkzdmoqj\report.json`（复用的步骤 A） | 1023 | 2026-10-22 20:41:32 | `804e69a73a748460d5da71d0a8aeacfd344da2c9eb5db74e5e2352255bf55d3b` |

`var\` 路径相对于 `E:\plug\agent-hub\.worktrees\appserver-s4-probe\`。分支合并时，把上表并入证据清单。
