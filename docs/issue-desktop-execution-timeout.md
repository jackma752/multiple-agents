# 安装版单次 Codex 执行时限可配置

授权：2026-10-27，用户询问如何调整或关闭截断长任务的执行超时。
基线：0.5.2，`fix/desktop-workspace`；本任务独立分支 `feat/desktop-timeout`。

## 问题

Codex 总执行时限固定 600 秒，长任务已写出文件但未返回 turn/completed 时被关闭，
调用进入 unknown，后续阶段被阻塞。页面的额度恢复复查间隔不是这个时限；100 分钟还超出其 1～60 分钟范围。

相关契约：`InvocationSnapshot.permissions`、`workspace.py`、`Policy.run_time_limit_seconds`。
执行时限必须进入不可变快照及审批哈希。后台超时检查和适配器采用同一份批准值；
工具审批过期、网络请求握手超时、人工暂停/停止不随总执行时限一同关闭。

## 验收要求

- 本机设置可调整 Codex 单次执行时限；0 表示不限时，旧配置沿用 600 秒。
- 只由已登录用户保存；保存沿用现有未消费审批撤销和运行中配置保护。
- 不限时仍可手动中断、停止本地进程；不能降低审批与阶段规则要求。
- 调用卡显示批准的时限；后台 reaper 不能用旧的 600 秒再次截断不限时调用。
- 提供已有 unknown 人工结算接口的页面入口，明确保留文件、不自动重跑、不视为产物验收通过。
- 不主动恢复或结算用户当前未知调用，不新增真实模型回合。
- 针对性回归和新安装包检查；不重跑全套或压力循环。

## 2026-10-28 实现与验证

- 桌面 profile 新增 `codex_timeout_seconds`，默认 600，0 为不限时，范围 0～86400 秒；页面按分钟输入。
- Codex 快照 `permissions.execution_timeout_seconds` 绑定审批；适配器启动前核对配置一致性。
- 用户采用的桌面运行时允许长时限/不限时；固定评审运行时准入、严格模式与工具权限不扩展。
- 适配器事件循环和内核超时检查采用同一时限；0 不进入总执行截止。既有工具卡 TTL、握手与人工中断保留。
- 超时 unknown 调用可由登录用户确认登记为 timed_out；沿用人工结算接口，记录授权者，保留文件，不创建重试。
- 页面在提交前解释额度复查间隔 1～60 分钟，与本机设置的执行时限区分。

针对性命令：

```powershell
python -m pytest tests/http/test_desktop.py tests/http/test_appserver_s6.py tests/policy/test_timeout_reconcile.py tests/policy/test_snapshot_binding.py tests/http/test_chat_page.py tests/http/test_async_and_tool_approval.py tests/adapters/test_desktop_workspace.py tests/policy/test_stop_confirmation.py tests/http/test_auth.py -q -o addopts=''
```

实际结果：**194 passed in 21.49s**。包含模拟时钟跨过 600 秒、0 时限仍可人工中断、旧审批撤销/配置不符零启动、后台时限一致、未知调用人工登记后改设置不重跑、跨源拒绝。

Headless Edge 使用拦截请求的独立页面夹具：默认 10 分钟、保存 0 与刷新保留、非法范围、人工确认/取消、运行中不显示结算按钮、额度复查校验均通过，无 JS 错误。记录在本工作树忽略的 `var/timeout-check/ui-check.json`。

没有新增真实模型请求，没有变更用户正在使用的服务、数据或已生成的文件。安装包的冻结验证另见 0.5.3 发行记录；真实模型长任务尚待用户升级后验收。
