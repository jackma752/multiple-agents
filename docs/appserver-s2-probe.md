# S2：一个固定回合的独立协议探针

日期：2026-10-17。分支 `feat/appserver-s2-probe`，基于 S1 提交 `bdc6b40`。

**当前只完成离线工具与验证，没有执行真实 S2。** S1 的“允许重新核验现装文件，仅做零模型 S1”不授权本次模型回合。普通 Hub 适配器的真实执行准入继续关闭；本工具只用于另行批准的诊断，不经过生产数据库，也不为生产调用预留/释放名额。

## 工具范围

- `tools/probes/appserver_s2.py`：默认启动本仓库 Python 桩进程；真实模式须同时传入两项明确确认。
- `packages/adapters/agenthub_adapters/appserver_s2_evidence.py`：按 thread、turn、item 的精确 ID 和到达顺序归集证据，先来的通知可在 RPC 响应返回后绑定。
- `tools/demo/stub_appserver_s2.py`：八个离线场景，仅读写 stdio，不联网、不调用模型、不读取凭据。

发送前白名单为 `initialize → initialized → thread/start → turn/start`。每个步骤只有一次；部分写入也计为发送尝试，不能重试。RPC 响应必须按原客户端的请求 ID 匹配；握手返回之前不发 initialized，新 thread 返回之前不发 turn。

thread 参数固定为新建的仓库外空临时目录、`sandbox=read-only`、`approvalPolicy=untrusted`。turn 参数再次固定 `sandboxPolicy={type:readOnly,networkAccess:false}` 和 `approvalPolicy=untrusted`，不指定或切换模型。唯一输入为：

```text
Reply with exactly: OK. Do not use tools, create subagents, or modify any files.
```

不能从命令行替换提示词、追加模型/配置、恢复会话、fork、发送第二回合或直接执行命令。任何服务端请求（包括工具审批、认证刷新、用户输入请求）都会使探测失败；不发送审批回答。发现工具/派生活动、其他 thread/turn、协议异常也失败并关闭本次持有句柄的进程。

**这不是工具或原生派生的事前隔离保证。** `untrusted` 可能允许服务端认为可信的命令；通知可能在活动开始后才到达。历史真实记录已经证明 `--disable multi_agent` 不能作为阻断保证。工具仍传入该参数，但保持能力 `unverified`，双重确认中的风险确认包含这些限制。提示词只表达任务要求，不充当安全阀。

## PASS 条件与状态

以下条件必须全部满足：

1. 恰好一次 `turn/start` 发送尝试，绑定非空 thread/turn ID。
2. 匹配回合恰好一个 `turn/started(status=inProgress)`。
3. 同一消息 item 的 `item/started → agentMessage/delta → item/completed` 完整有序；所有消息增量均属于该 item，拼接与最终文本均逐字为 `OK`。
4. 匹配的 `turn/completed(status=completed)` 恰好一个；`failed/interrupted` 是已知终态但不会判成功；`inProgress` 不是终态。
5. 收到匹配 thread/turn 的 `thread/tokenUsage/updated.tokenUsage.last`，保存服务端返回的非负整数用量字段，不按消息长度估算。允许用量晚于终态到达，等待上限一秒且受总期限约束。
6. 无协议/工具/派生异常、无超时，工作目录可检查且为空；自有进程已确认退出、退出码 0、reader 已收尾，没有清理错误。

真实模式协议期限 60 秒（单个 RPC 最多 20 秒）；进程清理另有 EOF、terminate、kill 的有界等待。成功回合后的观察尾窗最长一秒，不是持续监控。

`terminal_confirmed` 与 `process_stopped` **独立**。断线或超时后，即使本地进程退出码为 0，缺少匹配终态仍为 `verdict=unconfirmed`、`manual_review_required=true`。这不是已停止远端任务的声明，也不是对历史子 Agent 的结论。此工具不接内核，所以不会虚构“保留了生产配额”；真实诊断若不确定，不再追加调用，先人工核对证据。

## 固定身份、凭据与产物

