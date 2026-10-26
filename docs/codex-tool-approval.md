# Codex 真实工具的单次允许与拒绝

2026-10-26，用户要求补齐真实 app-server 工具审批。分支 `feat/codex-tool-approval`，从 main `4d717be` 建立。本次实现替代 S6 首版的“真实通道只能拒绝”；没有改变严格模式准入结论。**代码与离线链路已验证，首次真实工具允许仍待用户页面验收。**

## 用户看到的行为

1. 启动 Agent 调用仍需要逐次同意，或有效的阶段授权。
2. 收到本回合的命令执行/文件修改审批请求，显示“允许此操作”和“拒绝此操作”。命令卡显示原文、工作目录和申请的额外权限；文件卡显示路径、变更类型和差异，保留换行。
3. 允许只回答当前请求，不保存命令前缀、会话允许或策略修订。下一张卡仍单独决定。
4. 卡片显示实际送达的 `accept` / `decline`；这只说明决定已经发出，执行是否成功仍以工具和回合记录为准。拒绝和超时不会改发 `cancel` 去中断整个回合。
5. 中断、停止、过期、已结束的请求和重复点击不会产生新的允许。接入方撤回某一请求时，只作废对应卡片，不清空其他卡片。
6. 顶栏明确显示“用户监督可用 · 严格受限”，避免把严格准入的 `blocked` 误读成整条通道不可用。

对于没有提供单次 `accept`、扩大整场会话可写范围的 `grantRoot`、托管网络访问和独立权限授予请求，卡片显示具体不能允许的原因，保留拒绝。详情超过完整预览上限（单字段序列化后 8000 字符）时，请 Agent 分拆请求；不会通过截断后的内容批准整段未显示操作。

## 后端实现与契约

- 固定的 `CodexAppServerFactory` 允许请求级审批；原文件哈希/版本绑定、固定启动参数、用户监督同意、核验后的 read-only/on-request/user 设置，以及调用/预算门禁继续生效。未知工厂不因此获得准入。
- `request_decisions()` 根据请求方法、原始可选项和授权范围生成 Hub 的 `accept/decline` 选项；原始选项保留在鉴权卡片详情的 `provider_available_decisions`，不映射成会话允许按钮。`approval_scope=request` 表示单次范围；不能允许的原因存入 `approval_block_reason`。
- 审批必须提交当前 `card_hash`，绑定调用、JSON-RPC 请求、thread、turn、item、展示内容及期限。Worker 的工具 gate 在发送前原子认领，原有关闭/暂停/停止语义不变；适配器在送达时再核对请求范围。
- `item/started` 中的文件变更只用于鉴权卡片的审阅预览；正文不进入公共事件或无内容的工具活动摘要。
- 每轮推进先处理已入队的终态与 `serverRequest/resolved` 通知，再决定是否发送审批。`ToolGate.void(card_id)` 只作废该请求尚未发出的决定。已经认领为 sending 的决定仍可能在关闭后到达，不能声称能撤回。
- 独立 `item/permissions/requestApproval` 只能以 `permissions={}`、`scope=turn` 拒绝；不能将命令式的 `accept` 响应误用为权限授予。永久规则、`acceptForSession` 和策略修订未接入。
- 阶段自动规则可决定普通单次命令/文件卡，但不会因为命令前缀匹配而授予 `additionalPermissions` 或网络/会话范围。

协议依据：[OpenAI App Server 审批文档](https://learn.chatgpt.com/docs/app-server#approvals)，并对照本机固定 alpha.11.2 的已保存 schema。当前协议会继续演进，本次没有更换固定二进制，也没有执行新的真实探针。

## 离线验证

`tests/http/test_codex_tool_allow.py` 用假固定文件替代二进制，保留真实工厂类型、监督准入、审批接口及发送路径，只将底层传输接到离线协议 peer。peer 只有收到 accept 才写临时测试标记，因此可以检查审批前/拒绝后没有动作、允许后恰好对应本请求。

覆盖命令/文件允许与拒绝、错误 hash、重复点击、第二请求独立决定、会话授权不误放行、独立权限拒绝的响应格式、原请求被撤回及其他 thread 不影响本卡、额外权限不被自动规则隐式授权、完整预览上限。另用 bundled stdio 桩走文件审批。

浏览器离线走查：命令允许 → 文件差异展示并允许 → 命令拒绝，卡片记录与回合完成相符，没有页面脚本错误。隔离演示使用临时测试端口，与用户 8080 实例的数据分开；脚本、模拟数据和截图位于忽略的 `var/tool-approval-browser/`。最终实际测试数量见当前交接。

本轮最终受影响范围验证（没有运行全量测试）：

```text
python -m pytest -o addopts='' -q tests/http/test_codex_tool_allow.py tests/http/test_appserver_s6.py tests/http/test_async_and_tool_approval.py tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py tests/http/test_conversation_stream.py tests/http/test_chat_page.py tests/adapters/test_appserver_client.py tests/adapters/test_appserver_stdio.py tests/test_appserver_s6_4_collect.py --basetemp=E:\plug\.tmp\codex-tool-approval-handoff
278 passed in 45.64s
```

最终浏览器脚本输出：`PASS: command allow, file diff and allow, refusal, admission badge, no page errors`。隔离服务已正常停止。

## 用户在 8080 验收

当前工作树实现尚未合入 main。服务器已停止时，可在用户自己的 PowerShell 用下列完整路径启动本分支版本，继续使用原数据目录、8080 和固定 app-server 运行时：

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' 'E:\plug\agent-hub\.worktrees\codex-tool-approval\tools\demo\serve.py' --host 127.0.0.1 --port 8080 --adapter codex --codex-transport app-server --data-dir 'E:\plug\agent-hub\var\p46-user-demo\data' --files-root 'E:\plug\agent-hub\docs'
```

前提仍是已修正项目 Python 的 Medium 标签、用户进程能写长期 home、8090 代理和 CA 可用，且没有残留的 exec 身份覆盖或模拟器变量。目录权限修复的诊断在 `fix/codex-home-write-probe` 的交接中，本分支没有移动登录凭据或修改 Windows 权限。

打开 `http://127.0.0.1:8080/chat`，刷新页面，新建一次用户监督调用。先用“只执行 Get-Location 并返回实际目录”的小任务确认：启动调用后，工具卡出现两种决定；核对原文后点“允许此操作”，检查发出 accept、实际工具结果和回合终态。旧卡、已过期卡不用于这次验收。

本次不自动发起真实调用。用户的真实工具验收未执行前，不能将离线 peer 的标记文件当成 Codex 真正执行命令/修改文件的证据；也不能据此认定严格隔离、原生子 Agent 控制、远端停止或完整真实自动开发已通过。
