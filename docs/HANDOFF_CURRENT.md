# 当前交接：所有开发流程整合 main（2026-10-30）

- 用户明确要求把所有开发流程合并 main。本轮从 `d6beb98` 建立独立整合分支，核对七条原未合并分支，合入缺失的 Windows 写入预检修复、P3 证据口径纠偏和历史整理提案，并收录其余已等价整合/被替代分支的原提交历史。细节见 [完整整合记录](development-integration-2026-10-30.md)。
- 写入预检相关回归 **34 passed in 1.31s**；运行时代码只改 `codex_home_write_check`，保留 DeepSeek 配置及当前 P2–P6 实现。没有新增模型、网络或真实账号检查，没有重复长时间全量测试。
- 各原分支、工作树、证据与 README 备份 stash 保留。未跟踪的两个 pip 构建临时目录不算开发源码，未纳入 Git 或删除。
- 本轮不重打包、不修改当前运行的 Hub。已有 0.7.1 安装包清单仍指向 `5b80967`，不含新补入的预检函数修复；下一次构建应以最新 main 为基线。

---

# 之前交接：0.7.1 公式导出与符号排版（2026-10-30）

- 基于已整合的 0.7.0 开发线 `d765d19`；最终代码 `5b80967`，修复独立公式、正文符号、集合、整数索引、隐式乘法和数学应用被当成链接的问题。原始研究和证据不改，显示副本不进入后续审批或模型输入。
- 最终专项与指导流程 89 项通过；离线网页通过；用户报告第二版副本实际渲染 363 个数学片段，正文裸 TeX 残留/解析回退/脚本错误/网络请求均为 0。初次只检查已识别公式而漏掉裸 TeX 的问题已如实记在 [问题记录](issue-guidance-math-export.md)。
- `dist/p6-release-0.7.1` 的最终安装包和 ZIP 已重建并通过独立冻结程序检查，见 [发行与校验记录](p6-release-0.7.1.md)。没有启动安装器、重跑研究或真实模型，当前用户 Hub 不变。
- 用户本次另存阅读副本在被忽略的 `var/math-export-fix-20261030`，带 `v2` 的 HTML/Markdown 是最终副本；此前不带 v2 的初版仍原样保留。用户桌面的原 study.md 未覆盖。
- 用户已授权完成开发后整合项目，本分支可快进到本地 main；不推送，不移动历史证据或删除旧分支。多个运行版本必须使用不同数据目录，或先退出另一个版本；只换端口不能隔离数据。

---

# 之前交接：0.7.0 论文理解与研究指导（2026-10-30）

- 实现 `087acd8`，基于 `4784255`，包含 0.6.5 清单中的 `1c68d85`，不是从旧 main 开发。新工作树 `.worktrees/p6-research-guidance` / `feat/p6-research-guidance`。
- 新路径为核心方法讲解→数学单元和依赖→定向补读与局部审阅→研究候选→版本化论文项目，提供待审批的逐步追问和写作指导。快速综述和旧任务哈希/恢复保留，原研究不自动重做。
- 具体范围、实现与未验收项见 [实现说明](p6-guidance-implementation.md)。最终相关 84 项通过，离线浏览器检查通过；全量一次 1855 通过/2 失败，已按实际情况记录并定向复核，不称最终全量全绿。
- 0.7.0 安装包与 ZIP 位于 `dist/p6-release-0.7.0`，源码 `087acd875a80f542764050ae674566f2ebda33ef`。实际冻结程序在独立空数据与临时端口的检查通过，真实模型调用为 0；[发行记录与校验值](p6-release-0.7.0.md)。
- 用户已明确授权完成后整合 main；以本分支整合当前功能线，历史分支和证据保留，不把旧实现反向覆盖新代码。当前 8080、用户安装、资料与运行任务未改。
- 本地 main 已成功从 `98d515b` 快进到 `6186835`（实现与发行文档）；随后只登记本合并事实。主目录 README 的 Git 规范化内容与旧 HEAD 一致，仅工作副本格式差异，已逐字备份到忽略的 `var/pre-guidance-merge/README-before-0.7.0.md`，核对后规范化再快进。没有丢弃用户语义修改；没有推送、移动证据或删除旧分支。
- 首次科研质量验收仍需一篇实际核心论文的逐式人工检查。项目保存不调用模型，候选仍待查重与验证；不新增 OCR、自动实验、形式化证明或自动投稿。

---

# 之前交接：P6 论文研究首版（2026-10-30，离线验收）

- 分支 `feat/p6-paper-pipeline`，工作树 `E:\plug\agent-hub\.worktrees\p6-impl`，基于规划 `172eb64`（main `98d515b` 之上）。内核 `9b55e6d`（P6.1–P6.3），工作台/演示/打包 `6d803f4`。
- 2026-10-30 经用户同意，把 `feat/desktop-timeout`（0.5.2–0.5.5：工作区写入修复、执行时限、主题/Markdown/SVG/删除会话/0 为不限、KaTeX）合并进本分支（合并提交见 git log），因为 P6 原本基于 main，缺这些更新，论文页只有深色。`/papers` 改用共用 `display.css/js` 与主题选择；论文调用上限默认按预检估算，只有用户填 0 才不限。未合并 main、未推送；主目录 README 未提交改动未触碰。
- 实现与验证细节：[docs/p6-implementation.md](p6-implementation.md)。研究任务 = 一个 `plan="paper"` 的 P4.6 阶段；模型步骤全部经 `_ensure` → consume 门禁；检索/下载/解析/批次核对按版本化资源规则在执行时复检；全文补充两种策略、30 分钟可注入时钟、多批补充、竞争/重启/暂停/停止均有测试。
- 入口：`/papers`；离线演示 `tools/demo/serve.py --paper-demo`（模拟回复 + 合成 arXiv 语料，不联网、无模型）。
- 安装包 0.6.0（源码 `4db9cc0`）在 `dist\p6-release-0.6.0`；用户试用后指出论文表单不能像会话页那样选模型，0.6.1（源码 `1a05e66`）在 `dist\p6-release-0.6.1` 补上按角色选择模型与推理强度；0.6.2（`3236620`）补全 Claude Code 的 xhigh/max 强度与每角色“刷新模型”；0.6.3（`03ea5a2`）导出目录可在批准的资源规则内自选；0.6.4（`6a8a418`，当前推荐）修复 Hub 重启后残留“运行中”调用导致研究卡住，新增误停后“接续这项研究”与会话/研究归档，全量 1844 passed。用户真实的“不动点理论”研究已用 0.6.4 接续并执行完成；0.6.5（`1c68d85`，当前推荐）兼容省略 `ev-` 前缀的引用标记并新增“重新导出（不调用模型）”，在用户数据副本上重新导出后缺失引用 42→0、质量 needs_review（第 1–3 节审阅意见待人工处理）。校验值与冻结程序检查见 [发行记录](p6-release-0.6.0.md)。用户需退出当前 Hub 后覆盖安装；未运行安装向导。
- `tests/http/test_contract_api.py` 中按旧 600 秒默认写的 3 项已在本分支修复（`b047ade`），全量此前仅这 3 项失败。
- 测试环境：pypdf 5.1.0 装在被忽略的 `var/p6-deps`（`PYTHONPATH` 指向它），共享 `.venv` 未改；早先在工作树建 `.venv` 的 pip 安装卡住，留下被忽略/未跟踪的 `.venv`、`pip-build-*` 临时目录及几个 python 进程（均为本工作树自建，可直接删除/结束）。
- **未验证**：真实 arXiv 检索与 PDF 下载、真实模型在模板上的输出质量、新安装包中的论文入口。真实验收需按仓库规则另行授权一次小规模任务。
- 下一步：用户确认后做一次真实小主题验收（建议 3–5 篇、带审阅 Agent、设 Token 预算）；P6.5 多来源与语义检索；“超时后迟到资料重新纳入并重算”的显式流程。

