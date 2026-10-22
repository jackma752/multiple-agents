# S5 真实运行记录·第一次（2026-10-22）：FAIL——恢复后重放的用量事件被误判为外来 turn，回忆回合未被跟随

- 所测提交：`feat/appserver-s5-probe` @ `2cda212f4a240a436e29c2d405dc194d3a7298ec`。
- 被测程序：`bin\6257d9e8f63c0f2b\codex.exe`，SHA-256 `00c0f1e4…d67c`，`codex-cli 0.163.0-alpha.11`。复用步骤 A 的 `identity-hkzdmoqj`（COMPATIBLE）。
- 证书环境：certifi 2026.10.19，`cacert.pem` 的 SHA-256 为 `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785`；pip 26.2.2。
- 授权（用户原话）：“I approve one real S5 run on 2cda212, reusing identity-hkzdmoqj: a new dedicated thread, process B only after phase 1 passes the full health gate and process A has exited, at most 2 model turns, no answers to requests. I accept the listed limits. Stop on any anomaly, no retry.”
- 执行通道：Agent 自己的 shell，未经沙箱；命令行与获批内容逐字一致；只执行了一次，**没有重试**。
- **结论：FAIL**，原因代码为 `recall:foreign_turn`、`recall:terminal_not_matched`、`recall:turn_not_started_once`。
  - 阶段 1、健康检查、进程关系、恢复结构都通过了。
  - 回忆回合刚开始，探针就因为一个它判为“外来 turn”的事件停止了进程，所以**历史上下文是否恢复，没有得到验证**。
  - 按源码推断，这个事件是被测版本在恢复时**正常重放**的用量通知，属于探针判定缺陷，见“原因”一节。

## 执行前确认

- HEAD 为 `2cda212`，工作树干净。
- 固定程序的大小、SHA-256 和签名都没有变化；步骤 A 报告的哈希没有变化。
- certifi 证书包的哈希如上。长期 home 和凭据文件存在（只检查了是否存在）。8090 代理在监听。没有残留的工作目录。
- 运行前已存在的 codex 相关进程（ChatGPT 应用的 1628、10904、11244、1960，以及系统服务 9720）不是本次启动的，本次没有触碰。

## 执行

```powershell
Set-Location -LiteralPath 'E:\plug\agent-hub\.worktrees\appserver-s5-probe'
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s5.py --mode real --approve-two-model-turns --acknowledge-unverified-native-control --codex-home 'C:\Users\Administrator\AppData\Local\AgentHub\codex-home'
```

预检 11 项全部通过，退出码 1，时间为 2026-10-22 23:19:53–23:20:01（本地）。

| 部分 | 结果 |
|---|---|
| 阶段 1（进程 A，PID 10944） | 发出 `initialize → initialized → thread/start → turn/start`。新建 thread `01a16432-7556-7e71-9af8-ade1723188a6`，turn `01a16432-8237-7592-af40-522094590625`，状态 `completed`。设置已核验；没有工具活动；1 条助手消息（3 个增量，6 个字符，内容不保存）。用量：input 15754（cached 12416）、output 7。进程退出码 0；关闭前后都没有后代进程 |
| 健康检查 | `plant_health = {healthy: true, blockers: []}` |
| 进程关系 | A 已不在进程表中之后，才启动 B（PID 13496，与 A 不同）；`first_process_exited_before_second: true` |
| 恢复（进程 B） | `thread/resume` 没有被拒绝；返回的 thread ID 相同；设置已核验；`thread.turns` 中有 1 个回合，即阶段 1 的回合，状态 completed，其中含有写入暗号的那条 userMessage（`history_returned: true`）；响应中报告了 rollout 路径 |
| 回忆回合 | 发出 `turn/start`，绑定 turn `01a16432-9ec7-7631-be2f-fa1d54f0674d`。探针在此之前收到了一条 `thread/tokenUsage/updated`，turn 绑定后，该事件的 turn ID 与新 turn 不同，被判为 `foreign_turn`，探针**立即停止**，关闭进程 B。所以没有观察到回忆回合的 `turn/started` 或任何输出，`recall.contains` 为空 |
| 回合数 | 两个阶段共发出 2 次 `turn/start`，没有超过上限 |
| 本地清理 | 进程 B 退出码 0；关闭前后都没有后代进程；工作目录为空，已删除。运行后的 codex 相关进程与运行前完全相同 |
| 其他通知 | 回忆阶段另有一条 `thread/goal/cleared`，只记录了方法名 |

rollout 是同一个文件，共 23 行，记录了两个阶段：

