# T-0：能力与用量基线（2026-10-24，离线盘点）

- **依据**：`E:\plug\AGENT_HUB_TOKEN_SAVING_REQUIREMENTS.md` 第 5 节 T-0。完成标准是：能力表写明“支持 / 仅观测 / 未知”，并保存基线。
- **基于**：`feat/p3-two-agent` @ `44a5dcd`，即达到阶段 T 启动节点的提交。本轮分支为 `feat/t0-usage-baseline`。
- **授权**（用户原话）：“继续 T-0：能力与用量基线……本轮只做离线盘点，不追加真实调用。完成后提交能力表、用量基线和 T-1 的实施拆分；已收口的 S5 保持原样。”
- **方法**：全程只读。
  - 没有启动 Codex 或 Claude Code，没有模型回合，没有新的网页调研，没有改动任何代码。
  - 没有打开 `auth.json`、`.credentials.json` 或其他凭据文件。
  - 读取的内容：
    - 本仓库代码；
    - 已登记的真实运行记录；
    - AgentHub 专用 codex-home 下的 rollout，只取 `token_count` 的数字和事件类型标记，不取任何文本；
    - 两个执行库，以 `mode=ro&immutable=1` 打开，只取用量字段；
    - Step A 捕获的 alpha.11.2 schema；
    - Claude Code 身份核验时保存的 `--help`。
  - 列 `~\.codex\sessions` 目录时看到了用户自己会话的文件名，没有打开。
  - 所用文件的哈希见第 10 节。
- **判定口径**：字段含义只按本地 schema、真实记录和数字之间的算术关系判断。只能靠厂商文档才能确定的点，标为“文档”或“未验证”。

## 1. 结论摘要

1. **Claude Code 页面显示的“输入 2”不是总输入。** `input_tokens` 不含缓存。总输入等于 `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`：
   - 双 Agent 第二次的那一回合：2 + 635 + 1,161 = **1,798**；
   - 第一次单回合：2 + 1,750 + 0 = **1,752**。
   - 页面只取了 `input_tokens`，所以只显示 2。
2. **Codex 的 `inputTokens` 已包含缓存。** 缓存读取是输入的子集，`totalTokens = inputTokens + outputTokens`：
   - AgentHub codex-home 中全部 20 条有用量的 `token_count` 事件，`last` 和 `total` 都符合这个关系（见第 4 节）。
   - `reasoningOutputTokens` 是输出的子集：10-14 的一次运行中，15,661 + 24 = 15,685，推理用量 16 没有另外相加。
   - 因此两家的“输入”字段口径不同，不能并排直接显示或相加。
3. **Codex 的 `tokenUsage.last` 是单次模型请求的用量，`total` 是整个 thread 的累计。** 一个回合里有多次模型请求时（例如工具调用、拒绝后再回答），适配器只记下最后一次：
   - S3 第三次运行，一个回合有两次请求，实际消耗输入 **30,539**、输出 **79**；
   - 记录下来的是 15,328 / 7，少记了大约一半的输入和九成的输出。
   - 现有 PASS 记录都是只有一次请求的回合，所以不受影响；但凡是有工具活动的回合，都会系统性少记。
4. **真实协议的 `turn/completed` 不带用量**，schema 中的 `Turn` 没有 `usage` 字段。问题在于：
   - 适配器读取 `turn.usage` 这条路径只对离线桩有效；
   - 桩的普通回合也只从这条路径给出用量；
   - 所以现有离线测试验证的是一条真实服务端从不使用的路径。
5. **续接时的用量重放已经正确排除。** `total` 在冷恢复后继续累加，S5 和 S6-3 的算术都吻合；只是重放时只保存了 `last`，没有保存 `total`。
6. **中断、失败和原生子 Agent 的用量会变成“空白”。**
   - S4、S3 第一次和第二次、10-15 原生子 Agent 的子 thread，`token_count.info` 都为空，用量未知。
   - Hub 只在有回复时把用量写进消息元数据，没有回复的调用不留用量；页面也不显示“未知”，看起来等于 0。
7. **缓存和压缩都只能观测，不能控制。**
   - 缓存：Codex 的 `cacheWriteInputTokens` 一直是 0；Claude Code 有缓存读和缓存写两项。
   - Codex 压缩：有 `thread/compact/start` 和 `contextCompaction` item，但从未触发，适配器也没有处理。
   - Claude Code 压缩：CLI 有 `--autocompact`，但本适配器关闭了会话持久化，所以不适用。
8. **Hub 目前没有用量账本。** 没有归一化、累计、预算和未知标记，这些都是 T-1 的工作。