---

# 之前交接：补齐数学公式渲染（2026-10-29，0.5.5）

- 源码 `aba81d37b01a6e4e418825ac140c0cd17edecb17`，沿用 `feat/desktop-timeout` 独立工作树，包含下面 0.5.4 的功能。
- 0.5.4 漏接 LaTeX，导致截图中的二次函数原样显示。新增随包 KaTeX 0.16.22、字体、MIT 许可证及本地静态路径，四种公式分隔符可渲染；复制、原文与审批内容不改写。
- 30 项 HTTP/桌面回归通过；离线 Edge 检查公式/矩阵/分数/积分、代码保持、复制、主题、Markdown/SVG、窄屏均通过。实际冻结程序资源验证通过，无外部请求，0 次真实模型调用。
- 新包 `E:\plug\agent-hub\dist\p5-release-0.5.5`，详见 [0.5.5 发行记录](p5-release-0.5.5.md)。退出当前 Hub 后覆盖安装，原会话打开即可渲染历史回复，无需重新调用。
- 没有覆盖现有安装、操作当前 8080、修改用户任务或项目文件；main 和主目录 README 未动，未合并/推送。

---

# 上次交接：线上改进整合与执行时限（2026-10-28，0.5.4）

- 超时源码 `b6d5aec`、五项需求整合源码 `e01e905`；分支 `feat/desktop-timeout`，worktree `.worktrees/desktop-timeout`。
- 来源 `E:\plug\线上测试改进需求.txt`：主题、读写说明、Markdown/SVG、默认不限时与累计调用数、删除会话均完成。
- 安装版 execution_timeout_seconds 默认 0，兼容旧明确 Codex 值；进入审批，Codex/Claude 与后台时限一致。网络/工具等待独立保留。
- Policy 累计调用/子任务总量与 StageRules.max_calls 默认 0 不限，旧存储策略不自动放宽；并发、层级、授权、预算和最多三次修复仍有效。
- 会话删除采用 additive 标记表，拒绝在途/未知/开放阶段，撤销未用审批，不删除工作区文件或用量审计。页面关闭旧事件流避免复现已删除聊天。
- 549 项定向回归通过；Headless Edge 显示/原文复制/SVG 清除/主题/确认删除/760px 布局检查通过。
- 0.5.4 新包在主目录 `dist/p5-release-0.5.4`；实际冻结程序的启动、认证、保存、渲染、删除和退出检查通过，0 次真实模型调用。见[发行记录与校验值](p5-release-0.5.4.md)。
- 未操作用户当前 8080 服务、实际安装或项目输出文件；需用户退出后同目录覆盖升级。旧未知调用先核实结束再人工登记，不能接回已终止的进程。
- 未合 main、未推送；main 仍 `98d515b`，主目录用户未提交 README 改动保持原样。

---

# 之前交接：Windows 可写工作区修复（2026-10-27，0.5.2）

- 修复源码 `77d785b`，分支 `fix/desktop-workspace`，worktree `.worktrees/desktop-workspace`。
- 原因：Windows 未选沙箱后端时 Codex 把 workspace-write 降成 readOnly。在线调用在模型回合前被拒绝。
- 现在按线程配置后端与写入范围，保留已有后端配置；审批、工作区和回读校验保持。
- 205 项定向回归通过；本机无账号沙箱对照确认目录内可写、目录外拒绝，模型调用 0 次。
- 0.5.2 新包在主目录 `dist/p5-release-0.5.2`，冻结启动/配对/空配置/退出检查通过。
- 尚待用户更新安装并做真实模型编辑验收。未动当前 8080 服务、用户数据或 main，未推送。
- 细节和校验值：[发行记录](p5-release-0.5.2.md)、[问题记录](issue-desktop-workspace-write.md)。
- 主目录 README 的未提交用户修改保持原样，不纳入本次提交。

---

# P6 全文缺失与批次补充规划（2026-10-29）

- 用户要求请求阶段选择两种全文缺失策略：方案一首次询问是否补充，30 分钟未作选择默认按现有资料继续，已选择补充则等待；方案二在阅读阶段的补充请求处持续等待，不设超时默认继续。
- 当前纳入阅读的全部论文完成检查后，一次性返回所有缺失全文的作者/题名等参考文献式题录；不逐篇弹出。补充入口支持上传、引用文件和文件地址，改为首版必须项。
- 每批提交后若仍不完整，统一列出剩余项并询问一次；选是再等下一批，选否保留已补材料、完成阅读后继续；两种方案均适用。已选补充和再次选择均不套用首次 30 分钟超时规则。
- 新增 [专项流程与待验收场景](p6-fulltext-supplementation.md)，同步总方案、上游调查的待定项、提示词约束与外层规划。当前仍是 P6.0，仅文档，无运行实现/真实模型请求。
- 本轮提交可按 `docs(p6): define fulltext supplementation policies and waiting flow` 定位；沿用 `docs/p6-paper-pipeline`，不合并主分支。

---

# P6 上游源码与提示词调研（2026-10-29，上一轮）

- 用户明确本阶段仅调研、功能规划和约束，做好后再讨论开发；不开始 P6.1 或真实模型任务。
- 公开 GitHub 主分支固定提交：Paper-Agent `af2fc63382c91b3e48d6fa42fb171a471c952df3`，学术提示词库 `4029d941c9eebb7723bb62d0838102ec476da72f`。来源文件和 blob SHA 见 `p6-research-sources.json`。
- 新增 [上游调查与整合方案](p6-upstream-research.md)、[阶段模板设计](p6-academic-prompts.md)，同步现有 P6 方案和外层总规划的 P6.0 开发前边界。
- 确认当前上游有效阅读分支关闭全文嵌入、写作向量工具为占位；审阅缺失/失败可出现 passed=True，不能照搬质量状态。前文关于 embedding 为完整默认流程必需的说法须按此修正。
- 两仓库元数据 license=null、完整目录树未发现许可文件；本轮未复制代码/提示词全文。没有执行上游项目或真实模型评测，所有效果仍未验证。
- 本轮提交可按 `docs(p6): research upstream workflow and academic prompt design` 定位；沿用 `docs/p6-paper-pipeline`，仅文档变动，未合并主分支。

---

# P6 论文自动化设计（2026-10-29，上一轮）

