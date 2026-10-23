# 双 Agent 真实网页对话第二次运行（2026-10-24，PASS，只认定本次）：达到阶段 T 的启动节点

- **被测提交**：`feat/p3-two-agent` @ `5a2f55a`，即第一次失败后的四处修正。
- **授权**（用户原话）：“批准在 feat/p3-two-agent 5a2f55a 上按 docs/multi-agent.md 第 6 节执行一次双 Agent 真实网页对话：启动后先跑 --preflight，必须 PASS 才继续；Codex 1 个回合、Claude Code 1 个回合、一次经批准的交接；任何异常立即停止，不重试。”
- **被动核验**：全部通过。
  - HEAD 未变，工作树干净；
  - Codex 暂存运行时和存档一致；`claude.exe` 一致；certifi 一致；8090 在监听；8174 空闲；
  - 两个登录文件都存在（只检查是否存在）；没有 `AGENT_HUB_*` 变量；`agenthub-two-agent-run2` 事先不存在。
- **暗号**：重新即时生成，没有复用第一次的。这里只记录 SHA-256：`9cb90df5…d666`。

## 经过

1. 用 `p3-two-agent-real` 启动服务（已加 `--codex-transport app-server`，数据目录为 run2）。启动信息逐个 Agent 显示：
   - codex：`transport=app-server supervised=supervised`，binding 为 alpha.11.2 的固定身份；
   - claude-code：`transport=claude-code-cli supervised=supervised`，binding 为 2.1.308 的固定身份。
2. `two_agent_collect.py --preflight http://127.0.0.1:8174`：**PREFLIGHT PASS**。两个 Agent 都是 `identity_ok: true`、监督准入放行、严格准入阻止，codex 的 transport 为 app-server。
3. 网页连接后，选 codex 时页头标签为 `codex · app-server · blocked`（blocked 指严格模式），两个 Agent 的“用户监督模式”都没有限制标注。
4. 会话 A：codex，用户监督模式，发送固定提示词；勾选本次同意，点“同意并调用”。结果：已完成，provider 报告 completed，回复为暗号；用量为输入 15,753、输出 12。
5. 新建会话 B（没有消息，页面显示“还没有消息”），再从列表切回 A：历史完整。
6. 在 Codex 回复上点“交给其他 Agent”。选 claude-code，用户监督模式，输入不含暗号的固定指令，发送。审批卡显示：
   - `交接：codex #2（14 字）`；
   - “将发送的完整内容（185 字）”，暗号只出现在 `--- Handoff: reply of codex (Hub message 2) ---` 框内；
   - 监督边界文案来自 Claude Code 适配器自己。

   勾选本次同意，点“同意并调用”。结果：已完成，回复为同一个暗号；用量为输入 2、输出 15。
7. 截图，停止服务，运行一次判定脚本。
- 浏览器控制台中的错误都来自运行前：第一次运行留下的标签页在两次运行之间服务停止时不断重连（`ERR_CONNECTION_REFUSED`），以及重启后对旧会话的 404。本次的操作没有产生错误。

## 判定

`two_agent_collect.py --data-dir <run2> --code <暗号>`：**PASS**，`reason_codes` 为空。

| 项目 | 结果 |
|---|---|
| 规模 | 2 个会话，2 次调用，顺序为 codex → claude-code，都是 supervised，都 succeeded，审批都已消费，名额都已释放 |
| Codex | 绑定为固定身份；回复恰好为暗号；turn completed；没有工具活动或工具卡，没有原生子 Agent；事件和用量没有异常；本地进程确认退出，没有遗留后代进程；工作目录为空 |
| 交接 | 恰好 1 条，来源是 Codex 那条回复（message_id、agent、SHA-256 都一致）；发送内容等于“用户原文 + 固定交接框”；用户原文中没有暗号 |
| Claude Code | 绑定为固定身份；回复恰好为暗号；`num_turns=1`，权限拒绝为 0，退出码 0，没有超时；有用量；工作目录为空 |
| stderr | Codex 只有已知的 `failed to install system skills` ERROR；Claude Code 为空 |

**只认定**：
- 两个真实 Agent 经过同一个 Hub 网页，各完成一次 supervised 调用；
- 会话切换后历史保持；
- 一次经用户批准的交接把 Codex 的回复送到了 Claude Code。Claude Code 只能从交接框中得到暗号，并原样答出。

这一次满足阶段 T 的启动节点：两个真实 Agent、会话切换、经批准的结果交接。

**不认定**：
- 工具或原生子 Agent 的控制、取消或中断、Claude Code 的流式和会话恢复；
- 长对话或复杂交接的质量；
- 用量与预算（属于阶段 T）；
- 其他版本或模型。

## 证据（文件不入库，只登记哈希）

| 文件 | 字节 | SHA-256 |
|---|---|---|
| `E:\plug\agent-hub\.worktrees\p3-two-agent\var\two-agent\real-n_bfpgdh\result.json` | 1466 | `00e673d645d43ec5402756c3d18bfa046e20ff93b32b7914d881b1de41504149` |
| `%TEMP%\agenthub-two-agent-run2\codex-runs\_logs\inv-73b6f774dfa8-appserver-stderr.log` | 1315 | `b37d88d101ce1820a1d4fd5eccd291f7956032366ecb6e02a7d3c550929d1775` |
| `%LOCALAPPDATA%\AgentHub\codex-home\sessions\2026\10\24\rollout-2026-10-24T01-25-31-01a169cb-d9d2-7052-8f12-80bd1c8ae787.jsonl` | 50867 | `faf21c347e0ad60dcf84d8f70d1fc26a4790961e00b9e365933a8fd0b8b62d32` |
| `%LOCALAPPDATA%\AgentHub\claude-code-runs\_logs\inv-d0f1cfdf22cd-claude-stderr.log` | 0 | `e3b0c442…b855`（空） |
| 最终会话截图（会话工具结果目录 `mcp-Claude_Browser-blob-1792776409192-w1ubkc.jpg`） | 23794 | `9e9350bd75433ce05603db83b58b5ab15bbe9bfcfbf040c9cce2f5e351b76eae` |

rollout 和数据目录中的数据库含提示词和回复，只登记哈希，留在仓库外。