复用 S1 已核验文件的预检实现，**不继承 S1 的零回合授权**：

- 文件：`C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\9614f1d9b40d9c4f\codex.exe`。
- SHA-256：`e98c516f1c8935789aabe9122ee7e05ec4c3efd1ba6acf179242e031c495b99e`；大小 `334121776`。
- argv：固定文件 `app-server --stdio --disable multi_agent`；文件不符即启动 0，不自动查找替代文件。
- home：默认 `%LOCALAPPDATA%\AgentHub\codex-home`；只检查必要元数据和本进程可写性，不读取、复制或删除凭据内容。Codex 自身会复用登录并可能写入运行状态。
- 沿用 S1 的进程专用代理/CA 环境：默认 `http://127.0.0.1:8090`、certifi PEM；不改注册表、系统代理或生产配置。
- 结构化记录在忽略的 `var/appserver-s2/<run>/result.json`。仅保存固定 `OK`、ID、事件名称/计数、usage、状态与错误类型；不保存任意回复、reasoning、请求参数或远端错误原文。原始服务端 stderr 只留本地忽略目录，发布前仍需审阅，不作为公开日志。
- 临时工作目录独立创建于系统临时目录。仅在可确认没有存活自有进程且目录为空时调用非递归 `rmdir()`；非空/无法检查时保留并记录。不递归删除任何目录。结构化证据保留。长期 home、用户 `~/.codex`、共享 daemon 与历史会话不清理。

## 离线复核与待批准命令

CMD（worktree 没有 `.venv`）：

```bat
cd /d E:\plug\agent-hub\.worktrees\appserver-s2-probe
E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\appserver_s2.py --list
E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\appserver_s2.py --mode stub
E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\appserver_s2.py --mode stub --scenario turn-failed
```

八个场景：happy → PASS；turn-failed、no-usage → FAIL，但保留已确认终态；wrong-turn、approval、tool、disconnect、timeout → FAIL/未确认。用量标记 `synthetic_fixture`，不能充当真实能力证据。

实际验证：新增 **93 项**离线测试；最终全套 `python -m pytest -o addopts='' -q` → **489 passed in 26.59s**。涵盖八种真正 Python 子进程场景、发送前授权和白名单、早到通知、跨 ID 混淆、部分发送不重试、异常工作目录仍保存审计记录。独立 CLI `--mode stub` → `S2 PASSED`，进程退出、空临时目录已移除；证据 `var/appserver-s2/stub-gxqegnrt/result.json`。复核发现并修复“忽略额外消息 item 的 delta”和“未检查 turn/started 的 inProgress 状态”两处误报；回归已包含在上述最终结果中。

下面是**尚未执行、需要单独批准**的真实命令：

```bat
cd /d E:\plug\agent-hub\.worktrees\appserver-s2-probe
E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\appserver_s2.py --mode real --approve-one-model-turn --acknowledge-unverified-native-control --codex-home "%LOCALAPPDATA%\AgentHub\codex-home"
```

最多启动一个固定 app-server、发送一次固定主回合，花费账号用量；不保证内部不派生额外工作。遇异常即结束本次探测，不重试、不切模型、不改沙箱。若当前宿主沙箱不允许 home 写入，应在获批后只对这条命令申请执行权限；不搬运凭据、不扩大模型的只读参数。没有这次真实批准时只运行离线模式。

## 证据依据与后续

方法与载荷参考 [官方 app-server 文档](https://learn.chatgpt.com/docs/app-server) 和现存本机 schema：`ThreadStartParams`、`TurnStartParams`、`ThreadTokenUsageUpdatedNotification`。文档及旧捕获不能替代当前二进制的真实 S2；本轮没有为读 schema 再启动 Codex。

S2 即使成功，也只验证固定回合的协议/增量/用量形态，不证明网页 SSE、工具事前拦截、只读拒写、取消、子 Agent 阻断或会话恢复。S3/S4/S5 仍各需单独验证；S6 内核接入前必须解决原生派生可控性。两项 `0xc0000142` 问题仍 OPEN。
