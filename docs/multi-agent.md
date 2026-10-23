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
