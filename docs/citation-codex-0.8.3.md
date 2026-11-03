# 0.8.3 引用定位与 Codex 钩子接入修复

用户授权及方案见 [Issue](issue-citation-codex-083.md)，独立分支 `fix/citation-codex-083`，基线 `f2544cd`。不合并、不推送，不改变现有安装、账号配置或旧任务材料。

## 引用契约

- 新精读模板登记为 `research.study.v2`，引用短编号 `S01` 等由文档片段序号确定；发送子集时编号不漂移。模型可返回 `source_id`，后端解析为真实 `chunk_id`；旧 `chunk_id` 输出仍可校验。
- 校验仍是大小写、空白和软连字符标准化后的精确包含，最短 12 个字符。不采用模糊匹配。声明来源必须在本次提供材料中；重定位只扫描同一文档、本次提供的片段，唯一精确位置才接受。未知来源、跨文档、多个可能位置、缺失或改写继续拒绝。
- 引文从真实源文本切出，`source_location` 记录声明片段、匹配方式、片段 SHA-256、引文 SHA-256 及片段内起止范围。页码取真实源，不信任模型填写的页码/审计信息。`citation_audit` 保存实际发生的重定位。
- `CitationError.issues` 一次列出所有失败引用的单元 ID、引用索引与类型。初次精读的来源修复使用 `research.source-repair.v1`，只允许替换点名的来源槽位，解释、公式、依赖、其他来源不能改。候选结构与错误持久化在步骤中，暂停/接续可复用；修复调用仍走原阶段规则、审批、总预算和重试上限，不额外创造授权。
- `packages/contracts/agenthub/citations.py` 定义新增来源审计与错误结构。请求序列化、旧请求哈希、已完成研究与批准快照不改；新增字段是输出扩展。格式通过不证明结论被引文支持或推导正确，仍需独立审阅。

数学单元后续修订会合并已由后端生成的重定位历史；模型自己填写的 `citation_audit` 不被采信。来源修复不必重新生成全部讲解，但其在线修复回合尚未单独触发；本次真实模型直接生成的结果已经通过。

## PDF 阅读顺序

沿用现有 pypdf，不引入大型解析框架。新提取版本 `pypdf 5.1.0/footnote-order-v1` 使用文本位置、字体和编号识别靠近页底的小字体脚注块，移到页末并保留原内容。数学下标和没有脚注正文的编号不移动；复杂变换或无法与原抽取流对应时保留原顺序。

这是保守的版面启发式，仅对当前公开论文与受控样例验收，不保证所有 PDF 都能正确识别；不支持 OCR。原 PDF 的字节和哈希保留，旧文档不重写。新增 `paper_parse_audit` 表以文档 ID 保存页级原抽取/新文本哈希、移动范围及几何位置，用户文档信息中增加 `parse_provenance`；原表不改结构。

原 RAGAS 失败输出离线回查：旧 19 个材料片段中三处错位可精确重定位，第四处仍正确拒绝；对同一 PDF 单独重新解析后，28/28 引文可定位。此回查不变更旧模型输入、不改旧账本，也不将新材料冒充旧任务已验收。

## 实际在线复测

使用已配置 `deepseek-flash`、真实生产适配器/审批/Worker/预算；发送公开 RAGAS 新解析材料和新版模板，无模拟传输。仅一次新调用，输入 7,286、输出 5,455，共 **12,741 Token**。13 个讲解单元、38 条来源全部通过校验，后端唯一精确重定位 1 条。新总预算 24,000、单次预留 12,000；实际单次超过预留 741，已完整记账，预留不是 provider 硬截断。剩余 11,259，结果已通过，不继续调用。

这是精读结构与引用的在线定点验收，不是数学正确性认证，也没有重新运行完整检索、研究方案与写作流水线。旧在线记录仍保留。

## Codex 的真实结果

