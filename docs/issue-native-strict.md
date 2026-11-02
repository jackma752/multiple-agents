# 原生严格模式实施

2026-11-03。用户在复核官方文档后明确要求“继续”，承接严格模式修复。分支 `feat/native-strict`，基线 `5994892`。不推送、不合并、不替换当前安装或凭据；不追加付费模型请求。

先实现 Claude 的逐工具 HTTP 钩子与原生 Agent 启动审批，复用 ToolGate 的单次卡片、哈希、有效期、阶段状态及关闭点。原生调用登记使用与 Hub 子调用相同的 TreeQuotaRow 原子计数；未确认终态保留并发，完成不返还累计数。所有入口默认拒绝，未知事件、错误、重复回调、配置漂移不得产生第二次放行。

严格路径只开放可在批准目录内检查的文件工具和受控 Agent，Shell、PowerShell、MCP、插件、后台任务、会话恢复暂不开放。原生 Agent 默认关闭；用户明确在不可变快照中选择 `native_subagents=approved` 才能提出原生启动申请。启动只允许前台新实例；必须逐次批准。自动阶段未授权原生启动时拒绝，不能套用文件规则。子实例内部工具同样审批。

钩子服务仅绑定 127.0.0.1 的临时端口；每次执行独立随机凭据，认证失败不处理。设置在工作区外的临时文件中；文件路径、令牌、回调正文不得进入普通日志。`onFailure: block`、完整请求校验和只读/可写目录检查共同执行。严格启动使用新空目录，排除继承的项目定制；保持监督模式原有参数与行为。

Codex 先实现 `agents.enabled=false` 的请求及有效配置回读，核对当前采用版本。PreToolUse 的 spawn_agent/Agent 路线列入测试；官方明确的故障放行和未覆盖路径不能冒充完整强制控制。不能通过修改 allowed 绕过未知项。

验证分层：离线状态机与 HTTP 并发/重放/过期/关闭/目录越界；真实固定 CLI + 仅本机合成模型服务器（无真实账号、无真实模型），检查真正发生或未发生的文件操作与子启动；必要的未验证项目明确保留。不得用合成模型证明真实模型质量、真实费用或生产远端停止。

官方依据（2026-11-03）：
- https://code.claude.com/docs/en/hooks （PreToolUse、Agent、onFailure，自 2.1.295）
- https://code.claude.com/docs/en/sub-agents （深度、并发及恢复例外）
- https://code.claude.com/docs/en/cli-reference （safe-mode 禁止普通钩子）
- https://learn.chatgpt.com/docs/hooks （spawn_agent 匹配 Agent；错误/超时不保证拒绝）
- https://learn.chatgpt.com/docs/config-file/config-reference （agents.enabled）
