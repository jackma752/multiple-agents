# S6-3 第一次真实运行：PASS（只认定本次经过 Hub 的冷恢复后历史可用）

2026-10-23（本地时间）。申请见 [S6 规划](appserver-s6-plan.md)第 11 节（修订后）。

**用户指示原文**：“先修复 S6-3 两处判定问题，通过后执行一次真实运行：第二次调用必须为 succeeded、回合正常完成且暗号匹配，才能 PASS。failed 即使带有正确暗号也不能通过。后代检查明确区分‘已检查且为空’和‘检查不可用’。第一阶段不可用时，不提交第二次调用；第二阶段不可用时判 INCONCLUSIVE。补充失败回合带／不带暗号，以及两阶段后代检查不可用的反例，运行针对性回归后提交。更新执行申请中的被测提交，其余范围保持不变，不重跑 S6-2。”

判定修正提交为 `553fe1f`（针对性回归 744 passed）。之后执行了这一次真实运行。

## 执行前确认

| 项目 | 结果 |
|---|---|
| 提交与工作树 | HEAD `553fe1f`（`feat/appserver-s6`），工作树干净 |
| 身份环境变量 | 没有设置任何 `AGENT_HUB_*` 变量 |
| 固定程序 | `bin\6257d9e8f63c0f2b\codex.exe`，342187312 字节，SHA-256 `00c0f1e4…cd67c`，与常量一致 |
| 步骤 A 证据 | `identity-hkzdmoqj/report.json` 的 SHA-256 为 `804e69a7…5d3b`，没有变化（复用） |
| 证书包 | `50f79f98…0785` |
| 代理 | 127.0.0.1:8090 在监听 |
| CODEX_HOME | 存在，登录文件存在（没有读取内容） |

## 命令

```
Set-Location E:\plug\agent-hub\.worktrees\appserver-s6
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s6_3.py --mode real --approve-two-model-turns --acknowledge-supervised-boundary
```

输出 `S6-3 PASS []`，退出码 0。只执行了这一次。

## 结果

- **驱动预检**：9 项全部通过；监督准入放行，严格准入仍然阻止。绑定身份为 alpha.11 的固定身份。
- **调用 1**（写入，`inv-450d3a92e981`，独立审批并显式同意）：
  - 实际发出 `thread/start`，新 thread `01a16529-4ae8-7d93-83de-6e6901ff9662`，turn `01a16529-5418-…`；
  - 状态 succeeded，回合以 completed 结束，回复恰好是 `STORED`，用时 10.05 秒；
  - 用量：input 15785，cached 12416，output 7；
  - 没有工具活动，没有子 Agent，没有异常事件；本地进程（PID 8112）确认退出；后代检查两份快照都在，并且为空；名额已释放，回复中记录了 native_session。
- **进程关系**：PID 8112 离开进程表之后，才提交调用 2。
- **调用 2**（回忆，`inv-194e66c74f3a`，独立审批并显式同意）：
  - 内核绑定的 `resume_thread_id` 就是调用 1 的 thread；
  - 实际发出 `thread/resume`，返回同一个 thread；恢复结构中有 1 个回合，就是调用 1 的回合（completed，含 userMessage）；
  - 新 turn `01a16529-7271-…`，与调用 1 不同，没有复用；上一回合等于调用 1 的回合；
  - 实际发送的输入的哈希等于回忆提示词的哈希（`12a31114…efb8`），不含暗号；
  - 沿用调用 1 的工作目录；
  - 状态 succeeded，回合以 completed 结束，用时 7.86 秒；
  - 回复中含有暗号，并且恰好等于暗号（含有、恰好等于都为 true）。暗号只记录了 SHA-256（`3801b3a4…2bb8`）；
  - 用量：自己的用量为 input 16304，cached 15616，output 13。恢复时重放了一条上一回合的用量，数值与调用 1 的用量完全相同（15785/7），单独记入 `replayed_usage`，没有计入调用 2；没有用量异常；
  - 没有工具活动，没有子 Agent，没有异常事件；本地进程（PID 33168）确认退出；后代检查两份快照都在，并且为空；名额已释放。
- **预算**：模型回合 2 个，每次调用各启动 1 次，没有第三次调用。
- **工作目录**：`<系统临时目录>\agenthub-s6-3-data-v2ez4iud\codex-runs\inv-450d3a92e981`，在仓库外，运行后为空。
- **rollout 结构核对**（同一个文件包含两个回合；只统计记录类型，不读取正文）：
  - 两个 `turn_context` 都是 on-request、read-only，cwd 都是上述工作目录；
  - `multi_agent_version` 都是 `v2`，这只是模式字段，不是派生的证据；
  - payload 类型中没有 `function_call` 或 `custom_tool_call`。

## 观察到的现象

- 两份 stderr 中只有已知的 `failed to install system skills ... (os error 32)`，分别是 5 行和 6 行。与 S3、S4、S6-2 相同，是环境里已有的现象，不影响判定。
- 运行结束后，系统中仍只有 ChatGPT.exe 在 10-22 启动的 2 个 `codex.exe`，与本次运行无关。

## 只认定 / 不认定

- **只认定**：这一次经过 Hub（进程内的 API、审批、Worker、内核、app-server 适配器）的冷恢复可用：新进程、同一个 thread、同一个工作目录；回忆提示词中没有暗号，模型答出了暗号。两次调用各自审批并显式同意，各自结算并释放了名额。
- **不认定**：
  - 网页点击路径；
  - 长对话、分页历史、多次恢复；
  - 换 cwd 后恢复；
  - 工具审批（S6-4）、中断（S6-5）；
  - 原生子 Agent 的控制；
  - 其他版本或模型。

## 证据（不入库，只登记哈希）

| 文件 | SHA-256 |
|---|---|
| `var/appserver-s6-3/real-u5ktawg5/result.json`（只含暗号哈希和匹配结论） | `79cb1cccd5447df3c14e5cf83cf0b52fa66e9b0e78dcdf8d685a8c68571d0cd5` |
| `<数据目录>/codex-runs/_logs/inv-450d3a92e981-appserver-stderr.log`（1150 字节） | `14f692fdc1969e78a964a8bcb0863ae22acfe0ae5e79ec23af675dfa64d6aa46` |
| `<数据目录>/codex-runs/_logs/inv-194e66c74f3a-appserver-stderr.log`（1380 字节） | `4f7557300a449b17897c5cade7c5f65e16e267fe19af14397a208f3a4f8c6bc9` |
| CODEX_HOME 中这个 thread 的 rollout（63403 字节，两个回合） | `b960db2629b9e32697a2f572d8fd12d45c3e55a08507286aa532e0fa426e217e` |

数据目录中的数据库含有两次的提示词和回复（含暗号原文），留在仓库外，不入库。

授权已用完。没有重试，没有执行 S6-4、S6-5、S6-V 或 S7，没有重跑 S6-2。
