# 0.8.1 本地候选：严格模式入口与传输优化

2026-11-03。源码 `1b3487888c4af9b8611fba3e8b6bdba6b82b822f`，分支 `fix/strict-transmission`，包括上一轮 `e57089e` 的真实在线验收修复。功能与限制见 [严格模式/传输记录](strict-transmission-0.8.1.md) 及 [在线结果](p6-online-080.md)。没有合并、推送或替换用户当前安装。

本地产物位于本工作树 `dist/p6-release-0.8.1`，另有 `SHA256SUMS.txt`。Windows 10/11 x64 用户退出旧 Hub 后可试用安装包，便携 ZIP 须完整解压。公开版本仍以 GitHub 发行页为准。

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| AgentHub-0.8.1-windows-x64-setup.exe | 22871719 | `f8f7b67c0b22a4ca29cacb486a818e773dea0f43c8c9bd3e32012c8eb57f12b0` |
| AgentHub-0.8.1-windows-x64-portable.zip | 22570299 | `f4cb747796705bd11333c0fb5843e93c4dfa57fb5f97edad91474fb633f3a26b` |

## 验证

- HTTP/策略/论文/预算相关 **992 passed in 319.58s**；最后 Claude/准入/预检 **89 passed in 6.83s**；错误响应连接关闭后网络/材料/预检 **21 passed in 20.57s**。浏览器模式、默认模型、跨通道参数、推理选项与预算设置检查通过，无脚本错误。
- 从正常本机权限构建，未跳过完整性级别或包审计。独立核对清单 208 项大小/哈希，ZIP 209 项与包目录逐字节匹配且无重复路径。检查 exe 内 1,027 个 Python 模块，确认事件模块中确有 `ConversationWakeups`、论文网络模块导入 gzip；必需来源/实验/导出模块存在。测试、工具、指定 mock/stub、dev_auth 模块和固定开发令牌均未进入包。
- 真正冻结 exe 使用全新 UUID 数据目录及随机端口 57603、headless 模式：启动、匿名访问拒绝、只给本次实例配对、空 Agent 配置、设置/论文/严格预算静态入口、数学资源通过。实际创建固定测试消息并在 SSE 收到提交事件；没有 Agent 调用。之后通过本次实例自己的退出接口正常关闭，脚本/进程退出码 0。
- 冻结报告：`var/runs/result-empty-3952fd8e-d938-40b5-a093-16567cfed178.json`；独立包核对：`var/package-081-check.json`。不上传临时控制信息、Cookie、数据目录或原始模型研究输出。

本阶段新增真实模型调用 0；在线验收的 9 次调用/52,866 Token 另保留，不重复计算。没有启动安装器或另机实装。Codex/Claude 的严格原生控制及完整科研内容仍未通过真实验收；该限制在页面提前显示，不承诺全通道严格运行、出版级排版或模型生成速度提升。