- 用户明确论文自动化是最初目标，要求借鉴本地 Paper-Agent 并作为 Hub 功能纳入 P6。本轮在独立分支 `docs/p6-paper-pipeline` 登记 [设计与验收](p6-paper-pipeline.md)，没有新增运行实现。
- 论文任务优先，其他 Agent 扩展保留。先完成约 5 篇论文的检索、阅读、证据、综述和导出；向量模型不作为首版前置。
- 所有模型步骤继续走原授权/预算门禁；确定性网络/下载步骤需先补资源权限契约，不能绕过阶段规则。下一实施项是 P6.1 契约、状态机与固定资料离线流程。
- 参考项目根目录本轮未发现 LICENSE，当前未复制其源码或资源。未发送真实模型请求、未修改现有服务/安装包；主目录用户 README 改动未触碰。
- 外层 `E:\plug\AGENT_HUB_PLAN.md` 已更新 P6 摘要及本设计路径，该外层文件不在本仓库提交范围。本轮提交可按 `docs(p6): plan paper research pipeline` 定位；未合并主分支。

---

# 之前交接：项目图标更新（2026-10-27，0.5.1）

- 图标功能提交 `259dd60`，用户指定的原图保存在 `assets/branding/source.png`；无裁剪、无重绘，底部文字保留。
- 应用、安装器、卸载器、开始菜单、网页 favicon/页首与 README 使用相同图案。多尺寸 ICO 7 帧、网页 PNG 已生成。
- 受影响范围 38 项测试通过；图标接口 200 且字节一致；应用/setup/实际 Uninstall.exe 的 7 帧资源哈希与 ICO 一致，临时安装/卸载成功。
- 新包在根目录 `dist/p5-release-0.5.1/`，校验值见 `docs/project-icon.md`；原 `dist/p5-release/` 0.5.0 包保留。用户同时编辑的主目录 README 删除内容已保留，最终包源码为 `01dcdb0`，之后只登记验证。
- 用户询问自定义目录：安装向导能改程序目录；数据目录支持 AGENT_HUB_DESKTOP_HOME 环境变量，尚无图形化迁移入口；工作区独立设置。README 已解释，不扩展该功能。
- 没有新增真实模型请求，没有修改 8080 现有服务或用户账号资料。新版仍未做代码签名。

---
# P5 Windows 安装交付（2026-10-27）

- 本轮入口 `feat/p5-distribution`；包含模型管理/预算、DeepSeek、协作恢复、工具审批与复制修复。
- 原主目录 8 项未提交内容先保存为 `archive/pre-p5-local-20261027` 的 `d8e8ffc`，再以 `91e9dee` 整合历史。已确认的修复保留；未验证的 HTTP400 自动重发只留在归档，不进入发行代码。旧分支与证据标签没有删除或移动。
- P5 功能提交 `212487a`，后续安装与认证修正/验证见 `02a039d`、`88e7e09` 及此提交。
- 安装版新机器没有默认启用 Agent：`/settings` 扫描常见安装位置或手动指定 exe，用户确认哈希，选择已有登录目录。DeepSeek 以 API 接入，不要求桌面客户端。默认直连；程序、用户数据和工作区分离。
- 工作区路径/权限进入审批，Codex 使用其 cwd 与只读/允许写入策略；当前 Claude 原生工具关闭，Claude/DeepSeek 可接收明确文件引用，不声称具备相同的本机编辑能力。
- 独立窗口入口不依赖目标机 Python。随机本机配对拒绝开发固定令牌；再次打开开始菜单复用现有实例并换一次性浏览器票据，不重复启动调度器。
- 发行产物在主目录 `dist/p5-release/`：Windows x64 setup、便携 ZIP、SHA256SUMS。包内带 README、MANIFEST、PACKAGE_AUDIT 和第三方许可证。不带 tests、桩、探针、dev_auth、凭据、运行记录或开发文档。
- README 已按实际功能/操作顺序重写；本地 `开发文档.md` 解释架构、契约、接入扩展、测试、构建和维护；`docs/p5-distribution.md` 记录实际验证和范围。
- 完整集 1730 passed / 1 failed（旧 hygiene 源文件位置断言）；该测试更新为环境认证的真实行为验证后相关 35 项通过，重开能力加入后相关 36 项通过。没有为了旧位置断言第三次全量。安装/卸载、空数据启动与窗口版页面检查通过；0 次真实模型调用。
- 尚未在另一台物理机/干净 VM 上验收、未做商业签名、未推送/公开发布。更广义 P5 的 PR/部署/备份恢复与 P6 扩展另行推进。

---

以下为原有历史记录，路径/主分支状态可能已经过时；当前实现以本文件上方和 Git 为准。
# 当前交接：main 已合并 P4.6 并统一 8080 入口（2026-10-26）

## 最新集成入口：模型管理 + 协作恢复 + 复制修复（`86338b8`）

- 分支 `fix/model-collaboration-integration`，工作树 `E:\plug\agent-hub\.worktrees\model-collaboration`。
  基于协作修复 `7be4526`，接入工具允许 `243af8f` 和模型管理 `8d61668`（原提交 `b8a8891` / `c8e7961`）。
  冲突已同时保留错误显示、阶段选择校验和旧草稿恢复。main仍为83eb1ad，根目录未提交改动原样保留。
- 用户提出模型选择消失，原因是此前给出的collaboration-resume入口缺模型分支；此入口包含两者。
  Codex/Claude Code/DeepSeek模型选择、阶段固定模型、按模型用量和预算同时可用。DeepSeek目录为
  文档/配置候选，无自动远程查询；本次模型/强度进入请求，实际报告模型单独记录。
- 阶段两秒轮询原来每次替换整个消息DOM，导致选区丢失。现在无内容变化不替换，选区存在时暂存
  消息区更新并在取消选区后应用；侧栏仍更新。新增复制回复、复制完整发送内容按钮。
- 受影响回归 **704 passed in 108.80s**；Headless Edge在真实两秒轮询与摘要编辑下验证原生选字、
  延迟更新恢复、两种剪贴板复制、模型/强度控件，无JS错误。测试服务18191已停止，无真实模型调用。
- 用户先保存未完成摘要，再Ctrl+C重启本工作树的8080 multi。保留原data目录；完整命令见
  `docs/model-collaboration-integration.md`。此命令Codex/DeepSeek真实、Claude Code离线桩。
- 历史和现有阶段不改写：底部模型选择仅影响新调用，进行中的阶段仍按原授权模型执行。

## 最新：协作阻塞后继续修复 `5a9202e`

- 分支 `fix/collaboration-resume`，工作树 `E:\plug\agent-hub\.worktrees\collaboration-resume`，
  基于 `a522183`，包含前面 DeepSeek 密钥、HTTP诊断、最终回复与旧消息恢复修复。
- 用户计划#4第2项在配额拒绝后成为draft+revoked，被误视为进行中。现在只有“未启动且无外部ID，
  所有审批均关闭”才允许新阶段另建调用；旧审批不复用，已完成的第1项不重跑。未知或在途仍阻止重复。
- 同时修复阶段表单撤选Agent后的主/审阅/汇总角色同步、提交前校验，以及hidden控件仍显示的问题。
- 本工作树multi含Codex/Claude Code/DeepSeek，启动横幅打印source，防止切回缺少修复的根目录入口。
- 相关回归 **180 passed in 35.01s**，包含旧草稿关闭后继续、跳过已完成项、交接和汇总及重复启动保护。
  Headless Edge验证了表单显示/请求一致、非法选择0次提交、blocked顶部状态隐藏；无JS错误。
- 18190测试服务已停止。没有碰用户8080、原数据、root策略额度或其他Agent未提交文件；未合入main/推送。
- 用户需Ctrl+C重启，使用 `docs/issue-collaboration-resume.md` 的本工作树multi启动命令，
  再以“用会话里最新的计划”授权新阶段继续。保持已调高的根任务调用数，勾Codex/DeepSeek、主/汇总Codex。

