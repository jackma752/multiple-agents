# 当前交接：多 Agent 路由与手动交接（P3，2026-10-24，分支 `feat/p3-two-agent`）

- 经用户批准，从 `feat/appserver-s6` 新开本分支，用 `--no-ff` 合并了工作流 A（`feat/codex-runtime`）和工作流 B（`feat/claude-code-min`）。没有推送，没有合并到 main。
- 新增 `RoutingAdapter`（`AGENT_HUB_ADAPTER=multi`）：每个调用都在自己 Agent 的适配器上检查和运行；路由器层面一律拒绝。
- 新增手动交接：`handoff_message_ids`。组合后的输入由审批绑定，审批卡完整显示。
- 说明见 [多 Agent 对话](multi-agent.md)。离线测试和浏览器桩验证都已完成。
- 达到 T 启动节点还差的真实运行：Codex 的 S6-2 冒烟；Claude Code 的 `--max-turns` 决定、用户登录、一个回合；最后一次双 Agent 真实对话。每项都需要单独批准。

---

# 当前交接（工作流 A）：Codex 运行时另存与新步骤 A（2026-10-23）

- 起因：S6-4 已获批准，但在前置检查阶段停止，没有启动服务，也没有模型调用。原因是桌面更新在 18:20 删除了固定的 alpha.11（`bin\6257d9e8f63c0f2b`）。
- 运行时改放在 AgentHub 自己的目录，按内容寻址：`%LOCALAPPDATA%\AgentHub\runtimes\codex\sha256-<前 16 位>\`。工具为 `tools/probes/codex_runtime_stage.py`：复制前后按登记哈希核对，不覆盖已有文件，并写 `provenance.json`。暂存了两份：
  - `alpha.11.2`（推断版本，**未验证**），连同全部配套文件，作为运行时；
  - alpha.11 的原二进制（在 `.codex\.sandbox-bin` 找到，逐字节相同），**只作存档**：同版本的沙箱安装程序和 code-mode host 已无法找回。
- 新的零模型步骤 A（`tools/probes/codex_runtime_step_a.py`）已在 2026-10-23 23:35 经批准执行一次，结果 **IDENTICAL_SCHEMA**：
  - 版本为 `codex-cli 0.163.0-alpha.11.2`；
  - 71 项协议事实齐全，schema 与 alpha.11 逐字节相同；
  - 模型回合为 0，没有残留进程。
  - 源码差异：8 个提交，包括工具链、Windows MXC 沙箱迁移、requirements 注册表覆盖；S3 相关的源文件都没有变。
  - 详见 [Codex 运行时](codex-runtime.md) 第 7 节，标签 `evidence/codex-step-a-alpha11.2-pass`。
- 已重新固定：`PINNED_APPSERVER` 指向 AgentHub 副本；受影响的离线测试 324 项全部通过。
- S6-2 冒烟已于 2026-10-24 在 `e7237a3` 上经批准执行一次，**PASS**（只认定本次）：回复 `OK`，没有工具和子 Agent，进程确认退出，名额已释放。见 [S6-2 第二次实录](appserver-s6-2-2026-10-24-run2.md)，标签 `evidence/s6-2-real-run-2-pass`。
- 下一步：S6-4（工具拒绝，网页路径）和 S6-5（中断）分别申请。

---

# 当前交接（工作流 B）：Claude Code 最小接入，离线（2026-10-23，分支 `feat/claude-code-min`）

本节自成一体；同日另一分支也在修改本文件，合并时两节并列保留即可。

- 基于 `feat/appserver-s6` 的 `2ccae55`。提交号见本分支 `git log -1`（提交说明含本轮验证输出）。
- 新增 `ClaudeCodeAdapter`（`packages/adapters/agenthub_adapters/claude_code.py`），`AGENT_HUB_ADAPTER=claude-code` 选用；离线桩 `tools/demo/stub_claude_code.py`（按文档手写，不是捕获）；真实测试驱动 `tools/probes/claude_code_min.py`（默认桩模式）。
- 路径：Hub 调用 → 用户审批（监督模式需本次显式同意）→ Worker 消费审批并预留名额 → 一个 `claude -p` 进程（固定参数、提示词走 stdin、无工具、`--max-turns 1`、仓库外空工作目录）→ 解析 JSON result → 按现有规则结算。没有新增审批或配额机制。
- 准入：严格模式拒绝；监督模式只认代码常量中的 2.1.308（静态核对：路径、大小、SHA-256、签名），并且要求 `ZERO_MODEL_IDENTITY_REPORT` 已记录。当前为 `None`，**真实启动一律被拒**。
- **没有运行 Claude Code，没有模型回合，没有读取凭据。** 能力全部未验证，见 [Claude Code 适配器](claude-code-adapter.md) 与能力矩阵。
- 剩余阻塞：独立 CLI 没有可用登录（默认配置目录无 `.credentials.json`，桌面会话的认证不可借用）。需要用户在专用配置目录自行登录，以及用户批准真实测试（申请见 [Claude Code 适配器](claude-code-adapter.md) 第 6 节：步骤 A 零模型身份核验 → 离线提交记录报告哈希 → 步骤 L 用户登录 → 步骤 B 最多一个模型回合）。
- 另一个风险：固定文件是桌面应用管理的，桌面更新可能删除它，届时需要重新固定。
- **2026-10-23 23:35 步骤 A 经批准执行一次，结果 INCONCLUSIVE**：
  - 两条命令都返回 0，版本为 `2.1.308 (Claude Code)`，运行前后哈希一致，模型回合为 0；
  - 固定参数中只有 `--max-turns` 不在 `--help` 中，其余 8 个都在；
  - 按申请停止，**没有记录 `ZERO_MODEL_IDENTITY_REPORT`**，没有进入步骤 L 和 B；
  - 是否保留 `--max-turns` 等用户决定，详见 [Claude Code 适配器](claude-code-adapter.md) 第 7 节，标签 `evidence/cc-identity-real-run-1-inconclusive`。
- **2026-10-24**：用户选择 (a)，保留 `--max-turns`，`ZERO_MODEL_IDENTITY_REPORT` 已记录为 `b57a1d5f…`。下一步：
  - 用户自己登录（步骤 L）；
  - 申请最多一个模型回合（步骤 B）。
  - 两项都见 [Claude Code 适配器](claude-code-adapter.md) 第 8 节。
- **2026-10-24 用户已登录；步骤 B 经批准执行一次，PASS**（只认定本次）：
  - 回复 `OK`，`num_turns=1`，有用量；
  - 全部固定参数被接受，包括 `--max-turns`；
  - 名额已释放，stderr 为空。
  - 见 [单回合实录](claude-code-turn-2026-10-24-run1.md)，标签 `evidence/cc-turn-real-run-1-pass`。
- 下一步：把本分支和 `feat/codex-runtime` 的记录合并进 `feat/p3-two-agent`（需批准），再申请一次双 Agent 真实对话。

---

# 历史交接：网页会话、重连、命令展示与监督模式修复（S6-4 前置，2026-10-23）

- 修复了演示中发现的两个页面缺陷，并补齐了网页的监督模式入口，说明见 [会话页面](chat-frontend.md) 末节：
  - **会话切换与事件流竞态**：引入视图代际 `S.viewGen`，旧会话的任何异步结果都不能再写入当前会话；普通重连保留状态，只有 409 才完整重放；
  - **一次操作多建空会话**：只保留一个创建入口 `createConversation()`；
  - **命令展示**：严格拆分，无法可靠拆分时只显示原文；S3 的严格命令范围判断不变；
  - **监督模式**：调用时必须明确选择模式；监督调用要在“本次”勾选边界同意，默认不勾选，不沿用；后端检查保留。
- `tools/demo/serve.py` 新增 `--codex-transport {exec,app-server}`。选 app-server 时，启动信息打印代码常量中的固定身份和环境变量覆盖情况。
- 验证（离线）：
  - 针对性回归（子 agent 执行）：892 passed；
  - 浏览器在 demo 离线桩上逐项验收：跨 3 次重连、切换会话再返回都不丢失也不重复，倒计时正常，仍能拒绝；一次新建只产生一个会话；监督模式的同意不沿用。
- 网页修复已提交为 `afa7e7c`。
- S6-4 申请：
  - 新增只读的证据收集脚本 `tools/probes/appserver_s6_4_collect.py`，离线测试在 `tests/test_appserver_s6_4_collect.py`；
  - 申请见 S6 规划第 12 节，服务配置是 `.claude/launch.json` 中的 `s6-4-real-web`；
  - **未执行**，需要用户单独批准。

---

# 历史交接：S6-3 第一次真实运行 PASS（只认定本次，2026-10-23）

- 判定修正 `553fe1f` 之后，执行了一次，复用 `identity-hkzdmoqj`。执行前确认：提交、工作树、固定程序、步骤 A 报告和证书包都没有变化，代理在监听。
- 调用 1：新 thread 写入暗号；succeeded，回合 completed，回复 `STORED`；进程退出，后代检查已执行且为空。
- 调用 2：`native_resume` 实际发出 `thread/resume`，恢复的是同一个 thread，恢复结构中有调用 1 的回合；新 turn ID；输入就是回忆提示词，不含暗号；工作目录没有变；succeeded，回合 completed，回复恰好等于暗号。
- 一条上一回合的用量重放单独记录，没有计入调用 2。两次调用各自审批并显式同意，名额都已释放；没有工具活动，没有子 Agent，没有后代进程残留。
- 见 [S6-3 第一次实录](appserver-s6-3-2026-10-23-run1.md)，标签 `evidence/s6-3-real-run-1-pass`。授权已用完，没有执行 S6-4、S6-5、S6-V 或 S7。
- **仍未验证**：网页点击路径、长对话或多次恢复、换 cwd、S6-4（工具拒绝）、S6-5（中断）、子 Agent 控制。

---

# 历史交接：S6-3 判定修正（2026-10-23）

- 按用户要求，执行前修正了两处判定：
  - 调用 2 必须是 succeeded、回合以 completed 结束、回复含暗号才能 PASS，failed 带暗号也不通过；
  - 后代检查区分“已检查且为空”和“检查不可用”：第一阶段不可用时不提交调用 2，第二阶段不可用时判 INCONCLUSIVE。
- S6-2 驱动的同类缺陷一并修正，不重跑 S6-2。
- 补充的反例：失败回合带暗号、失败回合不带暗号（经过 Hub 的桩场景加判定函数），两个阶段的后代检查不可用，以及回合不是 completed。这 4 个经过 Hub 的反例在修正前的判定下全部失败。
- 执行申请（S6 规划第 11 节）只更新了被测提交和这两条判定，其余范围不变。

---

# 历史交接：S6-3 离线准备（2026-10-23）

- S6-2 按现有证据收口，不追加重复运行。
- S6-3 驱动 `tools/probes/appserver_s6_3.py`：
  - 两次调用都经过 API → 审批 → Worker → 适配器，各自审批，各自显式同意；
  - 第一次在新 thread 上写入随机暗号（`S6-` 加 10 位）；确认它的 app-server 进程已经不在进程表中，才提交第二次；
  - 第二次用 `native_resume` 恢复同一个 thread，沿用原工作目录，输入是固定的回忆提示词。
  - 判定分 PASS、FAIL、INCONCLUSIVE 三类：结构问题判 FAIL；能恢复但答不出暗号，或者出现工具、子 Agent 活动，判 INCONCLUSIVE。
- 离线反例都在 `tests/test_appserver_s6_3.py`：没有走 resume、恢复了错误的 thread、复用 turn ID、第一个进程没有退出（以及进程表读不到）、暗号或写入暗号的提示词进入第二次输入、重放用量被误计、第二次调用没有批准，另有回答缺失、工具或子 Agent 活动、预检失败、真实模式缺少确认参数。
- 适配器新增审计字段，并修正了后代进程判定中 PID 复用导致的误报（离线时发现 `acrotray.exe` 被误算为后代）。
- 没有真实运行。S6-3 的执行申请见 [S6 规划](appserver-s6-plan.md)第 11 节，需要用户单独批准；不接着执行 S6-4、S6-5、S6-V 或 S7。

---

# 历史交接：S6-2 第一次真实运行 PASS（只认定本次，2026-10-23）

- 按用户批准在 `26a9553` 上执行一次，复用 `identity-hkzdmoqj`。执行前确认：提交、工作树、固定程序、步骤 A 报告和证书包都没有变化，代理在监听。
- 结果 PASS：
  - 一次 supervised 调用经过进程内的 API、内核和 app-server 适配器，状态为 succeeded，启动 1 次，名额已释放；
  - 事件顺序正确，回复恰好是 `OK`，有服务端用量；
  - 没有工具活动，没有子 Agent，没有异常事件，没有后代进程残留；
  - 绑定身份一致，工作目录在仓库外并且为空。
- rollout 中 `multi_agent_version` 为 v2，只是模式字段，不是派生证据。stderr 中有 5 行“系统 skills 重装时文件被占用”的 ERROR，与 S3、S4 的真实运行完全相同，是环境里已有的现象。
- 见 [S6-2 第一次实录](appserver-s6-2-2026-10-23-run1.md)，标签 `evidence/s6-2-real-run-1-pass`。授权已用完，没有重试，没有执行 S6-3。
- **仍未验证**：网页点击路径、S6-3（续接）、S6-4（工具拒绝）、S6-5（中断）、S6-V、S7。

---

# 历史交接：S6-1 评审修正，S6-2 申请已提出（2026-10-23）

- 评审修正三处，说明见[会话后端契约](conversation-backend.md)的“S6-1”一节：
  - 身份复核改为比较双方，切换到 exec 等没有绑定接口的适配器也会被拒，`"unavailable"` 永不放行；
  - 工具输出通知计入工具活动，不保存原文；
  - 同一批通知中排在终态之后的事件计入 `late_events`。
- 统一了门禁相关用词：准入、身份复核、消费门禁、工具决定关闭（生效点）、认领。
- 新增 S6-2 驱动 `tools/probes/appserver_s6_2.py`：在进程内走 API 和内核，默认用离线桩，真实模式需要两个确认参数。测试在 `tests/test_appserver_s6_2.py`。
- 申请见 [S6 规划](appserver-s6-plan.md)第 10 节。**未执行**，需要用户单独批准。
- S1–S5 不需要重做。

---

# 历史交接：S6-1 离线加固（2026-10-23）

- 分支 `feat/appserver-s6`，目录 `E:\plug\agent-hub\.worktrees\appserver-s6`。说明见[会话后端契约](conversation-backend.md)的“S6-1”一节，能力表见 [adapter-capabilities](adapter-capabilities.md)。
- 完成 D1–D11：
  - 拒绝从不变成 cancel；
  - 固定设置传入并回读；
  - 卡片上的命令拆开展示，理由标为模型说明；
  - 观测工具 item；
  - 中断应答超时记为“尚未应答”；
  - thread 沿用首次的 cwd；
  - app-server 专用固定身份常量（alpha.11），不读环境变量；
  - 事件必须带 threadId；
  - 恢复时的用量重放单独记录；
  - 后代进程只记录；
  - 快照绑定适配器身份，消费前复核。
- 准入：真实 app-server 在严格模式下仍被阻止；监督模式只在全部固定条件满足时放行。真实 factory 的工具卡只能拒绝。
- 验证（Windows `.venv`，离线）：全量 **1042 passed in 200.99s**。新文件 `tests/http/test_appserver_s6.py` 共 65 项，连续 3 次通过；5 个变异体（cancel 回退、去掉消费前的身份复核、去掉 accept 抑制、放宽 threadId、跳过设置回读）都被测试发现。浏览器在 demo 离线桩上走完“发消息 → 批准 → 工具卡（原文、拆开后的命令、理由标注）→ 拒绝 → 完成”。没有执行 Codex。
- **仍未验证**：经过 Hub 的任何真实运行（S6-2 到 S6-5，每次单独批准）、子 Agent 关闭路径（S6-V）、accept 链（S7）。S4 收口和 S1–S5 的历史证据不变。

---

# 历史交接：S6-0 整合分支（2026-10-23）

- 分支 `feat/appserver-s6`，目录 `E:\plug\agent-hub\.worktrees\appserver-s6`：以 `feat/interrupt-stop`（98fbc44）为基础，合并 `plan/appserver-s6`（7a0c0a9，其中包含 `feat/appserver-s5-probe` 的 425281e）。没有 rebase，没有改动 main，没有推送。
- 冲突只有本文件：两条线的交接按“探针线、产品线”的顺序并列保留，原文不改。`README.md` 自动合并。
- 验证（Windows `.venv`，离线）：合并后全量 **977 passed in 190.06s**，其中包含两条线各自的测试和 S1–S5 探针的测试。没有执行 Codex。
- 方案见 [S6 规划](appserver-s6-plan.md)（修订 1）。下一步是 S6-1 离线加固（D1–D11）；真实运行仍需逐项批准。

---

# 历史交接：S6 方案修订 1（2026-10-23）

- 按用户指示修订 [S6 规划](appserver-s6-plan.md)：
  - 新增 D11：快照绑定 app-server 身份，消费前、启动前分别复核；
  - 完成有边界的 multi_agent / v2 源码核查（第 2.1 节，alpha.11）：`--disable multi_agent` 只关闭 Collab，模型声明了版本时不起作用；`[agents] enabled = false` 是候选关闭路径，尚未验证（S6-V，需另行批准）；会话的模式字段不是派生证据；
  - 写明监督模式中已有的停止边界和 CLI 绕过边界（第 4.11 节）；
  - 修正“作废卡一律 decline”：作废卡永远不发送任何决定，唯一的发送路径是认领（第 4.4 节）；
  - 更正 D6 的工作目录方案。
- 四项决定：用户指示继续推进 S6-0 和 S6-1，按推荐项执行，理由见第 8 节。
- 没有改代码，没有真实运行。S4 收口和 S1–S5 的历史证据不变。

---

# 历史交接：S6 离线规划完成，等待用户的四项决定（2026-10-23）

- 方案见 [S6 规划](appserver-s6-plan.md)，在 `plan/appserver-s6` 分支上，只有文档，没有改代码，也没有真实运行。
- 建议：真实 app-server 只在 supervised 下放行，strict 继续阻止；第一阶段工具只允许拒绝；固定身份写在代码常量中，不能被环境变量覆盖。
- 发现：产品适配器与 S3–S5 的实测结论有 10 处差距，其中 D1（拒绝会被改成 cancel）、D2（不设置、也不核验沙箱和审批设置）、D7（固定身份过期，并且可被覆盖）必须先修。
- 待用户决定：工具 accept、续接时的 cwd、固定身份的来源、合并方式。

---

# 历史交接：S5 第二次真实运行 PASS（只认定本次冷恢复后历史可用，2026-10-22 深夜）

- 在 `38288c4` 上运行，程序版本 alpha.11，certifi 证书包为 `50f79f98…0785`。
- 过程：
  - 阶段 1 在新的专用 thread 上完成，健康检查通过；
  - A（PID 18332）退出后，才启动 B（PID 32056）；
  - 冷恢复时带回了写入的回合和消息；
  - 恰好一条阶段 1 的用量重放（数值与阶段 1 相同）；
  - 回忆回合在提示词不含暗号的情况下**准确答出暗号**，回合正常完成，没有工具调用；
  - 工作目录已删除，没有后代进程残留。
- 见 [S5 第二次实录](appserver-s5-2026-10-22-run2.md)，标签 `evidence/s5-real-run-2-pass`。
- **仍未验证**：内核 `native_resume` 的接入、长对话和分页历史、原生工具和子 Agent 的事前控制、聊天功能的验收。S4 中输出进行中的中断、工具子进程的停止，也仍未验证。
- 所有分支都没有合并到 main，也没有 remote。

---

# 历史交接：S5 判定修订完成（只容许一条重放），等待第二次单回合授权（2026-10-22 深夜）

- 回忆阶段只容许一条 `thread/tokenUsage/updated`：带阶段 1 的 turn ID，在 `turn/started` 之前到达，单独记入 `replayed_usage`，不算回忆回合的用量。
- 第二条重放、在开始之后才到的重放、其他 turn 或其他 thread 的重放，仍判 FAIL。
- 测试：S5 75 项，连续 3 次通过；全量 813 passed。两个变异体都被测试发现。没有执行 Codex。
- 第一次运行的 FAIL 保持不变。第二次真实 S5 需要用户另行批准。

---

# 历史交接：S5 第一次真实运行 FAIL（重放用量被误判），上下文恢复仍未验证（2026-10-22 晚）

- 在 `2cda212` 上运行，程序版本 alpha.11，certifi 证书包为 `50f79f98…0785`。
- 已通过：阶段 1、健康检查、进程关系（A 为 PID 10944，退出后才启动 B，PID 13496）、恢复结构（`history_returned: true`）。
- 原因：回忆阶段收到一条 `thread/tokenUsage/updated`，带阶段 1 的 turn ID。按源码推断，这是被测版本在恢复时正常的用量重放。探针把它判为 `foreign_turn`，立即停止了进程，回忆回合在没有输出时就被中止了。
- **上下文是否恢复仍未验证。** 两次 `turn/start` 都在上限之内。见 [S5 第一次实录](appserver-s5-2026-10-22-run1.md)，标签 `evidence/s5-real-run-1-fail`。
- 没有重试。如果继续，需要先离线修订判定（只接受这一种重放），再另行申请。

---

# 历史交接：S5 三项修正完成，等待单次真实授权（2026-10-22 晚）

- **证书**：pip 升级到 26.2.2，certifi 升级到 2026.10.19，`cacert.pem` 的 SHA-256 为 `50f79f98…0785`。
  - `pip_trust.py --negative-control` 通过：PyPI 用 IP 访问时被证书错误拒绝；Codex 使用的证书包能验证 chatgpt.com。
  - pip 现在使用系统证书库，`cert` 只追加 CA。
  - 新旧环境的哈希都已登记，S1–S4 在旧环境下取得的证据保持原样。
- **S5 修正一**：启动 B 之前，`plant_health` 与最终判定使用同一组函数；后代进程有 3 秒宽限期。
- **S5 修正二**：工作目录删除失败判 INCONCLUSIVE（`workdir_cleanup_failed:<类型>`）。
- **测试**：S5 63 项，连续 3 次通过；pip_trust 13 项；全量 801 passed。没有执行 Codex。
- **下一步**：用户单独批准一次真实 S5（2 个回合）后才执行。

---

# 历史交接：S5 冷恢复探针离线准备完成，等待单次真实授权（2026-10-22）

- **S4 收口**：接受有限结论。输出进行中的中断、工具子进程的停止，保持未验证。
- **pip、证书**：`fix/pip-proxy-trust` 的 `f4a2866`。
  - 代理不拦截 TLS，没有需要信任的私有 CA；两个 certifi 包都没有被改动。
  - venv 的 `pip.ini` 让 pip 走代理，验证保持开启。
  - 新增 `tools/bootstrap/pip_trust.py` 用于检查。
- **S5**：分支 `feat/appserver-s5-probe`，目录 `E:\plug\agent-hub\.worktrees\appserver-s5-probe`。
  - 每次运行新建一个专用 thread。进程 A 写入随机暗号，A 退出后才启动进程 B；B 冷恢复同一 thread，再提出回忆问题。
  - 最多 2 个模型回合。
  - PASS 需要恢复响应中带着写入的回合和消息，并且回复中出现暗号；resume 成功本身不算。
  - 不恢复 S2–S4 的 thread。
  - 见 [S5 方案](appserver-s5-probe.md)。
- **测试**：S5 47 passed，全量 781 passed；S5 协议事实离线核对 11/11。没有执行 Codex。
- **下一步**：用户单独批准一次真实 S5（2 个回合）后，才执行方案中的固定命令。

---

# 历史交接：S4 第一次真实运行 PASS（只认定本次主回合中断通过，2026-10-22）

- 在 `36766da` 上运行，程序版本 `codex-cli 0.163.0-alpha.11`，复用 `identity-hkzdmoqj`。
- 过程：唯一一次绑定的 `turn/interrupt`，由 5 秒计时器触发 → 应答 `{}` → 本回合 `turn/completed(interrupted)`。
- 没有工具活动；本地进程退出码 0；关闭前后都没有后代进程；工作目录为空，已删除。
- 中断发生在首个输出之前，所以输出进行中的中断、工具子进程的停止、重复中断和用量上报都没有验证。见 [S4 第一次实录](appserver-s4-2026-10-22-run1.md)，标签 `evidence/s4-real-run-1-pass`。
- 授权已用完，没有重试；S5 没有运行，需要另行申请。

---

# 历史交接：S4 中断探针离线准备完成，等待单次真实授权（2026-10-22）

- 分支 `feat/appserver-s4-probe`，基于 S3 的 `85bfae9`；目录 `E:\plug\agent-hub\.worktrees\appserver-s4-probe`。
- 新增内容：
  - `tools/probes/appserver_s4.py`；
  - `packages/adapters/agenthub_adapters/appserver_s4_evidence.py`；
  - `tools/demo/stub_appserver_s4.py`；
  - `tests/test_appserver_s4.py`（65 项）；
  - [S4 方案](appserver-s4-probe.md)。
- 判定只看本回合自己的 `turn/completed(interrupted)`。中断应答、模型文字和本地进程停止，都不能证明已停止。主回合终态、工具活动和本地清理（含后代进程）分开记录。
- 已与 interrupt-stop 的既有契约逐条对照；被测版本 alpha.11 的中断语义已按源码离线核对；S4 协议事实在现有捕获上为 13/13。
- 测试：S4 65 passed，全量 725 passed。没有执行 Codex。S1–S3 的证据保持不变，S5 不会自动执行。
- 下一步：用户单独批准一次真实 S4 之后，才执行方案中的固定命令。

---

# 历史交接：S3 第三次真实运行 PASS（只认定本次拒绝链通过，2026-10-22）

当前状态：

- **第三次 S3 为 PASS**：在 `11ecc4f` 上运行，程序版本 `0.163.0-alpha.11`。
  - 过程：审批请求 → 只发送一次 decline → resolved → 同一命令以 `declined` 完成 → 回复 `DECLINED` → 回合 `completed`，并收到用量。
  - 执行证据：没有；进程创建被拒绝。
  - **只认定这一次的拒绝链通过**；全局拦截、只读拒写、子 Agent 控制和聊天功能都没有验收。
  - 见 [第三次实录](appserver-s3-2026-10-22-run3.md)，标签 `evidence/s3-real-run-3-pass`。
- 前两次真实 S3 都是 FAIL，起因都是当时的判定规则，都没有执行证据，拒绝链也都没有被触发；它们的协议层终态仍未确认。
- 现行判定由修订一和修订二组成（`3b7bf96`）。两处审批字段的改动已经用户审计，保留。
- 第三次的授权已用完，没有重试。S4/S5 未运行，需要另行申请。
- **阻塞**：S3 固定的 Codex 文件（`896a0166…`）已在 2026-10-22 19:50 被自动更新删除。新文件是 `bin\6257d9e8f63c0f2b\codex.exe`（SHA-256 `00c0f1e4…d67c`，签名有效，版本未知）。继续之前需要：重新固定、重新执行步骤 A，以及视版本情况重新核对源码依据。见 [S3 方案](appserver-s3-probe.md) 的“当前状态”。
- 已按批准完成阶段 0 和阶段 A：
  - 阶段 0：`0cea338` 重新固定，S3 相关测试 171 passed。
  - 阶段 A：COMPATIBLE 21/21，模型回合 0 次，**版本为 `0.163.0-alpha.11`**。
  - 离线源码核对：修订二的规则仍然适用，不需要改代码。见 [阶段 A 记录](appserver-s3-identity-2026-10-22.md)。
  - 阶段 B 之后另行获得批准，即上面的第三次运行。
- 详见 [S3 方案](appserver-s3-probe.md) 的“当前状态”一节。下面是按时间排列的经过。

- 在 `1fee92e`（10-18 暂存工作的 wip 提交）之上复核：离线测试、13 个 stub 场景、拒绝路径全部符合文档。
- **直接阻塞**：S1/S2 固定的 Codex 文件已被 10-19 的自动更新删除。S3 单独重新固定为当前安装文件（`896a0166b774037a`，SHA-256 `f6dbf48c…`，341312816 字节，OpenAI 签名有效，被动核验）；S1/S2 身份保持不变。新增零模型身份检查脚本 `tools/probes/appserver_s3_identity.py`。详见 [S3 方案](appserver-s3-probe.md)。
- 已按用户的一次性授权执行：步骤 A 为 COMPATIBLE（`codex-cli 0.163.0-alpha.10.2`，21/21 项）；步骤 B 结果为 **FAIL**（越界）：模型在任何审批请求之前就开始文字回复，探针中止，没有发出任何响应，也没有执行证据，终态未确认。拒绝链没有被触发。详见 [S3 实录](appserver-s3-2026-10-22.md)。授权已用完，没有重试；S4/S5 未运行。
- 修订一（离线，`58e4a7a`，自第二次运行起生效）：文字不再中止、也不决定结论；`text_format`、`approval_chain`、`terminal` 三部分分别记录，并带原因代码。S1/S2 的规则没有改动。修订测试共 21 项，相关回归 385 passed。详见 [S3 方案](appserver-s3-probe.md) 的修订一节。
- 第二次 S3（用户批准 `58e4a7a`，复用步骤 A）：**FAIL** `command_scope_mismatch`。模型没有先说话，直接发起了一个命令 item，其 `command` 与固定命令不完全一致，而且出现在任何审批请求之前；探针中止，没有发出任何响应，也没有正面执行证据。该命令是否已在沙箱中开始运行无法确定（原文按设计未保存），终态未确认，拒绝链仍未被触发。详见 [第二次实录](appserver-s3-2026-10-22-run2.md)。授权已用完，没有重试；S4/S5 未运行。（事后调查推断，命令原文未保存：内层命令与固定命令逐字相同，`command` 是包装形式。这三个待决问题已由修订二处理。）
- 修订二，离线修复（2026-10-22 晚，`1f9f7b7` 调查、`3b7bf96` 代码；两处审批字段的改动经用户审计保留；自第三次运行起生效）：
  - 已核对 rollout 第 12、13、16 行：内层 `cmd` 与固定命令逐字相同，并请求了 `require_escalated`；外层 `exec` 在 0.1 秒后中止；回合在本地记为 `turn_aborted / interrupted`。
  - 已按同版本标签 `rust-v0.163.0-alpha.10.2` 查清包装规则：会话 shell 的 argv 经 shlex 拼接后成为 `command`。第二次越界属于探针误判（推断）。
  - 探针改为把 `command` 拆回 argv，逐元素严格比对外层 argv 与内层固定命令，不做子串匹配。同时修正了审批字段的两处误判：接受 `environmentId: "local"`；不再要求 `availableDecisions` 中含 decline。
  - S3 相关 162 passed，全量 660 passed；变异检查中，子串匹配会让 37 项测试失败。
  - 没有启动真实 S3，没有运行 S4/S5。下一次 S3 需要另行申请。
  - 详见 [第二次实录·补充调查](appserver-s3-2026-10-22-run2.md) 和 [S3 方案](appserver-s3-probe.md) 的“命令包装与审批字段核实”一节。

---

# 历史交接：S3 离线工具完成，等待单次真实授权（2026-10-18）

- 分支 `feat/appserver-s3-probe`，基于 `023d0ab`；目录 `E:\plug\agent-hub\.worktrees\appserver-s3-probe`。
- 解释器 `E:\plug\agent-hub\.venv\Scripts\python.exe`，worktree 内无 .venv。
- S1/S2 已完成，**不得重跑**。S2 一次性授权已使用；本轮仅完成 S3 编码、桩进程与离线测试，未启动真实 Codex 或读取凭据。
- 新工具 `tools/probes/appserver_s3.py`：新 thread、固定 read-only/on-request/user reviewer，回读有效设置后才允许一次固定主回合；唯一命令审批响应为匹配请求的 decline，所有 accept/会话策略修订均不可发送。
- 判定要求实际审批链、完整发送的 decline、resolved、匹配 declined item、拒绝后的最终 DECLINED/增量/用量/终态及空目录；自述/缺证据不可 PASS。详细固定提示词、命令、风险与清理见 [S3 方案](appserver-s3-probe.md)。
- 修复复核发现的三处缺口：未回读 effective reviewer/sandbox；最终回复可能早于拒绝；未区分初始命令与 writeStdin/子命令回调。非空 exitCode 可能是合成拒绝码，单独记录，不伪称命令已执行。
- 新增 64 测试，全套 **553 passed in 34.56s**。独立 stub CLI → S3 PASSED；13 场景含权限回读反例，另验证非空工作目录保留。用量均为合成 fixture。
- 下一步：取得一次真实 S3 的单独批准后，执行方案中的固定命令；失败即停，无重试。上轮 S2/笼统“继续”不扩大此前逐次批准的限制。
- 本工具独立于生产内核/数据库，无生产配额行；缺匹配终态时 unconfirmed、人工核对，不能宣称保留生产配额或已停远端。
- 严格真实准入仍关闭。旧 8080、共享 daemon、历史子 Agent、长期 home 均不处理；两项 DLL 记录保持 OPEN。未合并、未推送，无 remote。

---

# 历史交接：中断与原生子 Agent 观测（2026-10-22 续）

- 分支 `feat/interrupt-stop`，目录 `E:\plug\agent-hub\.worktrees\interrupt-stop`，基于 `feat/chat-frontend` 的 `0a25e84`。提交号以 `git log` 为准。
- 完成：
  - `POST /api/invocations/{id}/interrupt` 向活动回合发送 `turn/interrupt`，只有收到终态才结算为 `canceled`；
  - 整树停止对在途调用改走中断路径，不会重复结算；
  - 按本机 schema 观测原生子 Agent（`stream.native_agent_observed`）；
  - 页面加入“中断本轮”及三种状态、原生子 Agent 提示和用量显示。

  说明见 [会话后端契约](conversation-backend.md) 末节和 [会话页面](chat-frontend.md)。
- 验证：新测试 8 项，连续 3 次通过；浏览器在 demo 桩上完成“工具卡等待时中断 → 已取消、卡片作废 → 刷新后恢复”；全量测试结果见提交说明。没有真实模型回合。
- 续（同一分支，第二个提交）：
  - 中断生效点：先关闭工具审批，再发送 `turn/interrupt`；
  - 发送前认领，生效点之后不再发出任何决定，已发出的如实记录；
  - 修复一处在生效点之后仍自动 decline 的泄漏；
  - 按用户决定：子 Agent 没有自身的结束证据时，调用为 unknown 并保留名额，父回合的实际终态单独保存。

  新增 17 项竞态和子 Agent 测试。
- 收尾（第三个提交）：
  - 统一措辞：关闭后不再授予新的认领；关闭前已认领的可能仍会发出、无法撤回，不声称已经写出；
  - 没有卡片的回答（建卡失败等）也必须经过 `may_answer()` 关闭门禁，关闭后什么都不发；
  - 竞态测试增至 24 项。
- 未完成：真实 S3–S5 仍需逐项授权；监督模式、unknown 裁决和配额设置的页面入口；第二个真实 Agent。两项 DLL 问题仍 OPEN。

---

# 历史交接：会话页面 `/chat` 演示闭环（2026-10-22）

- 分支 `feat/chat-frontend`，目录 `E:\plug\agent-hub\.worktrees\chat-frontend`，基于 `feat/async-tool-approval` 的 `4ba8099`。提交号以 `git log` 为准。
- 完成：零构建页面 `/chat`，用 fetch 流式读取 SSE，接上会话调用、异步 run 和工具审批卡；新增 `demo` 离线桩场景；`serve.py --appserver-stub demo --data-dir ...`。说明和浏览器实测结果见 [会话页面](chat-frontend.md)。
- 后端修复：建卡前先处理已排队的通知并 flush StreamSink，保证卡片之前的输出在事件流里排在卡片前面。
- 验证：浏览器在离线桩上走完 发消息 → 批准 → 逐步输出 → 工具卡 → 拒绝（另有一次过期）→ 终态 → 刷新恢复 → 新一轮；全量测试结果见本轮提交说明。没有真实模型回合，没有动 8080。
- 未做：监督模式、unknown 裁决和配额设置的页面入口；重绘目前是整体重建；真实 S3–S5 仍需逐项授权；两项 DLL 问题仍 OPEN。

---

# 历史交接：异步执行与工具审批往返（2026-10-22）

- 分支 `feat/async-tool-approval`，目录 `E:\plug\agent-hub\.worktrees\async-tool-approval`，基于 `feat/chat-workbench` 的 `c38d510`。本轮由 Agent 在本会话中直接提交（MinGit 副本可正常写入），提交号以 `git log` 为准。
- 完成：`/run` 改为同步认领、后台执行（202，`?wait=true` 保留等待模式）；工具审批卡 `tool_approvals`，每个请求一张，绑定 request/thread/turn/item 并带 `card_hash`，只有用户批准的卡回 `accept`，拒绝、过期、void 都不构成授权。说明见 [会话后端契约](conversation-backend.md) 末节。
- 门禁：认领阶段的所有检查都在消费审批之前完成，被拒时返回 409，审批保持原状。真实 app-server 仍被准入拒绝。卡的 `details` 只通过卡接口返回，事件只带 ID 和状态。
- 验证（Windows `.venv`，离线）：新文件 22 passed，连续 4 次；全量 **618 passed in 51.76s**。没有真实模型回合，没有读取凭据，没有动 8080。
- 未完成：网页前端（用 fetch 读取 SSE，展示审批卡与停止状态）、中断（S4）、原生子 thread 观测、用量事件、保留期。真实 S3–S5 仍需逐项授权；两项 DLL 问题仍 OPEN。
- 下一步建议：前端演示闭环——发消息 → 批准 → 逐步输出 → 工具卡 → 拒绝 → 终态 → 刷新后恢复 → 新一轮待审批。

---

# 历史交接：app-server 增量输出与原生续接（2026-10-21 续二）

- 分支 `feat/chat-workbench`，目录 `E:\plug\agent-hub\.worktrees\chat-workbench`。git 使用 `C:\Users\Administrator\AppData\Local\Programs\MinGit`（已加入用户 PATH）；`E:\ds_agent` 下的原件带 Low 完整性标签，无法写入任何正常目录。本轮和上一轮的改动由用户在自己的终端运行 `E:\plug\git-integration-2026-10-21\integrate.ps1` 提交。规则见 [Git 工作流](git-workflow.md)，证据见 [证据清单](evidence-manifest.md)。
- 完成：可选传输 `AGENT_HUB_CODEX_TRANSPORT=app-server`（`AppServerCodexAdapter`）；增量输出进入同一会话事件流；`context_mode=native_resume`，thread id 绑定进待审批快照。说明见 [会话后端契约](conversation-backend.md) 末节。
- 用户决定：增量文本存入专用表 `stream_chunks`，事件只保存 ID、序号和状态；SSE 和 `/events` 在服务端鉴权后组装文本；内容与事件同事务。
- 变更文件：`agenthub/contracts.py`、`agenthub/chat.py`、`agenthub_kernel/{db,events,worker,conversations,adapters,config}.py`、`agenthub_rt/conversations.py`、`agenthub_adapters/appserver_adapter.py`（新）、`appserver_stub.py`；测试 `tests/http/test_conversation_stream.py`（新，28 项）；文档为本文件、`conversation-backend.md`、`adapter-capabilities.md`、`README.md`。git 流程相关（另一个提交）：`AGENTS.md` 规则 6、`docs/git-workflow.md`、`docs/evidence-manifest.md`。
- 门禁：新增检查都在消费审批之前。真实 app-server 在严格和监督模式下都被拒；准入只放行离线桩。审批请求只回 decline，取消返回 unsupported，unknown 保留名额。
- 验证（Windows `.venv`）：开工前基线 567 passed；新文件 28 passed，连续 4 次；全量 **595 passed in 44.52s**。没有真实模型回合，没有读取凭据，没有动 8080。
- 未验证：真实工具审批（S3）、中断（S4）、续接效果（S5）；每一项都需要单独授权，不要重跑 S1/S2。两项 DLL 问题仍 OPEN。
- 下一步：前端用 fetch 流式消费 SSE（`payload.text` 已由服务端组装）；`stream_chunks` 的保留期和清理；需要时再申请 S3–S5。

---

# 历史交接：会话事件、游标补读与 SSE（2026-10-21 续）

- 分支 `feat/chat-workbench`，目录 `E:\plug\agent-hub\.worktrees\chat-workbench`，基于 `e947f07`。**本轮改动尚未提交**（本次连接只能读写文件、不能在你的电脑上执行 git），请先复核测试再自行提交。
- 完成：`conversation_events` + `conversation_event_cursors` 增量表；ORM `after_flush` 单点捕获消息/审批/调用状态/根任务停止·暂停·重试·unknown 结算，与状态变更同事务；每会话连续 seq；`GET /api/conversations/{id}/events?after_seq=` 游标补读；`GET /api/conversations/{id}/events/stream` SSE（`Last-Event-ID` 续读、心跳、超时关闭、409 `cursor_ahead`）。说明见 [会话后端契约](conversation-backend.md) 末节。
- 变更文件：`apps/worker/agenthub_kernel/events.py`（新）、`db.py`、`apps/api/agenthub_rt/conversations.py`、`api.py`（文档串）、`tests/http/test_conversation_events.py`（新，17 项）、本文件与 `docs/conversation-backend.md`。
- 审批、配额、unknown、停止门禁一行未改；事件载荷不含聊天原文；没有自动批准/重试/启动。
- 验证在云端 Linux 副本：受影响 54 passed（3 次）；全量 567 passed（设置 `USERPROFILE` 后；不设时仅 1 项 Windows 专属用例因缺该变量失败）。**Windows `.venv` 未复跑**。
- 下一步：app-server 事件桥接为可选传输（增量 delta 进入同一事件流），再接原生续接；前端最后用 fetch 流式消费 SSE。S3/S4/S5 真实回合仍须逐项授权，不要重跑 S1/S2；两项 DLL OPEN。

---

# 历史交接：会话后端（2026-10-21）

- 当前分支 `feat/chat-workbench`，目录 `E:\plug\agent-hub\.worktrees\chat-workbench`，基于 `812b215`。虽然分支名含 workbench，本轮只改后端；用户明确要求前端最后做。
- 先读本文件、仓库 `AGENTS.md`、`E:\plug\AGENT_HUB_PLAN.md` 和 [会话后端契约](conversation-backend.md)。确认 `git status` 和 `git log -1` 后再继续，不要回主目录的旧分支开发。
- 完成：持久化会话/消息、幂等创建待审批调用、同会话共享累计配额、目标调用执行、终态实际回复存储、恢复补回、人工重试的原输入和历史关联。
- 核心文件：`agenthub/chat.py`（契约）、API 与 kernel 的 `conversations.py`、`db.py`、`service.py`、`worker.py`。验收集中在 `tests/http/test_conversations.py`。
- 最终验证：`E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest -o addopts='' -q` → **550 passed in 30.00s**，全离线。本轮新增 37 项会话后端测试。
- 恢复的是本地历史，**不代表 provider 原生 session resume**。没有自动批准、自动重试或历史上下文转发。unknown 保留名额；现有真实控制边界不变。
- 本轮不修改前端、不追加真实模型调用、不操作凭据、不重启用户 8080 服务、不合并或推送。`appserver-s3-probe` 原有暂存修改未纳入此分支，两项 DLL OPEN。
- 下一步继续后端：持久化有序事件及读取游标、断线补读/SSE，再将 app-server 事件桥接为可选传输并接入原生续接。S3/S4/S5 真实回合仍逐项另行授权；不要重跑 S1/S2。第二个真实 Agent 待后续。
- 换模型接手时先核对本地工作目录及提交；不要让两个 Agent 同时写本目录。先执行受影响测试，再根据失败决定是否查 GitHub 参考代码。

---

# 历史交接：用户监督边界与手动停止（2026-10-20）

- 分支 `feat/manual-control-boundaries`，worktree `E:\plug\agent-hub\.worktrees\manual-control-boundaries`，基于 S2 `023d0ab`。
- 按用户要求保留严格模式，新增逐调用显式同意的监督模式；默认关闭 Hub 子任务协作扩展；手动停止只作用于自有本地入口进程，未知远端状态保留配额。
- 实现、接口、试用、限制与测试入口见 [本轮说明](manual-control-boundaries.md)。本轮未追加真实模型调用，未操作凭据、旧 8080 服务或共享 daemon。
- 另一目录 `appserver-s3-probe` 的 S3 离线修改保持原样，没有纳入本分支；后续整合前检查其提交状态，避免覆盖。
- 下一步：会话/事件桥接与网页增量输出；真实 S3/S4/S5 仍须各自授权。两项 DLL 记录 OPEN。
- 验证：全套 512 passed；之后补充一个 Popen 替换测试，边界组 24 passed（新增后未重复全套）。浏览器 Mock 流程通过，8156 测试服务已停止。未合并、未推送，原 S3 worktree 的 10 个已暂存文件保持原样。

---

# 历史交接：S2 工具与一次真实验证完成（2026-10-17）

- 分支：`feat/appserver-s2-probe`；基于 `bdc6b40`，工具提交 `f65bca8`，本提交归档真实结果。
- 目录：`E:\plug\agent-hub\.worktrees\appserver-s2-probe`；解释器 `E:\plug\agent-hub\.venv\Scripts\python.exe`。
- S1/S2 已通过，**不要重跑**。S2 准备完成后用户另行批准一次，已执行并 PASS。工具未读取/复制/删除凭据内容；Codex 自身复用登录并可能写入专用 home 运行状态。
- 普通执行准入仍默认拒绝真实 Codex；本探针未接入内核或生产数据库。原目录、8080 服务及其他分支未动，未合并/推送。

## 本轮成果

独立 `tools/probes/appserver_s2.py` 在发送前固定握手、新 thread 和一个固定提示词 turn，真实模式必须同时确认一次主回合与控制风险。禁止额外 turn、恢复、fork、工具审批响应；部分写入也不能重试。证据归集器精确匹配 thread/turn/item，分别判断匹配终态与本地进程退出；保存独立 usage 通知，缺少证据不补造。仓库外空临时工作目录仅在确认可安全清理时非递归移除，长期 home 不删除。

复核修复：额外消息 item 的 delta 被忽略、turn/started 未检查状态、finally 中工作目录检查异常丢失审计。均有回归。

## 实际验证

- 新增 93 项测试，最终全量 **489 passed in 26.59s**。
- 八个真实 Python 子进程桩场景：happy PASS；failed/no-usage/foreign-turn/approval/tool/disconnect/timeout 均正确拒绝，所有本次自有桩进程退出。
- 独立 CLI `--mode stub` → S2 PASSED；证据 `var/appserver-s2/stub-gxqegnrt/result.json`，仅合成用量。
- 真实 S2 → **S2 PASSED**，恰好一次 turn/start；最终 OK、同一 item 增量拼接 OK、匹配终态、服务端用量；自有 PID 7692 退出码 0，仓库外空临时目录已清理。证据 `var/appserver-s2/real-5j0vung8/result.json`，完整身份/授权/ID/用量见 [真实记录](appserver-s2-2026-10-17.md)。

## 下一步

用户在 S2 工具离线准备完成后明确回复“批准这一次 S2，接受上述限制”。此次一次性授权已用完。**下一步准备 S3 工具审批探针及离线正反对照，再申请一次真实 S3；不得重跑 S2或直接执行 S3。** [S2 方案](appserver-s2-probe.md) 中的真实命令只供审计，不是持续授权。

此探针不能保证工具或原生派生事前受控；历史 `--disable multi_agent` 绕过仍是实证。确认 S2 不等于解除 Hub 准入。没有匹配终态则标记 unconfirmed，保留证据、人工核对；此次终态已确认，无需该项裁决。它不占用生产内核名额，不能称“已保留生产配额”。继续保留历史子 Agent 状态未知、两项 DLL 问题 OPEN。

S3 审批、S4 中断、S5 恢复以及 SSE/第二个真实 Agent 尚待完成；本次不预授权这些回合。

---

# 历史交接：app-server S1 真实验证通过（2026-10-17）

## 分支与目录

- `fix/appserver-s1-verification`，基于 `fix/strict-adapter-gate` 的 `b6e6c88`。
- worktree：`E:\plug\agent-hub\.worktrees\appserver-s1-verification`。
- Python：`E:\plug\agent-hub\.venv\Scripts\python.exe`。
- 本次提交即代码/文档交付。原目录和其它分支未切换；未合并、未推送、未重启 8080。

## 已完成与真实边界

1. S1 发送前白名单、精确 home 预检、独占随机写探针、脱敏结果、reader join 与可信进程清理；修复 QueueStream 分片忙等及 stdio 短写。
2. 内存桩与真正 Python 子进程管道均已验证，`--mode process-stub` → `S1 PASSED`，自有进程退出。
3. 旧 Codex 固定文件不存在，首次仅预检失败（启动 0）；用户另行明确授权后，核验现装文件并建立 **S1 专用身份**，只启动 **一次** app-server，真实 S1 PASS，**模型回合 0**。
4. thread `01a1469e-eb8b-7dd1-a645-ffa3c3134b27`；发送恰为 `initialize → initialized → thread/start`，本次进程 PID 13632 已退出，退出码 0。证据 `var/appserver-s1/s1-real-euxv5bb_/result.json`。
5. 长期 home 保留；工具未读取/复制/删除凭据内容，Codex 自身复用登录并可写运行状态。没有触碰历史 DB 或清理共享 daemon。

完整身份、权限审核、命令、输出与限制见 [S1 实录](appserver-s1-2026-10-17.md)。现装文件的版本字符串和旧版本相同，但哈希不同；**exec 旧身份与后端拒绝真实调用的规则完全保留**。

## 验证

- 新增 33 项（S1 18 + stdio/QueueStream 15），相关三文件 **72 passed in 1.80s**。
- 全量 **396 passed in 16.07s**。
- 首次全量为 395 passed / 1 failed，原因是新测试用 Windows 默认 GBK 读取 UTF-8 JSON；已显式指定 UTF-8 后全量通过。
- `tools/probes/appserver_s1.py --mode process-stub`：PASS；真实 S1：PASS（以上单次，没有重试握手）。

## 下一步

**S1 已完成，不要重复运行。** 接下来准备 S2 单回合探针及离线正反对照，再单独申请一次真实模型回合。用户此次授权仅到零模型 S1，不包含 S2。现有 S1 工具按设计拒绝 `turn/start`，不得去掉它的白名单以凑通 S2。

S2 验证真实回合事件与载荷；S3 工具审批；S4 中断和确切终态；S5 thread 恢复；S6 满足严格控制条件后再接内核。app-server 尚未接入 `build_adapter()`。原生子 Agent 控制未解决，S1 不能作为恢复真实执行准入的依据。

SSE、会话恢复、第二个真实 Agent 仍未完成；历史原生子 Agent 状态未知；两项 `0xc0000142` 记录继续 OPEN。保持长期 home、现有会话和审计证据。勿将本地 app-server 进程已退出外推为远端任务、共享 daemon 或历史子 Agent 已停止。

---

# 历史交接：严格执行准入修复（2026-10-15）

## 当前工作目录

- 分支：`fix/strict-adapter-gate`
- 路径：`E:\plug\agent-hub\.worktrees\strict-adapter-gate`
- 基于 app-server 交接提交 `f364aea4b966544282fcf16f7bd0621228e7b971`；本文件所在提交即本轮交付。
- 解释器：`E:\plug\agent-hub\.venv\Scripts\python.exe`。
- 原主目录、其它分支、长期凭据、真实数据库和现有服务均未更改；未合并、未推送。

## 本轮完成

1. 按计划 §4.3 增加 `ExecutionAdmission` 契约：未知或未通过控制要求的适配器默认拒绝。Worker 在审批消费/配额预留之前检查；Codex 直接 `start()` 再检查。当前真实 Codex 不可启动；Mock 与固定离线桩可以继续验证。
2. API 返回明确准入原因；UI 不再把“传入禁用参数”显示成“子 Agent 已禁用”。历史真实调用工具也在接触凭据/启动进程前拒绝，避免侧门。
3. 复核原始 rollout 第 12 行，纠正漏报：子 thread 为 `01a13b43-e437-7c82-8f45-8deeeaa40082`，路径 `/root/standby`。现状仍未知，未关闭、未删除记录，也未将历史父调用改写为 unknown。
4. 按用户要求检索 GitHub 的 Codex 相关报告与 Vibe Kanban 执行器；只阅读参考，未引入第三方代码。来源和边界在 [实现说明](p3-strict-adapter-admission.md)。

## 验证

- 全量离线测试：`python -m pytest -o addopts='' -q` → **363 passed in 15.57s**。
- 最后整理后的受影响范围：准入、历史工具拒绝及停止测试 → **31 passed in 1.51s**。
- 新增准入/工具回归覆盖共 20 项；首次全量发现旧 NoCancel 测试桩未声明准入，已让该惰性测试桩显式声明 simulation，继续验证原 tree-stop 拒绝路径。
- `codex_verify.py --mode stub --codex-home <本轮独立临时目录> --port 8151` → **12/12 PASS，VERIFICATION PASSED**。只运行本仓库 Python 桩，无真实模型/用量。临时 HTTP 服务已由验证脚本关闭。
- app-server 代码未改；本轮没有启动真实 app-server 或真实 Codex，也未读取/复制凭据内容。

## 启动本轮版本（CMD）

**旧 8080 服务不会自动获得本轮修改。** 新版本独立端口和独立数据库用于验收：

```bat
cd /d "E:\plug\agent-hub\.worktrees\strict-adapter-gate" && "E:\plug\agent-hub\.venv\Scripts\python.exe" tools\demo\serve.py --adapter mock --port 8150
```

打开 `http://127.0.0.1:8150/ui`，体验 Mock 审批/调度流程。若改为 `--adapter codex`，页面应显示“已阻止新调用”，直接 HTTP drain 也会拒绝真实启动。不要继续在旧 8080 服务发起真实调用。

