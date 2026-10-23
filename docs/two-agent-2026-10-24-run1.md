# 双 Agent 真实网页对话第一次运行（2026-10-24，FAIL：Codex 被构造成 exec 传输，启动前就被拒）

- **被测提交**：`feat/p3-two-agent` @ `b6162e6`。
- **授权**（用户原话）：“批准在 feat/p3-two-agent b6162e6 上按 docs/multi-agent.md 第 5 节执行一次双 Agent 真实网页对话：Codex 1 个回合、Claude Code 1 个回合、一次经批准的交接；任何异常立即停止，不重试。”
- **前置检查**：全部通过。
  - HEAD 未变，工作树干净；
  - 暂存的 Codex 运行时 5 个文件和 alpha.11 存档都与登记值一致；
  - `claude.exe` 大小和哈希一致；certifi 一致；8090 在监听；8174 空闲；
  - 两个登录文件都存在（只检查是否存在）；
  - 没有 `AGENT_HUB_*` 变量；数据目录事先不存在。
- **暗号**：执行前即时生成。这里只记录它的 SHA-256，见 result.json 的 `code_sha256`。

## 经过

1. 用 `p3-two-agent-real` 启动服务。启动信息显示 `codex: real (supervised only)`、`claude-code: real (supervised only)`。
2. 连接后，两个 Agent 的“用户监督模式”都没有标注限制。**页面顶部的适配器标签显示的是 `codex · exec · blocked`，我在这一步没有核对它。**
3. 会话 A：Codex，用户监督模式，发送固定提示词；勾选本次同意，点“同意并调用”。调用随即变为“失败”。
4. 按停止规则立即停止：没有切换会话，没有交接，**没有调用 Claude Code**，没有重试。截图后停止服务，再运行一次只读判定脚本。

## 原因（只读核对）

- `launch.json` 中的 `p3-two-agent-real` 只传了 `--adapter multi`，**没有传 `--codex-transport app-server`**。`build_adapter("multi")` 按单独使用时的规则构造 Codex，而 `AGENT_HUB_CODEX_TRANSPORT` 没有设置，于是默认走 **exec 传输**。
- exec 适配器固定的是旧程序 `bin\2e5e00daee91c61d\codex.exe`（alpha.17.2），它已被桌面更新删除。启动前的身份检查因此拒绝了这次调用。
- 证据：
  - 调用状态为 `failed`；`started_at` 和 `adapter_invocation_id` 都为空；
  - 没有 `stream.*` 事件；exec 执行库表为空；没有为该调用创建工作目录（`codex-runs` 下只有 `_scratch`）；
  - 名额已释放；
  - 快照绑定为 `unknown`，即 exec 不声明身份。
- **结论：没有启动任何 Codex 进程，没有模型回合；Claude Code 没有被调用。**
- 申请里的问题：
  - 第 5 节的固定程序核对只检查了文件，没有检查运行中的 Hub 给 codex 用的是哪个适配器；
  - 启动信息也没有打印每个 Agent 的传输和绑定身份。

## 判定

`tools\probes\two_agent_collect.py --data-dir <run dir> --code <暗号>`：**FAIL**。

| 原因码 | 说明 |
|---|---|
| `codex_binding_mismatch` | 真实问题：快照绑定的是 exec 的 `unknown`，不是固定的 app-server 身份 |
| `codex_execution_evidence` | **误报**：没有 Codex 记录，也就没有工作目录，脚本把缺失当成了“非空”。实际上没有执行 |
| 其余（`calls_not_as_planned`、`no_handoff`、`claude_*` 等） | 都是因为第一步失败后按规则停止而没有展示 |

只认定：这一次没有达到 T 启动节点；Hub 在启动前拒绝了身份不符的 exec 调用，没有越过审批或启动进程。

## 证据（文件不入库，只登记哈希）

| 文件 | 字节 | SHA-256 |
|---|---|---|
| `E:\plug\agent-hub\.worktrees\p3-two-agent\var\two-agent\real-4uchg3mv\result.json` | 1771 | `af00baef734d3490b873d069cde8be4aba8859ebdf0ad6c23527bc87c110a3fa` |
| 失败后的页面截图（会话工具结果目录 `mcp-Claude_Browser-blob-1792775712151-d5ty4l.jpg`） | 20123 | `34005ba211544ab8f9ace3865d2c1c6ef6b4c0d2d98fc394f9f02c6e63e03cfb` |
| `%TEMP%\agenthub-two-agent-run1\agent-hub.db`（主文件；未合并的 WAL 642,752 字节一并留在仓库外） | 4096 | `d78c40febe1ba7ec83660b78e3f6ab7bc45ab822b8f21b03b16b9cb4f3b3a259` |

## 需要的修正（离线，未做）

1. `multi` 下 Codex 一律用 app-server 传输：exec 已被阻止，也不属于多 Agent 路径；或者在没有指定 app-server 时拒绝构造 `multi`。
2. 启动信息逐个 Agent 打印传输、绑定身份和监督准入状态。
3. 申请的前置检查增加一项：服务启动后、任何调用之前，读取 `/api/adapter`，确认 codex 的 `transport` 为 `app-server`，两个 Agent 的 `binding_identity` 都等于固定值。否则停止。
4. 判定脚本：没有执行记录时判 INCONCLUSIVE（`*_record_missing`），不再算作执行证据。