## 最新：DeepSeek 已有真实回复；最终消息投影修复 `15bc92e`

- 用户执行 `inv-630801980461`，`deepseek-flash` / finish_reason=stop，输入34、输出79，
  但旧代码缺 final_message 和 last_result，正式助手消息未入库。不是网络或鉴权再次失败。
- 提交 `15bc92e` 同时补上成功 payload 字段与 `last_result()`，旧 FINISHED+finish_reason 的
  `reply` 可由已有恢复流程补成助手消息一次，无需追加请求或改写适配器原记录。
- 完整回复可解锁依赖和汇总；截断/失败/缺少结束证据不提供最终回复；不声明原生续接。
- 相关回归 **96 passed in 11.57s**，含两项 DeepSeek 子任务依赖交接及汇总、旧消息幂等恢复。
- 真实成功元数据（不含密钥/提示词/回复原文）在 `var/deepseek-real-success-metadata.json`，
  大小/哈希见 `docs/evidence-manifest.md`。仅确认用户本次真实 HTTP 成功；投影修复待重启确认。
- 用户 Ctrl+C 重启同一分支/数据目录的8080服务，再刷新原会话，检查旧回复恢复即可；不用重发测试。
- 用户提供的根目录 final_message 改动原样保留，工作树实现另补完整读取路径；未合入 main/推送。

## 最新补充：DeepSeek 鉴权空 400 已定位（`27285e2`）

- 用户重启后新模型仍 400，读取新诊断后确认需继续查鉴权；此前默认模型修复并未解决该故障。
- 原凭据末尾含 **U+0016（Ctrl+V）**，即使只读 GET `/models`，直连及代理均空 400。
  现在读取时在内存去掉该末尾粘贴残留，内部控制字符仍在发 HTTP 前拒绝；凭据文件没有改写。
- 同一路径清理后的 metadata GET **HTTP 200**，返回 `deepseek-flash` / `deepseek-v4-pro`。
  这是只读鉴权和模型目录验证，没有生成模型回复，也没有输出密钥。
- 实现提交 `27285e2`，沿用本工作树/分支；专项 **47 passed in 0.94s**（覆盖此前45项）。
  用户仍需 Ctrl+C 重启当前8080服务，再人工批准一条简短消息验收生成。
- main 和根工作树的其他未提交改动没有修改；未合入 main/推送。

## 本工作树最新：DeepSeek HTTP 400 修复

- 实现提交 `7c95189`，分支 `fix/deepseek-http400`，目录
  `E:\plug\agent-hub\.worktrees\deepseek-http400`，基于 main `83eb1ad`，没有合入 main/推送。
- 三次历史失败都使用 `deepseek-chat`；官方已公告该名字 2026-07-24 退役。默认配置和空值
  回退改为当前 `deepseek-flash`。旧 400 响应为空，唯一根因不能由旧证据确认；真实成功待验证。
- 错误保留限长 HTTP 诊断和正文状态，关闭错误响应，移除回显密钥/输入；聊天页终态及刷新后
  显示判定说明。一个批准只发一次请求，不加入猜测性重试。
- 验证：受影响范围 **171 passed in 25.17s**；补用例后的 DeepSeek 专项 **45 passed in 0.91s**
  （两组重叠，不相加）。Headless Edge 在独立离线 18189 服务验证即时/刷新错误展示，无 JS 错误，
  截图 `var/browser400/failure.png`。自建测试服务已停止，用户 8080 未操作。
- 只读核对了三次适配器失败与快照身份；没有读取真实凭据，没有发送带真实密钥的请求。
  官方端点不带鉴权的 GET/空 POST 返回 401，只用于连通诊断。旧数据未变。
- main 原有未提交改动（stream_options 重试、multi 接入、登录/探测工具等）原样保留，未复制或提交。
- 用户从本工作树启动，设 `AGENT_HUB_DEEPSEEK_MODEL=deepseek-flash`，清除 DeepSeek stub 后用
  `--port 8080 --adapter deepseek --data-dir E:\plug\agent-hub\var\p46-user-demo\data`；完整命令与
  来源见 `docs/p5-deepseek-adapter.md` 顶部修复节。批准一次简短消息进行真实验证。

## 最新：DeepSeek 适配器落地（P5，2026-10-26 17:00）

- **形态**：一次调用 = 一条 OpenAI 兼容 `POST {base}/chat/completions` 流式请求。请求体只有
  `model / messages / stream / stream_options`，**没有 `tools`**；不启本地进程、无工具通道、无子
  Agent 通道 → 因此**严格模式准入**（`code=no_local_execution_channel`），残余风险写进 `reason`：
  提示词会发往 provider，且**无法中途取消**。
- **凭据**：`%LOCALAPPDATA%\AgentHub\deepseek\credentials.json`（`AGENT_HUB_DEEPSEEK_HOME` 只改目录）。
  仓库内路径直接拒；**不读**环境变量里的 key；页面只显示"文件是否存在/大小"，从不显示内容。
- **失败语义**：无 gate_token / 无凭据 / 401 / 402 → 终态拒绝、零花费；429 → 带 `provider_quota`
  让阶段层"等待额度恢复"；断流·超时 → `unknown` 且**保留槽位**；无 `finish_reason` → failed（绝不
  算成功）。
- **离线验证**：`tests/adapters/test_deepseek_adapter.py` 39 项全过（凭据四种拒绝、请求体无 tools、
  SSE 解析与 `[DONE]`、四种失败语义、用量映射、`cancel` 报 unsupported）。
- **未验证**（别当结论）：真实 `api.deepseek.com` 往返、真实 SSE 分片节奏、
  `prompt_cache_hit_tokens` 真实语义、429 `Retry-After` 真实格式、代理链路。清单在
  `docs/p5-deepseek-adapter.md` §7。
- **怎么开**：真调用 `tools/demo/serve.py --adapter deepseek`（需要你自己的 `sk-...`）；离线
  `--adapter deepseek --deepseek-stub ok|quota|unauthorized|truncated|empty`。
- **刻意不做**：不进 `multi`（`multi` 仍是 codex + claude-code，已验收的断言不动）；首版不含 tools
  与会话恢复。

## P4.6d 离线验收：四步全部通过（2026-10-26 19:56）

按 `docs/p46-offline-acceptance.md` 在 8080 上跑完，**全部是离线桩**（`--appserver-stub demo`
`--claude-code-stub autopilot`），不涉及真实模型调用。

| 步 | 内容 | 结果 |
|---|---|---|
| 1 | 手动发起并批准一次调用 | **PASS** — 已完成、严格模式、审批 consumed、回复 `Offline stage step completed.`、用量 输入 12 / 输出 3 |
| 2 | `DEMO_RETRY` 失败后自动修复 | **PASS** — 两次调用（"按阶段规则自动批准（main）"），修复计数 `main 1/3`，第二次含 `Repair attempt 1 of 3 …` |
| 3 | `DEMO_QUOTA` 等待→自动继续 | **PASS** — `10:44:26` 失败（`usage limit reached (offline fixture)`）→ `10:44:32` 自动重试**已完成**，提示词带 `Continue the interrupted stage step…`，阶段已完成，调用 2/12 |
| 4 | 依赖计划 + 汇总 | **PASS** — `plan → item:1 → item:2 → collect` 依次**已完成**，调用 4/12，用量 输入 48 / 输出 12，全程无逐张批准，汇总结论见消息 #53（交接 #49 / #51） |