## 下一步与未决

下一项是 app-server 的真实 S1（0 个模型回合），具体方案仍见下方历史交接；S2+ 每个真实模型回合需单独批准。**即使 app-server 握手成功，也不能恢复严格执行准入**；必须先验证原生派生可关闭或完整受控，工具权限/终态也不能用协议存在代替行为验证。

SSE、会话恢复、第二个真实 Agent 尚未实现；历史子 Agent 停止状态未知；Python/Codex 的 `0xc0000142` 两条记录均 OPEN。未合并前，后续开发从本分支继续，不要回到主目录的旧实现。

---

# 历史交接：app-server 原型（以下为 2026-10-14 记录）

日期：2026-10-14。用途：**交接给下一轮**。只记录事实与可执行步骤，不含未验证的结论。

---

## 1. 分支 / worktree / 提交

| 项 | 值 |
|---|---|
| 工作分支 | `feat/appserver-proto` |
| worktree | `E:\plug\agent-hub\.worktrees\appserver-proto` |
| 当前提交 | **`5ecc330eb69a42d80960fd0ab6c3e0d06fcc8762`**（`fix(appserver): mandatory two-part handshake, JSONL stdio, no session-scoped consent`） |
| 上一提交 | `31f5b06`（可行性与最小原型） |
| 工作树 | **干净**（无未提交改动） |
| remote | **无**；**未合并、未推送** |
| 其它分支 | `feat/p3-wrapup` @ `42a61bb`（P3 口径修正）、`feat/p3-codex-probe` @ `8b24d0a`、`main` @ `1d4a303` 均未改动 |
| 解释器 | `E:\plug\agent-hub\.venv\Scripts\python.exe`（**worktree 内没有 `.venv`，必须用绝对路径**） |

