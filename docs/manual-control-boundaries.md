# 用户监督模式与手动停止

日期：2026-10-20。用户要求：不可控时提供手动停止、明确边界，子 Agent 功能可做成插件。此决定修订原方案严格控制作为唯一可运行条件的要求，不把未知能力升级为支持。

## 本次交付

| 功能 | 行为与边界 |
|---|---|
| 严格模式（默认） | 原准入不变；Codex 原生派生不受控时拒绝启动 |
| 用户监督模式 | 快照记录 `execution_mode=supervised`，审批卡明示原生派生及工具操作无法逐次拦截、远端停止无法保证；用户逐次确认后才通过 consume 门禁 |
| 暂停调度 | 不再启动后续调用，现有调用继续；可恢复 |
| 停止调度并请求取消 | 停止整树后续调度；仅适配器确认远端终态才释放名额 |
| 结束本次本地进程 | 仅用 runner 持有的 Popen 句柄终止本次入口进程，不遍历 PID、不按进程名强杀、不触碰共享 daemon；不保证其后代或远端停止 |
| Hub 子任务协作扩展 | 根策略 `collaboration_enabled=false` 默认关闭，启用后仍逐项审批及计数。关闭时后端拒绝新子任务，并在 consume 再检查；已运行调用不因开关变化而消失 |

“扩展”当前是内置的可选协作功能边界，不是动态加载第三方代码的插件系统；插件市场不在本次范围。开关只管 Hub 分派，不能禁用 Codex 自己的原生子 Agent。界面显示的配额也仅覆盖 Hub 登记的调用。

## 调用与接口契约

- `POST /api/invocations`：`execution_mode` 仅接受 `strict` / `supervised`，缺省 strict。模式参与不可变快照哈希；旧 strict 快照的哈希保持兼容。重试保留模式但仍创建新的待批单。
- `POST /api/approvals/{id}/approve`：supervised 必须带 JSON `{"accept_unmanaged_native_agents": true}`，只有 user 身份可批准。字符串 `"true"` 不接受；未明确同意返回 `boundary_consent_required`。
- `GET /api/adapter`：分别返回 `execution_admission` 与 `supervised_admission`。监督模式仅限固定身份、read-only、传入禁用子 Agent 参数、无任意命令前缀、保留自有进程句柄、本地时限大于 0 且不超过 600 秒；实际启动仍重新校验文件大小和 SHA-256。禁用参数不作阻断保证。
- `POST /api/invocations/{id}/stop-local`：仅 user 可调用；只接受 starting/running/unknown。先持久化整树停调度、unknown 及审计事件，再操作本进程的句柄。返回 `local_stop_requested`、`local_process_exited`、`remote_stop_confirmed=false`、`quota_retained=true`，不把本地退出当远端终态。
- `PATCH /api/tasks/{id}/policy`：`collaboration_enabled` 严格布尔值，默认 false。修改走原有策略版本递增和撤销未消费审批流程；累计用量及运行中的预留不清零。旧库策略缺少此字段时按 false 处理。

停止事务和完成事务均先取得 SQLite 写锁，避免读到旧状态后互相覆盖；本地停止先落库时，迟到的成功结果不能自动释放预留。重启后只有审计记录而没有进程句柄时，不靠存储的 PID 杀进程。unknown 通过已有用户裁决接口处理；只看到本地退出不能自动重试。

使用范围仍为本机私人控制台。API 应与执行 Worker 使用同一进程实例，才能持有可停止的句柄；独立 Worker 或服务重启后返回未确认，需要人工核对。停止入口不会杀整个进程树。不能将本地最大等待时间说成模型远端硬性时限。

## 本地试用

本分支基于 S2 的 `023d0ab`；没有把另一 worktree 尚未提交的 S3 修改混入。本分支未合并前，主目录和原 8080 服务不会自动更新。

CMD 中运行以下一整行，启动独立 Mock 页面，不需要账号、不调用模型：

```bat
cd /d E:\plug\agent-hub\.worktrees\manual-control-boundaries && E:\plug\agent-hub\.venv\Scripts\python.exe tools\demo\serve.py --adapter mock --port 8156
```

打开 `http://127.0.0.1:8156/ui` → 连接 → 新建会话 → 选择模式 → 输入任务 → 创建调用 → 审批 → 推进调度。协作开关在右侧策略面板，点“应用”才生效。运行过程中轮询任务状态，以便显示本地停止按钮；这仍不是 SSE。

Mock 没有本地进程，故不显示本地停止按钮；按钮的真实进程行为以自动测试启动的 Python 子进程验证。真实 Codex 模式必须先配置**已核验**的 `AGENT_HUB_CODEX_PATH` / `SHA256` / `SIZE` / `VERSION`、仓库外 CODEX_HOME、代理及 CA。当前源码默认仍固定旧版 exe；文件不存在或身份不符时拒绝执行，不会自动选择新文件。这次没有重建身份基线或访问凭据。

监督模式不能代替工具级权限控制，不能承诺“只运行主 Agent”。仅接受这个范围的用户可选择该模式；不接受时使用严格模式或 Mock。

## 验证与后续

关键测试：`tests/policy/test_manual_boundaries.py`，覆盖显式同意、身份漂移、无审批零启动、过期/拒绝/撤销、快照模式篡改、协作开关授权及消费期复查、自有进程停止与无关进程保留、停止完成竞争、unknown 禁止自动重试、HTTP 权限。

测试中的外部进程只有本地 Python 睡眠子进程，没有调用 Codex、模型、网络或读取登录文件。原配额测试显式启用协作扩展，以继续验证配额规则。

实测记录：全套 `python -m pytest -o addopts='' -q --tb=short` 为 **512 passed in 27.26s**。随后补充“完整监督准入路径、仅替换 Popen”的回归，边界组 `tests/policy/test_manual_boundaries.py` 为 **24 passed in 1.66s**；新增后未重复全套。浏览器实点独立 Mock 服务，未批准启动 0 次、批准后启动 1 次，协作默认关闭；截图保留在 `var/ui-check/supervised-mock.png`。8156 测试服务已优雅停止，旧 8080 未操作。

下一项主体工作是 app-server 会话/事件接入内核与网页：续问每轮独立审批、增量输出、持久化事件和刷新恢复。S3/S4/S5 的真实验证仍按原约定逐次批准；不重跑 S1/S2。原生派生控制作为严格模式的能力改进继续跟踪，不再阻塞用户监督模式的功能开发。Python 与 Codex 两项 `0xc0000142` 均保持 OPEN。
