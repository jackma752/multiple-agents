# app-server S2 真实单回合验证（2026-10-17）

**结果：S2 PASS。用户单独批准后，仅执行一次固定主回合，回复逐字为 `OK`。** 工具代码版本 `f65bca8`，分支 `feat/appserver-s2-probe`。没有重试、换模型或改变模型只读参数。

## 授权与命令

用户明确答复：“批准这一次 S2，接受上述限制”。确认问题已说明会花费账号用量，原生子 Agent 禁用曾失效，不能保证工具/派生事前受控，本地关闭不保证远端停止。先前 S1 的零回合授权未被当成 S2 授权。

在 `E:\plug\agent-hub\.worktrees\appserver-s2-probe` 执行：

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s2.py --mode real --approve-one-model-turn --acknowledge-unverified-native-control --codex-home 'C:\Users\Administrator\AppData\Local\AgentHub\codex-home'
```

此次通过工具权限审核，使专用长期 home 可写；模型 thread/turn 参数仍为 read-only。预检通过固定路径、大小、SHA-256、home 位于仓库外/可写、登录凭据存在、CA 可读；身份与 [S1 实录](appserver-s1-2026-10-17.md) 相同。子进程 argv 为固定文件 `app-server --stdio --disable multi_agent`。

## 实际结果

| 项 | 结果 |
|---|---|
| 工具退出码 | 0 |
| 发送 | `initialize(id=1) → initialized → thread/start(id=2) → turn/start(id=3)` |
| turn/start 发送尝试 | **1** |
| thread_id | `01a14a36-a8d2-7f81-b603-5d369b427c91` |
| turn_id | `01a14a36-b25c-7680-b6e2-e441609bc8c9` |
| 最终回复 / 增量拼接 | **OK / OK** |
| 匹配终态 | `turn/completed(status=completed)`；`terminal_confirmed=true`、`verdict=finished` |
| 归集器七项检查 | 全部 true |
| 自有 app-server | PID **7692**，确认退出，退出码 **0**，reader 已收尾 |
| 协议 / reader 错误 | `client_violation=false`、`reader_error_type=null` |
| 工作目录 | 本次独占仓库外临时目录，事后为空，已非递归移除 |
| 需人工核对的未确认状态 | `manual_review_required=false` |

独立服务端用量事件 `thread/tokenUsage/updated` 返回的 `last`：

```json
{"inputTokens":15043,"outputTokens":5,"cachedInputTokens":12544,"reasoningOutputTokens":0,"totalTokens":15048,"cacheWriteInputTokens":0}
```

仅记录 token 字段；没有换算金额，不把 cached tokens 另外加到总量。

thread 绑定后记录的通知顺序（共 14 个；不是整个握手期间的所有通知）：

```text
mcpServer/startupStatus/updated
thread/started
thread/status/changed
turn/started
mcpServer/startupStatus/updated
item/started
item/completed
item/started
item/agentMessage/delta
item/completed
thread/tokenUsage/updated
account/rateLimits/updated
thread/status/changed
turn/completed
```

关键流为同一消息 item 的 started → delta → completed，增量拼成 OK；用量事件在回合完成前到达。本次不能验证所有可能的通知交错。

## 证据、清理与能力边界

脱敏结果：`var/appserver-s2/real-5j0vung8/result.json`；原始 stderr 216 字节，留在同一被 Git 忽略的本地目录。未提交原始日志或凭据。stderr 对 `c0000142`、`UnknownIssuer`、`Reconnecting`、`timed out`、`failed to refresh available models` 的计数均为 0；**两项 DLL 问题仍 OPEN**。

仅本次空目录 `%TEMP%\agenthub-s2-work-j78wzkom` 被移除，自有 app-server 已退出。工具未读取/复制/删除凭据内容；Codex 自身复用登录并可能写长期 home 的运行状态。没有删除长期 home、用户 `~/.codex`、历史会话，也没有处理共享 daemon 或历史子 Agent。

本次验证了当前文件身份下的一次最小真实 app-server 回合、消息增量、最终文本、匹配终态与真实用量。**不能据此升级**原生子 Agent 控制、工具启动前审批、只读拒写、取消或会话恢复。没有出现相应活动通知不构成不存在后台工作的证明。普通 Hub 真实执行仍被准入门禁拒绝，app-server 未接入 `build_adapter()`，网页 SSE 未实现。

## 代码验证与下一步

运行前最终全量 **489 passed in 26.59s**，新增 93 项。真实运行之后只更新证据与状态文档，没有修改代码、没有再跑模型回合。

S1、S2 已完成，不重复执行。下一步准备 S3 工具审批往返探针与离线对照，真实 S3 仍需另行批准；S4 中断、S5 恢复、S6 内核接入尚未完成。保持独立分支，无合并、推送或旧服务重启。
