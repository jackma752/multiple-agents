# 多 Agent 对话：按 Agent 路由与手动交接（P3，2026-10-24，离线）

这一步对应 Token 节省阶段 T 的启动节点：至少两个真实 Agent 通过同一个 Hub 对话、切换，并完成一次用户批准的结果交接。本轮只做了离线实现和桩验证；两个真实 Agent 各自的真实运行都还需要单独批准（见第 4 节）。

## 1. 按 Agent 路由

- 以前一个 Hub 进程只服务一个适配器。现在新增 `RoutingAdapter`（`packages/adapters/agenthub_adapters/routing.py`），用 `AGENT_HUB_ADAPTER=multi` 或 `serve.py --adapter multi` 启用：
  - `codex` 交给 Codex 适配器，按 `AGENT_HUB_CODEX_TRANSPORT` 构造；
  - `claude-code` 交给 Claude Code 适配器；
  - 两者的构造方式与单独使用时完全相同，信任条件也不放宽。
- 内核按调用快照里的 `agent_id` 取到对应 Agent 的适配器（`adapter_for`）。下列检查都在该适配器上做：
  - 准入（严格 / 监督）；
  - 身份绑定复核（D11）；
  - 原生续接、工具审批；
  - 启动和结算；
  - 回复写回（`capture_reply` 也按 Agent 取对应的执行库）。
- 本地停止、中断、取消、恢复时的状态查询，也按调用自己的 Agent 找适配器。
- 失败时一律关闭：
  - 路由器自身的准入一律拒绝（`agent_not_resolved`），绑定身份为 `unavailable`；漏掉按 Agent 选择的路径也无法启动。
  - 没有适配器服务的 Agent：在消费审批之前拒绝（`agent_not_served`），审批保持原样，不启动、不占名额。
- 新调用的 `adapter_version` 是该 Agent 适配器的绑定身份。按某个 Agent 身份批准的调用，不能拿到另一个 Agent 上运行（`adapter_identity_changed`）。
- `/api/adapter` 对路由器返回 `kind: "multi"` 和每个 Agent 的摘要（`agents`）。会话页按当前选中的 Agent 显示准入状态和监督边界文案。

## 2. 手动交接

- 在会话页上，对某条 Agent 回复点“交给其他 Agent”，它就会附在下一次调用里；最多 3 条，切换会话或发送后自动清空。
- API：`POST /api/conversations/{cid}/invocations` 增加 `handoff_message_ids`，只接受**同一会话中的 Agent 回复**。其他会话的消息、用户消息或不存在的 id，一律返回 404 `handoff_not_found`，什么也不创建。
- 发给 Agent 的输入是：用户输入的原文，加上按固定格式框起来的交接内容：

  ```
  <用户输入>

  --- Handoff: reply of <agent> (Hub message <id>) ---
  <回复原文>
  --- End of handoff ---
  ```

  组合后的文本写入快照的 `task_input`，审批绑定的就是它。快照的 `handoff` 记录每段来源：`message_id`、`agent_id`、`chars`、`sha256`。用户消息本身只保存用户输入的原文。
- 审批卡显示交接来源，并在等待审批时完整展开“将发送的完整内容”。批准之后来源消息即使被改动，发出的仍是批准时的快照。
- 限制：
  - 交接文本合计不超过 20,000 字，组合后不超过 100,000 字，否则返回 409 `handoff_too_large`；
  - 重复 id 或超过 3 条返回 422；
  - 交接是请求去重键的一部分。
- 交接调用仍是一次普通调用：逐次审批、计数、占名额；监督模式仍需本次的显式同意。交接不带来任何新的工具授权。
- 没有交接字段的快照，哈希与以前完全相同。

## 3. 验证（离线）

- `tests/http/test_multi_agent.py`（17 项）：
  - 两个 Agent 各在自己的适配器上运行，绑定各自的身份；
  - 路由器层面一律拒绝；未服务的 Agent 在消费审批前被拒；身份对调被拒；
  - 按调用找到所属适配器；回复从各自的执行库恢复；
  - `describe` 和 `/api/adapter` 列出每个 Agent；`build_adapter("multi")` 的构造与单独使用一致；
  - 交接：发出的内容等于批准的内容；批准后改动来源不影响发送；来源必须在本会话；各项限制；去重键；快照哈希兼容。
- 浏览器，两个离线桩（`.claude/launch.json` 的 `p3-two-agent-demo`，端口 8173）：
  - 同一会话先由 codex 回复（演示场景里的工具卡点了拒绝）；
  - 再选中这条回复交给 claude-code，审批卡显示来源和完整内容；
  - 批准后 claude-code 回复，交接选择自动清空；控制台没有错误。

## 4. 达到 T 启动节点还差什么（真实运行，每项单独批准）

- **Codex**：在重新固定的 alpha.11.2 上做一次 S6-2 冒烟（[Codex 运行时](codex-runtime.md) 第 8 节）。
- **Claude Code**：
  - 先由用户决定 `--max-turns` 的处理（[Claude Code 适配器](claude-code-adapter.md) 第 7 节）；
  - 用户自己登录专用配置目录；
  - 最多一个模型回合。
