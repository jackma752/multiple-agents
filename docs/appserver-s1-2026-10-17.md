# app-server S1 实际验证（2026-10-17）

**结果：真实 S1 PASS；0 次模型回合。** 只证明 JSONL 传输、正常的两步握手、一个空 thread 与本次进程退出。没有测试真实服务器如何拒绝省略握手的请求，也没有测试模型、工具审批、中断、恢复或原生子 Agent。

## 身份与授权

首次预检发现原固定路径 `bin/2e5e00daee91c61d/codex.exe` 不存在，立即停止：进程启动 0，发送消息 0，尚未检查/写入凭据目录。证据：`var/appserver-s1/s1-real-eq9q7hxt/result.json`。

用户随后明确批准：“允许重新核验现装文件，仅做零模型 S1”。只读文件核验与 `--version` / `app-server --help` 后，建立 **S1 探针专用**身份：

| 项 | 实测值 |
|---|---|
| 路径 | `C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\9614f1d9b40d9c4f\codex.exe` |
| 大小 | 334121776 |
| SHA-256 | `e98c516f1c8935789aabe9122ee7e05ec4c3efd1ba6acf179242e031c495b99e` |
| 签名 | Windows Authenticode：Valid，OpenAI OpCo, LLC |
| CLI 版本 | `codex-cli 0.162.0-alpha.17.2`（版本字符串相同，哈希不同，不能当同一文件） |
| 参数 | 本机 help 确认 `--stdio` 与 `--listen stdio://` |

未更新 exec 适配器的旧身份，也未解除真实执行准入。探针拒绝通过环境变量偷偷替换此身份。

## 实际执行

工作目录：`E:\plug\agent-hub\.worktrees\appserver-s1-verification`。解释器：主仓库 `.venv\Scripts\python.exe`。

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools/probes/appserver_s1.py --mode real --i-understand-this-starts-an-app-server-process --codex-home 'C:\Users\Administrator\AppData\Local\AgentHub\codex-home'
```

普通执行环境的 `--version` 报 PATH alias 写入拒绝。经已有授权、工具权限审核后，在允许专用 home 写入运行状态的环境中执行上述一次 S1；这不改变 thread 的 `read-only` 参数。

- 子进程 argv：固定 `codex.exe app-server --stdio`，自有 PID **13632**。
- 仅子进程环境设置专用 `CODEX_HOME`、`HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:8090`、回环 `NO_PROXY`、certifi CA；未改系统代理或登录配置。
- 发送顺序恰为 `initialize(id=1)` → `initialized` → `thread/start(id=2)`；没有 `turn/start`。
- 空 thread：`01a1469e-eb8b-7dd1-a645-ffa3c3134b27`；thread 参数显式 `sandbox=read-only, approvalPolicy=never`，工作目录是本轮专用空目录。
- 收到两个匹配响应，以及 `remoteControl/status/changed`、`account/updated`、`thread/started`、`mcpServer/startupStatus/updated` 通知。只记录名称/id，不保存账号或通知载荷。
- `process_stopped=true`，退出码 **0**，`protocol_violation=false`，`reader_error=null`。
- 原始 stderr 216 字节；`c0000142`、`UnknownIssuer` 均 0 命中。这不证明 DLL 问题已解决。

脱敏结果：`var/appserver-s1/s1-real-euxv5bb_/result.json`；原始 stderr 仅留在被 Git 忽略的本地运行目录，未提交。只证明本次拥有句柄的 app-server 已退出，**不证明共享 daemon 或历史子 Agent 停止**。

未读取、复制或删除凭据内容；app-server 正常复用现有登录，并可能写入专用 home 的运行状态。长期 home 保留，不声称“全程未写 home”。

## 为使 S1 可信而完成的修复

1. 请求**发送前**白名单：只允许固定握手和一个空 thread；不允许 turn、恢复、fork、命令、审批响应或重复 thread。服务端若意外请求审批或发出 turn/item 活动，失败并关闭自有进程。
2. 正确检查实际选择的 home；写入探针改用独占随机文件，避免覆盖旧文件或并发冲突。
3. 成功必须同时满足协议检查和确认退出。清理采用 EOF → 有界等待 → terminate → kill → wait；启动失败关 stderr，结束关管道；不再把退出进程算成存活。
4. 完成 reader join 后保存证据，避免迟到消息漏报；只有 method/id/type 进入结构化记录。
5. 清理先核对解析后的路径、跨盘关系、home 重叠与 link/junction；失败保留证据，不删长期 home。
6. 修复内存流接收分片时的无限忙等；真实管道支持短写并防止消息字节交错。

离线命令：`python tools/probes/appserver_s1.py --mode process-stub` → **S1 PASSED**，自有 Python 桩进程已退出。此前“环境不支持管道”的结论不再适用于本次环境。

全量测试：**396 passed in 16.07s**；相关测试：**72 passed in 1.80s**（新加 33 项）。首次全量捕获到测试使用默认 GBK 读取 UTF-8 JSON 的问题，修正后全量通过。

## 参考与下一步

先按用户要求查阅 [官方 GitHub stdio 实现](https://github.com/openai/codex/blob/main/codex-rs/app-server-transport/src/transport/stdio.rs)，再核对 [官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)。当前 main 文档只用于理解协议，不能替代本机身份和行为验证；未引入第三方代码。

下一步 S2 是**另一个需要单独批准的真实模型回合**；尚未执行或自动预授权。需先准备受限单回合探针及离线反向用例，再申请一次最小回合。S3 工具审批、S4 中断、S5 恢复之后才考虑 S6 内核集成；原生派生可控前不得恢复严格执行准入。两项 DLL 记录继续 OPEN。
