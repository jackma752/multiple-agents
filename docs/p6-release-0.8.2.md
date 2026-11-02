# 0.8.2 本地候选与验收

2026-11-03。分支 `feat/native-strict`，源码实现 `b179a9cb3f4176be2a6cab420cf4a6f45873c3a9`。构建和实际功能范围见 [原生严格模式](native-strict.md)。这是本地候选，未安装、合并或推送，原 profile 与研究数据没有改变。

产物目录：`E:\plug\agent-hub\.worktrees\native-strict\dist\p6-release-0.8.2`。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `AgentHub-0.8.2-windows-x64-setup.exe` | 22895072 | `fe740d49dca255970cfecf0b88096bfa180dd1090f65f3ef93f1441e792684ec` |
| `AgentHub-0.8.2-windows-x64-portable.zip` | 22595075 | `9a5011282574123a4eb2d539fd9c4994aec56f8e56f92e0c6e99bc2f2e99a358` |

构建：`E:\plug\.p5-build\Scripts\python.exe tools/distribution/build.py --output dist/p6-release-0.8.2 --nsis E:\plug\.p5-build\nsis\nsis-3.11\makensis.exe`，成功退出 0。构建产物按白名单及内嵌模块表检查，不包含账号、运行数据库、pytest、开发认证模块或模拟适配器。

## 验证

- 受影响 HTTP、策略、论文、适配器及预算回归：**1395 passed in 348.02s**。
- 最后审批、事件与准入关键路径：**86 passed in 22.42s**；最终能力元数据修改后 Claude 适配器：**70 passed in 2.38s**。各组有重叠，不相加。
- Claude 真正的固定 CLI + 本机合成模型：13 项通过；生产 Worker 消费审批至真实 CLI、子任务/内部文件写入审批、succeeded 结算链通过。零在线模型请求，不将合成用量计入真实费用或声称验证了真实模型质量。
- Codex 配置回读及禁用 V1 子任务场景通过；启用子任务的 PreToolUse 拒绝场景**失败**（钩子没有触发，却实际创建 1 个合成子任务）。失败证据保留，Codex 严格准入仍阻止。这版没有实现所有 Codex 严格能力。
- 浏览器：Claude strict 可以选择原生子任务逐次审批；换到 Codex 会清除并禁用该选择；没有控制台错误。截图 `var/native-ui-verified.jpg`，仅是隔离页面，不是用户真实会话。
- 包内容：208 份 manifest 文件的长度/哈希吻合；ZIP 209 份文件逐字节吻合，无重名路径；内嵌 1030 个模块。新 `agenthub.native`、`agenthub_kernel.native_tools`、`agenthub_adapters.claude_hooks` 和原论文模块均在真正冻结程序中，开发 Token 不存在。
- 真正运行冻结 `AgentHub.exe --headless`，新建空 profile、随机端口 50114：未认证 401、临时配对、无已配置 Agent、本机设置、论文入口/预算、新原生选项 HTML、数学静态资源、提交后 SSE 往返及自身 shutdown 均通过，进程退出 0。SQLite 只读核对 native_agents 表存在且为 0 行；没有创建真实调用。

初次受限执行环境的冻结程序启动期限检查未通过，未得到应用错误报告；保留该空测试目录。正常用户权限下另建全新目录重测通过，没有改变程序源码或绕过应用认证。未运行安装器、覆盖旧实例或使用真实登录目录。

程序启用 Claude 严格配置仍须采用已核验 2.1.308/SHA、配置工作区。首版只有文件工具和一层前台 general-purpose 原生子任务，Shell/MCP/后台/原生续接/子任务独立 Token 预算不开放；其余版本继续拒绝严格准入。说明、HTTP 契约和完整边界见 [native-strict.md](native-strict.md)。
