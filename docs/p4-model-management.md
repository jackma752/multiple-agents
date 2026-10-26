# P4 补充：模型管理与分模型用量

日期：2026-10-26。状态：功能代码与离线页面验证已完成，首次真实模型目录/选择/预算验收待用户进行；本轮真实模型调用为 0。实现位于 `feat/p4-model-management`，main 未合并。

用户完成页面体验后提出：用量应以单模型为中心，必须看见 Agent 使用的模型，并能切换模型。后续明确：**每模型独立 Token 预算也要做；右侧“用量与预算”按 Agent → 模型展示，同时保留该 Agent 的总花费，例如 Codex 下分别列出 6.0 Astra、6.1 Sol 的用量，再显示 Codex 合计。跨 Agent 的全局总 Token 才是可选项。** 示例是用户给出的显示层次，不是已经核验的账号可用模型列表。这两项纳入 P4 收口，先于 P5。安装包、换机配置与一键安装接入机制留在 P5 的交付配套；新 Agent 接入及其安装配方留在 P6，本次不启动这些工作。

## 1. 已核对的实现基础

核对 main `7058f51`，不操作正在进行的其他测试任务。

| 位置 | 已有基础 | 缺口 |
|---|---|---|
| `packages/contracts/agenthub/usage.py`、`apps/worker/agenthub_kernel/usage_ledger.py` | 每次调用有可空的 `model` 字段、输入/输出/缓存、完整性；一次调用只记一次账 | 汇总和页面主要按 Agent；没有模型维度视图与多模型明细账 |
| `packages/adapters/agenthub_adapters/appserver_adapter.py` | 保存 `thread/start` 或 `thread/resume` 回应的 model，并传给用量报告 | 没有模型列表或选择入口；没有向 start/turn 发送用户选择；线程配置回读不等于逐次推理的实际模型证明 |
| `packages/adapters/agenthub_adapters/claude_code.py` | 保存 CLI 的 `modelUsage` 数字与模型名；多个模型时仍保留 partial | 尚未将多模型用量独立归账；不能把整次用量都算给其中一个模型 |
| `InvocationSnapshot`、阶段规则与页面 | 有不可变快照、授权绑定、Agent 选择及用量区 | 缺少贯穿手动调用、子任务、审阅和汇总的模型选择契约 |

这些是实现基础，不代表查看/切换模型或分模型统计已经完成。旧记录没有可信模型字段时显示“模型未知”，不按 Agent 名、用户后来选择或回复中的自述补填。

## 2. 右侧用量与预算：Agent → 模型 → Agent 合计

1. 主入口固定在右侧已有的“用量与预算”。按 Agent 分组，每组列出各模型，再展示该 Agent 合计；左侧会话列表不新增另一套模型表或预算表单。模型行显示名称、接入方、输入、输出、缓存读写、调用次数、预算/剩余及未知/不完整标记，不再把 `codex` / `claude-code` 当成模型名称。
2. 保留本次调用、当前会话、根任务、全部会话的统计范围切换，并显示当前范围。Agent 合计包含同一范围内该 Agent 的所有模型及未归属调用，调用按 invocation 去重。切换模型或压缩续聊不会清掉旧模型的消耗。
3. “显示全部 Agent 总计”是可选项，默认关闭，记住用户的显示偏好；**Agent 内各模型的合计默认保留**。关闭全局总计只影响显示，不删除账本，也不关闭用户已经设置的预算门禁。若总预算导致拦截，调用卡仍必须说明拦截原因和预算来源。
4. 明细分组键区分 Agent、接入方和模型完整 ID；同名模型来自不同提供方不能无条件合并。同一模型经不同 Agent 调用时分别列在对应 Agent 下，Agent 的合计不重复包含另一 Agent 的记录。
5. 同一次调用存在多个模型时，使用有明确语义的逐模型字段分列；整次总数与逐模型明细不能再相加。缺少可验证分摊信息的部分留在“模型未归属/用量不完整”，不平均分摊，不计为零，不全部记给主模型。
6. 计算保持既有口径：输入已含缓存，推理 Token 属于输出子项时不再另加；重放事件和重启恢复不重复入账。
7. “花费”首版以实际回报的 Token 为核心。金额仅在存在可靠来源时另行注明是接入方报告或估算；不把 Token 数或 API 标价解释为订阅账号的实际扣款/剩余额度。

### 已确认：每模型独立 Token 预算