## 2. 已完成内容（本轮范围）

### 2.1 两处指定修复（已在 `5ecc330` 中，无需重做）

| 修复 | 实现位置 | 行为 |
|---|---|---|
| **`initialize` → `initialized` 两步握手强制** | `appserver.py:58`（`HandshakeRequired`）、`:626`（`INITIALIZED = "initialized"`）、`AppServerSession.initialize/_require_handshake` | 握手完成前 `thread/start`、`thread/resume`、`turn/start`、`turn/interrupt` **本地拒绝**且**请求不上线**（测试断言服务端 `received == []`）；`AppServerSession.initialize()` 先发请求、再发 `initialized` 通知，之后才置 `handshake_complete=True`。依据：本机 schema `ClientNotification.json` 的 `oneOf` **只有一个**变体 `initialized` |
| **禁止 `acceptForSession`** | `appserver.py:665` `SUPPORTED_DECISIONS = frozenset({"accept", "decline", "cancel"})`，`ApprovalPolicy.__post_init__/validate/decide`，`answer_approval` | 三个入口全部拒绝：①直接应答 ②策略构造/事后篡改（`rules` 被改也会在 `decide()` 处拒）③自动应答 `auto_answer_pending()`；每个入口都断言"未发出任何应答"。理由：它放宽的是**后续**命令的提示 |