- **双 Agent**：两项都通过后，申请一次有界的真实对话：同一会话中 Codex 一个回合、Claude Code 一个回合，加一次经批准的交接。

未验证：真实 Agent 之间的交接效果；接收方如何理解交接内容；交接对用量的影响（属于阶段 T）。

**2026-10-24 进展**：Codex 的 S6-2 第二次运行（`evidence/s6-2-real-run-2-pass`）和 Claude Code 的第一次单回合（`evidence/cc-turn-real-run-1-pass`）都已 PASS，记录已合并进本分支。只剩下第 5 节的双 Agent 真实对话。

## 5. 双 Agent 真实网页对话申请

**状态（2026-10-24）**：经批准在 `b6162e6` 上执行一次，结果 **FAIL**：`multi` 把 Codex 构造成了 exec 传输，启动前就被身份检查拒绝。没有启动进程，没有模型回合，没有调用 Claude Code。见 [第一次实录](two-agent-2026-10-24-run1.md)，标签 `evidence/two-agent-real-run-1-fail`。授权已用完；修正后需要重新申请。下文保留申请原文。

- **目的**：在同一个 Hub 网页中：
  - 真实的 Codex 回复一个暗号；
  - 切换到另一个会话再切回；
  - 用户把这条回复交给真实的 Claude Code；
  - Claude Code 只能从交接内容里得到暗号，并原样答出。

  这一次即满足阶段 T 的启动节点（只认定当次）。
- **被测提交**：`feat/p3-two-agent` 上包含本节的提交。执行前确认 HEAD 和工作树都没有变化。
- **固定程序**（执行前被动核验）：
  - Codex：AgentHub 副本 `runtimes\codex\sha256-c81ed464e5e7cafd\codex.exe`（alpha.11.2），五个暂存文件的哈希与登记值一致；
  - Claude Code：`claude.exe` 2.1.308，297,376,928 字节，`c9796ba2…ea53`；
  - certifi `50f79f98…0785`；8090 在监听；
  - CODEX_HOME 有登录文件，Claude Code 专用配置目录有登录文件（都只检查是否存在）；
  - 没有 `AGENT_HUB_*` 变量；端口 8174 空闲。
- **启动**：用预览工具启动 `.claude/launch.json` 中的 `p3-two-agent-real`，等价于：

  ```
  E:\plug\agent-hub\.venv\Scripts\python.exe E:\plug\agent-hub\.worktrees\p3-two-agent\tools\demo\serve.py --port 8174 --adapter multi --data-dir C:\Users\Administrator\AppData\Local\Temp\agenthub-two-agent-run1 --no-pid-file
  ```

  数据目录必须是新的，执行前确认不存在。启动信息必须显示两个 Agent 都是 real：codex 为 `real (supervised only)`，claude-code 为 `real`。否则停止。
- **暗号**：执行前即时生成，格式为 `HANDOFF-` 加 6 位随机大写字母和数字。只在本次两条提示词里使用，结果文件只记录它的 SHA-256。
- **网页操作**（每一步只做一次）：
  1. 打开 `http://localhost:8174/chat`，点“连接”。分别选 codex 和 claude-code，确认两者的“用户监督模式”都没有标“离线模拟”或“不放行”。
  2. 点“+ 新会话”（会话 A）。Agent 选 codex，模式选“用户监督模式”，输入：`Reply with exactly: <暗号>. Do not use tools, create subagents, or modify any files.`，发送。在调用卡上勾选本次监督边界同意，点“同意并调用”。等待回复，时限 300 秒。
  3. 点“+ 新会话”（会话 B，不发消息）；再点会话列表中的 A 切回，确认历史完整。
  4. 在 Codex 的回复上点“交给其他 Agent”。Agent 选 claude-code，模式选“用户监督模式”，输入：`Reply with exactly the code that appears in the handoff below, and nothing else. Do not use tools.`（不含暗号），发送。确认审批卡显示“交接：codex #…”，并且完整内容中暗号只出现在交接框内。勾选本次同意，点“同意并调用”。等待回复，时限 180 秒。
  5. 截图（交接审批卡、最终会话），停止服务。
  - 如果出现工具卡，点“拒绝”，本次判 INCONCLUSIVE。不发第三条消息，不重试。
- **预算**：2 个会话，2 次 supervised 调用，最多 2 个模型回合（Codex 1 个、Claude Code 1 个）。Claude Code 的 `--max-budget-usd 1.00` 是客户端估算。
- **判定**：服务停止后运行 `tools\probes\two_agent_collect.py --data-dir <上述目录> --code <暗号>`。脚本只读，结果写到 `var/two-agent/real-*/result.json`。
  - **FAIL**（越过边界）：
    - 超出预算；
    - 有调用不是监督模式；
    - 任一绑定身份不符；
    - 发出过 accept；
    - 任一工作目录非空；
    - 交接来源不是 Codex 的那条回复，或发送内容不等于“用户原文 + 固定交接框”；
    - 用户原文中含暗号。
  - **INCONCLUSIVE**（没有展示出来）：
    - 调用顺序或会话数不符，或交接跨了会话、缺少交接；
    - 有调用没有 succeeded，或审批未消费，或名额未释放；
    - Codex 回复不是恰好暗号，或回合未完成，或有工具或子 Agent 活动，或有事件或用量异常，或清理未确认；
    - Claude Code 回复不是恰好暗号，或回合数不是 1，或有权限拒绝，或退出码非 0，或超时，或没有用量。
  - **PASS**：以上都没有。
  - 网页层面由我如实记录：两个 Agent 的状态显示、会话切换、交接审批卡的内容、两条回复的显示。