## 2. 能力表

状态取值：
- **支持（已验证，只认定当次）**：有真实运行记录；
- **仅观测**：能读到 provider 报告的数据，但不能控制；
- **已实现、未验证**：代码已写，没有真实证据；
- **未接入**：provider 有，Hub 没有用；
- **未知**；
- **不可控**。

| 能力 | Codex app-server（alpha.11.2 AgentHub 副本，现用传输） | Claude Code 2.1.308（现用） |
|---|---|---|
| 用量来源 | `thread/tokenUsage/updated`，含 `last`、`total`、`modelContextWindow`。每次模型请求发一条（S3：一个回合 2 条）。**支持（已验证）** | `-p --output-format json` 的 result：`usage`、`total_cost_usd`、`modelUsage`、`num_turns`。**支持（已验证，两次）** |
| 字段口径 | `inputTokens` 含 `cachedInputTokens`；`outputTokens` 含 `reasoningOutputTokens`；`totalTokens` = 输入 + 输出。**已验证（算术）**。`cacheWriteInputTokens` 一直是 0，它是否属于输入的子集**未知** | `input_tokens` 不含缓存；缓存写和缓存读单独计数。总输入要把三项相加，2 次观测的算术成立。缓存写入的单价倍率和 TTL 拆分，适配器没有保存，**未知** |
| 本回合归属 | 本回合用量应等于本回合结束时的 `total` 减去回合开始前的 `total`。**适配器取的是最后一条 `last`，多请求回合会少记（缺陷 D-T0-1）** | 每次调用是一个新进程，没有会话持久化，result 就是本次调用的用量。是否包含 CLI 内部的辅助请求**未知**；两次运行的 `modelUsage` 都只有 `claude-opus-5-5` |
| 缓存读 | **仅观测**：新 thread 第一次请求就命中 12,416（10-22 之后），之前是 12,544。S6-3 续接后升到 15,616，S5 续接后仍是 12,416。命中情况不可控 | **仅观测**：第一次 0，第二次 1,161（跨进程复用了前缀） |
| 缓存写 | 字段存在，一直是 0 | **仅观测**：1,750 和 635 |
| 推理用量 | **仅观测**：属于输出的子集，大多数运行为 0；10-14、10-15 的几次在 14 到 35 之间 | 没有这个字段，**未知** |
| 原始用量保留 | 适配器保存一条 `last` 的全部 6 个字段，以及重放的 `last`；**不保存 `total` 和逐条更新** | 只保存 4 个数字字段；`server_tool_use`、`service_tier`、`modelUsage` 的数字被丢弃，stdout 解析后删除（D-T0-5） |
| 金额 | 不提供 | `total_cost_usd` 是 CLI 的**客户端估算**，不是账单（0.014088、0.0056202） |
| 订阅额度信号 | `account/rateLimits/updated`（`usedPercent`、窗口、重置时间）：S2 收到 1 条，S3 第三次 2 条，S4（没有用量）1 条。`account/usage/read`（可以按 thread 查询估算额度）。**未接入**，都没有调用或记录 | 没有观测到 |
| 失败或中断时的用量 | 首个输出之前中断（S4）和被拒绝的回合（S3 第一次和第二次）：`token_count.info` 为空，**未知**。是否计费**未知** | 有 `is_error` 或 `max-turns` 结果时仍可能带用量（桩场景），真实情况**未验证** |
| 原生子 Agent 的用量 | 子 thread 自己有 rollout（10-15：子 thread 被中止，`info` 为空）。父回合的 `total` **不包含**子 thread；到达本连接的子 thread 通知目前只计为异常（D-T0-7）。**不可控，只能部分观测** | 不观测（没有 stream-json）；工具已请求关闭，**未验证** |
| Hub 侧历史 | 会话、消息持久化，SSE 续读（已有） | 同左 |
| 原生续接 | `thread/resume` 冷恢复。**支持（已验证）**：独立探针 S5 一次，经过 Hub 的 S6-3 一次。长对话、多次恢复**未验证** | CLI 有 `--resume`、`--continue`、`--fork-session`、`--session-id`；适配器固定使用 `--no-session-persistence`，**未接入** |
| 续接时的用量重放 | 恢复时一条，带上一回合的 turn ID，在 `turn/started` 之前到达；单独记为 `replayed_usage`，**不计入本回合（已验证两次）**。重放中的 `total` 没有保存 | 不适用 |
| 原生压缩 | `thread/compact/start`（手动）、`contextCompaction` item、已废弃的 `thread/compacted`、`AutoCompactTokenLimitScope`（schema）。**未接入**，从未触发（rollout 中没有压缩标记），压缩是否重置 `total` **未知** | CLI 有 `--autocompact <auto|tokens>`；`--system-prompt-snapshot` 写明压缩后才更换快照。本适配器每次都是新会话，**不适用、未验证** |
| 发送前计数 | 没有计数接口，**未知**；Hub 不可见的固定开销见第 4.3 节 | CLI 没有计数参数；API 的 token counting 不等于 CLI，**未接入** |
| 硬上限 | `TurnStartParams` 中没有输出或总量上限字段：**只能用软预算**，加上现有的次数和时限 | `--max-turns 1`（被接受）、`--max-budget-usd 1.00`（金额，只在 `--print` 下生效，执行效果**未验证**）。没有 token 上限 |
| 上下文窗口 | `modelContextWindow` = 475,000（每条更新都带） | 未提供 |
| 稳定前缀与缓存参数 | Hub 不控制 Codex 的请求构造；`thread/fork` 的 `experimentalPredictionMode` 写明可以提高缓存复用，**未接入、未验证** | `--exclude-dynamic-system-prompt-sections`、`--system-prompt-snapshot`（help 文本），**未接入、未验证** |