桩服务同样强制握手：未收到 `initialized` 前的线程/回合请求回 **`-32002`** 并记录违规（`appserver_stub.py`，字段 `initialize_seen`/`initialized_seen`/`handshake_violations`/`handshake_ok()`）。

### 2.2 本轮其余已完成项

* **stdio 明确为官方 JSONL**：`AppServerClient` 与 `StdioTransport` 的 framing **默认值均为 JSONL**；LSP 头与嗅探（`FRAMING_AUTO`）降级为**显式诊断**，不会自动选中。修正了文档中"framing 未说明""initialize 可选"两处错误表述。
* **S1 验证脚本**：`tools/probes/appserver_s1.py`（`--list` 可先审阅），含真实模式与 stub 模式。
* **可行性文档**：`docs/appserver-feasibility.md`（三层证据分类表、内核衔接设计、原型说明、S1–S6 验收计划、引用来源与取证限制）。
* **原型**：`agenthub_adapters/appserver.py`（客户端/会话/判定）、`agenthub_adapters/appserver_stub.py`（12 场景桩）、`tools/demo/stub_appserver.py`（stdio 桩）。
* **`codex exec` 适配器一行未改**，仍是默认；app-server **未**接入 `build_adapter()`。

## 3. 实际测试结果

| 命令 | 结果 |
|---|---|
| `python -m pytest tests/adapters/test_appserver_client.py`（本轮唯一一次运行，按要求只跑受影响范围） | **39 passed in 1.34s** |
| 上一轮完整套件（`5ecc330` 提交前最后一次全量运行） | **343 passed** |
| `tools/probes/appserver_s1.py --mode stub`（离线正反对照，已实跑，exit 0） | 正面：握手 + 1 个空 thread 全 PASS；反面①握手前请求→本地 `HandshakeRequired` 且 `received=[]`；反面②只发 `initialize`→服务端 `-32002` |