- **只认定**：
  - 两个真实 Agent 经过同一个 Hub 网页各完成一次 supervised 调用；
  - 会话切换后历史保持；
  - 一次经用户批准的交接把 Codex 的回复送到了 Claude Code，Claude Code 用它答出了暗号。
- **不认定**：
  - 工具或子 Agent 控制、取消或中断、Claude Code 流式、会话恢复；
  - 长对话或复杂交接的质量；
  - 用量与预算（阶段 T）；
  - 其他版本或模型。
- **停止规则**：任何检查失败或异常立即停止，不重试，不换模型，不放宽权限或参数；保留代理与 CA，不关闭证书验证。运行结束后停止服务。
- **证据**（原文件都不入库，只登记 SHA-256）：
  - result.json；
  - Codex 的 stderr 日志和 rollout；
  - Claude Code 的 stderr 日志；
  - 截图。

  数据目录中的数据库含提示词和回复，留在仓库外。写运行记录，打 `evidence/two-agent-real-run-1-<pass|fail|inconclusive>` 标签。
- **风险**：
  - 两个 Agent 各消耗一个回合的额度；
  - Codex 可能尝试工具（只读沙箱，判 INCONCLUSIVE），或给暗号加上标点；
  - Claude Code 可能多说话；
  - Codex 的 stderr 很可能出现已知的 skills ERROR；
  - 桌面更新可能删除 Claude Code 的固定文件，届时身份核验失败，调用被拒（一律关闭）。

## 6. 双 Agent 真实网页对话：第二次申请（修正后）

**状态（2026-10-24）**：经批准在 `5a2f55a` 上执行一次：前置检查 PASS，判定 **PASS**（只认定本次），达到阶段 T 的启动节点。见 [第二次实录](two-agent-2026-10-24-run2.md)，标签 `evidence/two-agent-real-run-2-pass`。授权已用完。下文保留申请原文。第一次运行 FAIL 的原因和修正见 [第一次实录](two-agent-2026-10-24-run1.md)。

### 6.1 已做的修正（离线，本提交）

1. `build_adapter("multi")` 中，Codex 一律用 app-server 传输，`AGENT_HUB_CODEX_TRANSPORT` 无论设为什么都不能让它退回 exec。测试覆盖了未设置和设为 `exec` 两种情况，结果都是常量构造的 `CodexAppServerFactory`。
2. `serve.py --adapter multi` 先构造一次适配器并交给应用，启动信息逐个 Agent 打印 `transport`、`supervised` 状态和 `binding`。用离线桩启动核对过：codex 为 `transport=app-server`。
3. 新增前置检查 `two_agent_collect.py --preflight <url>`：只读 `/api/adapter`，不经代理。必须同时满足：
   - kind 为 multi；
   - codex 的 transport 为 app-server；
   - 两个 Agent 的 binding 都等于固定常量；
   - 监督准入都放行，严格准入都阻止，身份核对都通过。

   用离线桩服务测试，正确返回 STOP。
4. 判定脚本：没有执行记录时判 INCONCLUSIVE（`*_record_missing`），不再算作执行证据。

### 6.2 申请

- **被测提交**：`feat/p3-two-agent` 上包含本节的提交。执行前确认 HEAD 和工作树都没有变化。
- **前置**：与第 5 节相同（固定程序和登录文件只做被动核验）。数据目录换成新的 `%TEMP%\agenthub-two-agent-run2`，执行前确认它不存在。
- **启动**：`.claude/launch.json` 的 `p3-two-agent-real`，已加上 `--codex-transport app-server`，并改用新的数据目录。启动信息必须显示：
  - codex 为 `transport=app-server supervised=supervised`，binding 为固定的 alpha.11.2 身份；
  - claude-code 为 `supervised=supervised`，binding 为固定的 2.1.308 身份。

  任何一项不符就停止。
- **新增：调用前的前置检查**。服务启动后、打开网页之前运行一次：

  ```powershell
  Set-Location E:\plug\agent-hub\.worktrees\p3-two-agent
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\two_agent_collect.py --preflight http://127.0.0.1:8174
  ```

  必须输出 `PREFLIGHT PASS`，否则停止服务，不做任何调用。网页连接后再核对页头标签：选 codex 时应显示 `app-server`。
- **暗号**：重新即时生成，不复用第一次的。
- **网页操作、预算、判定、只认定 / 不认定、停止规则、证据、风险**：与第 5 节相同。
  - 判定命令中的数据目录换成 run2；
  - 证据标签为 `evidence/two-agent-real-run-2-<pass|fail|inconclusive>`。
