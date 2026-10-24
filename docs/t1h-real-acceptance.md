# T-1h 最小真实验收申请

**状态（2026-10-24）**：经批准，在 `bd73e95` 上执行了一次，判定 **PASS**（只认定本次）。见 [第一次实录](t1h-2026-10-24-run1.md)，标签 `evidence/t1h-real-run-1-pass`。授权已用完。下文保留申请原文。

## 目的

在同一个 Hub 网页、同一个会话、严格 token 预算下，用真实 Codex（app-server）和真实 Claude Code 各完成一次 supervised 调用。两次调用都带同一个文件引用，引用的文件里有一个暗号，两个 Agent 都只能从文件片段中得到暗号并原样答出。由此确认 T-1 在真实 Agent 上的几项结果：

- 审批时看到的“发送前预览”，各部分之和就是 Agent 实际收到的文本；
- 文件片段与磁盘上的文件逐行一致；
- 用量按归一化口径完整入账；
- 严格预算的预留和结算与账本一致。

## 被测与前置

- **被测提交**：`feat/t1-context-refs` 上包含本文件的提交。执行前确认 HEAD 和工作树都没有变化。
- **固定程序**（只做被动核验，与 [双 Agent 申请](multi-agent.md) 第 5 节相同）：
  - Codex：AgentHub 副本 alpha.11.2；
  - Claude Code：2.1.308，`c9796ba2…ea53`；
  - certifi 版本；8090 端口在监听；
  - 两个登录文件只检查是否存在；
  - 没有 `AGENT_HUB_*` 变量；端口 8177 空闲。
- **暗号**：执行前即时生成，格式为 `T1H-` 加 6 位随机大写字母和数字。只写进测试文件，不出现在任何消息里；结果中只记录它的 SHA-256。
- **测试文件**：新建 `%TEMP%\agenthub-t1h-files\code.md`，共三行：`T-1h fixture`、`CODE: <暗号>`、`end`。该目录作为文件根目录，执行前确认它不存在。
- **数据目录**：新的 `%TEMP%\agenthub-t1h-run1`，执行前确认它不存在。

## 步骤（每步只做一次）

1. 用预览工具启动 `.claude/launch.json` 中的 `t1h-real`，等价于：

   ```
   serve.py --port 8177 --adapter multi --codex-transport app-server --data-dir %TEMP%\agenthub-t1h-run1 --files-root %TEMP%\agenthub-t1h-files --no-pid-file
   ```

   启动信息必须显示：codex 为 `transport=app-server supervised=supervised`，claude-code 为 `supervised=supervised`，`Files` 为上述目录。
2. 前置检查：`tools\probes\t1h_collect.py --preflight http://127.0.0.1:8177` 必须输出 `PREFLIGHT PASS`，否则停止服务，不做任何调用。
3. 打开 `http://localhost:8177/chat`，点“连接”，然后点“+ 新会话”。
   - 设置预算：Agent 选 codex，模式选“用户监督模式”，发送 `budget setup` 但**不批准**，用来建立任务树。
   - 在用量栏的预算表单中填写：总 200,000，单次 60,000，模式“严格”，保存。保存会撤销刚才那条未批准的调用。
4. **Codex 调用**：
   - 填写：Agent 选 codex，模式选“用户监督模式”，输入 `Reply with exactly the code in the referenced file, and nothing else. Do not use tools.`；在引用输入框中添加 `code.md:L1-3`。
   - 发送前检查：发送前预览列出文件 `code.md` 第 1-3 行。
   - 发送后检查：审批卡显示同样的预览，预算行显示严格预算，执行时预留 60,000。
   - 勾选本次监督同意，点“同意并调用”，时限 300 秒。
5. **Claude Code 调用**：Agent 选 claude-code，其余与第 4 步相同，时限 180 秒。
6. 截图：发送前预览、Codex 的审批卡、最终的会话和用量栏。然后停止服务。
- 出现工具卡时点“拒绝”，本次判 INCONCLUSIVE。不发第三条消息，不重试。

## 预算

- 1 个会话，3 次调用：opener 被撤销、从未运行，另外 2 次 supervised 调用。
- 最多 2 个模型回合：Codex 1 个，Claude Code 1 个。
- Claude Code 的 `--max-budget-usd 1.00` 是客户端估算。

## 判定

服务停止后运行：

```
tools\probes\t1h_collect.py --data-dir %TEMP%\agenthub-t1h-run1 --fixture %TEMP%\agenthub-t1h-files\code.md --code <暗号>
```

脚本只读，结果写到 `var/t1h/real-*/result.json`。离线时已用桩把同样的流程跑通，结果为 PASS（`tests/test_t1h_collect.py`）。

- **FAIL**（越过边界或账目不对）：
  - 运行的调用超过 2 次，或会话超过 1 个；
  - 有调用不是 supervised；
  - 绑定身份不符；
  - 发出过 accept；
  - 工作目录非空；
  - 预览不能描述快照中的发送文本；
  - 适配器记录的输入哈希不等于批准的发送文本；
  - 片段与测试文件不一致；
  - 用户原文中含暗号；
  - 预算计数器不等于各调用行之和，或仍有在途预留，或已知用量不等于 complete 账本行之和。
- **INCONCLUSIVE**（没有展示出来）：
  - 调用没有 succeeded；
  - 回复不是恰好暗号；
  - 有工具活动；
  - 用量不是 complete；
  - 预算项没有结算；
  - 调用顺序不符；
  - 名额没有释放；
  - 缺少执行记录。
- **PASS**：以上都没有。
- 结果中另外记录（只是观测，不参与判定）：每次调用的 Hub 可见估算，以及实际的 `input_total`、缓存读和输出。两者的差距就是 provider 侧上下文的大小。

## 只认定 / 不认定

- **只认定（本次）**：
  - 两个真实 Agent 收到的正是审批时预览并绑定的文本，其中包含文件片段；
  - 用量完整入账；
  - 严格预算的预留和结算与账本一致。
- **不认定**：
  - Codex 多请求回合的入账：需要一个工具被拒绝的回合，可以以后单独申请；
  - 续接时重放的 total；
  - 估算的准确度；
  - `@diff`；
  - 长文件或多文件的效果；
  - 其他版本或模型。

## 停止规则与证据

- **停止规则**：任何检查失败或出现异常立即停止，不重试，不换模型，不放宽权限或参数；保留代理和 CA，不关闭证书验证。运行结束后停止服务。
- **证据**：result.json、截图、两个 Agent 的 stderr 日志，只登记 SHA-256，不入库。数据目录和测试文件都留在仓库外。写运行记录，并打标签 `evidence/t1h-real-run-1-<pass|fail|inconclusive>`。

## 风险

- 两个 Agent 各消耗一个回合的额度。
- Codex 可能尝试工具（只读沙箱，判 INCONCLUSIVE），或给暗号加标点。
- Codex 的 stderr 很可能出现已知的 skills ERROR。
- 桌面更新可能删除 Claude Code 的固定程序，届时身份核验失败，调用被拒。
