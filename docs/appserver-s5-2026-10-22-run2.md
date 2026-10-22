# S5 真实运行记录·第二次（2026-10-22）：PASS——冷恢复后历史上下文可用（只认定本次）

- 所测提交：`feat/appserver-s5-probe` @ `38288c414bf3495a9e8162a49719a58065a291f0`，即用量重放修订之后的版本。
- 被测程序：`bin\6257d9e8f63c0f2b\codex.exe`，SHA-256 `00c0f1e4…d67c`，`codex-cli 0.163.0-alpha.11`。复用步骤 A 的 `identity-hkzdmoqj`（COMPATIBLE）。
- 证书环境：certifi 2026.10.19，`cacert.pem` 的 SHA-256 为 `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785`；pip 26.2.2。
- 授权（用户原话，按钮消息在 300 字符处被截断）：“I approve one real S5 run on 38288c4, reusing identity-hkzdmoqj: a new dedicated thread, process B only after phase 1 passes the full health gate and A has exited, at most 2 model turns, no answers to requests, only the one planted-turn usage replay tolerated. I accept the listed limits. Stop on any”。被截断的结尾按申请原文理解为 “Stop on any anomaly, no retry.”。
- 执行通道：Agent 自己的 shell，未经沙箱；命令行与获批内容逐字一致；只执行了一次。
- **结论：PASS**。`reason_codes` 为空，退出码 0。**只认定这一次冷恢复后，历史上下文对模型可用。** 这不代表内核中的 `native_resume` 已接入，也不代表真实聊天功能已验收。

## 执行前确认

- HEAD 为 `38288c4`，工作树干净。
- 固定程序的大小、SHA-256 和签名都没有变化；步骤 A 报告和证书包的哈希都没有变化。
- 长期 home 和凭据文件存在（只检查了是否存在）。8090 代理在监听。没有残留的工作目录。
- 运行前已存在的 codex 相关进程（1628、10904、11244、1960，以及服务 9720）不是本次启动的，本次没有触碰；运行后与运行前完全相同。

## 执行

```powershell
Set-Location -LiteralPath 'E:\plug\agent-hub\.worktrees\appserver-s5-probe'
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s5.py --mode real --approve-two-model-turns --acknowledge-unverified-native-control --codex-home 'C:\Users\Administrator\AppData\Local\AgentHub\codex-home'
```

预检 11 项全部通过。输出 `S5 PASSED`，退出码 0，时间为 2026-10-22 23:54:51–23:55:10（本地）。

| 部分 | 结果 |
|---|---|
| 阶段 1（进程 A，PID 18332） | 发出 `initialize → initialized → thread/start → turn/start`。新建专用 thread `01a16452-77ab-7983-93e9-a6a44ec1ab25`，turn `01a16452-81f3-76a2-80b3-ef50d96e7ba2`，状态 `completed`。设置已核验；没有工具活动；1 条助手消息（3 个增量，6 个字符）。用量：input 15751（cached 12416）、output 7、total 15758。退出码 0；关闭前后都没有后代进程 |
| 健康检查 | `plant_health = {healthy: true, blockers: []}` |
| 进程关系 | A 已不在进程表中之后，才启动 B（PID 32056，与 A 不同）；`first_process_exited_before_second: true` |
| 恢复（进程 B） | `thread/resume` 没有被拒绝；返回的 thread ID 相同；设置已核验；`thread.turns` 中有 1 个回合，即阶段 1 的回合，状态 completed，其中含有写入暗号的那条 userMessage（`history_returned: true`）；响应中报告了 rollout 路径 |
| 用量重放 | 回忆回合 `turn/started` 之前恰好一条，带阶段 1 的 turn ID，单独记入 `replayed_usage`（`well_formed: true`）。**它的数值与阶段 1 回合的用量完全相同**（15751 / 7 / 12416 / 15758），直接印证了第一次运行的原因分析 |
| 回忆回合 | 发出 `turn/start`，turn `01a16452-9eb1-71b2-b0bf-3c1077c812bc`，与阶段 1 不同。只有一次 `turn/started(inProgress)`；匹配的 `turn/completed` 状态为 `completed`；没有工具活动和服务端请求；1 条助手消息（7 个增量，13 个字符）。回合**自己的**用量：input 16270（cached 12416）、output 11、total 16281，与重放分开记录 |
| **暗号** | 回忆提示词中不含暗号（`recall_prompt_contains_code_word: false`）；回复中含有暗号，`contains: true`，并且恰好就是暗号本身，`exact: true`。暗号长 13 个字符，结果中只保存它的 SHA-256 |
| 回合数 | 两个阶段共 2 次 `turn/start`，没有超过上限 |
| 本地清理 | B 退出码 0；关闭前后都没有后代进程；工作目录为空，已删除，没有清理错误 |
| 其他通知 | `mcpServer/startupStatus/updated`、`thread/status/changed`、`account/rateLimits/updated`、`thread/goal/cleared`、`account/updated`，都只记录了方法名 |