- 第 3 步附带修掉一个真缺陷：暂停/策略升级会撤销未消费审批，使额度探测调用停在 `draft`，
  而租约没人收回 → 阶段永久"等待额度恢复"。修复见 `be31c25`
  （`quota_probe_stranded` 事件 + 释放租约），回归测试
  `tests/http/test_autopilot_recovery.py::test_probe_stranded_by_a_revoked_approval_does_not_pin_the_bucket`。
- 第 4 步在**新会话**里跑（旧树里前几轮的子 Agent 已占满 `max_subagents_total=3`），
  并先把「子 Agent 总量」抬到 10、「并发」抬到 3（策略 v2）。
- **这一步证明的是**：拆分/依赖顺序/逐次审批替代/阶段收尾与本地汇总这条链在离线桩上成立。
  **不证明**：任何真实 provider 的行为，也不证明"真实子 Agent 已被 Hub 配额约束"。

## 第一条真实在线回合（codex / app-server，2026-10-26 12:00）

- **结果**：`inv-371025d822df` 经 `codex` / `app-server` 通道完成，`turn/completed with status
  completed`，thread `01a1765d-493e-7bb2-be9a-127cb19749cb`，用量 输入 16,787（缓存读 12,416，
  命中 74%）/ 输出 77（含 reasoning 42），审批 consumed，该次 stderr 日志 **0 字节**。
  详见 `docs/p47-codex-real-run-2026-10-26.md`。
- **前提**：python 进程此前为 `Low` 完整性级别（读不到 MSIX 包目录、写不了 `CODEX_HOME` 状态库），
  用户调整权限后为 `Medium`，同一通道随即成功。只观测到这一组对照，不宣称是唯一原因。
- **口径**：取消/中断、工具调用拦截、原生子 Agent 受控、会话恢复、流式、适配器版本标识**都仍未验证**；
  严格模式准入仍是 `blocked / native_subagents_uncontrolled`（徽标显示 blocked 指严格模式，与本次
  "用户监督模式"成功不矛盾）；模型自称的 "GPT-6" 是自述，不是证据。
- **环境阻断（已各自留痕，全部 OPEN）**：
  `docs/issue-codex-exec-pin-removed.md`（exec 钉住的 `0.162.0-alpha.17.2` 目录被自动更新移除）、
  `docs/issue-appserver-codex-home-sqlite.md`（app-server 需可写 `CODEX_HOME`）、
  `docs/issue-python-low-integrity.md`（`python.exe` 曾被降为 `Low`，读不到 MSIX 目录）。
  这三条跟踪的是**根因**，不是"现在能不能跑"；不要因为一次成功调用把它们标记为已修复。

## 本轮（整合 + 合入 main + 统一 8080）

- **运行目录/日常入口**：`E:\plug\agent-hub`（根工作树，已切到 `main`）。不再需要进任何阶段工作树。
- **合并方式**：`main` `fd54556` → **fast-forward** `d1fe9f8`（= `feat/p46-autopilot`），25 files changed, +2609 −29。没有 merge commit，没有推送，没有删除任何分支/工作树/标签。
- **合并后修复（本提交）**：
  1. `tools/probes/appserver_s1.py` 的"CODEX_HOME 位于仓库之外"检查把工作区写成 `REPO/../..`，归一化后是**盘符根**（`E:\`），导致同盘任何路径都被判为"仓库内"、该检查永不可能通过 → 改为 `dirname(REPO)`。
  2. `tests/test_appserver_s1.py` 该用例改用 hermetic 的假 REPO，并**新增**一个负向回归（仓库内 / 紧邻仓库都必须被拒）。
  3. 根路径 `/` → `/chat`（307），避免直接敲地址得到 404。
- **测试（实际输出）**：
  ```text
  python -m pytest -o addopts='' -q --basetemp='E:\plug\.tmp\pt'
  1587 passed, 1 skipped in 305.19s
  ```
  修复前同命令：`6 failed, 1580 passed, 1 skipped`；其中 5 个是**临时目录落在仓库内**造成的假失败
  （`TEMP` 不可写时 `tempfile.gettempdir()` 回退到 cwd=仓库根，凡是"路径必须在仓库外"的用例全部假失败），
  1 个是上面的探针 bug。**在本沙箱跑测试必须带 `--basetemp` 指向仓库外，否则会出现这类假失败。**
- **8080 统一入口**：`http://127.0.0.1:8080/chat`（`/ui` 控制台、`/docs` API、事件流同源；`/` 跳 `/chat`）。
  启动命令与数据目录见下节；`.claude\launch.json` 已收敛为单一 `agent-hub-8080`，旧 15 个配置归档到 `.claude\launch.archive.json`。
- **仍未改变的口径**：P4.6d **用户人工验收尚未进行**；真实额度错误格式、真实自动阶段、真实工具自动允许仍**未验证**；未新增真实模型调用；未推送。

### 日常启动（PowerShell）

```powershell
Set-Location 'E:\plug\agent-hub'
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools/demo/serve.py --host 127.0.0.1 --port 8080 --adapter multi --appserver-stub demo --claude-code-stub autopilot --data-dir var/p46-user-demo/data --files-root docs
```

停止：`& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools/demo/stop.py --port 8080`（按 PID，脚本会校验归属）。

### 待办

- 三支仍有独有提交的分支待定：`feat/p3-wrapup` `42a61bb`、`fix/approved-only` `071c918`、`plan/branch-consolidation` `9026aa0`（只核对结论，不机械合并）。
- 真实通道的未验证项仍按各自文档挂着：取消/中断、工具拦截、原生子 Agent 受控、会话恢复，
  以及 DeepSeek 的真实往返（`docs/p5-deepseek-adapter.md` §7）。
- 三份环境阻断 issue 仍 OPEN（`docs/issue-codex-exec-pin-removed.md`、
  `docs/issue-appserver-codex-home-sqlite.md`、`docs/issue-python-low-integrity.md`）。

P4.6d 离线验收四步已全部通过，见本文件顶部「P4.6d 离线验收」一节；P5 的 DeepSeek 通道已落地，
仍未跑过一次真实往返。

---

# 历史交接：P4.6 代码完成，待用户离线验收（2026-10-26，分支 `feat/p46-autopilot`）

- 工作树：`E:\plug\agent-hub\.worktrees\p46-autopilot`，分支 `feat/p46-autopilot`。
- 起点：main `fd54556`；设计/授权规则提交 `68cdb10`。
- 实现提交：`02e34ba`；同秒新阶段排序修正：`635169f`。
- Claude Code 先完成阶段契约、表、调度器、API 和部分页面；额度耗尽后，用户在本 Codex 会话要求接续完成。Codex 补齐恢复、页面、验证和文档；两个执行者均记入实现提交。
- 本段历史时点：main 仍为 `fd54556`，尚未合并（现已被上一节取代）。

## 已实现