命令（worktree 内）：
```bat
cd /d E:\plug\agent-hub\.worktrees\appserver-proto
E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest tests\adapters\test_appserver_client.py -q
```

## 4. 尚未修完 / 未验证的问题

**代码层面（已知、未修）**

1. **真实 app-server 从未运行**：framing 是否真为 JSONL、握手是否被服务器强制、`thread/start` 的真实载荷，全部**未验证**。
2. **`StdioTransport` 未经离线执行**：本沙箱拒绝原生进程的**管道** stdio，因此真实传输路径只在代码层写就，未被跑过；stderr 已落文件句柄（可用），但 stdin/stdout 管道需要在允许管道的终端里验证。
3. **内核未接入**：`build_adapter()` 仍只返回 Mock/Codex(exec)；app-server 的 `AppServerSession` 尚未与 `PolicyService.consume()` / 配额门禁串联（设计已写在 `docs/appserver-feasibility.md` §4，实现未做）。
4. **`turn/steer`、`review/start`、`thread/fork`、`thread/list`、`thread/read`** 未在客户端提供。
5. **审批等待语义**未测：服务端在客户端不回复时的行为（文档未承诺）、`availableDecisions` 是否总给出。
6. **中断的真实效果**未测：`turn/interrupt` 只被文档描述为"请求"，适配器 `cancel` 仍返回 `unsupported`（正确现状）。

