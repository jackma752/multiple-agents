# P6 论文研究：实现与验证记录

分支 `feat/p6-paper-pipeline`，工作树 `.worktrees/p6-impl`，基于规划提交 `172eb64`（`5efabbc`、`517c133`、`172eb64` 为规划；main `98d515b` 是三者的父提交）。授权来源：用户 2026-10-30 在对话中下达“开始 P6 论文自动化功能开发”指令。

规划与约束见 [总方案](p6-paper-pipeline.md)、[全文补充](p6-fulltext-supplementation.md)、[模板设计](p6-academic-prompts.md)、[上游调研](p6-upstream-research.md)。本文记录实际实现。**所有验证均为离线模拟**：模型回复来自 `tools/demo/paper_stub_model.py`，arXiv 与 PDF 来自 `tools/demo/paper_fixtures.py` 的合成语料；没有真实检索、下载或付费模型回合。

## 1. 如何接入内核（没有第二条执行路径）

| 能力 | 做法 |
| --- | --- |
| 授权 | 研究任务 = 一个 P4.6 阶段，`StageRules.plan == "paper"`。新增 `StageRules.paper`（任务 ID、请求哈希、资源规则及其哈希），进入 `rules_hash`；未用论文的阶段哈希不变。仍要求每个 Agent 先手动成功一次、监督模式明确同意、一棵任务树同时只有一个开放阶段 |
| 模型调用 | 每个模型步骤调用 `Autopilot._ensure`：普通会话回合 → 快照 → 阶段规则内批准（`StageGrantRow`）→ `Worker.submit` → `PolicyService.consume`。审批、策略版本、阶段状态、配额、token 预算、模型预算、未知状态、额度等待、修复次数全部沿用原路径，计入同一根任务 |
| 格式修复 | 回复不符合结构 → `Autopilot._retry`（`retry_limit=2`），修复调用同样计数计量；用尽后对单篇/单节记为失败，对规划/筛选等全局步骤阻塞阶段 |
| 工具步骤 | 检索、下载+解析、批次核对写为 `paper_steps`（kind=tool）。`run_tools` 先在短事务中复核：阶段仍可运行、规则哈希未变、任务绑定当前阶段，然后在锁外执行；每个 URL 跳转都按资源规则检查 |
| 调度 | 由现有 Autopilot 循环推进；等待补充时不创建调用、不占并发槽、不询问模型 |
| 恢复 | 步骤幂等键 = 输入版本/步骤/目标/输入哈希；已成功步骤不重跑。重新授权（新阶段）时，旧阶段已成功但未处理的回复直接复用；旧调用状态未知或仍在运行则阻塞，不重放 |

## 2. 新增公共契约 `agenthub.papers`

- `ResearchRequest`：主题、年份、目标篇数（1–30，默认 5）、语言（zh/en）、全文策略、来源（首版仅 arxiv）、每次检索候选数、章节数。
- `ResourceRules`（版本 1）：来源、是否下载 PDF、最大字节、是否允许上传、本地目录白名单（realpath）、URL 补充模式、可接收论文内容的 Agent。`resource_decision` 是纯函数；`check_url` 拒绝非 http(s)、带凭据、非 80/443 端口、loopback/内网/链路本地/保留地址及解析到这些地址的域名。
- 状态：任务 `running/waiting_input/paused/stopped/blocked/completed`；步骤 `pending/running/succeeded/failed/blocked/unknown`；补充请求 `awaiting_choice/awaiting_files/validating_batch/awaiting_more_choice/resolved`；质量 `pending/passed/failed/unavailable/needs_review`。
- `choice_transition`、`timeout_due`、`estimate_calls`、`reference_text`（只用真实元数据，缺项标记为未知）。

`StageRules` 的变更：新增可选字段 `paper` 与 `PLAN_PAPER`；通用阶段表单（`StageIn`）不接受 `paper` 计划。适配器能力表不受影响（没有新的适配器能力）。

## 3. 数据（`apps/worker/agenthub_kernel/papers/db.py`）

新增表：`paper_schema`（版本 1）、`paper_tasks`、`paper_steps`、`paper_records`、`paper_documents`、`paper_chunks`、`paper_evidence`、`paper_supplement_requests`、`paper_missing_items`、`paper_supplement_batches`、`paper_supplement_files`、`paper_sections`、`paper_artifacts`。全部为新增表，旧库启动时由 `create_all` 补建，不修改已有表。文件存放在数据目录 `papers/<任务ID>/`（documents、supplements、exports/v<n>），按 SHA-256 命名，读取时复核路径与哈希。

## 4. 流水线

`plan → search → screen → fetch → fulltext_gate → read → synthesize → outline → write ⇄ review → abstract → export`