1. **P4.6a 阶段授权与调度**：同用户、Agent、适配器身份及执行边界手动成功一次后，可一次授权阶段目标与规则；自动启动、依赖分派、可选审阅及汇总。所有调用仍走原审批/配额/预算执行入口。工具卡按明确前缀或文件规则决定，并记录阶段授权来源。
2. **P4.6b 重试与收口**：普通错误首次失败后默认最多三次修复，检查点跨调用/重启保存。修复输入包含失败事实及改方法、再验证的要求；审阅失败交回主 Agent 修复并重新审阅。完成/阻塞均保存本地总结，不额外花一次模型调用才能写总结。未知执行状态不自动重试。
3. **P4.6c 额度恢复**：明确额度错误进入持久等待；可靠 reset/Retry-After 优先，否则从可设间隔退避（默认 5 分钟，最长 60 分钟）。同接入方共用一次恢复探测；等待不占普通重试次数，调用仍入账。独立节点可继续。服务重新启动先恢复调用状态，再推进阶段。暂停/停止优先。
4. **页面**：右栏自动执行表单、一次规则确认、修复/等待/总结状态，顶栏暂停/停止；手动调用与工具操作按钮改名，避免两个“拒绝”混淆。
5. **演示**：Claude Code `autopilot` 离线场景，含正常、`DEMO_RETRY`、`DEMO_QUOTA` 和两项依赖计划。服务普通 API 入口与 `serve.py` 都启用后台调度；测试可禁用后台循环并注入时钟。
6. **收尾修复**：同秒创建的调用/阶段使用 SQLite 插入次序打破时间戳平局；避免随机 ID 排序将旧失败调用或旧阶段误作最新。

## 实际验证

在当前工作树，使用 `E:\plug\agent-hub\.venv\Scripts\python.exe`：

```text
python -m pytest -o addopts='' -q
1 failed, 1583 passed in 314.24s
```

唯一失败为 `test_independent_item_runs_while_provider_waits_and_completed_items_are_not_repeated`，暴露同秒重试排序错误，已修复并补固定复现用例。之后没有再跑整套或压力循环；对最终修复跑相关范围：

```text
python -m pytest -o addopts='' -q tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py tests/http/test_collab.py tests/http/test_chat_page.py tests/http/test_async_and_tool_approval.py tests/http/test_conversations.py tests/http/test_usage_ledger.py tests/policy tests/adapters/test_claude_code_adapter.py tests/adapters/test_appserver_stdio.py
418 passed in 49.24s
```

追加阶段排序修正后的相关验证：

```text
python -m pytest -o addopts='' -q tests/http/test_autopilot.py tests/http/test_autopilot_recovery.py
50 passed in 12.02s
```

两组有重叠，不把数量相加，不将本轮描述成“最终代码全量已通过”。最新失败缓存为空，差异格式检查通过。

离线 Edge 页面走查：手动成功证明 → 表单授权 → 普通失败自动修复 → 额度等待 → 暂停 → 刷新页面 → 继续 → 完成总结，PASS，无页面脚本错误。最终页面 JavaScript 语法检查通过。脚本和截图在被忽略的 `var/p46-browser-check/`，最终演示数据在 `var/p46-browser-final/data`；只使用模拟输入。

本轮自己启动的 8186 演示服务已通过 shutdown 接口正常关闭。没有停止用户其他服务。

## 下一步与边界

- **P4.6d 用户验收尚未进行**：按 [离线操作指南](p46-offline-acceptance.md) 从打开 PowerShell、启动后台开始。先只用 Claude Code 模拟器跑通自动重试与额度恢复，再看协作；用户继续自己操作。验收后再决定合并 main、进入 P5，不重复旧真实运行。
- 真实额度错误格式、真实自动阶段和真实工具自动允许均**未验证**。本轮没有消耗 Codex / Claude Code 真实调用额度。
- Claude Code 仍不支持原生续接；独立调用携带原目标/依赖结果/失败证据。Codex 恢复绑定失败调用自己的 thread。没有只读额度查询接口，恢复探测会算一次受规则限制的调用。
- 自动模式不扩大原生派生、工具沙箱、远端停止边界。阶段停止阻止新调用；在途调用是否停止必须看适配器确认。规则不可原地编辑，变更需停止旧阶段并重新授权。当前额度桶按一个 Hub 内一个接入方一套凭据划分，未实现多账号桶。
- Hub 预算/累计调用/子任务上限可能先于三次修复耗尽，程序按阻塞总结，不自动扩额。未设审阅 Agent 时，以成功调用并收到回复作为流程完成依据，业务质量仍需验收。
- 仓库外的 `E:\plug\AGENT_HUB_PLAN.md`、`E:\plug\AGENT_HUB_AUTOMATION_REQUIREMENTS.md` 已同步状态；它们不属于此仓库提交。

---

# 历史交接：P4.5 界面整理（2026-10-25，分支 `feat/p45-ui`，基于 `feat/p4-collab`）

- 已实现：
  - 三栏布局：用量与预算、阶段摘要、协作计划移到可折叠的右栏，窄窗口时改为抽屉；
  - 本地生成的 5 字会话标题，可以改名（新接口 `PATCH /api/conversations/{cid}`）；
  - 压缩继续的会话沿用原标题，带“续”标签，不加时间；
  - 汇总按钮的文字随所选 Agent 更新；新建调用后时间线滚动到它的审批卡。
- 说明见 [会话后端契约](conversation-backend.md) 的 P4.5 一节。
- 验证：新增测试 9 项，相关已有测试 112 项，全部通过；桩页面在 8183 上检查过一次。没有跑全量测试。
- 合入 main：`feat/p45-ui` 包含 `feat/p4-collab`，main 可以直接快进到它。是否合并等用户决定。
- **离线人工演示（2026-10-25，用户操作，桩 Agent，8182）**：基本覆盖了已开发的功能。
  - 已演示：
    - 工具卡拒绝与批准、停止调用；
    - 文件片段引用；
    - 改策略后撤销审批；
    - 摘要、压缩并继续，并附带摘要；
    - 手动交接、续接原生 thread；
    - 协作计划按依赖分派两项，以及汇总（被拒一次后再交给 claude-code）；
    - 输出偏好；
    - 严格单次预算超额被拒，以及超出预留时记超支；
    - 软预算只提示，总预算用完后拒绝；
    - 新界面：三栏、5 字标题、改名、“续”标签。
  - 没有演示：
    - 由主 Agent 拆分计划：桩的回复里没有计划格式的行；离线测试已覆盖；
    - 状态未知时的人工裁决。
  - 演示中发现并已修复：
    - 半屏窗口看不到右栏（`a688f78`）；
    - 预算表单只给英文 422 提示（`f32bdba`）。
- 下一步：决定是否合入 main，然后进入 P5。

---

# 历史交接：P4 受控自动协作已收口（2026-10-25，分支 `feat/p4-collab`）

- 阶段 T（T-0 到 T-3）已完成；main 已本地快进到 `6060460`（含 S5 修复），未推送。S4 的同类问题留作待办。
- **P4 收口结论**：
  - 首版协作功能已完成：协作开关 → 计划（手写，或由主 Agent 拆分后读回）→ 按依赖逐项分派深度 1 的子任务，每项单独审批 → 汇总交给一个 Agent。说明见 [P4 受控自动协作](p4-collaboration.md)。
  - 真实分派、依赖顺序和结果交接已验证；完整的一次 PASS 尚未获得。
  - 两次真实运行都是 INCONCLUSIVE，记录保留，不再申请第三次：
    - [第一次](p4-2026-10-25-run1.md)，标签 `evidence/p4-real-run-1-inconclusive`；
    - [第二次](p4-2026-10-25-run2.md)，标签 `evidence/p4-real-run-2-inconclusive`。
