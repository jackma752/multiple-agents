# 0.8.2 真实在线模型验收（2026-11-03）

用户请求“在线模型测试做一下”，后回复“继续”。独立分支 `test/online-strict-082`，基线 `54255a8`；约束见 [Issue](issue-online-strict-082.md)。本轮确认 Claude 严格工具/子任务的真实模型允许链，筛选重新生成已补齐全部候选；精确引文仍被生产校验器拒绝，因此不能宣布完整科研在线验收通过。

| 项目 | 真实结果 | 判定 |
|---|---|---|
| Claude 文件审批 | Read 与允许的 Write 分别审批送达；拒绝的 Write 也记录送达，目标文件未生成 | 通过 |
| Claude 原生子任务 | Agent 启动单独审批；子任务的 Read、Write 分别审批，含已识别的子任务来源；一层前台子任务结束，全树累计 1、运行 0 | 通过 |
| DeepSeek 20 候选筛选 | 原先遗漏的候选获得判断，生产校验器接受 20/20 | 通过 |
| DeepSeek RAGAS 精确引文 | 28 条中指定块匹配 24 条，4 条失败；3 条实际在另一个已提供块，1 条在所有已提供块中均未找到 | 未通过，正确拒绝 |

实际模型为 CLI 返回的 `claude-sonnet-5-5` 和配置的 `deepseek-flash`。使用生产 launcher、runner、适配器、SQLite、审批、配额、预算与 Worker；无替换模型端点、模拟传输或修改 launcher 环境。已有凭据只通过原路径交给实际通道，未读取或复制 Claude 登录文件；只发送新建验收文件、公开候选与公开论文片段。原生子任务未开放 Shell/MCP/后台/原生续接。

两次 Claude 顶层 CLI 调用各保留生产 `$1.00` 客户端费用估算保护、240 秒本地时限；第二次含一个原生子任务，不把它说成仅两次底层 API 请求。CLI 返回费用估算合计 `$0.0710005`；输入 40,904、输出 836。子任务独立用量及 CLI 是否完整覆盖所有辅助请求仍未验证，不以这些数值声称全树计量已通过。

DeepSeek 两次新调用的输入 11,460、输出 6,045，合计 **17,505 Token**，均有完整 provider 用量。新总预算 20,000、每次预留 8,000；剩余 2,495，不再启动修复调用。预留不是 provider 单回合硬上限：讲解轮实际 11,627 已超过 8,000 预留，实际支出如实记账。旧 52,866 Token 的数据库只读，未重置旧预算或重放旧调用。DeepSeek 未获得直接费用数值，不推算金额。

原文匹配规则保持空白/大小写标准化后的精确包含，不做模糊放行。当前明确剩余问题是引用块 ID 选择错误和一条未找到的文本；以后应改进来源选择与错误修复输入，并另做真实复测。不能把引文通过当作数学推导或研究质量已经通过。

这次论文测试是使用原失败公开材料、附具体校验错误的定点重新生成：20 候选及 RAGAS 19/25 个块保持；未附先前输出摘录，未通过完整阶段调度器恢复旧任务。未重新验证检索、审稿、导出与端到端流水线。Codex 的已知严格准入缺口保留，本轮未发送 Codex 在线请求。

可追溯性：文件轮最初被测试脚本错误判定为失败，原因是它期望工具卡 `status=consumed`，实际契约使用 `approved` 加 `delivery=sent`；驱动修正并核对旧数据库与文件后，仅执行尚未开始的子任务轮，无重复在线文件调用。原始误判报告保留。子任务首次审批服务因平台用量限制未执行，用户“继续”后正常审核成功。

原始响应/审计数据库/引文核对留在忽略目录 `var/online-082`，哈希见 [证据清单](evidence-manifest.md)。结束时四个 Hub 顶层调用均结算成功，两个隔离库的运行/未知调用均为 0、全树运行名额均为 0；论文内容是否可接受另按上述校验判断。本轮不改运行时产品实现，不重打包、不改当前安装或用户研究，不合并、不推送。

复现命令（所有在线入口都需要显式 `--approve-online-test`，旧目录不可重跑）：

```powershell
E:\plug\agent-hub\.venv\Scripts\python.exe tools/probes/strict_online.py --approve-online-test --phase inspect --run var/online-082
E:\plug\agent-hub\.venv\Scripts\python.exe tools/probes/strict_online.py --approve-online-test --phase claude --run var/online-082
# 仅用于本次驱动误判纠正后的未执行第二轮，不能重放前一轮：
E:\plug\agent-hub\.venv\Scripts\python.exe tools/probes/strict_online.py --approve-online-test --phase claude-child --run var/online-082
E:\plug\agent-hub\.venv\Scripts\python.exe tools/probes/strict_online.py --approve-online-test --phase paper --run var/online-082
# 只读重新汇总，不读取登录配置、不发送模型请求：
E:\plug\agent-hub\.venv\Scripts\python.exe tools/probes/strict_online.py --approve-online-test --phase audit --run var/online-082
```

离线验证：原驱动 4 项与原生工具审批、Claude Worker 准入、论文指导、预算相关 62 项（共 66 项）通过，实际输出以 `[100%]` 结束，退出码 0。最终驱动专项含新增子任务终态判断 **5 passed in 0.05s**；原相关组只读收集确认 **62 tests collected in 0.08s**。没有重新运行此前已通过的全量 1,395 项，不能把本轮专项写成全量在线通过。

```powershell
$env:PYTHONPATH='E:\plug\agent-hub\.worktrees\p6-impl\var\p6-deps'
E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest tests/test_strict_online_probe.py tests/policy/test_native_tools.py tests/policy/test_claude_code_gate.py tests/papers/test_guidance.py tests/papers/test_paper_budget.py -q
E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest -o addopts= tests/test_strict_online_probe.py -q
```
