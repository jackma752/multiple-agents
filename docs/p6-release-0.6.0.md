# 0.6.3：导出目录可自选（当前推荐安装）

源码 `03ea5a21ba4409252a39026ce67e32aa367f6b82`。新建表单增加“导出到”（已存在、可写的绝对路径，可空）。该目录进入批准的资源规则并在预检中显示；导出仍保存在 Hub 任务目录供页面下载，同时复制到 `<导出目录>\<任务ID>\v<版本>\`，每个路径按规则复检；复制失败在成果区提示，不影响导出。未设导出目录的旧任务规则哈希不变。含 0.6.2 的全部改动。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| AgentHub-0.6.3-windows-x64-setup.exe | 22523624 | `19b1facf03c36778f6d4a5d1ddb73adab46196a27d59a7765ab95ae18b5b4cf3` |
| AgentHub-0.6.3-windows-x64-portable.zip | 22222591 | `812d31bf27c7289b3840beb682818eff2d52aa319bb3ad6876fa7bca79283825` |

验证：`tests/papers` 与模型/自动阶段相关共 113 passed（复制内容与下载一致、目录不存在 422、旧哈希不变、目录外与 `..` 路径拒绝）；离线演示预检显示复制位置；冻结程序临时数据目录检查通过，打包页面含新字段，退出后进程结束。未运行安装向导。

# 0.6.2：推理强度补全

源码 `3236620d22841024b2044c7a9635ab0af382e2a1`。Claude Code 推理强度候选由 low/medium/high 补为 low/medium/high/xhigh（超高）/max（最高）：2026-10-30 静态读取固定的 2.1.308 二进制，`--effort` 按这五档校验，所选模型不支持的档位由 CLI 自动降级（降级后的实际档位未验证）。DeepSeek 原已有 none/low/high/max。Codex 的强度（含 xhigh、ultra 等预设）来自接入方 `model/list`，需点“刷新模型”；论文页原先只有主 Agent 有刷新按钮，现在每个角色都有，并在目录未读取时提示。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| AgentHub-0.6.2-windows-x64-setup.exe | 22521053 | `05c8b39d1d342e347e0b4f366fe5a0477753a0d00cd409ba99abb5f6e6932c21` |
| AgentHub-0.6.2-windows-x64-portable.zip | 22220791 | `24e8a0d961b1b57898cd532436d65439b3fccfd8616e8f551f860a71d9b296f0` |

验证：模型管理、Claude 适配器、论文与会话页测试 158 passed（新增：目录列出五档，max 以 `--effort max` 传给 CLI）；冻结程序检查通过。Codex 刷新在离线演示中未覆盖（演示无 Codex）。

# 0.6.1：论文研究页按角色选择模型

源码 `1a05e66c697a0cd9f94370a4e5144c1284431ff8`。用户在 0.6.0 中发现论文研究表单只能手填主 Agent 的模型。0.6.1 起与会话页一致：主 Agent 和审阅 Agent 各自从模型目录选择（也可输入模型 ID），推理强度随模型列出，默认带入会话页记住的选择，可“刷新模型”；审阅与主 Agent 相同时沿用主 Agent 的模型（一个阶段内每个 Agent 一个模型）。选择进入阶段规则和每次调用快照。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| AgentHub-0.6.1-windows-x64-setup.exe | 22521600 | `282b267dd70ed38adf338f31123a2497840b1656b52a73cbb008c19072cbba5a` |
| AgentHub-0.6.1-windows-x64-portable.zip | 22221426 | `195b644124912dbd268405b2f20631f2475f6728bb8c8d554c8bb7957b5863c2` |

位置 `E:\plug\agent-hub\dist\p6-release-0.6.1`，205 项文件，审计通过。验证：`tests/papers` 61 passed；离线演示浏览器检查两个模型目录、强度随模型变化、同 Agent 审阅禁用；冻结程序在临时数据目录 headless 检查与 0.6.0 相同各项通过，打包页面含新控件，退出后进程结束。未运行安装向导。升级：退出 Hub 后用 0.6.1 setup 覆盖原目录。

---

# 0.6.0：论文研究（P6 首版）

发行源码 `4db9cc01f28eab407d700dedcc54264f35683455`，分支 `feat/p6-paper-pipeline`。在 0.5.5（`feat/desktop-timeout`，主题、Markdown/SVG、公式、0 为不限、删除会话、执行时限、工作区写入修复）基础上加入论文研究，见 [P6 实现记录](p6-implementation.md)。未合并 main，未推送。

## 用户可见变化

- 新入口 `/papers`（会话页顶栏“论文研究”）：输入主题、年份、篇数、语言、全文缺失策略、模型与预算 → 预检 → 批准开始；查看进度、全文补充（上传/路径/地址，按批次核对）、论文清单、证据与原文片段、正文、成果下载。
- 主题与其他页面一致（跟随系统/浅色/深色）。
- 首版来源仅 arXiv；成品是可追溯的综述/研究简报，执行完成与质量通过分别标示。

## 产物与校验值

位置：`E:\plug\agent-hub\dist\p6-release-0.6.0`。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| AgentHub-0.6.0-windows-x64-setup.exe | 22520160 | `87eded9cc3bb5d65e2003bad05370b283842a8c80c7ee60dff8b00be51536af5` |
| AgentHub-0.6.0-windows-x64-portable.zip | 22219278 | `0f1767ff87102b35c63ff673e69cbe6ce8fbaf67ebaf9ba67effb3a528de4a66` |

构建：独立构建环境 `E:\plug\.p5-build`（Python 3.12.0、PyInstaller 6.17.0 等按 `packaging-requirements.txt` 固定），新增 pypdf 5.1.0（本地已下载的官方 wheel，离线安装到构建环境）；NSIS 3.11 同 0.5.x。205 项打包文件；原有包审计通过。嵌入模块表包含 `agenthub_kernel.papers.*` 全部 11 个模块与 `pypdf`；不含 pytest、模拟适配器、开发认证、`tools.demo` 的论文模拟器与合成语料；第三方许可证目录含 `pypdf-LICENSE`、`KaTeX-LICENSE.txt`；MANIFEST 记录 version 0.6.0、源码提交与 pypdf 5.1.0。

## 冻结程序检查（全新临时数据目录、headless、端口 18299）

未认证 `/api/papers` 401；`/papers` 200，带主题选择与共用样式，不含开发令牌；单次票据配对 200、重复使用 401；配对后 `/api/papers` 200（空列表），预检在没有已接入 Agent 时返回明确阻塞；`/assets/display.css` 200；会话页含“论文研究”入口；无 Origin 的写请求 403；`/api/desktop/shutdown` 后进程退出码 0。未接触用户现有安装、配置、8080 服务或项目文件；0 次真实模型调用、0 次真实检索。

## 未验证

- 未运行安装向导（避免改动现有安装注册信息）；沿用 0.5.x 已验证的安装器模板。
- 冻结程序内的 PDF 解析只通过模块表核对，没有在冻结程序里实际解析 PDF（需要已接入 Agent 并开始一项研究才会触发）；源码环境的解析测试已通过。
- 真实 arXiv 检索、真实 PDF 下载、真实模型输出质量；另一台物理机；代码签名。

## 升级

先在当前 Hub 点“退出 Hub”，再运行 0.6.0 setup，选择原安装目录覆盖，不需先卸载或删除数据。新表在启动时自动补建，旧数据不变。论文研究需先在会话页对要用的 Agent 各手动成功调用一次（阶段授权规则）。