固定 `codex-cli 0.163.0-alpha.11.2`，SHA `c81ed464e5e7cafd09a5fa26af390c480bed8e9fab2a87a54b285fbd56e7122a`。新空 home、真实 CLI、本机合成 Responses 服务，无真实登录、在线模型或付费请求。先读取钩子 `currentHash`/`trustStatus`，在隔离配置内登记本轮已审核固定脚本的信任，再重启并读取确认；不使用绕过信任参数。

| 场景 | 钩子状态 | 原生子任务数 | 严格要求 |
|---|---|---:|---|
| 正常返回 deny | trusted，实际触发 | 0 | 本场景通过 |
| 返回损坏 JSON | trusted，实际触发 | 1 | 不通过 |
| 超时 | trusted，实际触发 | 1 | 不通过 |

探针初次适配把实际 `preToolUse`/`sha256:` 协议字段当成未知，模型调用为零即停止，修正后保留旧证据另建目录测试。钩子信任缺项已修正，正常拒绝已证实；故障放行属于当前 CLI 机制的限制，Hub 不用提示词或简单超时杀进程伪装可靠拦截。严格入口仍在启动前阻止，诊断改为实际失败原因；因此本轮 Codex 在线调用为 0。不能声称所有 Codex 版本均如此，也不能声称 Codex 全部严格能力已修复。

## 验证与交付

大回归最初发现 1 个已有用量收尾竞争：完成事件已到、队列暂空就关闭传输，随后到达的报告偶尔丢失。已增加 100ms 的有界收尾读取窗口，不处理新的授权，不重置窗口；超过窗口仍缺失的用量保持 unknown，迟到报告保持 partial，不冒充完整。用事件同步的回归样例确保在完成事件已取走后才发送用量。用量专项与归一化 **48 passed in 4.29s**。

初步结构/来源/解析与指导回归 50 项通过，局部修复/预算/端到端专项 34 项通过，钩子协议与来源关键组 24 项通过。首次大回归 1419 通过/1 失败，修复上述收尾竞争后，论文、策略、适配器、HTTP、预算完整相关回归 **1421 passed in 443.67s**。最后增加审计重启和数学修订保留历史用例，重新执行来源/指导/预算/权限专项 **49 passed in 15.70s**。各组有重叠，不相加。

实际命令（环境 `PYTHONPATH=E:\plug\agent-hub\.worktrees\p6-impl\var\p6-deps`，Python 为 `E:\plug\agent-hub\.venv\Scripts\python.exe`）：

```text
python -m pytest -o addopts= tests/papers tests/policy tests/adapters tests/http tests/test_budget_rules.py -q --junitxml=var/citation-083/regression-final.xml
python -m pytest -o addopts= tests/papers/test_citation_resolution.py tests/papers/test_guidance.py tests/papers/test_paper_budget.py tests/papers/test_paper_gates.py -q --junitxml=var/citation-083/final-citation-tests-v2.xml
```

源码提交 `2774aeb` 后构建 0.8.3 本地候选。208 份清单文件、209 份 ZIP 文件、1032 个内嵌模块及实际冻结程序的独立启动/配对/科研页面/数学资源/SSE/新表/退出检查通过，见 [本地候选与校验值](p6-release-0.8.3.md)。初次检查脚本的认证端点和资源路径误判及原报告均保留。未安装或发布。原始证据留忽略目录 `var/`，仅登记哈希。

参考实现与官方依据：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、[工具注册与 Agent 别名](https://github.com/openai/codex/blob/3b5483307d70da48ece3da8cefcaae9a19888651/codex-rs/core/src/tools/registry.rs)、[Instructor 精确引文校验](https://github.com/567-labs/instructor/blob/main/docs/examples/exact_citations.md)、[LlamaIndex 引用编号](https://github.com/run-llama/llama_index/blob/619d74664e8404612038bc15a1a40c686037277c/llama-index-core/llama_index/core/query_engine/citation_query_engine.py)。仅借鉴机制，不宣称这些框架已经在本项目运行。
