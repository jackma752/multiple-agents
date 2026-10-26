# 模型管理与协作版本整合、复制体验修复（2026-10-26）

用户完成真实 Codex→DeepSeek→汇总后，发现模型选择消失；选中审阅/摘要内容又被持续重绘清掉。

## 原因和本次结果

之前给用户的 `collaboration-resume` 启动工作树基于没有合入模型管理的 main，遗漏了独立分支
`feat/p4-model-management` 的功能。模型并非被权限隐藏。此工作树将工具审批 `b8a8891`、模型
设计 `bb90201`/`734ff05` 及实现 `c8e7961` 接到协作修复 `7be4526` 上；解决冲突时同时保留
了错误展示、阶段角色清理、撤销草稿恢复和 hidden 样式。main 和根目录其他 Agent 的改动未动。

现在同一份代码提供：

- Codex/Claude Code/DeepSeek 三路、手动/自动调用、已完成项跳过、未知调用保护；
- Composer 的模型及推理强度，自动阶段的每 Agent 固定模型；选中值进入审批 hash；
- 右侧 Agent→模型用量与独立模型预算，Agent合计、可选全局合计；旧账本可继续读；
- DeepSeek 支持本次请求的 model/reasoning_effort，模型候选来自配置与官方文档（不是账号实时目录），
  canonical 强度为 none/low/high/max。已配置完整模型ID可手填；没有静默换模型；
- DeepSeek 的真实返回模型优先记录；缺少返回模型时标为请求配置来源，不冒充服务商报告；
- 旧 DeepSeek 完整回复恢复、Ctrl+V密钥尾字符清理及 HTTP 诊断均包含在内。

## 选字和复制

后台每两秒轮询阶段，旧 renderNow 无条件 `timeline.replaceChildren()`，即使没有新消息也
重建所有节点，使浏览器选区消失。这不是整页重载。

现在按消息/调用/工具卡等实际状态计算消息区签名，状态没变时保留 DOM。选区与消息区相交时，
数据继续接收、侧栏继续刷新，消息区等选区清除后再应用变更。会话切换仍立即显示新会话。
已有鼠标按下/释放保护保留。回复新增“复制回复”，调用卡新增“复制发送内容”，均复制完整原文。
操作由用户点击触发，写入本地系统剪贴板；复制失败时提示手工 Ctrl+C。

## 验证（无追加真实模型调用）

```text
python -m pytest -o addopts='' -q tests/http/test_model_management.py tests/http/test_deepseek_models.py tests/http/test_deepseek_replies.py tests/http/test_deepseek_failures.py tests/adapters/test_deepseek_adapter.py tests/http/test_collaboration_resume.py tests/http/test_budget_admission.py tests/http/test_usage_ledger.py tests/http/test_usage_views.py tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py tests/http/test_conversation_stream.py tests/http/test_conversations.py tests/http/test_chat_page.py tests/http/test_appserver_s6.py tests/http/test_async_and_tool_approval.py tests/http/test_codex_tool_allow.py tests/http/test_collab.py tests/http/test_context_preview.py tests/http/test_stage_summary.py tests/http/test_policy_api.py tests/adapters/test_claude_code_adapter.py tests/adapters/test_codex_adapter.py tests/adapters/test_usage_reports.py tests/policy/test_snapshot_binding.py tests/policy/test_policy_versioning.py tests/policy/test_codex_gate.py tests/policy/test_claude_code_gate.py tests/test_usage_normalization.py tests/test_budget_rules.py tests/test_claude_code_min.py tests/test_context_preview.py tests/test_collab_plan.py tests/http/test_multi_agent.py
704 passed in 108.80s
```

新增 DeepSeek 专项验证目录读取不发请求、两个模型/强度到达传输层并归账、一个模型预算拒绝
不消费审批且另一模型可运行、自动阶段传入用户批准的固定模型。属于受影响范围，不是全量测试。

Headless Edge 在独立18191离线服务验证：模型/强度选择；原生选区跨过多次真实两秒轮询；
编辑摘要时仍可选字；选区存在时暂存文本更新、清除后显示；两种复制按钮复制原文一致；无JS错误。
测试服务已停止，产物在被忽略的 `var/model-copy-check/`，用户8080服务和运行数据没有操作。

## 当前启动入口

工作树 `E:\plug\agent-hub\.worktrees\model-collaboration`，分支 `fix/model-collaboration-integration`。
停止旧服务器（Ctrl+C），在 PowerShell 执行：

```powershell
$env:AGENT_HUB_DEEPSEEK_MODEL = 'deepseek-flash'
Remove-Item Env:AGENT_HUB_APPSERVER_STUB,Env:AGENT_HUB_DEEPSEEK_STUB,Env:AGENT_HUB_CODEX_PREFIX -ErrorAction SilentlyContinue
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' 'E:\plug\agent-hub\.worktrees\model-collaboration\tools\demo\serve.py' --host 127.0.0.1 --port 8080 --adapter multi --codex-transport app-server --claude-code-stub success-usage --data-dir 'E:\plug\agent-hub\var\p46-user-demo\data' --files-root 'E:\plug\agent-hub\docs'
```

Codex/DeepSeek为真实通道，Claude Code保留离线桩。沿用原数据，刷新 `/chat` 后应同时看到模型
选择和复制按钮。查看/复制历史不启动模型；Codex“刷新模型”只读取目录，会启动本地元数据进程。
运行中的阶段保持原来批准的模型，修改底部选择仅影响后续新调用。