- 每个 Agent 下的模型行可独立设置总 Token 预算、单次预留、提醒阈值和严格/软模式，沿用现有输入（含缓存）加输出的账本口径；不把单次预留宣称为运行中的硬截断上限。
- 首版预算作用范围沿用既有根任务树，模型键再细分到 Agent/接入方/模型。页面写清“统计范围”和“预算范围”；切换统计时间或视图不会重置预算。压缩续聊、子任务、审阅、重试及汇总继续计入同一根任务和对应 Agent/模型。
- 一项模型预算耗尽，只阻止使用该模型的新调用；其他有预算的模型仍可按原授权运行。根任务总预算已设置时两层都要满足；Agent 合计是默认统计项，本次不额外推导成必须设置的 Agent 总预算。
- 执行前依据审批绑定的模型检查并原子预留；模型预算与已配置的根任务预算在同一事务内成功或回滚。并发请求不能抢用同一余额；拒绝不得启动进程或消耗审批。默认模型尚未明确时，严格预算不能把它当作“没有预算的未知模型”而绕过检查。
- 结算按接入方实际可归属的模型用量记账；完整、部分和未知语义保留。模型发生变化或一次使用多模型时，保留请求值与实际归属差异，调整预留和结算，不在请求模型与实际模型各记一份费用。不能归属的消耗保持未结算/未知，不能凭空给其他模型填零。
- 改模型或预算后沿用调用快照、策略版本及阶段授权的变更流程；自动执行不能自行扩大模型预算，切模型也不能绕过总预算。

### 展示示意

```text
用量与预算                        统计范围：当前根任务
Codex
  6.0 Astra    输入… / 输出…    预算… / 剩余…  [设置]
  6.1 Sol      输入… / 输出…    预算… / 剩余…  [设置]
  模型未归属   …（存在时显示，不能算作 0）
  Codex 合计   已知输入… / 输出…，另有…次不完整

Claude Code
  各模型明细与独立预算…
  Claude Code 合计…

[ ] 显示全部 Agent 总计
```

模型名示例不意味着已核实可用；展示候选仍以对应适配器和实际账号环境的来源为准。已归属明细与 Agent 合计保持可核对，不把 Agent 合计再作为一笔用量入账。

## 3. 查看与切换模型

### 页面

- 输入区排列为“Agent → 模型 →（支持时）推理强度 → 执行模式”。切换 Agent 后刷新对应候选，不把上一家模型 ID 带给另一家。
- 提供“使用接入方默认”选项，同时展示已知的默认配置及其来源；无法读取时明确写“默认模型未确认”，不能只显示“默认”让用户误以为已知。
- 模型候选标明来源、获取时间和能力；候选目录不保证账号已获授权，目录不可用时显示原因。不能硬编码本助手的模型列表作为用户 CLI 的模型列表。
- 审批卡显示本次请求的模型/强度或“接入方默认”，结束后显示接入方配置回读和实际报告信息。请求值与报告值不同时，两者都保留并提示差异。
- 已结束的消息和用量保留当时模型；切换选择只影响后续新调用。运行中的调用不因下拉框变化而被修改。

### 来源与执行

- 分开保存 `requested_model`（用户请求）、配置回读的模型、实际报告的模型及各自来源；未知字段允许为空。模型回答“我是某模型”不是身份来源。
- 模型列表和切换能力由适配器声明，不能因为页面有下拉框就声称所有 Agent 都支持；已接入的 Codex app-server 与 Claude Code 优先实现，后续适配器沿用此契约。
- Codex：对照固定版本 schema 实现 `model/list`、`thread/start`/`thread/resume`/`turn/start` 参数和结果。只读取目录时不发模型回合；需要启动本地元数据进程的刷新操作在页面明确说明，不能伪装成纯本地缓存读取。缓存目录与适配器身份绑定，不自动运行真实验收。
- Claude Code：通过本次进程的受支持参数选择模型；候选来源、别名解析和能力需按固定版本核对。不能为了切换 Hub 下一次调用而修改用户 CLI 的全局设置。
- 不支持原线程切模型的接入方明确说明，并提供新建/摘要续聊路径；不能为了切模型悄悄丢弃历史。支持续接切换时，继续绑定原 thread 与新模型选择。
- 拒绝不可用模型时展示原因，不静默换到其他模型。接入方内部回退若可观测，记录实际报告；不可观测时保留未知，不能把请求值写成实际值。

### 审批与自动协作

- 模型及推理强度写入不可变调用快照及 hash。改动后创建新的待审批调用，旧批准不得用于新模型；不影响已有无模型字段的历史记录读取。
- 自动阶段明确允许的模型及默认继承规则；主 Agent、计划生成、各子任务、审阅、修复重试、汇总的模型选择都可追溯。模型不能由 Agent 输出自行扩大。
- 更新正在进行阶段的模型规则应通过阶段设置及既有授权变更流程；不得在后台绕过阶段授权。所有选择均保留原来的调用、次数和预算门禁。