**Codex exec 传输**（历史）：exec 固定的程序已经被删除，`multi` 下 Codex 一律使用 app-server。exec 的用量来自 `turn.completed.usage`，单回合观测过 3 次。它在 `exec resume` 时是按回合还是按 thread 累计，**未验证**；T-1 不处理 exec。

## 3. 字段口径与归一化（T-1a 的规格）

归一化记录的字段：
- 身份：`provider`、`transport`、`model`、`binding`（快照中的 `adapter_version`）、`thread_id`、`turn_id`；
- 来源与完整性：`source`、`completeness`；
- 数量：`input_total`、`input_uncached`、`cache_read`、`cache_write`、`output_total`、`reasoning`；
- 原始与版本：`raw`（只含白名单中的数字字段）、`mapping_version`。

| 归一字段 | Codex app-server `TokenUsageBreakdown` | Claude Code `result.usage` |
|---|---|---|
| `input_total` | `inputTokens` | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` |
| `cache_read` | `cachedInputTokens`，属于输入的子集 | `cache_read_input_tokens` |
| `cache_write` | `cacheWriteInputTokens`。非 0 时记为 `mapping_unverified`，不参与下面的减法 | `cache_creation_input_tokens` |
| `input_uncached` | `inputTokens − cachedInputTokens` | `input_tokens` |
| `output_total` | `outputTokens` | `output_tokens` |
| `reasoning` | `reasoningOutputTokens`，属于输出的子集，**不再相加** | `null`（未知） |
| 核对 | `totalTokens == inputTokens + outputTokens`，不成立就记 `mapping_mismatch` | 没有 provider 总量字段，不自造 |

- 缓存命中率 = `cache_read / input_total`，与总输入、金额分开显示，不当作节省量。
- 映射带版本号：provider、CLI 版本或 schema 变化后，旧记录保持原来的映射，不重新解释。

## 4. 用量基线（真实运行，只读整理）

### 4.1 Codex（模型均为 `gpt-6.1-sol`，窗口 475,000）

“记录值”是当时探针或适配器保存的值。“本回合实耗”取自 rollout 的 `token_count`：回合结束时的 `total` 减去回合开始前的 `total`。数字依次为输入、其中缓存、输出、推理。

| 运行（版本） | 回合形态 | 请求数 | 记录值 | 本回合实耗（rollout） | 是否一致 |
|---|---|---|---|---|---|
| 10-14 只读拒写探测（exec） | 单回合 | 1 | 未单独登记 | 15,661 / 12,544 / 24 / 16 | — |
| 10-15 原生子 Agent 事件，父 thread（exec） | 有工具调用和子 Agent | 2 | 未登记 | 30,198 / 27,392 / 84 / 0（最后一次请求：15,141 / 14,848 / 22） | — |
| 10-15 原生子 Agent 事件，子 thread（exec） | 被中止 | — | — | `info` 为空，**未知** | — |
| S2（10-17，当时的固定版本） | 单回合 | 1 | 15,043 / 12,544 / 5 / 0 | 相同 | 一致 |
| S3 第一次、第二次（10-22） | 被拒绝的回合 | — | 无 | `info` 为空，**未知** | — |
| S3 第三次（10-22，alpha.11） | 命令审批被拒绝 → 回复 | 2 | 15,328 / 12,416 / 7 / 0 | **30,539 / 24,832 / 79 / 0** | **少记**（D-T0-1） |
| S4（10-22，alpha.11） | 首个输出之前中断 | — | 无 | `info` 为空，**未知** | — |
| S5 第一次，种入（alpha.11） | 单回合 | 1 | 15,754 / 12,416 / 7 | 相同 | 一致 |
| S5 第一次，回忆 | 被探针中止 | — | — | 没有 `token_count`，**未知** | — |
| S5 第二次，种入 | 单回合 | 1 | 15,751 / 12,416 / 7 | 相同 | 一致 |
| S5 第二次，回忆（冷恢复） | 单回合 | 1 | 16,270 / 12,416 / 11；重放单独记录，数值等于种入回合 | 32,021 − 15,751 = 16,270 | 一致 |
| S6-2 第一次（alpha.11） | 单回合 | 1 | 15,760 / 12,416 / 5 / 0 | 相同 | 一致 |
| S6-3 调用 1（alpha.11） | 单回合 | 1 | 15,785 / 12,416 / 7 | 相同 | 一致 |
| S6-3 调用 2（native_resume） | 单回合 | 1 | 16,304 / 15,616 / 13；重放 15,785 / 7 单独记录 | 32,089 − 15,785 = 16,304 | 一致 |
| S6-2 第二次（alpha.11.2） | 单回合 | 1 | 15,183 / 12,416 / 5 / 0 | 相同 | 一致 |
| 双 Agent 第二次 Codex（alpha.11.2） | 单回合 | 1 | 15,753 / 12,416 / 12 / 0 | 相同 | 一致 |

算术核对（`total = input + output`）：例如 15,043 + 5 = 15,048，30,539 + 79 = 30,618，15,661 + 24 = 15,685（推理 16 没有另加）。20 条有用量的 `token_count`，`last` 和 `total` 都成立，另有 4 条的 `info` 为空。

### 4.2 Claude Code（模型 `claude-opus-5-5`，2.1.308）

| 运行 | input | 缓存写 | 缓存读 | **总输入** | output | 缓存读占比 | CLI 估算金额 |
|---|---|---|---|---|---|---|---|
| 单回合 PASS（10-24，`evidence/cc-turn-real-run-1-pass`） | 2 | 1,750 | 0 | **1,752** | 4 | 0% | $0.014088 |
| 双 Agent 第二次（10-24，`evidence/two-agent-real-run-2-pass`） | 2 | 635 | 1,161 | **1,798** | 15 | 64.6% | $0.0056202 |

- 两次都是 `num_turns=1`，`modelUsage` 只有一个模型，工具已请求关闭，每次都是新会话。
- 第二次有缓存读，说明前缀缓存跨进程复用了；复用窗口不由 Hub 控制。
- 金额是 CLI 的估算，不按 API 单价换算成账单。

### 4.3 Hub 可见部分与 provider 实际输入的差距（只是观测，不是预测）

- **Codex**：S6-2 第二次的提示词很短，provider 输入为 15,183 token，其中 12,416 是缓存。也就是说，每个新 thread 的回合大约有 1.5 万 token 的固定开销（基础指令、工具定义、环境信息），这部分 Hub 看不到。
- **Claude Code**：双 Agent 第二次发送了 185 个字符，总输入 1,798 token；工具已请求关闭，固定开销约 1.7–1.8 千 token。
- T-1 的发送前预览可以把这些值标为“同一配置下的历史观测”，但**不能**当作本次的预估，也不能并进“Hub 可见估算”。

## 5. 去重规则（T-1 必须遵守）

| 来源 | 规则 | 依据 |
|---|---|---|
| 续接时的用量重放 | 不计入本回合。另存 `last` 和 `total`；它的 `total` 作为本回合的起点（这个用法**未验证**，目前只有 `last` 的记录） | S5、S6-3 |
| 同一回合的多条更新 | 本回合 = 最后的 `total` − 起点。新 thread 的起点为 0：14 个新 thread 的第一条更新都是 `total == last`，已验证。不把各条 `last` 直接相加作为唯一依据：同一条更新重复投递时会多算。各条 `last` 之和与 `total` 之差不一致时，记 `partial` 并计为异常 | S3 第三次、10-15 |
| 重复投递的同一条通知 | 按 `total` 去重；`total` 没有变化就不增加 | 推导 |
| 恢复时没有收到重放 | 起点未知：退回到各条 `last` 之和，记为 `partial` | 推导 |
| 终态之后才到的用量 | 不丢弃，记为 `late_usage`，完整性为 `partial`（目前这类事件计入 `late_events`，用量直接被忽略） | S6 D8 |
| provider 的累计计数 | Codex 的 `total` 是 thread 累计，**不能**跨回合相加。Claude Code 每次调用一个进程，result 就是本次；以后启用 `--resume` 前，要先验证 result 是否累计 | 第 4 节 |
| SSE 重放、重启补回（`restore_replies`）、重复的结束通知 | 账本以 `invocation_id` 为唯一键，幂等写入；SSE 事件不携带用量，只从数据库读取 | 现有契约 |
| 会话、根任务、按 Agent 的汇总 | 都是对账本的查询视图，不再写第二份 | 需求 T1 |
| 原生子 Agent 的 thread | 单独记为 `native_child_usage`，标明“不计入 Hub 配额”；父调用的覆盖度为 `partial` | 10-15、D-T0-7 |

## 6. 用量缺失时一律标为“未知”

- `completeness` 取 `complete`、`partial` 或 `unknown`。字段为 `null` 表示未知，**永远不写成 0**。
- 判为未知的情况：
  - 没有用量通知（首个输出之前中断、被拒绝的回合）；
  - 进程超时、被杀或断线；
  - 回合失败且没有用量；
  - 用量格式不合规；
  - 映射未验证（Codex 的 `cacheWriteInputTokens` 不为 0）；
  - 原生子 Agent 没有用量；
  - Claude Code 的 result 缺少 `usage`（桩场景 `success-no-usage`）。
- **失败和中断也要入账**：调用结算时无论有没有回复都写一条记录，不再依赖 `capture_reply`。
- 显示：
  - 单条记录写“用量未知（原因）”；
  - 汇总写“已知 X，另有 N 次未知”，不给出看似精确的总数或节省率。
- 预算：未知的调用保留为未结算项。严格预算模式下，有未结算项时阻止不能可靠计量的新调用；用户可以明确切换为软预算。

## 7. 本轮发现的缺陷（T-1 修复，本轮不改代码）

| 编号 | 严重度 | 现象 | 影响 | 修复归入 |
|---|---|---|---|---|
| D-T0-1 | 高 | app-server 适配器用每条 `thread/tokenUsage/updated` 的 `last` 覆盖本回合用量（`appserver_adapter.py:903-907`） | 多请求的回合少记，S3 第三次输入只记了约 50%、输出约 9% | T-1a |
| D-T0-2 | 中 | 页面 `usageText()` 直接显示 `inputTokens` / `input_tokens`（`chat.html:774-781`） | Claude Code 显示 2，实际 1,798；两家口径混在一起 | T-1b |
| D-T0-3 | 中 | 用量只在有回复时写进消息元数据（`conversations.py:234-241`）；没有用量时也不显示“未知” | 失败、中断的消耗不可见，看起来等于 0 | T-1a、T-1b |
| D-T0-4 | 低 | 适配器读取 `turn/completed.turn.usage`（`appserver_adapter.py:878-880`），schema 的 `Turn` 中没有这个字段；桩的普通回合只从这里给出用量（`appserver_stub.py:469`） | 离线测试走的是真实服务端不使用的路径 | T-1a：桩改为发 `thread/tokenUsage/updated`，适配器去掉这条读取 |
| D-T0-5 | 低 | Claude Code 适配器只保留 4 个数字字段，stdout 解析后删除 | 以后无法重新归一化，丢失按模型的数字 | T-1a |
| D-T0-6 | 低 | 重放只保存 `last` | 无法用 `total` 计算续接回合的起点 | T-1a |
| D-T0-7 | 低 | 子 thread 的用量通知只计为异常 | 原生子 Agent 的消耗不入账 | T-1a（记录并标为 partial），不升级成可控 |

**2026-10-24 后续**：D-T0-1 到 D-T0-7 已在 T-1a（`feat/t1-usage-ledger`）中离线修复，见 [会话后端契约](conversation-backend.md) 末节。本文的基线数字和结论不变；真实对照仍在 T-1h。

## 8. T-1 实施拆分

T-1 = T1（用量账本、预览、预算）+ T2（`@` 文件片段、引用预览、本地候选）。

- 按依赖顺序分两条分支，依次进行（不并行开会话）：
  - `feat/t1-usage-ledger`：T-1a、T-1b、T-1c；
  - `feat/t1-context-refs`：T-1d、T-1e、T-1f、T-1g。
- 每一步都是契约 → 实现 → 离线测试 → 提交；契约变化同步更新文档和能力表（AGENTS 规则 5）。
- 真实对照运行放在最后，每次单独申请。

| 步骤 | 内容 | 主要改动 | 离线验收（测试用第 4 节的真实数字作为 fixture，不含任何文本） |
|---|---|---|---|
| **T-1a 用量归一化与账本** | 第 3 节的映射加版本号；新表 `usage_ledger`（`invocation_id` 唯一，带 root、会话和 Agent 维度），结算时在同一事务内写入，失败和中断也写；修复 D-T0-1、3、4、5、6、7 | `contracts.py`（`UsageRecord`、映射版本）、`db.py`、`worker.py` 结算路径、`appserver_adapter.py`（逐条保存 `last`/`total`，按第 5 节计算）、`claude_code.py`（扩大数字白名单）、`appserver_stub.py`（改为真实路径）。旧记录标为 `legacy_last`，有工具活动的标为可能少记 | S3 第三次的两条更新 → 30,539 / 24,832 / 79；S5、S6-3 的续接排除重放，差值正确；重复通知幂等；S4 没有用量 → unknown；Claude Code 2 / 635 / 1,161 / 15 → 总输入 1,798；缺少 usage → unknown；cacheWrite 不为 0 → mapping_unverified；`total` 与 `last` 之和不一致 → partial；重启补回不重复入账 |
| **T-1b 用量视图** | API 返回单次调用、会话、根任务和按 Agent 的汇总（都是查询视图）；页面显示归一化后的口径和“未知” | `api.py`、`chat.html`、`index.html` | 页面显示“输入 1,798（缓存读 1,161 / 写 635）/ 输出 15”和“输入 15,753（缓存读 12,416）/ 输出 12”；未知时显示原因；汇总写“已知 X + N 次未知”；现有 UI 测试更新 |
| **T-1c 预算与原子准入** | 策略字段：单次输入预算、根任务总预算、提醒阈值，模式为 strict 或 soft（走已有的策略版本流程，改动时撤销未消费的审批）；预留量与已有名额在同一个 CAS 中占用；结算时释放预留并记入实耗，未知的保留为未结算；审批卡显示估算、已用、在途和未知 | `db.py`（`TreeQuotaRow` 加 token 列）、`service.py` 准入、`worker.py` 结算、页面审批卡 | 两个 Worker 争最后一点额度，只启动一个；已知超出预算 → 启动次数为 0；严格模式下有未结算项 → 阻止新调用；软预算放行并提醒；重启后预留和未结算项不丢；改动预算会撤销未消费的审批。只有软上限，不承诺精确到 token 的停止 |
| **T-1d 发送前上下文预览** | 列出组成部分：适配器固定框架、用户输入、交接框、文件片段；本地估算方法写明“估算”，首版按字符，不安装 tokenizer；provider 内部不可见的部分单独标为未知，可以附第 4.3 节的历史观测；预览绑定进审批快照 | `chat.py`、`conversations.py`、契约、页面 | 预览的各部分之和等于实际发送的文本；不可见部分不并进估算；预览变化会让审批失效 |
| **T-1e `@` 引用解析与路径边界** | 先定语法：`@路径`、`@"含空格路径":L起-L止`、`@diff`，首版要求不带歧义地支持盘符和中文路径；在解析后的真实路径上检查边界（大小写、junction/symlink、`..`）；默认排除凭据、`.env`、`var/`、依赖和构建产物、证据目录；限制单文件和总大小 | 新模块（kernel 或 adapters 共用）、契约 | 单文件、行范围、空格、中文、盘符、diff、重叠和重复区间都能正确处理；只选片段时不会带上全文；越界和敏感路径被拒；从聊天文本中发现的路径不扩大读取权限 |
| **T-1f 不可变上下文包与审批绑定** | 片段内容哈希、范围和仓库修订进入 `payload_hash`；执行时使用批准的快照；重新读取后内容变了，就重新生成待审批调用；Hub 自己把内容展开进提示词，不透传 `@` | `contracts.py`、`chat.py`、`worker.py` | 批准后修改文件 → 不会偷换内容；哈希只作引用标识，正文照常发送；新 thread 或换 Agent 时不假设对方已经读过 |
| **T-1g 本地候选建议** | 根据用户提到的路径、符号、错误位置和当前 diff，在本地有上限地检索，结果只作为待发送的引用卡；不静默读全盘，不使用云端 embedding | 新模块、页面 | 候选不会自动发送；批准时一并确认显示出来的上下文；最终发送的内容与预览一致 |
| **T-1h 文档与真实对照申请** | 更新 `conversation-backend.md`、能力表和交接文档；准备真实对照的申请（不执行） | docs | 申请列出：一个带工具拒绝的 Codex 回合，用来验证多请求入账和重放的 `total`；一个 Claude Code 回合，用来验证账本和页面口径。各自需要单独批准 |

**T-1 不做的**：压缩和交接包（T-2）；缓存参数和按需加载（T-3）；金额换算；订阅额度接入（`account/rateLimits`、`account/usage/read` 先列为 T-1 之后的候选，接入前需要能力验证）；exec 传输。

## 9. 未验证 / 不认定

- 重放中的 `total` 是否等于上一回合结束时的 thread 累计（只有 `last` 的记录，两者在单回合 thread 上相等）。
- `cacheWriteInputTokens` 的含义，以及它是否属于输入的子集（一直是 0）。
- 压缩（手动或自动）是否重置或改写 `total`，压缩本身的请求是否计入。
- 被中断或被拒绝的回合是否产生了计费消耗（只知道没有上报）。
- Claude Code 在 `--resume` 下 result 用量是否累计；是否有未出现在 `modelUsage` 中的辅助请求；`--max-budget-usd` 是否真的生效。
- 缓存的 TTL 和复用条件（两家都只能观测）。
- 金额：Claude Code 的 `total_cost_usd` 是估算；Codex 没有金额字段。
- 本基线只覆盖固定提示词下的极短回合，不代表真实任务的消耗。

## 10. 输入证据（不入库，只登记哈希）

本轮没有产生新的证据文件。下面是读取过的文件；其中已在原运行记录中登记的，哈希与原记录一致。

| 文件 | 字节 | SHA-256 | 用途 |
|---|---|---|---|
| `%LOCALAPPDATA%\AgentHub\codex-home\sessions\2026\10\14\rollout-2026-10-14T21-39-02-01a13aa3-448f-7ac2-845c-3bfeb1e0654c.jsonl` | 45725 | `4eed5934751ce03a5d547d7868f4851f5ef4eb418ae4816301847920c64f64d4` | 算术核对 |
| `…\14\rollout-2026-10-14T22-39-56-01a13adb-05da-7df1-93d0-8c97350ca916.jsonl` | 53326 | `6d858d075079dbbaf0b0df57702c7c643c0f5da7aa307bb2dfd5df7f0c474696` | 推理用量属于输出的子集 |
| `…\14\rollout-2026-10-14T23-02-38-01a13aef-cc2c-7423-9fc0-fd949df588d2.jsonl` | 53351 | `1a510ed4c9f3cd19fbad01f6ebb5bb95aad95238f145048b251ce2cf6d2a8cd8` | 算术核对 |
| `…\15\rollout-2026-10-15T00-33-18-01a13b42-d0e0-75a3-87dc-5a524e75e097.jsonl` | 57364 | `f975ce9e9097ebefe12d9007a5d54eac030b60498482be6411dfd5502c766c18` | 两次请求，算术核对 |
| `…\15\rollout-2026-10-15T00-34-17-01a13b43-bdeb-74e2-be6f-127478b1ab37.jsonl` | 49565 | `5582aa297bf70eacab2ab417994b329db594c4e97ab819fc8d8477265dd0eaa0` | 父 thread 两次请求 |
| `…\15\rollout-2026-10-15T00-34-27-01a13b43-e437-7c82-8f45-8deeeaa40082.jsonl` | 78570 | `65f52f7b7921a8b5ae1f5eb2299a66684cf1088db8825a588ca85807e9c89b2a` | 子 thread 用量为空 |
| `…\15\rollout-2026-10-15T00-35-23-01a13b44-bf15-7713-800c-b8134f2c15de.jsonl` | 52342 | `528d0870e1529cc56874f77925cfc187759e9feb7aa1712c1f267fe7c233707a` | 两次请求，算术核对 |
| `…\17\rollout-2026-10-17T22-14-20-01a14a36-a8d2-7f81-b603-5d369b427c91.jsonl` | 46320 | `4d6b070fe5d22f522fcd26314b8661646039b838588b62a52fa67b0a720ae42f` | S2 |
| `…\22\rollout-2026-10-22T02-45-14-01a15fc8-1db1-7072-9530-53c2f4eae728.jsonl` | 44558 | `fd4f0bb1c8d09c064711e7879aca6545aac8a2fd73ccffda828089fb4fad3e95` | S3 第一次，用量为空 |
| `…\22\rollout-2026-10-22T03-02-17-01a15fd7-ba60-7b01-9a22-aef7fe05772e.jsonl` | 50337 | `f4bd7cf5e7c6abca19d0f7ffc3bb8be7e6b698be3760061ecfb299216d4f4502` | S3 第二次，用量为空（已登记） |
| `…\22\rollout-2026-10-22T21-19-39-01a163c4-5ccd-72b3-957a-36a7b2ffa77b.jsonl` | 49421 | `486e522f6fbf3baf849bc85c9355f4e7f92a22ec3b29556cc93924ad0c934dd6` | S3 第三次，两次请求 |
| `…\22\rollout-2026-10-22T21-49-29-01a163df-b41f-7050-803a-c7d3022bf26b.jsonl` | 48961 | `149d311951d5537334e327a83a13338fc2ee31cb30f7ea4ff75fe259a945f8f4` | S4，用量为空 |
| `…\22\rollout-2026-10-22T23-19-53-01a16432-7556-7e71-9af8-ade1723188a6.jsonl` | 55200 | `d6977a02fd97b15d60b3f23a82bb42f0add87917b72a5d2ad8523d74a93593fb` | S5 第一次 |
| `…\22\rollout-2026-10-22T23-54-51-01a16452-77ab-7983-93e9-a6a44ec1ab25.jsonl` | 62962 | `c838a989df946ed8d7874323e7dc7418fe7ec21c5743a7b3155a0b049902fdf8` | S5 第二次，累计跨冷恢复 |
| `…\23\rollout-2026-10-23T02-48-18-01a164f1-496b-7792-9165-6de8c824dd23.jsonl` | 50832 | `50f7dcdbb30778540baf7b94459a080331230b2801bff5981adb4cb1e18697c4` | S6-2 第一次 |
| `…\23\rollout-2026-10-23T03-49-29-01a16529-4ae8-7d93-83de-6e6901ff9662.jsonl` | 63403 | `b960db2629b9e32697a2f572d8fd12d45c3e55a08507286aa532e0fa426e217e` | S6-3 |
| `…\24\rollout-2026-10-24T00-22-34-01a16992-36d1-7b11-abe9-6c6782cb532b.jsonl` | 46149 | `b0d1b07680b63055c24009a6ba6bf59116d872c11913d828ebd72393103a2aee` | S6-2 第二次 |
| `…\24\rollout-2026-10-24T01-25-31-01a169cb-d9d2-7052-8f12-80bd1c8ae787.jsonl` | 50867 | `faf21c347e0ad60dcf84d8f70d1fc26a4790961e00b9e365933a8fd0b8b62d32` | 双 Agent 第二次（已登记） |
| `appserver-s3-probe\var\appserver-s3\real-3z3ium0p\result.json` | 7659 | `0cfd8e56873e7dd3e268d1fd5e61ab48d21204d433556bc932baec8f28343f7a` | S3 第三次：2 条用量更新，记录了最后一条 |
| `appserver-s6\var\appserver-s6-3\real-u5ktawg5\result.json` | 5269 | `79cb1cccd5447df3c14e5cf83cf0b52fa66e9b0e78dcdf8d685a8c68571d0cd5` | S6-3 用量与重放 |
| `claude-code-min\var\claude-code-min\turn-real-jrjo5m_r\report.json` | 3645 | `f091ca8a23926a6a7f4d3bb9b07803ac725e71190269dcd2fc6b3b22a419f619` | Claude Code 单回合（已登记） |
| `claude-code-min\var\claude-code-min\identity-real-v6v3sh1q\help.txt` | 22319 | `b10f5c36adfe3285f6c5a0d18284f466e6c4486499de56ef62745e13312a7fbc` | 续接、压缩、预算参数 |
| `p3-two-agent\var\two-agent\real-n_bfpgdh\result.json` | 1466 | `00e673d645d43ec5402756c3d18bfa046e20ff93b32b7914d881b1de41504149` | 双 Agent 判定（已登记） |
| `%TEMP%\agenthub-two-agent-run2\claude-code-executions.db` | 12288 | `238fa3ce2e266bc73e6793961a30afc0710d23a7060935c40f77d53a0c7c7859` | Claude Code 2 / 635 / 1,161 / 15 |
| `%TEMP%\agenthub-two-agent-run2\appserver-executions.db` | 12288 | `daf78ae88d77c41eeceb97b614c6ff5ac9141bc1551128294c7579eb6656ac87` | Codex 15,753 / 12,416 / 12 |
| `codex-runtime\var\codex-runtime\step-a-alpha.11.2-5t52dkuz\schema\codex_app_server_protocol.v2.schemas.json` | 776767 | `f826863e71e4e5678d6900a87c916414a1c5a06ca805d42037e8f91a762688e9` | 用量、压缩、续接的 schema |

工作树内的路径相对于 `E:\plug\agent-hub\.worktrees\`；`…` 表示 `%LOCALAPPDATA%\AgentHub\codex-home\sessions\2026\10`。rollout 和数据库中含有提示词与回复，本文只引用了数字。
