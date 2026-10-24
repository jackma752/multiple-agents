# P4 最小真实协作验收申请

**状态（2026-10-25）**：经批准在 `86257d2` 上执行一次，判定为 **INCONCLUSIVE**（只认定本次）。唯一的原因是 #2 的 Claude Code 用量为 partial：报告了两个模型，且 `num_turns=2`。所有边界、输入和依赖检查都通过。见 [第一次实录](p4-2026-10-25-run1.md)，标签 `evidence/p4-real-run-1-inconclusive`。授权已用完。下文保留申请原文。

## 目的

在同一个 Hub 网页、同一个会话中，用真实 Codex 和真实 Claude Code 走一遍受控协作的首版闭环：开启协作扩展 → 手写两项计划 → 按依赖分派两个子任务，逐个审批 → 汇总一次。由此确认在真实 Agent 上：

- 子任务是深度 1 的调用，分派顺序受依赖约束；
- 后一项实际收到的，正是前一项的结果和自己的目标（与审批一致）；
- 汇总调用带上两项结果；
- 每次调用都是用户监督模式下逐个批准的。

## 被测与前置

- **被测提交**：`feat/p4-collab` 上包含本文件的提交。执行前确认 HEAD 和工作树都没有变化。
- **固定程序与环境**（只做被动核验，与 [T-1h 申请](t1h-real-acceptance.md) 相同）：
  - Codex alpha.11.2 固定程序和暂存文件与 `provenance.json` 一致；
  - Claude Code 2.1.308 一致；
  - certifi 一致；8090 在监听；
  - 两个登录文件只检查是否存在；
  - 没有 `AGENT_HUB_*` 变量；8181 空闲。
- **数据目录**：新的 `%TEMP%\agenthub-p4-run1`，执行前确认它不存在。
- **暗号**：执行前即时生成，格式为 `P4-` 加 6 位随机大写字母和数字。只写进第 1 项的目标。结果只记录它的 SHA-256。

## 步骤（每步只做一次）

1. 用 `.claude/launch.json` 的 `p4-real` 启动，等价于：

   ```
   serve.py --port 8181 --adapter multi --codex-transport app-server --data-dir %TEMP%\agenthub-p4-run1 --no-pid-file
   ```

   启动信息必须显示：codex 为 `transport=app-server supervised=supervised`，claude-code 为 `supervised=supervised`，两者都是固定身份。
2. 前置检查：`tools\probes\p4_collect.py --preflight http://127.0.0.1:8181` 必须输出 `PREFLIGHT PASS`，否则停止。
3. 打开 `http://localhost:8181/chat`，点“连接”，然后点“+ 新会话”。用 codex、用户监督模式发送 `start`，但**不批准**，只用来建立任务树。
4. 在“协作计划”栏点“开启”。策略升一个版本，并撤销刚才那条未批准的调用。
5. 点“写协作计划”，填两项后保存：
   - #1 codex：`Reply with exactly: <暗号>. Do not use tools.`
   - #2 claude-code，依赖 1：`Reply with exactly the code that appears in the handoff above, and nothing else. Do not use tools.`
6. 执行模式选“用户监督模式”。分派 #1，在审批卡上勾选同意并批准，时限 300 秒。确认 #2 在 #1 完成之前没有“分派”按钮。
7. 分派 #2：审批卡应显示“交接：codex #…”，完整内容为“#1 的结果 + #2 的目标”。勾选同意并批准，时限 180 秒。
8. Agent 选 claude-code，点“把结果汇总交给 claude-code”。审批卡应带 2 段交接。勾选同意并批准，时限 180 秒。
9. 截图（计划栏、#2 的审批卡、最终会话），停止服务。
- 出现工具卡时点“拒绝”，本次判 INCONCLUSIVE。不额外发消息，不重试。

## 预算

- 1 个会话，1 个计划；4 次调用，其中 opener 被撤销、从未运行。
- 实际运行 3 次 supervised 调用：Codex 1 次，Claude Code 2 次，共 3 个模型回合。
- 子实例按默认限制计算：总数 3，并发 2。本次用 2 个。
- Claude Code 的 `--max-budget-usd 1.00` 是客户端估算。

## 判定

服务停止后运行：

```
tools\probes\p4_collect.py --data-dir %TEMP%\agenthub-p4-run1 --code <暗号>
```

脚本只读，结果写到 `var/p4/real-*/result.json`。离线时已用桩把同样的流程跑通，结果为 PASS（`tests/test_p4_collect.py`）。

- **FAIL**（越过边界）：
  - 运行的调用超过 3 次，或会话、计划多于 1 个，或某项被分派多于 1 次；
  - 有调用不是 supervised；
  - 绑定身份不符；
  - 发出过 accept；
  - 工作目录非空；
  - 适配器收到的输入与批准的不同；
  - 子任务不是深度 1，或汇总不是根级；
  - #2 的输入不是“#1 回复的交接框 + 自身目标”；
  - 汇总的交接不是两项回复；
  - 暗号出现在 #2 的目标或汇总的请求中。
- **INCONCLUSIVE**（没有展示出来）：
  - 调用缺失或没有 succeeded；
  - 有工具活动；
  - 用量不完整；
  - 计划不是“codex → claude-code（依赖 1）”的手写计划；
  - 汇总请求被改动；
  - 名额没有释放；
  - #1 或 #2 的回复不是恰好暗号；
  - 汇总回复中没有暗号。
- **PASS**：以上都没有。

## 只认定 / 不认定

- **只认定（本次）**：在真实 Codex 和 Claude Code 上，手写计划的两个子任务以深度 1 运行，按依赖逐个审批；后一项收到的正是前一项的结果；汇总调用带上两项结果。
- **不认定**：
  - 由主 Agent 提议的计划（“请主 Agent 拆分任务”）；
  - 失败重试，以及状态未知后的裁决；
  - 并发子任务；
  - 子 Agent 上限在真实运行中的表现；
  - 原生子 Agent 的控制；
  - 其他版本或模型。

## 停止规则与证据

- **停止规则**：任何检查失败或出现异常立即停止，不重试，不换模型，不放宽权限或参数；保留代理和 CA，不关闭证书验证。运行结束后停止服务。
- **证据**：result.json、截图、两个 Agent 的 stderr 日志，以及 Codex 的会话记录（rollout），只登记 SHA-256，不入库。数据目录留在仓库外。写运行记录，并打标签 `evidence/p4-real-run-1-<pass|fail|inconclusive>`。

## 风险

- 三个回合的额度：Codex 1 个，Claude Code 2 个。
- Codex 可能尝试工具（只读沙箱，判 INCONCLUSIVE），或给暗号加标点。
- Claude Code 的汇总回复可能较长，但只要求其中含暗号。
- Codex 的 stderr 很可能出现已知的 skills ERROR。
- 桌面更新可能删除 Claude Code 的固定程序，届时身份核验失败，调用被拒。