**能力结论（保持未验证，不得改口径）**

* 只读拒写：**未验证**；那次真实探测结果为 **INCONCLUSIVE**（探针未取得实际尝试及拒绝证据）。
* 取消、工具启动前拦截、原生子 Agent 阻断：**未验证**。
* `0xc0000142` ×2：`docs/issue-python-0xc0000142.md`、`docs/issue-codex-0xc0000142.md` **均保持 OPEN**。

**环境限制（如实记录）**：`developers.openai.com` 与 `github.com` 在本机 DNS 失败；官方依据取自 `raw.githubusercontent.com` 的官方仓库文件；`mintlify.wiki` 镜像**不作官方依据**（其 exec 形状与官方源码冲突）。

## 5. S1 准备进度与下一步准确操作

**进度**：脚本与离线正反对照**已完成**；真实模式**尚未执行**（本轮明确不执行）。S1 不产生任何模型回合。

**下一步（由你手工执行，一行 CMD）**：

```bat
cd /d E:\plug\agent-hub\.worktrees\appserver-proto && E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\appserver_s1.py --mode real --i-understand-this-starts-an-app-server-process --codex-home "%LOCALAPPDATA%\AgentHub\codex-home"
```

* 若想顺带删除本次运行目录，末尾加 ` --clean`（默认保留证据）。
* **脚本会做**：校验可执行文件路径 + sha256 + size → 校验 `CODEX_HOME` 是目录、有 `auth.json`、本进程可写 → 启动 **1 个** `codex.exe app-server --stdio` → `initialize` + `initialized` → `thread/start` **最多一次**（cwd 为本次专用空目录）→ 线级断言禁止项 → 终止自己启动的进程。
* **脚本不会做**：任何 `turn/start`（**0 次模型回合、不消耗配额**）、`thread/fork`、`command/exec`、审批请求、第二个 thread；不重试；不放宽。
* **证据位置**：`var/appserver-s1/<run>/appserver-stderr.log`、`var/appserver-s1/<run>/wire-transcript.json`。
* **清理边界**：只终止本脚本启动的进程；`--clean` 仅删本次运行目录，且拒绝删除 `var/appserver-s1/` 之外或位于 `%LOCALAPPDATA%`/`%USERPROFILE%` 内的路径。**长期 `CODEX_HOME` 与用户 `~/.codex` 一律保留。**

**S1 之后（逐次批准，勿跳步）**：S2 一次最小回合（验证真实事件顺序与载荷）→ S3 审批往返（真实审批请求上 `decline`，不批准执行命令）→ S4 中断（测真实终态与子进程是否真停）→ S5 `thread/resume` + `turn/start` → S6 通过后才考虑把 app-server 接入 `build_adapter()` 作为可选传输。详细判定与批准点见 `docs/appserver-feasibility.md` §7。

## 6. 交接注意事项（硬性）

1. **不要**在真实模式跑 S1 之外的东西；S2+ 每次都要单独批准。
2. **不要**把 `acceptForSession` / 结构化决策加成"支持"——它们被刻意排除。
3. **不要**把"文档支持 + 本机接口存在"写成"行为已验证"；三层证据必须分开。
4. **不要**删除长期 `CODEX_HOME` 或 `~/.codex`；不要在未确认停止的情况下释放配额。
5. 提交前至少运行受影响范围的测试；未经批准不合并、不推送。
