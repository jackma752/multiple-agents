# 线上测试改进需求

来源：2026-10-28 用户要求实现 `E:\plug\线上测试改进需求.txt` 五项，并整合执行超时设置。

1. 会话、设置和控制台提供跟随系统/浅色/深色主题，保存非敏感显示偏好。
2. 区分工作区只读/允许写入、Agent 实际能力、工具规则和文件引用，不把历史拒绝当成当前权限。
3. 助手回复渲染 Markdown 和 SVG；复制与审批保留原文，不执行 HTML/脚本、不自动加载外链资源。
4. 新任务累计调用/子任务总数、阶段调用数和执行总时长默认不限，0 为不限。保留旧任务明确限制、并发、层级、审批、预算、最多三次修复与网络/工具审批超时。
5. 用户确认删除已停止的自有会话，从列表移除并禁止继续提交，保留用量审计和项目文件；先处理在途、未知调用和开放阶段，撤销未用审批。

契约：Policy/StageRules 的相应数值以 0 表示无限；安装配置 execution_timeout_seconds 默认 0，兼容 codex_timeout_seconds，进入审批；新增 deleted_conversations 表以 additive 方式升级旧库，DELETE /api/conversations/{cid} 持执行层写锁且仅本人可操作。

不新增真实模型调用，不变更当前服务和运行任务；独立 worktree 离线验证并构建整合包。

## 实现与验收（2026-10-28）

- 三页主题共用非敏感本机偏好；浅色和深色均可随时切换，设置页保留读写能力说明。
- Markdown 使用 DOM 文本节点，支持标题、强调、列表、表格、代码、引用和链接。SVG 白名单重建并重命名局部 ID，去除脚本/事件/foreignObject/外链。原文和格式化显示可切换，复制/交接内容不改写。
- 新默认无限使用 0，明确正数继续生效；总量 SQL 检查跳过 0，上层并发、预算和授权不跳过。旧 profile 明确时限保留；没有时限字段的安装版采用 0。Claude 本机进程支持无限等待，但仍可停止；DeepSeek 网络空闲读取超时独立保留。
- 会话删除是保留审计的逻辑移除。后端在 Worker 同一事务锁内拒绝在途/未知/开放阶段，撤销未用审批；前端关闭旧流并切换视图代次。工作区文件不受影响。
- 原超时诊断在重启恢复后保留，旧未知调用仍需用户确认结束，不自动重放。

实际回归命令：

```powershell
python -m pytest tests/policy tests/http/test_conversation_delete.py tests/http/test_desktop.py tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py tests/http/test_chat_page.py tests/http/test_conversations.py tests/http/test_stage_summary.py tests/http/test_policy_api.py tests/http/test_appserver_s6.py tests/http/test_async_and_tool_approval.py tests/adapters/test_claude_code_adapter.py tests/adapters/test_desktop_workspace.py tests/http/test_auth.py -q -o addopts='' --tb=short
```

结果：**549 passed in 60.96s**。旧非法策略断言调整为负数；总量 0 改为无限后，并发 0 仍拒绝；页面路由检查包含桌面条件路由。

实际 Headless Edge 执行 `tests/browser/online-display.cjs`：主题持久化、0 默认、权限说明、表格/代码/SVG、恶意 SVG 清除、无外链自动请求、复制原文、删除确认、760px 窄屏均通过，无脚本错误。需要 Node/Playwright 与本机 Edge，仅离线请求夹具。报告和截图在忽略的 `var/timeout-check/`。

安装包版本 0.5.4，发行检查另记。未执行真实模型长任务或另一台物理机验收，不声明它们已通过。