- **已知限制**（保留，不据此重做协作功能）：
  - Claude Code 有时报告两个模型、`num_turns=2`；
  - Claude Code 可能以安全防护为由拒答。

  Hub 对这些情况的失败状态、partial 用量和名额处理都正确。
- **UI 待办**：
  - 切换 Agent 后，汇总按钮文字不会立即更新；
  - 窗口较窄时消息区过矮。

  下次修改页面时处理。
- **合入 main 的范围**：`6060460..feat/p4-collab`，main 可以直接快进。包括：
  - `0382b4e`：P4 协作功能；
  - `c026d88`：unknown 修复；
  - `86257d2`：真实验收申请和判定脚本；
  - `c9daccc`：第一次实录；
  - `06357d8`：独立的回合数检查；
  - `080fe56`：Claude Code `result_meta` 和第二次申请；
  - `4d3f3bc`：第二次实录；
  - 本交接提交。
- **测试依据**：
  - 全量测试 1523 项，在 `c026d88` 上通过；
  - 之后的产品代码改动只有 `080fe56` 中 Claude Code 执行记录新增的 `result_meta` 字段，相关测试 156 项通过；
  - 按决定不再追加全量测试或压力测试。
- 放到以后：多计划展示、计划版本管理、自动派发。
- **后续主线**：
  1. 先做 **P4.5 界面整理**（2026-10-25 用户提出，设计见 `AGENT_HUB_PLAN.md` §6 的“P4.5 界面整理”）：
     - 5 字会话标题；
     - 压缩继续的会话不加时间；
     - 用量、摘要、协作计划移到右栏；
     - 修复汇总按钮文字。
  2. 再做 P5 Git 产物与发布。

---

# 历史交接：T-3 上下文在前、缓存命中率、输出偏好（2026-10-24，分支 `feat/t3-cache-output`，离线）

- 基于 T-2 修复 `369f3ba`，main 仍为 `8230cc3`。说明见 [会话后端契约](conversation-backend.md) 的 T-3 一节。
- 发送内容改为上下文在前（摘要 → 文件 → 输出偏好 → 交接 → 历史）、用户消息在最后，形成可缓存的稳定前缀。
- 缓存命中率单独显示，不是节省量；输出偏好是可见、经审批的软目标，不截断回复。
- 未做：切换模型或推理强度（需要逐个适配器验证）、真实缓存命中对照（需要另行申请）。阶段收口时运行全量测试。

---

# 历史交接：T-2 阶段摘要与压缩并继续（2026-10-24，分支 `feat/t2-compress`，离线）

- 基于 main `2c771c7`（T-1 已经本地快进合并，全量 1489 通过，未推送）。
- 已完成流程：写摘要或由调用生成草稿 → 编辑并保存为新版本 → 附带到消息，发给任一 Agent → 压缩并继续，在新会话中继续，原会话保留。说明见 [会话后端契约](conversation-backend.md) 的 T-2 一节。
- 摘要和其中的文件片段都是审批绑定的发送内容，不发送历史。生成草稿是普通调用，照常经过审批和预算。
- 修复：压缩并继续后换新会话、新 provider thread，但沿用原任务树，策略、累计用量、预留和未结算项都不重置。因预算被拒的任务压缩后仍被拒。
- 未做：provider 原生压缩、阈值提醒、真实验证（需要另行申请）。阶段收口时运行全量测试。

---

# 历史交接：T-1g 文件建议 + T-1h 申请（2026-10-24，分支 `feat/t1-context-refs`，离线）

- T-1g（最小可用版）：根据消息中的路径和行号、已选文件、本会话最近的引用，建议文件或片段；用户点击后才加入，然后走原来的预览和审批。超过单次预算时提示缩小范围，不自动截断，也不扩大上下文。说明见 [会话后端契约](conversation-backend.md) 的 T-1g 一节。
- T-1h：按 [申请](t1h-real-acceptance.md) 在 `bd73e95` 上执行一次，**PASS**（只认定本次），见 [实录](t1h-2026-10-24-run1.md)。真实 Codex 和 Claude Code 收到的正是审批时预览的文本（含文件片段），用量完整入账，严格预算的预留和结算与账本一致（17,082）。
- 放到以后：`@diff`、文件修改提醒、在消息正文中直接写 `@`、目录引用。

---

# 历史交接：T-1e/T-1f 文件引用（2026-10-24，分支 `feat/t1-context-refs`，离线）

- 已完成流程：选择文件或行范围 → 发送前预览 → 审批 → 发送。说明见 [会话后端契约](conversation-backend.md) 的 T-1e/T-1f 一节。
- 需要配置 `AGENT_HUB_FILES_ROOT`（或 `serve.py --files-root`），否则引用被拒绝。
- 边界：在真实路径上检查是否在根目录内；凭据、依赖、构建产物、`var/` 和证据目录被排除；有大小上限。
- 绑定：片段的哈希、行范围和文件哈希进入审批；执行时使用批准的快照。
- 影响使用的未做事项：
  - `@diff`；
  - 不能在消息正文中直接写 `@`；
  - 审批卡不会提示“文件已被修改”；
  - 本地候选建议（T-1g）。

---

# 历史交接：T-1d 发送前上下文预览（2026-10-24，分支 `feat/t1-context-refs`，离线）

- 从 T-1c 修补提交 `b46c70f` 新建分支，没有真实调用，没有合并 main。说明见 [会话后端契约](conversation-backend.md) 的 T-1d 一节。
- 契约 `agenthub.context`：
  - 每次会话调用的输入都拆成几部分：用户输入、分隔、交接框架、交接正文；文件片段预留给 T-1f。
  - 每部分记录字数、估算和 SHA-256，各部分恰好拼成实际发送的文本。
  - provider 侧标为未知，可附历史观测，不计入估算。
  - 预览写入 `InvocationSnapshot.context_preview`，进入 `payload_hash`；批准后被改动，以 `snapshot_invalid` 或 `snapshot_changed` 拒绝，启动次数为 0。
- 预算估算改用预览合计；审批卡新增“上下文预览”。
- 下一步：
  - T-1e：`@` 引用的语法、解析与路径边界；
  - T-1f：不可变上下文包，片段进入预览和审批；
  - T-1g：本地候选建议。
  - T-1h 的真实对照需要单独批准。

---

# 历史交接：T-1c 策略切换修补（2026-10-24，分支 `feat/t1-usage-ledger`，离线）

- 接着 T-1c（`738bbec`）做，没有真实调用。说明见 [会话后端契约](conversation-backend.md) 的“策略切换修补”一节。
- 漏洞：首次开启预算，或关闭后重新开启时，无预算期间获准、仍在执行的调用没有预留，原来会被计数器和审批卡预览忽略。
- 修复：
  - 每次有预算的准入都先对账，把这类调用记为“用量未知且无预留”：严格模式拒绝新调用，软模式提醒 `unreserved_in_flight`；
  - 旧调用继续执行，审批不改写；
  - 结算后风险解除，实际用量只计一次；
  - 审批卡预览与准入使用同一套计算。