- 检索：arXiv export API，检索词来自已校验的计划并做转义；去重依次按来源 ID、DOI、规范化题名 + 第一作者姓，绝不只按题名合并，合并原因保留。零结果、全部检索失败、筛选后无相关论文、无可核对证据时跳到导出“资料不足说明”，质量为 failed。
- 筛选：每批 20 篇，必须对每个候选给出决定；按 include、uncertain 顺序取目标篇数；无摘要只能 uncertain。
- 获取：只下载授权来源的 PDF；损坏、加密、扫描件（首版无 OCR）、非 PDF、超限、地址不在授权内都记录原因。
- 阅读：全文按关键词选片段（16k 字符预算，记录选中/总数），摘要级单独标记，无摘要无全文的只保留题录，不调用模型。每条证据必须带可在所给片段中逐字找到的原文，否则丢弃并记录；位置为 PDF 页序号（不是印刷页码）和字符范围。
- 综合/大纲/写作：只能引用已有 evidence_id；正文用 `[ev:ID]`，导出时按真实元数据编号；未知 ID 保留为“[引用缺失:ID]”并使质量失败；无证据章节写成缺口说明，不调用模型。
- 审阅：独立审阅 Agent（可选）。pass 才算通过；“pass 但列出重大问题”按 revise 处理；每节最多修订 1 次；审阅失败为 unavailable，修订后仍有问题为 needs_review。
- 导出：`review.md`、`references.json`、`evidence.json`、`research-report.json`。执行状态与质量状态分开记录；非 passed 时 Markdown 顶部标为草稿并写明原因。

模板：`research.plan/screen/extract/synthesize/outline/section/review/abstract.v1`，自行编写，每个声明输入、输出结构、证据规则与缺失处理；资料放在标注“untrusted”的数据块中。步骤记录模板 ID/版本/哈希、调用 ID；模型与用量来自调用快照与 usage ledger。

## 5. 全文缺失与补充

- 所有纳入论文的获取/解析都结束后才生成一份请求（每个输入版本唯一），按参考文献格式列出全部缺失论文、原因与摘要可读性，可整份下载。
- 方案一：`awaiting_choice`，截止时间 = 首次询问 + 30 分钟，持久化、不因刷新/重启重置。选择补充立即清除截止时间；未选择到期由调度器在写锁内一次性转为 `timeout_continue`。任何上传/引用入口先在同一事务中记录“选择补充”；页面在发送文件前调用 `/supplement/begin`。
- 方案二：直接 `awaiting_files`，无截止时间。
- 批次：上传（多文件）、已授权目录中的本地路径、HTTP(S) 地址；提交后统一核对：错论文（按文件开头内容的题名判断，不看文件名）、坏 PDF、扫描件、不可达地址、重复内容、模糊匹配都逐项显示，不算补齐。未补齐时转 `awaiting_more_choice`，每批只问一次，不设超时；选择是开启下一批，选择否保留已补资料并继续。
- 暂停、停止或存在未知调用时不应用超时；恢复后按原截止时间判断。超时后迟到的补充返回 409 `request_closed`，不会悄悄并入。

## 6. 接口与入口

`/api/papers/preflight`、`POST /api/papers`（创建即授权）、`GET /api/papers[/{id}]`、`/api/papers/{id}/stage/{pause|resume|stop}`、`/reauthorize`、`/supplement/{choice|begin|files|refs|submit}`、`/supplement/references.txt`、`/evidence`、`/chunks/{id}`、`/artifacts/{name}`。全部要求用户身份并校验任务归属；Agent/Worker 令牌 403。

## 7. 离线验证（实际命令与输出）

```text
$env:PYTHONPATH="$PWD\var\p6-deps"   # pypdf 5.1.0 wheel 装在被忽略的 var/，不改动共享 .venv
python -m pytest -o addopts='' -q --basetemp='E:\plug\.tmp\p6\pt' tests/papers
58 passed in 30.71s
python -m pytest -o addopts='' -q --basetemp=... tests/papers/test_paper_units.py tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py tests/http/test_collab.py tests/http/test_chat_page.py tests/http/test_conversations.py tests/http/test_usage_ledger.py tests/policy
339 passed in 76.11s
```

覆盖：固定资料端到端到导出；两种策略；完整缺失清单；多批部分补充；明确拒绝；方案二与已选补充的无限等待；可注入时钟的 30 分钟边界（29:59 不触发、30:00 触发一次）；选择与超时竞争；迟到补充拒绝；重复提交/重复点击/重复创建；重启后截止时间不变；暂停阻止超时、恢复后生效；停止后零新调用/零新下载；未授权、Agent 令牌、预算不足、阶段暂停后审批在门禁处被拒，适配器启动为零；未知调用阻塞且重新授权被拒；停止后重新授权复用已完成步骤；错论文、坏 PDF、扫描件、不可达地址、重复内容、目录外路径、loopback 地址、重定向到内网；无结果、无摘要、伪造原文、未知引用、格式修复、审阅修订、无审阅者。

## 8. 已知限制（未验证项不得写成支持）

- **未做真实验收**：真实 arXiv 检索、真实 PDF 下载解析、真实模型在这些模板上的输出质量都未验证；按仓库规则需另行取得授权。
- 片段选择是关键词打分，不是语义检索；长论文只读选中片段，报告中写明覆盖范围。
- 题名匹配基于文件开头 1500 字符；模糊匹配需用户在下一批指定目标论文，首版没有“确认匹配”按钮。
- 超时后迟到的资料只被拒绝并说明，首版没有“新版本重新纳入并重算下游”的操作。
- DNS 检查与连接之间存在重新解析的时间窗（DNS rebinding）未完全消除；arXiv 下载限定主机名。
- 单阶段最多 50 次调用；超出时按真实步骤数在预检中拒绝并给出继续条件，不拆分根任务。
