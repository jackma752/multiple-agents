# 原生严格模式：Claude 文件工具配置

2026-11-03，分支 `feat/native-strict`，基线 `5994892`。授权：用户在官方能力复核后要求“继续”。本轮新增真实 CLI 进程验证，模型端为本机合成服务器；没有读取真实登录文件、调用在线模型或替换当前安装。

## 当前能力

Claude 2.1.308，SHA-256 `c9796ba22a9221586b36371b36f5b66198d66eb4badf6a4cf4a5f7b07becea53`，可通过 `file-hooks-v1` 严格准入。只认代码登记的二进制/配置组合；升级、路径/身份/参数漂移、缺少内核 ToolGate 均不能放行。启动前重新计算完整二进制哈希。真实适配器绑定身份增加配置版本，旧批准不能自动获得新工具权限；离线夹具身份保持兼容。

- 开放 Read、Grep、Glob、Write、Edit；每项请求成为原有 ToolApprovalRow 卡片。写入还要求本次工作区是 workspace-write。路径越界、链接、配置目录和 Windows 特殊路径直接拒绝。没有 Shell、PowerShell、MCP 或任意脚本执行入口。
- 原生 Agent 默认从工具池移除并配置 deny。用户显式选择 `native_subagents=approved`、开启会话协作并设置允许的深度和并发后，可逐次审批新的 **general-purpose 前台子任务**。子任务使用 Hub 提供的固定定义，不能加载同名用户自定义定义；首版原生深度为一层、每次 CLI 至多一个同时运行的原生子任务。Hub 全树限制可以更低。
- 原生启动在 ToolGate.claim 的同一写事务内占用 TreeQuotaRow：与 Hub 分派共享累计数、并发和调用次数。一次卡片只能消费一次；完成只释放并发，不退累计数。子任务内部工具仍需独立批准。未匹配启动、缺失终态或故障保持 unknown/占位，主调用不能伪装成成功完成。
- 原生子任务暂不支持独立 Token 预算预留。存在总 Token 预算或分模型预算时，在主模型启动前拒绝这项配置；可关闭原生子任务，使用已有 Hub 子任务。普通 CLI 内部模型步骤的预算仍不是 Token 硬上限。
- 手动审批复用卡片哈希、有效期、一次性 claim、撤销/停止关闭点。自动阶段只可按已批准的文件变更规则允许 Write/Edit，其余原生工具及 Agent 启动默认拒绝；首版不新增自动原生委派授权规则。

## 调用与界面

`POST /api/conversations/{cid}/invocations` 新增 `native_subagents: "disabled" | "approved"`，默认 disabled。只有 Claude strict 接受 approved。选择参与请求去重哈希和 snapshot.permissions；默认值不改变旧哈希。切换通道/模式和发送完成后，界面复位为关闭。调用卡显示选项，子任务卡显示实际生命周期。

NativeAgentRow 是新增表，不修改旧表结构。`native_agent.status` SSE 只含 invocation_id、native_agent_id、tool_approval_id、state、depth；用于刷新卡片和全树计数，不携带提示词或工具结果。卡片正文只通过现有用户认证/会话所有者端点读取。

CLI 在新的空工作目录运行，普通配置来源关闭，命令/技能入口关闭，只加载本次 --settings 与固定 --agents 定义。批准工作区通过 --add-dir 告知 CLI，额外目录的记忆文件加载显式关闭；文件访问仍由内核检查。工作区与执行配置目录重叠时拒绝。每次随机 HTTP 凭据只写工作区外的临时设置，绑定 127.0.0.1、会话和 cwd；回调服务没有批准接口。内容限长、重放拒绝、错误默认 deny，所有 HTTP hook 配置 `onFailure: block`。结束仅删除本次确切设置文件及空目录。

保持原监督模式的 safe-mode/空工具路径。按用户选择查询该模式自身准入，严格配置可用不能绕过监督配置的独立约束。未知文件或其他版本继续拒绝严格模式。

## 实际验证