- 下一步：T-1d（发送前上下文预览、`@` 文件引用和输入上下文预算）。从本修补提交新建分支 `feat/t1-context-refs`，暂不合并 main。

---

# 历史交接：T-1c token 预算与原子准入（2026-10-24，分支 `feat/t1-usage-ledger`，离线）

- 接着 T-1b（`cc9d28a`）做，没有真实调用。说明见 [会话后端契约](conversation-backend.md) 的 T-1c 一节：
  - 策略新增总预算、单次预算、提醒阈值和 strict/soft 模式（契约变更，旧策略不受影响）；
  - 准入在 consume 中，用条件 UPDATE 原子地预留；拒绝时先回滚，审批保持未消费，启动次数为 0；
  - 结算跟随账本，未知用量保留预留、不归零；
  - 审批卡显示估算、已用、在途预留和未知部分，页面可以修改预算。
- 单次预算**不是硬上限**：两家适配器都不能中途截断，超出的部分只能事后标为 overrun。
- 下一步：T-1d（发送前上下文预览）及之后的 `@` 引用（T-1e 到 T-1g）。建议另开分支 `feat/t1-context-refs`，可以先把本分支合并进去，需要批准。

---

# 历史交接：T-1b 用量视图（2026-10-24，分支 `feat/t1-usage-ledger`，离线）

- 接着 T-1a（`02084f0`）做，没有真实调用。说明见 [会话后端契约](conversation-backend.md) 的 T-1b 一节：
  - 根任务视图：包含子任务调用，并按任务、按 Agent 分组；
  - 会话视图新增 `root_task`；
  - 新增 `GET /api/usage/summary`：当前用户在本实例中的全部会话，按 Agent、按天、按会话分组，可按日期过滤；
  - 页面：会话用量栏，以及左栏的“全部会话 · 近 7 天”。
- **项目**：Hub 还没有项目实体（方案 §5 的 `projects` 尚未实现），所以项目视图暂时是“本实例的全部会话”，接口和页面都写明了“未区分项目”。是否先实现项目实体，由用户决定。
- 下一步：T-1c（预算与原子准入）。

---

# 历史交接：T-1a 用量归一化与账本（2026-10-24，分支 `feat/t1-usage-ledger`，离线）

- 基于 `feat/t0-usage-baseline` @ `22e34ec`。没有真实调用，没有读取凭据；S5 没有改动。
- D-T0-1 到 D-T0-7 已在离线桩上修复，说明见 [会话后端契约](conversation-backend.md) 末节：
  - 新契约 `agenthub.usage`（映射版本 1，未知不写 0）；
  - app-server 回合用量按 thread `total` 之差计算，并与各次请求之和交叉核对；
  - 内核新表 `usage_ledger`：每次调用一行，与结算同一事务，失败、中断也写；只能提升、不能降级；重启时补记；
  - `GET /api/conversations/{cid}/usage`；
  - 页面按归一化口径显示，未知显示原因。
- 验证：新增离线测试 60 项；全量结果见本轮提交说明；浏览器在离线桩 `t1-usage-demo`（8175）上核对。
- 未验证：真实 Codex 和 Claude Code 上的入账，T-1h 单独申请。
- 下一步：T-1b（用量视图：根任务、按 Agent），然后是 T-1c（预算与原子准入）。

---

# 历史交接：T-0 能力与用量基线（2026-10-24，分支 `feat/t0-usage-baseline`，离线）

- 基于 `feat/p3-two-agent` @ `44a5dcd`，只改文档。没有真实调用，没有改代码，没有读取凭据；已收口的 S5 没有改动。
- 交付：[T-0 基线](t0-usage-baseline.md)，包括能力表、字段口径与归一化、Codex 16 行和 Claude Code 2 行的用量基线、去重规则、未知标记规则、缺陷 D-T0-1 到 D-T0-7，以及 T-1 拆分（T-1a 到 T-1h）。能力矩阵顶部加了摘要。
- 关键发现：
  - Claude Code 显示的“输入 2”不含缓存，总输入为 1,798；
  - Codex 适配器只记最后一次请求的用量，S3 第三次少记了约一半；
  - 中断和失败的用量为空白，没有标为未知；
  - `turn/completed` 不带用量，桩的普通回合走的是真实服务端不使用的路径。
- 下一步：T-1a（用量归一化与账本，分支 `feat/t1-usage-ledger`），离线进行。真实对照运行放在 T-1h 申请，需要单独批准。
- 未做：合并到 main（需要批准）；Codex S6-4 和 S6-5。

---

# 历史交接：双 Agent 真实网页对话 PASS，达到阶段 T 的启动节点（2026-10-24，分支 `feat/p3-two-agent`）

- 经批准在 `5a2f55a` 上执行第二次双 Agent 真实网页对话：
  - `--preflight` PASS；
  - Codex 回复暗号；会话切换后历史保持；经批准的交接让 Claude Code 答出同一暗号；
  - 判定 **PASS**（只认定本次）。
  - 见 [第二次实录](two-agent-2026-10-24-run2.md)，标签 `evidence/two-agent-real-run-2-pass`。
- 按 `AGENT_HUB_TOKEN_SAVING_REQUIREMENTS.md` 第 6 节，下一项工作是 T-0（适配器可见的用量、历史、缓存和压缩能力盘点与基线），然后是 T-1（用量预算 + `@` 文件片段）。
- 未做：
  - 合并到 main，需要用户批准（见 `plan/branch-consolidation` 的方案）；
  - Codex 的 S6-4 和 S6-5，属于加固，不是 T 的前提。

---

# 历史交接：多 Agent 路由与手动交接（P3，2026-10-24，分支 `feat/p3-two-agent`）

- 经用户批准，从 `feat/appserver-s6` 新开本分支，用 `--no-ff` 合并了工作流 A（`feat/codex-runtime`）和工作流 B（`feat/claude-code-min`）。没有推送，没有合并到 main。
- 新增 `RoutingAdapter`（`AGENT_HUB_ADAPTER=multi`）：每个调用都在自己 Agent 的适配器上检查和运行；路由器层面一律拒绝。
- 新增手动交接：`handoff_message_ids`。组合后的输入由审批绑定，审批卡完整显示。
- 说明见 [多 Agent 对话](multi-agent.md)。离线测试和浏览器桩验证都已完成。
- 2026-10-24 进展：
  - 经批准合并了两条记录分支：Codex S6-2 第二次运行 PASS；Claude Code 身份已记录，其第一次单回合 PASS。
  - 受影响的离线测试 279 项通过。
  - 新增只读判定脚本 `tools/probes/two_agent_collect.py`，离线测试 11 项。
  - 双 Agent 真实网页对话已经批准执行一次，结果 **FAIL**：
    - 启动配置没有传 `--codex-transport app-server`，`multi` 下的 Codex 走了 exec；exec 固定的旧程序已被删除，启动前就被拒绝；
    - 没有进程，没有模型回合，没有调用 Claude Code；
    - 见 [第一次实录](two-agent-2026-10-24-run1.md)。
  - 四处离线修正已完成：
    - multi 下 Codex 固定用 app-server；
    - 启动信息按 Agent 打印；
    - 新增 `--preflight` 检查 `/api/adapter`；
    - 判定脚本对缺失记录判 INCONCLUSIVE。
  - 第二次申请见 [多 Agent 对话](multi-agent.md) 第 6 节，未执行。

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
