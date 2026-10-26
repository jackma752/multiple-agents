# 协作阶段被配额拒绝后继续（2026-10-26）

用户场景：Codex 已完成计划 #4 第1项；第2项 DeepSeek 在启动前因任务树累计调用上限被拒。
旧阶段收口撤销未消费审批，调用变成 `draft + revoked`。用户调高上限、以现有计划开新阶段后，
立即被 `item_in_progress` 拦截，新阶段调用数0。

## 原因与修复

- `collab.item_state()` 只按 invocation.status 判断，`draft` 一律视为 awaiting，未看审批已关闭。
  现在只有同时满足“启动前状态、没有 started_at、没有外部执行 ID、所有审批都已撤销/拒绝/过期”
  才视为可重新分派的 failed。旧记录保持原状；新阶段使用新快照、新审批和新授权来源。
- 已完成且有回复的第1项由已有调度器跳过。第2项生成新调用，带第1项原有回复，之后汇总两项。
  pending/approved 审批、启动证据、unknown 状态仍阻止重复分派；旧审批不能复用。
- 同一阶段内发现其未启动调用审批已关闭时，直接阻塞并提示另开阶段，避免永久等待。
- 阶段表单撤选主 Agent 后，清空主角色并显示“请选择主 Agent”；提交前核对主/审阅/汇总均属于
  已勾选 Agent，避免界面显示Codex、内部仍提交Claude Code。复选框改变后立即更新角色列表。
- `.chip` 样式覆盖了浏览器默认的 hidden 显示规则，使已阻塞的顶部状态仍显示“自动执行中”。
  增加显式 `[hidden]` 样式，收口后隐藏顶部运行控件，右侧保留实际阻塞/完成状态。

## 此修复工作树包含的接入

分支 `fix/collaboration-resume`，目录 `E:\plug\agent-hub\.worktrees\collaboration-resume`。
基于 `a522183`，包含先前 DeepSeek 密钥 Ctrl+V 清理、HTTP诊断、最终回复及旧记录恢复修复。
同时把 DeepSeek 注册到 multi 路由，让本工作树能直接用于 Codex→DeepSeek→Codex 合作；
每个适配器仍独立走原审批门禁。根目录另一工作 Agent 的未提交文件没有修改或直接搬入本分支。
启动横幅显示 source，便于确认正在运行哪份工作树。

## 用户继续方式

用户在自己的8080服务窗口按 Ctrl+C 后执行：

```powershell
$env:AGENT_HUB_DEEPSEEK_MODEL = 'deepseek-flash'
Remove-Item Env:AGENT_HUB_APPSERVER_STUB,Env:AGENT_HUB_DEEPSEEK_STUB,Env:AGENT_HUB_CODEX_PREFIX -ErrorAction SilentlyContinue
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' 'E:\plug\agent-hub\.worktrees\collaboration-resume\tools\demo\serve.py' --host 127.0.0.1 --port 8080 --adapter multi --codex-transport app-server --claude-code-stub success-usage --data-dir 'E:\plug\agent-hub\var\p46-user-demo\data' --files-root 'E:\plug\agent-hub\docs'
```

此命令接真实 Codex/DeepSeek，Claude Code 保留离线桩。核对横幅 source 为本工作树。
刷新原会话，以“用会话里最新的计划”新开阶段，勾选Codex/DeepSeek、主和汇总选Codex，
用户监督模式并同意边界；显式批准阶段。调度器跳过已完成项，继续第2项。
保留用户已经调高的任务树上限，不自动加额度、不移动main、不修改或删除运行数据。

## 验证

初轮恢复/阶段/协作/多适配器回归：86 passed in 26.71s，均离线。
最终受影响范围（包含前面的 DeepSeek 修复）：

```text
python -m pytest -o addopts='' -q tests/http/test_collaboration_resume.py tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py tests/http/test_collab.py tests/http/test_multi_agent.py tests/http/test_chat_page.py tests/http/test_conversations.py tests/http/test_deepseek_replies.py tests/http/test_deepseek_failures.py tests/adapters/test_deepseek_adapter.py
180 passed in 35.01s
```

Headless Edge 检查：撤选原主Agent后显示空选项；无效角色发送0次stage POST；明确选Codex后
提交的 main/collect/agents 与画面一致；blocked 顶部控件隐藏；无JS错误。
独立18190测试服务已停止。没有启动或停止用户8080，没有追加真实模型调用。