真实固定 Claude CLI + 空配置 + 本机合成 Anthropic 协议服务器：13 项通过，包括拒绝/允许写入、HTTP 错误、损坏 JSON、超时、无人批准、目录越界、原生 Agent 关闭、启动拒绝、子内部拒绝/允许、子内部命令工具不存在、用户同名子 Agent 及批准工作区项目钩子未被加载。最后通过生产 `Worker.run_one -> consume -> start_streaming -> ProcessRunner -> ClaudeHookBridge -> ToolGate` 再验证子任务允许链，结算 succeeded；仅模型端点被测试代码换成本机合成端点。没有真实账号、在线 Token 或模型质量验收。

初次测试发现 2.1.308 默认异步启动 Agent，无法取得可靠终态；保留失败证据。显式设置 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`、`CLAUDE_CODE_FORK_SUBAGENT=0` 后取得完整 SubagentStart/内部 PreToolUse/SubagentStop 链路及正确的 agent_id，并释放并发。不是把失败判成通过。

离线测试覆盖路径、只读、未批准、超时、审批后策略变更/关闭、并发竞争、重复回调、全树共享计数、未知状态占位、预算拒绝、HTTP 认证/会话/cwd、空响应拒绝、生命周期事件不含正文、旧哈希兼容。`python -m pytest tests/http tests/policy tests/papers tests/adapters tests/test_budget_rules.py -o addopts='' -q --junitxml=var/native-final-all.xml`：**1395 passed in 348.02s**。最后改动另有关键路径回归，结果在交接登记。浏览器检查 Claude strict 可选逐次审批、切换 Codex 自动清除且禁用、无控制台错误；截图 var/native-ui-verified.jpg。

## Codex 的实际进度

新增严格启动参数 `-c agents.enabled=false`，在 thread/start/turn/start 前要求 config/read 明确返回布尔 false；键缺失、字符串 false 或其他值一律拒绝。当前固定 `codex-cli 0.163.0-alpha.11.2` 在全新空 home 上完成 initialize + config/read，确认为 false；thread/start 和 turn/start 都为 0。

补充合成 Responses 服务器实测：gpt-5.4 本地配置、agents.enabled=false 时不提供原生 Agent 工具，强制返回带 namespace 的 spawn_agent 调用仍未创建子线程。相同固定版本启用 agents，并配置匹配全部工具的 PreToolUse 拒绝脚本后，**钩子未触发，实际完成一次 spawnAgent 并创建一个合成子任务**；脚本正确以非零退出报告 scenario_verified=false。未解释该版本/配置为何未触发，不能推论所有新版本都不支持。早期不带 namespace 的探测只是 unsupported call，保留原件，未当作阻断通过。

因此真实严格准入仍为 `appserver_real_unverified`；监督行为保持。零模型配置回读和当前 V1 合成场景证明了一部分，不证明所有模型版本、主线程工具、子线程审批和故障拒绝都满足严格要求。没有用文档支持或单次 config/read 代替实测，也没有开启未知能力。

## 使用

2026-11-03 后续修复：隔离探针补齐 `hooks/list`、当前哈希、信任登记及重新读回。相同固定 Codex 二进制的原生启动拒绝已通过，旧“钩子未触发”的证据属于缺少信任核验的接入场景，不能说明平台不支持。新增真实 CLI/合成模型故障测试却确认：受信任钩子返回损坏 JSON 或超时后，各仍创建一个子任务。因此故障拒绝条件不满足，严格准入继续阻止；用户监督模式未改变。测试未读取用户登录文件或修改真实用户钩子，未使用绕过信任参数。详见 [0.8.3 修复记录](citation-codex-0.8.3.md)。

采用与上述 SHA 相同的 Claude 程序，选择工作区及只读/允许写入，然后在对话中选 Claude + 严格模式。文件工具会逐次出卡。需要原生子任务时先在已有会话右侧开启协作并保存限制，再创建新的调用、选择“原生子 Agent：逐次审批”。启用子任务不能替代启动审批，也不批准子内部写入。

不支持原生会话恢复、任意命令工具、MCP、后台任务、多层原生委派、原生子任务单独停止或远端硬取消。不把文件工具配置称为操作系统级沙箱；没有证明可以防护 CLI 自身漏洞或其他同权限本机程序的篡改。

官方依据：[Claude Hooks](https://code.claude.com/docs/en/hooks)、[Claude Subagents](https://code.claude.com/docs/en/sub-agents)、[CLI 参数](https://code.claude.com/docs/en/cli-reference)、[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、[Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)。