rollout 是同一个文件，共 29 行，**没有任何 `custom_tool_call` 或 `function_call`**：

- 第 2–16 行：阶段 1，以 `task_complete` 结束；
- 第 17、18 行：恢复时的 `thread_settings_applied`；
- 第 19–29 行：回忆回合，以 `task_complete` 结束。

两个回合的 `turn_context`（第 8、22 行）一致：on-request、reviewer user、read-only、文件系统受限、网络受限，模型 `gpt-6.1-sol`，cwd 为同一个工作目录。

stderr：两个阶段各有与以往相同的 `codex_skills_extension` os error 32；回忆阶段另有一行 `failed to refresh available models: request timed out`，没有影响回合；没有其他内容。

## 这次运行证明了什么、没证明什么

- **已证明（只针对本次运行）**：对 `codex-cli 0.163.0-alpha.11`、固定提示词和固定权限：
  - 在进程 A 中新建 thread，写入一个随机暗号；
  - A 退出之后，新进程 B 冷恢复同一个 thread：恢复响应中带回了写入的回合和那条消息，B 继续向同一个 rollout 追加内容；
  - 在 B 中开的新回合里，提示词中没有暗号，模型却**准确答出了暗号**，回合正常结束，没有任何工具调用。这说明历史上下文在冷恢复之后**对模型确实可用**。
  - 被测版本在恢复时，会对附着的连接重放一次阶段 1 的用量。
- **没有证明**：
  - Hub 内核中的 `native_resume` 已接入（本探针不接内核）；
  - 长对话、分页历史模式、多次恢复的行为；
  - 其他版本或模型上的行为；
  - 原生工具和子 Agent 的事前控制；
  - 真实聊天功能的验收。
- 第一次运行的 FAIL 保持不变。本次一次性授权已经用完，没有重试。S1–S4 的证据保持不变。

## 证据（文件不入库，只登记哈希）

| 文件 | 字节 | 修改时间 | SHA-256 |
|---|---|---|---|
| `var\appserver-s5\real-y0jtevvf\result.json` | 10836 | 2026-10-22 23:55:10 | `7019e4d788a124df10a2df1a503c1b6aba168904071bc68a086982a88819ae74` |
| `var\appserver-s5\real-y0jtevvf\stderr-plant.log` | 1150 | 2026-10-22 23:54:51 | `1c13504d246a994077721fd165ddd92e9b020149002e18d59336d8f7805bc966` |
| `var\appserver-s5\real-y0jtevvf\stderr-recall.log` | 1499 | 2026-10-22 23:55:09 | `f9a074cb58b7e61dd75468d9b31cba48b675c0d8e19212ef3205e92156ac838b` |
| 长期 home 下 `sessions\2026\10\22\rollout-2026-10-22T23-54-51-01a16452-77ab-7983-93e9-a6a44ec1ab25.jsonl`（29 行，两个阶段都在其中，含暗号、提示词和模型输出，不入库） | 62962 | 2026-10-22 23:55:09 | `c838a989df946ed8d7874323e7dc7418fe7ec21c5743a7b3155a0b049902fdf8` |
| `E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\identity-hkzdmoqj\report.json`（复用的步骤 A） | 1023 | 2026-10-22 20:41:32 | `804e69a73a748460d5da71d0a8aeacfd344da2c9eb5db74e5e2352255bf55d3b` |

`var\` 路径相对于 `E:\plug\agent-hub\.worktrees\appserver-s5-probe\`。分支合并时，把上表并入证据清单。
