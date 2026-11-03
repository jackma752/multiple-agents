# 0.8.3 本地候选与验收

2026-11-03。分支 `fix/citation-codex-083`，源码实现 `2774aeb959d1ce72ae3bc72d04402a190805ee2f`。功能、方案与测试见 [修复记录](citation-codex-0.8.3.md)。已构建本地候选，未安装、合并或推送；当前运行实例、真实用户 profile 与旧研究资料未改。

产物目录：`E:\plug\agent-hub\.worktrees\citation-codex-083\dist\p6-release-0.8.3`。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `AgentHub-0.8.3-windows-x64-setup.exe` | 22908705 | `a1659bba6e415d65fec2c324560100498f98713af538293440ab14aa0e8ad005` |
| `AgentHub-0.8.3-windows-x64-portable.zip` | 22607378 | `9762d5e90cf786f6a916b9ccc24777d5b96f82cb25c8bc3489af072571cbd542` |

构建命令成功退出 0：

```text
E:\plug\.p5-build\Scripts\python.exe tools/distribution/build.py --output dist/p6-release-0.8.3 --nsis E:\plug\.p5-build\nsis\nsis-3.11\makensis.exe
```

实际运行 `E:\plug\.p5-build\Scripts\python.exe var/release_smoke_083.py`，最终检查退出 0：

- 208 份 manifest 文件的长度与哈希一致；ZIP 209 份文件与实际目录逐字节相同，无重复文件名。
- 真正 EXE 内嵌模块表共 1032 项，包含新增契约、引用定位和 PDF 解析/流程模块；无模拟适配器、开发认证模块或 pytest。发布内容经过凭据/数据库/开发 Token 排除检查。
- 真正冻结程序以 `--headless` 启动，独立空资料目录 `var/release-smoke-083-v3/empty-profile`，随机端口 52496。未配对读取科研接口为 401；配对、票据只能使用一次、空配置、科研/设置页面、数学静态资源、保存自拟消息及 SSE 事件往返均通过。
- 调用自身 shutdown，进程退出 0。只读核对 `paper_parse_audit` 表存在、0 行；invocations 为 0。没有模型调用或使用真实登录。

初次检查脚本错误地等待公开 `/api/agents` 返回 401，导致启动期限判定失败，测试进程由脚本终止；第二次误用静态资源路径而收到 404，程序自身正常退出。已按真实认证接口 `/api/papers`、同源请求和页面实际 `/assets/katex` 地址修正检查脚本，另建空目录重测通过。原失败报告与脚本保留，不修改程序来迁就检查。

源码相关回归 1421 项通过，最后引用/指导/预算/权限专项 49 项通过（组之间重叠）；真实 DeepSeek 定点在线 38/38 引文通过、12,741 Token。未完整重跑科研流水线或认证数学正确性。Codex 受信任 deny 已通过，但超时/损坏响应仍放行，严格模式继续阻止；不能把本地候选称为 Codex 严格模式全部通过。

试运行另一个副本时，应指定独立资料目录和空闲端口。例如在 PowerShell 中将 `AGENT_HUB_DESKTOP_HOME` 设为新目录，再启动解压后的 `AgentHub.exe --port 18223`。只改端口无法隔离默认资料目录；本轮未替用户切换安装或复用真实资料。
