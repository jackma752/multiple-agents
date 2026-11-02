# P6 科研补齐：0.8.0 本地候选版

2026-11-02，基于 main `434e241`，独立分支 `feat/p6-research-completion`。对应 [本轮需求与约束](issue-research-completion.md)。

## 产品对照与范围

只读查看 [ClawsGo 主机页](https://app.clawsgo.cn/hosts)：当前账户无主机，页面有云主机/本地设备、任务、智能体、技能，以及概览、文件、Git、主机、步骤、交付物等入口。未创建主机、付费或运行线上研究。[官网](https://clawsgo.cn/)展示远程 GPU、浏览器、LaTeX 编辑与图表修改，尚未逐项实测，不能据此认定其科研质量、引用准确率或复现能力。

本项目已经具备多 Agent 调用、预算/授权、证据与全文补充、核心论文数学讲解、研究候选及版本化项目。这次补多源检索、可移植成果、实验记录；它们接入现有流程。远程机器、云 GPU、无人值守实验执行、富文本 LaTeX 编辑与图表编辑仍未开发，本版不称为 ClawsGo 的完整替代品。

## 使用

新建论文研究时勾选来源，预检会展示来源、网络范围及随来源数增加的预计调用；批准后才执行检索。默认 arXiv。外部出版商 PDF 默认不会因选择来源而自动获准，需要在高级设置明确允许公网全文下载。缺少全文继续沿用两种补充方案和批量题录。

研究完成后可直接下载 `research-bundle.zip`，或分别下载各文件。原文 Markdown、结构化证据、来源审计继续保留；每份 Markdown 增加 HTML/DOCX/TEX 副本。ZIP 内清单记录大小和 SHA-256（清单本身及外层 ZIP 不自引用）。不附原论文 PDF、数据库或账号配置。HTML 是转义后的离线文字阅读稿，可由浏览器打印 PDF；DOCX/TEX 是可编辑文字稿，公式仍保留源文，未验证 Word/LaTeX 排版效果，未自动调用编译器。

旧研究点击“重新导出（不调用模型）”即可生成新格式。研究完成后打开“实验记录”，填写数据并保存版本。相同数据集、条件、指标、优化方向和数据来源的一组以第一项作基线；计算样本数、均值、样本标准差、差值，不推断统计显著性或因果。真实用户记录也未被系统独立验证，演示/合成数据与它分组比较。JSON 保留字段与样本，CSV 提供逐样本明细；保存会重建成果包，不改写论文结论、不调用模型。

## 公共契约与兼容

- `ResearchRequest.sources` 增加 `openalex`、`crossref`、`semantic_scholar`，拒绝重复来源。旧默认字段、arXiv 请求、步骤标识及规则哈希保持。
- `ResourceRules.allow_public_fulltext` 默认 false；false 时不进入序列化，保留旧哈希。true 必须显式批准。每一跳仍检查协议、主机、解析地址、私网和资源上限；不开放上传或本地文件范围。
- 规划的三组查询逐一搜索各来源，不新增模型规划步骤。按来源 ID、标准化 DOI、题名与首作者去重；已知 DOI 冲突/题名歧义不合并。合并记录保存来源，重复结果可补摘要/OA 地址。某源失败不阻止其他源，全部失败保持资料不足，报告 `source_results` 和任务 `search_sources` 保存细节。
- GET/PUT `/api/papers/{task_id}/experiments` 只允许用户访问本人任务。PUT 仅研究结束后，参数为 `expected_version` 与 `records`，并发版本冲突 409；新表 `paper_experiment_revisions` 增量创建，不修改旧表。记录契约见 `packages/contracts/agenthub/experiments.py`：最多 30 项，每项 1–1000 个有限数值，区分 reported/demo，不接受布尔、NaN 或无穷。
- 成果下载增加正确的 HTML/DOCX/TEX/ZIP/CSV 类型，预检列出新增文件。既有鉴权、根任务计数、暂停/停止和模型调用门禁保持；Agent 适配器能力未扩大，不修改其能力表。
- 打包收集全部论文模块，覆盖延迟导入的来源、实验和导出模块。无需新增 Python 依赖，不引入上游代码/资源。

## 官方接口依据

按 [OpenAlex API](https://help.openalex.org/api/) 和 [认证说明](https://help.openalex.org/api/authentication/)、[Crossref 访问说明](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication)、[Semantic Scholar API](https://www.semanticscholar.org/product/api) 独立实现。当前只使用匿名、有限页数检索，没有接入第三方 API 密钥；可用性受网络和共享限流影响，401/403/429 会如实记录，不会自动注册、购买或绕过限制。Crossref 的 TDM 链接不视为开放全文。

## 验证与边界

- `python -m pytest tests/papers -o addopts='' -q --junitxml=var/papers-regression.xml`：**174 passed in 49.23s**。包含多源结构/部分失败、DOI 冲突与来源别名、旧规则哈希、网络门禁、全文补充、导出校验和实验版本/鉴权/重启。
- `python -m pytest tests/policy tests/http/test_contract_api.py tests/http/test_desktop.py tests/test_collab_plan.py -o addopts='' -q --junitxml=var/core-regression.xml`：**261 passed in 42.83s**。
- 最后核对恢复兼容时，保留了默认规划输入中原有的 arXiv 大小写，避免旧步骤哈希变化；补充冻结的旧输入哈希与预检成果列表断言。`python -m pytest tests/papers/test_research_completion.py tests/papers/test_paper_units.py -o addopts='' -q`：**39 passed in 3.27s**。初次新断言写错年份分隔符，修正后通过。
- 浏览器使用单独端口 18222 和 `var/completion-demo`，完成离线研究、导出、实验录入/保存/比较/重新打开，检查多来源预检、窄屏和控制台。演示数据明确标记；真实模型调用 0，不操作现有用户研究。
- 来源 HTTP 实际可用性、真实模型研究质量、其他机器安装、出版级排版均未验收。冻结程序及本地包校验单独登记在发行记录。
