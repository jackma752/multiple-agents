# AionUi 接入审计简报（P0 硬条件验证）

生成时间：2026-10-13。审计对象为本机已下载源码，未运行、未安装。

## 源码位置（只读，不要修改）

- AionUi 前端/桌面：`E:\ds_agent\workplace\vendor\AionUi\AionUi-main`
- AionCore 后端：`E:\ds_agent\workplace\vendor\AionCore\AionCore-main`
  - 团队协作核心 crate：`crates\aionui-team`（`src\service.rs` 203KB、`src\session.rs` 177KB、`src\provisioning.rs` 71KB、`src\mcp\server.rs` 75KB、`src\work_coordinator\coordinator.rs` 65KB）
  - 自带审计文档：`crates\aionui-team\docs\`（`mcp.md`、`internals.md`、`api.md`、`phase1\backend-audit.md` 等）
  - 角色提示与工具授权元数据：`crates\aionui-team-prompts`

## 三个硬条件（必须逐条给出结论）

1. **未同意能否阻止委派**：主 Agent / 子 Agent 创建与任务分派的执行路径上，是否存在"用户审批"这一必需环节？没有审批时，代码能否阻止适配器/子 Agent 启动？
2. **数量限制能否覆盖整棵任务树**：是否存在子 Agent 总量、并发、嵌套深度、调用次数、运行时长的限制？作用域是单团队、单会话还是整个根任务树？是否由用户配置、是否后端强制？
3. **原生子 Agent 能否禁用或拦截**：各 CLI 产品（Claude Code、Codex 等）自带的子 Agent / 后台委派能力，能否在启动前关闭、或在同一审批与计数链路中拦截？

## 证据要求

- 每条结论必须给 `文件路径:行号` 与关键代码/文档原文片段。
- 区分三类：**代码已实现** / **仅文档或提示词约定** / **未找到**。
- 提示词里写"不要调用子 Agent"不算实现，必须标注为"仅提示词"。
- 找不到就写"未找到"，不要推测为存在。能力状态用 `已实现 / 部分实现 / 未实现 / 未验证`。

## 已知背景（避免重复劳动）

- ACP 权限 PRD（`AionUi-main\docs\prds\conversations\acp\permissions.md`）明确：审批卡片针对**敏感工具操作**（写文件、跑命令）；存在**免确认模式 / YOLO**，开启后所有带可选项的请求自动选第一个选项通过；团队模式下权限请求**无超时**；该文档自己标注 F-PERM-01 缺 30 分钟超时自动拒绝。
- AionCore 使用 SQLite + JWT/CSRF，团队有 mailbox、task board、work coordinator、scheduler。
- 审计目标是判断 AionUi 能否作为"每次委派必须批准 + 全树总量限制"的基础项目，**不是**做完整安全审计。