- 第 2–16 行：阶段 1，`task_complete`。
- 第 17、18 行：恢复时的 `thread_settings_applied`。
- 第 19–21 行：回忆回合开始，`task_started` 与用户消息。
- 第 22、23 行：约 40 ms 后 `turn_aborted / interrupted`，由探针关闭进程 B 引起。回忆回合**没有任何模型输出**。

第二个进程向**同一个** rollout 追加了内容，这本身就是“冷恢复读写的是磁盘上的同一个 thread”的旁证。

stderr：

- 两个阶段各有与以往相同的 `codex_skills_extension` os error 32；
- 阶段 1 另有一行 `failed to refresh available models: request timed out`（模型列表刷新超时，没有影响本回合）；
- 没有其他内容。

## 原因（推断：事件的 turn ID 没有保存，无法逐字核对）

被测版本 [app-server/src/request_processors/token_usage_replay.rs @ alpha.11](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/app-server/src/request_processors/token_usage_replay.rs)：

- 连接附着到一个 thread 时，会向该连接单独发送一条“恢复后的用量更新”`thread/tokenUsage/updated`。注释写明这是生命周期重放，不是模型事件。
- 其 turn ID 取“最近一次 TokenCount 记录时处于活动状态的回合”。

本次 rollout 中，恢复之前最后一条 token 记录（第 14、15 行）属于阶段 1 的回合。所以这条通知带的是**阶段 1 的 turn ID**。

探针的规则是“绑定 turn 之后，凡是 turn ID 不同的绑定事件都算外来”，没有为这种合法重放留出例外，于是误判为越界，并在回忆回合刚开始时停止了进程。

## 这次运行证明了什么、没证明什么

- **已证明（只针对本次运行）**：对 `codex-cli 0.163.0-alpha.11`：
  - 新建 thread 的写入回合正常完成；
  - 进程 A 退出之后，新进程 B 能冷恢复同一个 thread；
  - 恢复响应中带回了写入的回合和那条消息，并且第二个进程向同一个 rollout 追加了内容。
- **没有证明**：
  - **模型在恢复后是否看得到历史**：回忆回合被探针提前中止了，这是 S5 的核心，仍然未验证；
  - 回忆回合的终态和用量；
  - Hub 内核的 `native_resume`；
  - 真实聊天功能的验收。
- 本次一次性授权已经用完；没有重试，没有换模型。S1–S4 的证据保持不变。

## 证据（文件不入库，只登记哈希）

| 文件 | 字节 | 修改时间 | SHA-256 |
|---|---|---|---|
| `var\appserver-s5\real-xkcouffu\result.json` | 8407 | 2026-10-22 23:20:01 | `586d73c939947ae5dc45c1c653a03c09354b52a23c985233ad1143fd8ea5dfe2` |
| `var\appserver-s5\real-xkcouffu\stderr-plant.log` | 1269 | 2026-10-22 23:19:55 | `6e6ec0e8e697d3da834d74b724c2a4fafbb6d4276ce8b9768e60c6e97fa409b5` |
| `var\appserver-s5\real-xkcouffu\stderr-recall.log` | 1380 | 2026-10-22 23:20:01 | `4e5898efe09047f34f28b17aa226792f925f4bb9360d3a545231b16d0a2870b9` |
| 长期 home 下 `sessions\2026\10\22\rollout-2026-10-22T23-19-53-01a16432-7556-7e71-9af8-ade1723188a6.jsonl`（23 行，两个阶段都在其中，含暗号、提示词和模型输出，不入库） | 55200 | 2026-10-22 23:20:01 | `d6977a02fd97b15d60b3f23a82bb42f0add87917b72a5d2ad8523d74a93593fb` |
| `E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\identity-hkzdmoqj\report.json`（复用的步骤 A） | 1023 | 2026-10-22 20:41:32 | `804e69a73a748460d5da71d0a8aeacfd344da2c9eb5db74e5e2352255bf55d3b` |

`var\` 路径相对于 `E:\plug\agent-hub\.worktrees\appserver-s5-probe\`。

## 对下一步的影响（只是记录，未实施）

如果继续 S5，需要先离线修订判定：在回忆阶段，**只**接受这一类重放事件。条件是：

- 方法为 `thread/tokenUsage/updated`；
- turn ID 恰好等于阶段 1 的 turn；
- 出现在回忆回合的 `turn/started` 之前；
- 最多一条。

这条事件单独记为重放用量，不计入回忆回合的用量。其他外来 ID 仍判 FAIL。修订完成并通过离线验证后，再另行申请一次真实 S5。