## 4. 实施与验收

| 步骤 | 交付 | 针对性验收 |
|---|---|---|
| A：模型元数据与选择契约 | 请求值/配置回读/实际报告、目录能力、快照与适配器参数 | 选项确实到达 offline peer/CLI argv；不支持时明确返回；旧快照兼容；模型变更不能复用批准 |
| B：分模型明细、Agent 合计与模型预算 | 单/多模型归账、Agent/模型查询、根任务内每模型预算与原子预留 | 同一 Codex 下两个模型分别记账且合计可核对；多模型不重复；未归属保留；某模型达限仅拦该模型；根任务总限额仍有效；并发/拒绝不多扣；重放/恢复不重复 |
| C：右侧页面与协作接线 | Agent → 模型明细和预算 → Agent 合计、模型选择与来源、全局总计开关 | 原“用量与预算”区显示层次正确；每模型预算可改；切 Agent 刷新候选；旧消息不变；全局开关不隐藏 Agent 合计或关闭门禁；子任务/汇总模型有来源 |
| D：阶段收口 | 用户离线操作验收与文档 | 覆盖“选模型 → 审批 → 返回用量 → 换模型 → 两行明细及 Agent 合计 → 单模型预算拦截 → 全局总计开关”；真实验收需用户另行指定，不自动追加 |

只跑相应契约、适配器、账本、审批、自动阶段和页面的受影响测试；全量留到阶段收口，不主动开压力测试或反复真实试跑。

## 5. 文档依据和验证边界

2026-10-26 查阅：

