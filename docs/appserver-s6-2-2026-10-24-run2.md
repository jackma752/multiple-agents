# S6-2 第二次真实运行：新运行时上的冒烟（2026-10-24，PASS，只认定本次）

- **被测提交**：`feat/codex-runtime` @ `e7237a3`。执行前确认 HEAD 未变、工作树干净、进程环境中没有 `AGENT_HUB_*` 变量、代理 8090 在监听、暂存的运行时存在。
- **被测程序**：AgentHub 副本 `%LOCALAPPDATA%\AgentHub\runtimes\codex\sha256-c81ed464e5e7cafd\codex.exe`，`codex-cli 0.163.0-alpha.11.2`（步骤 A 的结果为 IDENTICAL_SCHEMA，见 [Codex 运行时](codex-runtime.md) 第 7 节）。
- **授权**（用户原话）：“批准在 feat/codex-runtime e7237a3 上按 docs/codex-runtime.md 第 8 节执行一次 S6-2 冒烟：最多 1 个模型回合；任何异常立即停止，不重试，不接着执行 S6-4。”
- **命令**（只执行一次，退出码 0，输出 `S6-2 PASSED`）：

  ```powershell
  Set-Location E:\plug\agent-hub\.worktrees\codex-runtime
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s6_2.py --mode real --approve-one-model-turn --acknowledge-supervised-boundary
  ```

## 结果

| 项目 | 结果 |
|---|---|
| 前置检查 | 9 项全部通过：没有身份覆盖、factory 为常量构造的真实 factory、固定身份一致、CA 哈希一致、CODEX_HOME 在仓库外且有登录文件并可写、监督准入放行、严格准入仍被阻止 |
| 绑定身份 | `codex-app-server@codex-cli 0.163.0-alpha.11.2;sha256=c81ed464…122a;size=336134960`，与快照一致 |
| 调用 | `succeeded`；启动 1 次；名额已释放（held 0）；run 返回 HTTP 200 |
| 事件顺序 | starting(4) < turn_started(5) < 第一条 delta(6) < turn_ended(7) < succeeded(9) |
| 回复 | 恰好是 `OK` |
| 回合 | `turn/completed`，状态 completed；没有恢复，没有重放用量 |
| 用量 | 输入 15,183（缓存 12,416，缓存写入 0），输出 5，推理 0，合计 15,188 |
| 工具与子 Agent | 没有工具活动、工具卡、被拒或未答的请求，也没有原生子 Agent；`tool_process_state` 为 none |
| 事件异常 | 外来事件 0，迟到事件 0，用量异常 0 |
| 清理 | 本地进程确认退出；关闭前和宽限期 3 秒后都没有后代进程；工作目录为空 |
| stderr | 5 行，都是已知的 `failed to install system skills`（文件被占用）ERROR，S3、S4 和 S6-2 第一次运行中都出现过 |

**只认定**：在 alpha.11.2 的 AgentHub 副本上，经过 Hub（进程内 API、内核、适配器、事件库）的 supervised 最小回合可用一次。

**不认定**：工具审批（S6-4）、中断（S6-5）、续接、网页路径、子 Agent 控制、沙箱。S6-4 没有接着执行。

## 证据（文件不入库，只登记哈希）

| 文件 | 字节 | SHA-256 |
|---|---|---|
| `E:\plug\agent-hub\.worktrees\codex-runtime\var\appserver-s6-2\real-p2pmgzqg\result.json` | 2391 | `a0016a053f8bd804eb3fc87e9c26bcd67de8f22d2e5c686a2c8be8bbc669bf38` |
| `%TEMP%\agenthub-s6-2-data-p4ykfhq8\codex-runs\_logs\inv-031359bbcf53-appserver-stderr.log` | 1150 | `a530cc635d24f1cb796a9023ace324f55e37156b5e1797352699225932d58427` |
| `%LOCALAPPDATA%\AgentHub\codex-home\sessions\2026\10\24\rollout-2026-10-24T00-22-34-01a16992-36d1-7b11-abe9-6c6782cb532b.jsonl` | 46149 | `b0d1b07680b63055c24009a6ba6bf59116d872c11913d828ebd72393103a2aee` |

rollout 中含提示词、模型输出和账号限额信息，这里只登记哈希，没有读取。数据目录中的数据库留在仓库外，供审计。
