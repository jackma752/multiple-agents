# Claude Code 第一次真实单回合（2026-10-24，PASS，只认定本次）

- **被测提交**：`feat/claude-code-min` @ `c554fd1`。执行前确认：
  - HEAD 未变，工作树干净；
  - 固定的 `claude.exe` 为 297,376,928 字节，SHA-256 `c9796ba2…ea53`，与常量一致；
  - 8090 在监听；certifi 哈希为 `50f79f98…0785`；
  - 没有 `AGENT_HUB_*` 变量；专用配置目录中有登录文件（只检查是否存在，没有打开）。
- **登录**：用户在自己的终端里登录专用配置目录（第 8.2 节）。助手没有参与，也没有读取凭据。
- **授权**（用户原话）：“批准在 feat/claude-code-min c554fd1 上按 docs/claude-code-adapter.md 第 8.3 节执行一次步骤 B：最多 1 个模型回合；任何异常立即停止，不重试。”
- **命令**（只执行一次，退出码 0，输出 `TURN PASS []`）：

  ```powershell
  Set-Location E:\plug\agent-hub\.worktrees\claude-code-min
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\claude_code_min.py --step turn --mode real --approve-one-model-turn --acknowledge-supervised-boundary
  ```

## 结果

| 项目 | 结果 |
|---|---|
| 预检 | 9 项全部通过：身份报告已记录、启动器为常量构造的真实启动器、配置目录在仓库外且有登录文件、工作目录在仓库外、CA 一致、监督准入放行、严格准入仍被阻止、固定身份一致 |
| 绑定身份 | `claude-code@2.1.308;sha256=c9796ba2…ea53;size=297376928`，与快照一致 |
| 调用 | `succeeded`；启动 1 次；名额已释放（held 0）；run 返回 HTTP 200，耗时 4.11 秒 |
| 进程 | 退出码 0，没有超时，耗时 3.39 秒；stdout 为一个 result 对象（1588 字节，解析后删除）；stderr 为空 |
| result | `subtype: success`，`is_error: false`，`num_turns: 1`，`permission_denials` 为 0 |
| 回复 | 恰好是 `OK` |
| 用量 | input 2，cache_creation 1750，cache_read 0，output 4；`total_cost_usd` 估算 0.014088；模型 `claude-opus-5-5` |
| 固定参数 | `-p --output-format json --safe-mode --tools "" --strict-mcp-config --disable-slash-commands --permission-mode dontAsk --max-turns 1 --max-budget-usd 1.00 --no-session-persistence`，**全部被 CLI 接受**，包括 `--help` 中没有列出的 `--max-turns` |
| 环境 | 去掉了继承的 `ANTHROPIC_BASE_URL`、全部 `CLAUDE*` 会话变量和代理变量；设置了专用 `CLAUDE_CONFIG_DIR`、`DISABLE_AUTOUPDATER`、关闭非必要流量、代理 8090、`NODE_EXTRA_CA_CERTS`（追加证书，不关闭验证） |
| 工作目录 | 为空 |

**只认定**：
- 固定版本 2.1.308 能以这组参数非交互运行一次，`--max-turns` 被接受；
- stdin 传入提示词可用；JSON result 的实际形状与文档一致；
- 回复和用量能经过 Hub（进程内 API、审批与本次同意、Worker、适配器）结算并写入会话；
- 代理路径可用。

**不认定**：
- 工具确实被关闭：模型没有尝试使用工具，不等于不能用；
- 原生子 Agent 可控；取消或中断；流式；会话恢复；后代进程（本适配器不检查）；
- 配置目录之外的文件是否被写入；
- 长对话或其他提示词下的行为；
- 金额：`total_cost_usd` 是客户端估算，不是账单。

## 证据（文件不入库，只登记哈希）

| 文件 | 字节 | SHA-256 |
|---|---|---|
| `E:\plug\agent-hub\.worktrees\claude-code-min\var\claude-code-min\turn-real-jrjo5m_r\report.json` | — | `f091ca8a23926a6a7f4d3bb9b07803ac725e71190269dcd2fc6b3b22a419f619` |
| `%TEMP%\agenthub-cc-data-ui59w88g\runs\_logs\inv-02f490837135-claude-stderr.log` | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`（空文件） |

数据目录中的数据库（含提示词和回复）留在仓库外，供审计。