- [OpenAI Docs：App Server 的模型目录及回合参数](https://learn.chatgpt.com/docs/app-server#models)：目录含候选、默认及推理强度；线程/回合支持模型参数。可用内容随客户端、账号与版本变化，文档示例不是用户的实际模型列表。
- [Claude Code：模型设置](https://code.claude.com/docs/en/model-config#setting-your-model) 与 [CLI 参数](https://code.claude.com/docs/en/cli-reference)：官方提供启动时的模型参数。当前 Hub 固定版本尚未完成这些参数的运行验证。
- 本机已保存的 alpha.11.2 schema 中存在 `ModelListParams`、`ModelListResponse`、`ThreadStartParams.model`、`TurnStartParams.model/effort`。这是静态依据，不是本次真实调用成功的证据。

本次未执行任何 Agent、未读写登录凭据、未更换二进制、未更新价格表或测试模型额度；运行实现与真实可用性仍待以上步骤完成。

## 6. 本轮实现与操作说明

上文第 5 节末句记录的是设计阶段；本节更新实现状态。运行实现已经完成并通过以下离线验证，真实可用性没有因此升级为已验证。

### 页面

- 输入区新增模型 ID 输入/候选列表、推理强度、刷新模型和默认配置提示。模型选项按 Agent 保存；切 Agent 后使用各自选择。模型与显示偏好可保存在本地，登录 token 仍仅放页签 sessionStorage。
- 右侧“用量与预算”新增当前会话/根任务/全部会话近 7 天的范围选择。默认根任务；按 Agent 分组显示模型行和可展开的独立预算，每组末尾保留 Agent 合计。全局总计默认关闭，可选打开并记住偏好；原根任务预算收在“整个任务总预算（可选）”，显示开关不会停用它。
- 审批卡显示请求模型/强度；结束后列出接入方报告或线程配置回读的模型。两者不同会提示可能的别名解析/内部切换，不把请求值改写成实际值。
- 自动阶段可为每个 Agent 填写模型与强度，并在一次规则确认中显示；手动子任务与汇总也带对应 Agent 的选择，仍分别生成审批快照。

### 数据和执行接口

- `InvocationSnapshot.model`、`reasoning_effort` 与可选的 `model_provider` 绑定审批 hash；未使用时省略，保留旧快照/请求 hash。当前 provider 是接口通道键 `codex` / `claude-code`，没有新增同一 Agent 的多账号或多网关配置。
- `GET /api/agents/{agent}/models` 只读缓存/该用户历史，无进程启动。`POST /api/agents/{agent}/models/refresh` 是用户明确点击的元数据刷新：Codex 可启动固定的本地 app-server，发送 initialize、model/list 和 config/read，再关闭；不发 thread/start 或 turn/start，不产生模型回合。
- Codex 模型/强度传入 thread/start、thread/resume 和 turn/start；请求了明确模型但线程回读不一致时，在模型回合启动前拒绝。Claude Code 使用本次进程的 `--model` / `--effort`；exec 复用原 `-m` 路径，强度选择未开放。无对应声明的适配器在消耗批准前拒绝，不能静默忽略。
- Claude Code 没有接入账号模型目录 RPC，页面候选来自明确标注的 CLI 别名和历史记录，也能手填完整 ID。**设置模型预算后，须使用完整模型 ID；未解析的 opus/sonnet/haiku 不能绕过预算归属检查。** 模型目录和历史出现不代表账号当前可用。
- 新增 `model_usage` 拆分表和模型预算预留/准入表，由现有 create_all 新建，未修改旧表结构。单次调用原账本仍是 Agent 和全局合计来源；多模型组件只有与整次输入/输出对齐时才拆分，不能对齐留在未归属，不再重复加一笔调用总量。旧记录不按当前选择猜填模型。
- `Policy.model_budgets` 的键为紧凑 JSON `[agent, provider, model]`，值为 `{total, per_call, warn_percent, mode}`。沿用根任务作用范围，与根任务预算在同一个 SQLite writer 事务中检查、预留或回滚；模型计数在准入前从账本与预留记录对账，并通过条件更新检查余额。
- 未知消耗保留未结算预留，补报后按差额对账；恢复或重放不会重复计费。模型预算阻断不消耗审批、不占槽，其他模型在原授权和其他限额内可继续。单次仍是预留，不提供模型运行中的 Token 硬截断。

### 实际验证

受影响范围命令（没有运行全量或压力测试）：

```text
python -m pytest -o addopts='' -q tests/http/test_model_management.py tests/http/test_budget_admission.py tests/http/test_usage_ledger.py tests/http/test_usage_views.py tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py tests/http/test_conversation_stream.py tests/http/test_conversations.py tests/http/test_chat_page.py tests/http/test_appserver_s6.py tests/http/test_async_and_tool_approval.py tests/http/test_codex_tool_allow.py tests/http/test_collab.py tests/http/test_context_preview.py tests/http/test_stage_summary.py tests/http/test_policy_api.py tests/adapters/test_claude_code_adapter.py tests/adapters/test_codex_adapter.py tests/adapters/test_usage_reports.py tests/policy/test_snapshot_binding.py tests/policy/test_policy_versioning.py tests/policy/test_codex_gate.py tests/policy/test_claude_code_gate.py tests/test_usage_normalization.py tests/test_budget_rules.py tests/test_claude_code_min.py tests/test_context_preview.py tests/test_collab_plan.py --basetemp=E:\plug\.tmp\model-final-affected
619 passed in 97.73s
```

补上“不支持的适配器不能忽略模型”和“CLI 别名不能绕过模型预算”后，模型专项完整重跑：`tests/http/test_model_management.py --basetemp=E:\plug\.tmp\model-final-feature` → `20 passed in 5.18s`（与上述测试存在重叠，不相加作为总测试数）。

隔离 Edge 浏览器走查结果：`PASS: model discovery/selection, two models and agents, subtotals, per-model budget refusal, other model allowed, total preference and reload; no page errors`。截图和脚本在忽略的 `var/model-browser/`，8188 测试服务已正常退出，未操作用户的 8080 服务。

本轮另修正三个旧测试的预期：页面布局文案、旧请求 hash 测试排除新增的空模型字段、允许非敏感显示偏好使用 localStorage 而继续限制 token 为 sessionStorage。没有靠改预期取消审批或预算检查。

### 用户在 8080 离线体验

先关闭旧服务器，再从用户自己的 PowerShell 启动本工作树版本。以下命令明确使用两个离线桩，数据与原在线演示目录分开；没有真实模型：

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' 'E:\plug\agent-hub\.worktrees\p4-model-management\tools\demo\serve.py' --host 127.0.0.1 --port 8080 --adapter multi --appserver-stub usage-multi-request --claude-code-stub success-usage --data-dir 'E:\plug\agent-hub\var\p4-model-demo\data' --files-root 'E:\plug\agent-hub\docs'
```

打开 `http://127.0.0.1:8080/chat`，新建会话，选 Codex，点“刷新模型”，分别以 stub-model、stub-model-2 发送并手动启动一次；右侧应出现两行及 codex 合计。每行可展开模型预算；先建立待审批消息就能产生根任务，不必先花一次真实调用才能设置预算。Claude Code 桩可手填示例模型 ID，返回对应模拟用量。真实目录、模型参数和账户可用性由用户后续单独验收，不以这个演示替代。
